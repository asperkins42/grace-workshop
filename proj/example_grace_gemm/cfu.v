`ifndef CFU_WITH_RAM_INCLUDED
`define CFU_WITH_RAM_INCLUDED
`include "rams_tdp_rf_rf.v"
`endif
`ifndef CFU_WITH_GEMM_INCLUDED
`define CFU_WITH_GEMM_INCLUDED
`include "gemm16x16.v"
`endif

`default_nettype none

// CFU that streams 256 contiguous 32-bit values from HBM into a local scratchpad
// and dispatches a fixed 16x16 GEMM that accumulates into scratchpad 2.
// - funct7 = 1 : load scratchpad 0 from HBM (rs1 = CFU/HBM byte address, 32-byte aligned).
// - funct7 = 2 : load scratchpad 1 from HBM (rs1 = CFU/HBM byte address, 32-byte aligned).
// - funct7 = 3 : run MAC (GEMM16x16) using sp0, sp1, accumulating into sp2.
// - funct7 = 8/9/10: read word from scratchpad 0/1/2 (rs1 = index [0-255]).
// - funct7 = 11: dump scratchpad 2 to HBM (rs1 = byte address, 32-byte aligned).
// - funct7 = 12: clear scratchpad 2 (the accumulator) to zero.
// Load/dump/MAC/clear return 0xBEEF0000 on success, 0xDEADxxxx on error.
module Cfu (
    input  wire         clk,
    input  wire         reset,

    // Command from CPU
    input  wire         cmd_valid,
    output wire         cmd_ready,
    input  wire [9:0]   cmd_payload_function_id,
    input  wire [31:0]  cmd_payload_inputs_0,
    input  wire [31:0]  cmd_payload_inputs_1,

    // Response to CPU
    output reg          rsp_valid,
    input  wire         rsp_ready,
    output reg  [31:0]  rsp_payload_outputs_0,

    // AXI Master (to HBM)
    output reg  [3:0]   m_axi_awid,
    output reg  [32:0]  m_axi_awaddr,
    output reg  [7:0]   m_axi_awlen,
    output reg  [2:0]   m_axi_awsize,
    output reg  [1:0]   m_axi_awburst,
    output wire         m_axi_awvalid,
    input  wire         m_axi_awready,

    output wire [255:0] m_axi_wdata,
    output wire [31:0]  m_axi_wstrb,
    output wire         m_axi_wlast,
    output wire         m_axi_wvalid,
    input  wire         m_axi_wready,

    input  wire [3:0]   m_axi_bid,
    input  wire [1:0]   m_axi_bresp,
    input  wire         m_axi_bvalid,
    output wire         m_axi_bready,

    output reg  [3:0]   m_axi_arid,
    output reg  [32:0]  m_axi_araddr,
    output reg  [7:0]   m_axi_arlen,
    output reg  [2:0]   m_axi_arsize,
    output reg  [1:0]   m_axi_arburst,
    output wire         m_axi_arvalid,
    input  wire         m_axi_arready,

    input  wire [3:0]   m_axi_rid,
    input  wire [255:0] m_axi_rdata,
    input  wire [1:0]   m_axi_rresp,
    input  wire         m_axi_rlast,
    input  wire         m_axi_rvalid,
    output wire         m_axi_rready

    // AXI masters 2..32 (unused, see unused_axi_ports.vh)
`define UNUSED_AXI_PORT_DECLS
`include "unused_axi_ports.vh"
`undef UNUSED_AXI_PORT_DECLS
);
`include "unused_axi_ports.vh"

    // ---------------------------------------------------------------------
    // Opcode decode / constants
    wire [6:0] funct7 = cmd_payload_function_id[9:3];

    localparam [6:0] FUNCT_LOAD_SP0 = 7'd1;
    localparam [6:0] FUNCT_LOAD_SP1 = 7'd2;
    localparam [6:0] FUNCT_MAC      = 7'd3;
    localparam [6:0] FUNCT_PEEK_SP0 = 7'd8;
    localparam [6:0] FUNCT_PEEK_SP1 = 7'd9;
    localparam [6:0] FUNCT_PEEK_SP2 = 7'd10;
    localparam [6:0] FUNCT_DUMP_SP2 = 7'd11;
    localparam [6:0] FUNCT_CLR_SP2  = 7'd12;


    localparam [31:0] STATUS_OK          = 32'hBEEF0000;
    localparam [31:0] STATUS_ADDR_ALIGN  = 32'hDEAD1000;
    localparam [31:0] STATUS_AXI_FAULT   = 32'hDEAD2000;
    localparam [31:0] STATUS_SP_RANGE    = 32'hDEAD3000;
    localparam [31:0] STATUS_UNSUPPORTED = 32'hDEADFFFF;
    localparam [31:0] RSP_BAD_INDEX      = 32'hDEAD4000;

    localparam [3:0] ST_INIT      = 4'd0;
    localparam [3:0] ST_IDLE      = 4'd1;
    localparam [3:0] ST_AR        = 4'd2;
    localparam [3:0] ST_STREAM    = 4'd3;
    localparam [3:0] ST_PEEK      = 4'd4;
    localparam [3:0] ST_PEEK_WAIT = 4'd5;
    localparam [3:0] ST_RESP      = 4'd6;
    localparam [3:0] ST_PEEK_RESP = 4'd7;
    localparam [3:0] ST_DUMP_AW   = 4'd8;
    localparam [3:0] ST_DUMP_FILL = 4'd9;
    localparam [3:0] ST_DUMP_W    = 4'd10;
    localparam [3:0] ST_DUMP_B    = 4'd11;
    localparam [3:0] ST_MAC_START = 4'd12;
    localparam [3:0] ST_MAC_WAIT  = 4'd13;
    localparam [3:0] ST_CLR_SP2   = 4'd14;

    localparam [5:0] MAX_BURST_BEATS = 6'd1;
    localparam [5:0] TOTAL_BEATS     = 6'd32;
    localparam [15:0] WAIT_LIMIT     = 16'd4095;

    reg [3:0] state;

    // AXI read tracking
    reg        ar_pending;
    reg [8:0]  write_addr;   // counts words written to scratchpad, includes overflow bit
    reg [255:0] rdata_buf;
    reg [2:0]  lane_idx;
    reg        have_buffer;
    reg        pending_last;
    reg        axi_error;
    reg        sp_range_error;
    reg [7:0]  beats_remaining;
    reg [7:0]  current_beats;
    reg [32:0] next_araddr;
    reg [15:0] wait_counter;
    reg        wait_timeout;
    reg [7:0]  clear_addr;

    // Scratchpad read tracking
    reg [7:0] peek_addr;

    // Scratchpad 0 memory (port A = loader/peek/dump, port B = GEMM reads)
    reg         sp0a_en;
    reg         sp0a_we;
    reg  [7:0]  sp0a_addr;
    reg  [31:0] sp0a_di;
    wire [31:0] sp0a_do;
    wire        sp0b_en;
    wire [7:0]  sp0b_addr;
    wire [31:0] sp0b_do;
    reg  [7:0]  init_addr;
    reg  [1:0]  init_sp;

    // Scratchpad 1 memory
    reg         sp1a_en;
    reg         sp1a_we;
    reg  [7:0]  sp1a_addr;
    reg  [31:0] sp1a_di;
    wire [31:0] sp1a_do;
    wire        sp1b_en;
    wire [7:0]  sp1b_addr;
    wire [31:0] sp1b_do;

    // Scratchpad 2 memory (port B = GEMM read/accumulate write)
    reg         sp2a_en;
    reg         sp2a_we;
    reg  [7:0]  sp2a_addr;
    reg  [31:0] sp2a_di;
    wire [31:0] sp2a_do;
    wire        sp2b_en;
    wire [7:0]  sp2b_addr;
    wire [31:0] sp2b_do;

    reg [1:0]  active_load_sp;
    reg [1:0]  active_peek_sp;
    reg        aw_pending;
    reg        w_pending;
    reg [8:0]  dump_addr;
    reg        dump_wait_data;
    reg        dump_data_ready;
    reg [255:0] dump_wbuf;
    reg [32:0] next_awaddr;

    // GEMM engine interface
    reg         gemm_start;
    wire        gemm_busy;
    wire        gemm_done;
    wire        gemm_a_en;
    wire        gemm_a_we;
    wire [7:0]  gemm_a_addr;
    wire [31:0] gemm_a_di;
    wire        gemm_b_en;
    wire        gemm_b_we;
    wire [7:0]  gemm_b_addr;
    wire [31:0] gemm_b_di;
    wire        gemm_c_en;
    wire        gemm_c_we;
    wire [7:0]  gemm_c_addr;
    wire [31:0] gemm_c_di;

    rams_tdp_rf_rf #(
        .DATA_WIDTH(32),
        .ADDR_WIDTH(8)
    ) u_sp0 (
        .clka  (clk),
        .ena   (sp0a_en),
        .wea   (sp0a_we),
        .addra (sp0a_addr),
        .dia   (sp0a_di),
        .doa   (sp0a_do),
        .clkb  (clk),
        .enb   (sp0b_en),
        .web   (gemm_a_we),
        .addrb (sp0b_addr),
        .dib   (gemm_a_di),
        .dob   (sp0b_do)
    );

    rams_tdp_rf_rf #(
        .DATA_WIDTH(32),
        .ADDR_WIDTH(8)
    ) u_sp1 (
        .clka  (clk),
        .ena   (sp1a_en),
        .wea   (sp1a_we),
        .addra (sp1a_addr),
        .dia   (sp1a_di),
        .doa   (sp1a_do),
        .clkb  (clk),
        .enb   (sp1b_en),
        .web   (gemm_b_we),
        .addrb (sp1b_addr),
        .dib   (gemm_b_di),
        .dob   (sp1b_do)
    );

    rams_tdp_rf_rf #(
        .DATA_WIDTH(32),
        .ADDR_WIDTH(8)
    ) u_sp2 (
        .clka  (clk),
        .ena   (sp2a_en),
        .wea   (sp2a_we),
        .addra (sp2a_addr),
        .dia   (sp2a_di),
        .doa   (sp2a_do),
        .clkb  (clk),
        .enb   (sp2b_en),
        .web   (gemm_c_we),
        .addrb (sp2b_addr),
        .dib   (gemm_c_di),
        .dob   (sp2b_do)
    );

    // GEMM engine consumes port-B of the scratchpads.
    assign sp0b_en   = gemm_a_en;
    assign sp0b_addr = gemm_a_addr;
    assign sp1b_en   = gemm_b_en;
    assign sp1b_addr = gemm_b_addr;
    assign sp2b_en   = gemm_c_en;
    assign sp2b_addr = gemm_c_addr;

    gemm16x16 #(
        .DATA_WIDTH (32),
        .ADDR_WIDTH (8),
        .M_DIM      (16),
        .N_DIM      (16),
        .K_DIM      (16),
        .K_TILE     (16)
    ) u_gemm (
        .clk    (clk),
        .reset  (reset),
        .start  (gemm_start),
        .base_a (8'd0),
        .base_b (8'd0),
        .base_c (8'd0),
        .busy   (gemm_busy),
        .done   (gemm_done),

        .a_en   (gemm_a_en),
        .a_we   (gemm_a_we),
        .a_addr (gemm_a_addr),
        .a_di   (gemm_a_di),
        .a_dout (sp0b_do),

        .b_en   (gemm_b_en),
        .b_we   (gemm_b_we),
        .b_addr (gemm_b_addr),
        .b_di   (gemm_b_di),
        .b_dout (sp1b_do),

        .c_en   (gemm_c_en),
        .c_we   (gemm_c_we),
        .c_addr (gemm_c_addr),
        .c_di   (gemm_c_di),
        .c_dout (sp2b_do)
    );

    // AXI handshakes. VALID comes straight from the *_pending flags, so the
    // state machine's "pending && ready" is exactly the AXI "valid && ready"
    // transfer, and VALID stays high until that transfer happens. READY is
    // high only in the state that consumes the beat.
    assign m_axi_arvalid = ar_pending;
    assign m_axi_awvalid = aw_pending;
    assign m_axi_wvalid  = w_pending;
    assign m_axi_wdata   = dump_wbuf;
    assign m_axi_wstrb   = 32'hFFFF_FFFF;
    assign m_axi_wlast   = 1'b1;           // every write burst is one beat
    assign m_axi_rready  = (state == ST_STREAM) && !have_buffer && !wait_timeout;
    assign m_axi_bready  = (state == ST_DUMP_B) && !wait_timeout;

    // Single outstanding command at a time
    wire cmd_fire = cmd_valid && cmd_ready;
    assign cmd_ready = (state == ST_IDLE) && !rsp_valid;

    // Helper: select 32-bit lane from 256-bit AXI beat
    function [31:0] lane_word;
        input [255:0] data;
        input [2:0]   lane;
        begin
            case (lane)
                3'd0: lane_word = data[ 31:  0];
                3'd1: lane_word = data[ 63: 32];
                3'd2: lane_word = data[ 95: 64];
                3'd3: lane_word = data[127: 96];
                3'd4: lane_word = data[159:128];
                3'd5: lane_word = data[191:160];
                3'd6: lane_word = data[223:192];
                default: lane_word = data[255:224];
            endcase
        end
    endfunction

    function [5:0] clamp_beats;
        input [5:0] beats;
        begin
            if (beats > MAX_BURST_BEATS)
                clamp_beats = MAX_BURST_BEATS;
            else
                clamp_beats = beats;
        end
    endfunction

    function [7:0] beats_to_len;
        input [6:0] beats;
        begin
            if (beats == 7'd0)
                beats_to_len = 8'd0;
            else
                beats_to_len = beats - 7'd1;
        end
    endfunction

    // ---------------------------------------------------------------------
    // Main control/state machine
    always @(posedge clk) begin
        if (reset) begin
            state                 <= ST_INIT;
            rsp_valid             <= 1'b0;
            rsp_payload_outputs_0 <= 32'd0;

            m_axi_awid    <= 4'd0;
            m_axi_awaddr  <= 33'd0;
            m_axi_awlen   <= 8'd0;
            m_axi_awsize  <= 3'd0;
            m_axi_awburst <= 2'd0;

            // Read channel defaults
            m_axi_arid    <= 4'd0;
            m_axi_araddr  <= 33'd0;
            m_axi_arlen   <= 8'd0;
            m_axi_arsize  <= 3'd0;
            m_axi_arburst <= 2'd0;

            ar_pending  <= 1'b0;
            write_addr  <= 9'd0;
            rdata_buf   <= 256'd0;
            lane_idx    <= 3'd0;
            have_buffer <= 1'b0;
            pending_last<= 1'b0;
            axi_error   <= 1'b0;
            sp_range_error <= 1'b0;
            peek_addr   <= 8'd0;
            beats_remaining <= 7'd0;
            current_beats   <= 7'd0;
            next_araddr     <= 33'd0;
            wait_counter    <= 16'd0;
            wait_timeout    <= 1'b0;

            sp0a_en   <= 1'b0;
            sp0a_we   <= 1'b0;
            sp0a_addr <= 8'd0;
            sp0a_di   <= 32'd0;
            init_addr <= 8'd0;
            init_sp   <= 2'd0;
            sp1a_en   <= 1'b0;
            sp1a_we   <= 1'b0;
            sp1a_addr <= 8'd0;
            sp1a_di   <= 32'd0;
            sp2a_en   <= 1'b0;
            sp2a_we   <= 1'b0;
            sp2a_addr <= 8'd0;
            sp2a_di   <= 32'd0;
            active_load_sp <= 2'd0;
            active_peek_sp <= 2'd0;
            aw_pending <= 1'b0;
            w_pending  <= 1'b0;
            dump_addr  <= 9'd0;
            dump_wait_data <= 1'b0;
            dump_wbuf  <= 256'd0;
            next_awaddr <= 33'd0;
            dump_data_ready <= 1'b0;
            gemm_start <= 1'b0;
            clear_addr <= 8'd0;
        end else begin
            // Drop response once host consumes it.
            if (rsp_valid && rsp_ready) begin
                rsp_valid <= 1'b0;
                if (state == ST_RESP) begin
                    state <= ST_IDLE;
                end
            end

            // Default scratchpad controls (explicit enable when needed)
            sp0a_en <= 1'b0;
            sp0a_we <= 1'b0;
            sp1a_en <= 1'b0;
            sp1a_we <= 1'b0;
            sp2a_en <= 1'b0;
            sp2a_we <= 1'b0;

            gemm_start <= 1'b0;

            dump_data_ready <= dump_wait_data;

            if ((state == ST_AR && ar_pending && !m_axi_arready) ||
                (state == ST_STREAM && !have_buffer && !m_axi_rvalid) ||
                (state == ST_DUMP_AW && ((aw_pending && !m_axi_awready) || (w_pending && !m_axi_wready))) ||
                (state == ST_DUMP_W && !m_axi_wready) ||
                (state == ST_DUMP_B && !m_axi_bvalid)) begin
                if (wait_counter == WAIT_LIMIT) begin
                    wait_timeout <= 1'b1;
                end else begin
                    wait_counter <= wait_counter + 16'd1;
                    wait_timeout <= 1'b0;
                end
            end else begin
                wait_counter <= 16'd0;
                wait_timeout <= 1'b0;
            end

            case (state)
                ST_INIT: begin
                    case (init_sp)
                        2'd0: begin
                            sp0a_en   <= 1'b1;
                            sp0a_we   <= 1'b1;
                            sp0a_addr <= init_addr;
                            sp0a_di   <= 32'h1CED0000 | init_addr;
                        end
                        2'd1: begin
                            sp1a_en   <= 1'b1;
                            sp1a_we   <= 1'b1;
                            sp1a_addr <= init_addr;
                            sp1a_di   <= 32'h1CED1000 | init_addr;
                        end
                        default: begin
                            sp2a_en   <= 1'b1;
                            sp2a_we   <= 1'b1;
                            sp2a_addr <= init_addr;
                            sp2a_di   <= 32'h1CED2000 | init_addr;
                        end
                    endcase
                    if (init_addr == 8'd255) begin
                        init_addr <= 8'd0;
                        if (init_sp == 2'd2) begin
                            state   <= ST_IDLE;
                        end else begin
                            init_sp <= init_sp + 2'd1;
                        end
                    end else begin
                        init_addr <= init_addr + 8'd1;
                    end
                end

                ST_IDLE: begin
                    if (cmd_fire) begin
                        case (funct7)
                            FUNCT_LOAD_SP0,
                            FUNCT_LOAD_SP1: begin
                                // Require 32-byte alignment to match 256-bit AXI bus.
                                if (cmd_payload_inputs_0[4:0] != 5'd0) begin
                                    rsp_payload_outputs_0 <= STATUS_ADDR_ALIGN;
                                    rsp_valid             <= 1'b1;
                                    state                 <= ST_RESP;
                                end else begin
                                    active_load_sp <= (funct7 == FUNCT_LOAD_SP0) ? 2'd0 : 2'd1;
                                    m_axi_araddr  <= {1'b0, cmd_payload_inputs_0};
                                    m_axi_arlen   <= beats_to_len(clamp_beats(TOTAL_BEATS)); // limit burst length
                                    m_axi_arsize  <= 3'd5;   // 32 bytes per beat
                                    m_axi_arburst <= 2'b01;  // INCR
                                    // Ensure ID consistent even though only one outstanding read.
                                    m_axi_arid    <= 4'd0;

                                    ar_pending   <= 1'b1;
                                    current_beats <= clamp_beats(TOTAL_BEATS);
                                    beats_remaining <= TOTAL_BEATS;
                                    next_araddr  <= {1'b0, cmd_payload_inputs_0};
                                    write_addr   <= 9'd0;
                                    lane_idx     <= 3'd0;
                                    have_buffer    <= 1'b0;
                                    pending_last   <= 1'b0;
                                    axi_error      <= 1'b0;
                                    sp_range_error <= 1'b0;
                                    state          <= ST_AR;
                                end
                            end

                            FUNCT_MAC: begin
                                gemm_start <= 1'b1;
                                state      <= ST_MAC_START;
                            end

                            FUNCT_PEEK_SP0,
                            FUNCT_PEEK_SP1,
                            FUNCT_PEEK_SP2: begin
                                if (cmd_payload_inputs_0[31:8] != 24'd0) begin
                                    rsp_payload_outputs_0 <= RSP_BAD_INDEX;
                                    rsp_valid             <= 1'b1;
                                    state                 <= ST_RESP;
                                end else begin
                                    peek_addr <= cmd_payload_inputs_0[7:0];
                                    case (funct7)
                                        FUNCT_PEEK_SP0: active_peek_sp <= 2'd0;
                                        FUNCT_PEEK_SP1: active_peek_sp <= 2'd1;
                                        default:        active_peek_sp <= 2'd2;
                                    endcase
                                    state     <= ST_PEEK;
                                end
                            end

                            FUNCT_DUMP_SP2: begin
                                if (cmd_payload_inputs_0[4:0] != 5'd0) begin
                                    rsp_payload_outputs_0 <= STATUS_ADDR_ALIGN;
                                    rsp_valid             <= 1'b1;
                                    state                 <= ST_RESP;
                                end else begin
                                    m_axi_awaddr  <= {1'b0, cmd_payload_inputs_0};
                                    m_axi_awlen   <= beats_to_len(clamp_beats(TOTAL_BEATS));
                                    m_axi_awsize  <= 3'd5;
                                    m_axi_awburst <= 2'b01;
                                    m_axi_awid    <= 4'd0;

                                    current_beats <= clamp_beats(TOTAL_BEATS);
                                    beats_remaining <= TOTAL_BEATS;
                                    next_awaddr   <= {1'b0, cmd_payload_inputs_0};
                                    dump_addr     <= 9'd0;
                                    lane_idx      <= 3'd0;
                                    dump_wait_data<= 1'b0;
                                    axi_error     <= 1'b0;
                                    sp_range_error<= 1'b0;
                                    aw_pending    <= 1'b0;
                                    w_pending     <= 1'b0;
                                    state         <= ST_DUMP_FILL;
                                end
                            end

                            FUNCT_CLR_SP2: begin
                                clear_addr <= 8'd0;
                                state      <= ST_CLR_SP2;
                            end

                            default: begin
                                rsp_payload_outputs_0 <= STATUS_UNSUPPORTED;
                                rsp_valid             <= 1'b1;
                                state                 <= ST_RESP;
                            end
                        endcase
                    end
                end

                ST_AR: begin
                    if (wait_timeout) begin
                        ar_pending  <= 1'b0;
                        axi_error   <= 1'b1;
                        rsp_payload_outputs_0 <= STATUS_AXI_FAULT;
                        rsp_valid   <= 1'b1;
                        state       <= ST_RESP;
                    end else begin
                        // Keep ARVALID asserted via ar_pending until slave accepts.
                        if (ar_pending && m_axi_arready) begin
                            ar_pending  <= 1'b0;
                            state       <= ST_STREAM;
                            beats_remaining <= (beats_remaining > current_beats) ?
                                                (beats_remaining - current_beats) : 7'd0;
                            next_araddr <= next_araddr + {current_beats, 5'd0};
                        end
                    end
                end

                ST_STREAM: begin
                    if (wait_timeout) begin
                        ar_pending  <= 1'b0;
                        have_buffer <= 1'b0;
                        pending_last<= 1'b0;
                        axi_error   <= 1'b1;
                        rsp_payload_outputs_0 <= STATUS_AXI_FAULT;
                        rsp_valid   <= 1'b1;
                        state       <= ST_RESP;
                    end else if (!have_buffer) begin
                        // RREADY is high here, so RVALID means a beat transfers.
                        if (m_axi_rvalid) begin
                            rdata_buf    <= m_axi_rdata;
                            have_buffer  <= 1'b1;
                            lane_idx     <= 3'd0;
                            pending_last <= m_axi_rlast;
                            axi_error    <= axi_error | (m_axi_rresp != 2'b00);
                        end
                    end else begin
                        // Write one 32-bit lane per cycle into scratchpad.
                        if (!write_addr[8]) begin
                            case (active_load_sp)
                                2'd0: begin
                                    sp0a_en   <= 1'b1;
                                    sp0a_we   <= 1'b1;
                                    sp0a_addr <= write_addr[7:0];
                                    sp0a_di   <= lane_word(rdata_buf, lane_idx);
                                end
                                2'd1: begin
                                    sp1a_en   <= 1'b1;
                                    sp1a_we   <= 1'b1;
                                    sp1a_addr <= write_addr[7:0];
                                    sp1a_di   <= lane_word(rdata_buf, lane_idx);
                                end
                                default: begin
                                    sp2a_en   <= 1'b1;
                                    sp2a_we   <= 1'b1;
                                    sp2a_addr <= write_addr[7:0];
                                    sp2a_di   <= lane_word(rdata_buf, lane_idx);
                                end
                            endcase
                            write_addr <= write_addr + 9'd1;
                        end else begin
                            sp_range_error <= 1'b1;
                        end

                        lane_idx <= lane_idx + 3'd1;

                        if (lane_idx == 3'd7) begin
                            have_buffer  <= 1'b0;
                            // Final beat? Determine status and produce response or request next burst.
                            if (pending_last) begin
                                pending_last <= 1'b0;
                                if (beats_remaining != 7'd0) begin
                                    current_beats <= clamp_beats(beats_remaining);
                                    m_axi_arlen   <= beats_to_len(clamp_beats(beats_remaining));
                                    m_axi_araddr  <= next_araddr;
                                    ar_pending    <= 1'b1;
                                    state         <= ST_AR;
                                end else begin
                                    rsp_payload_outputs_0 <= (sp_range_error ? STATUS_SP_RANGE :
                                                               (axi_error ? STATUS_AXI_FAULT : STATUS_OK));
                                    rsp_valid             <= 1'b1;
                                    state                 <= ST_RESP;
                                end
                            end
                        end
                    end
                end

                ST_DUMP_AW: begin
                    if (wait_timeout) begin
                        aw_pending  <= 1'b0;
                        w_pending   <= 1'b0;
                        axi_error   <= 1'b1;
                        rsp_payload_outputs_0 <= STATUS_AXI_FAULT;
                        rsp_valid   <= 1'b1;
                        state       <= ST_RESP;
                    end else begin
                        if (aw_pending && m_axi_awready) begin
                            aw_pending  <= 1'b0;
                            beats_remaining <= (beats_remaining > current_beats) ?
                                                (beats_remaining - current_beats) : 7'd0;
                            next_awaddr <= next_awaddr + {current_beats, 5'd0};
                        end
                        if (w_pending && m_axi_wready) begin
                            w_pending <= 1'b0;
                        end
                        if (!aw_pending && !w_pending) begin
                            state <= ST_DUMP_B;
                        end
                    end
                end

                ST_DUMP_FILL: begin
                    if (!dump_wait_data && !dump_data_ready) begin
                        if (dump_addr[8]) begin
                            sp_range_error <= 1'b1;
                            rsp_payload_outputs_0 <= STATUS_SP_RANGE;
                            rsp_valid             <= 1'b1;
                            state                 <= ST_RESP;
                        end else begin
                            sp2a_en   <= 1'b1;
                            sp2a_we   <= 1'b0;
                            sp2a_addr <= dump_addr[7:0];
                            dump_addr <= dump_addr + 9'd1;
                            dump_wait_data <= 1'b1;
                        end
                    end else if (dump_data_ready) begin
                        case (lane_idx)
                            3'd0: dump_wbuf[ 31:  0] <= sp2a_do;
                            3'd1: dump_wbuf[ 63: 32] <= sp2a_do;
                            3'd2: dump_wbuf[ 95: 64] <= sp2a_do;
                            3'd3: dump_wbuf[127: 96] <= sp2a_do;
                            3'd4: dump_wbuf[159:128] <= sp2a_do;
                            3'd5: dump_wbuf[191:160] <= sp2a_do;
                            3'd6: dump_wbuf[223:192] <= sp2a_do;
                            default: dump_wbuf[255:224] <= sp2a_do;
                        endcase
                        if (lane_idx == 3'd7) begin
                            lane_idx       <= 3'd0;
                            dump_wait_data <= 1'b0;
                            dump_data_ready<= 1'b0;
                            m_axi_awaddr  <= next_awaddr;
                            m_axi_awlen   <= beats_to_len(current_beats);
                            m_axi_awsize  <= 3'd5;
                            m_axi_awburst <= 2'b01;
                            m_axi_awid    <= 4'd0;
                            aw_pending    <= 1'b1;
                            w_pending     <= 1'b1;
                            state          <= ST_DUMP_AW;
                        end else begin
                            lane_idx       <= lane_idx + 3'd1;
                            dump_wait_data <= 1'b0;
                            dump_data_ready<= 1'b0;
                        end
                    end
                end

                ST_DUMP_B: begin
                    if (wait_timeout) begin
                        axi_error   <= 1'b1;
                        rsp_payload_outputs_0 <= STATUS_AXI_FAULT;
                        rsp_valid   <= 1'b1;
                        state       <= ST_RESP;
                    end else begin
                        // BREADY is high here, so BVALID means the response transfers.
                        if (m_axi_bvalid) begin
                            axi_error <= axi_error | (m_axi_bresp != 2'b00);
                            if (beats_remaining != 7'd0) begin
                                current_beats <= clamp_beats(beats_remaining);
                                lane_idx      <= 3'd0;
                                dump_wait_data<= 1'b0;
                                w_pending     <= 1'b0;
                                state         <= ST_DUMP_FILL;
                            end else begin
                                rsp_payload_outputs_0 <= (axi_error ? STATUS_AXI_FAULT : STATUS_OK);
                                rsp_valid             <= 1'b1;
                                state                 <= ST_RESP;
                            end
                        end
                    end
                end

                ST_MAC_START: begin
                    // Pulse start for one cycle then wait for completion.
                    state <= ST_MAC_WAIT;
                end

                ST_MAC_WAIT: begin
                    if (gemm_done && !gemm_busy) begin
                        rsp_payload_outputs_0 <= STATUS_OK;
                        rsp_valid             <= 1'b1;
                        state                 <= ST_RESP;
                    end
                end

                ST_CLR_SP2: begin
                    sp2a_en   <= 1'b1;
                    sp2a_we   <= 1'b1;
                    sp2a_addr <= clear_addr;
                    sp2a_di   <= 32'd0;
                    if (clear_addr == 8'hFF) begin
                        rsp_payload_outputs_0 <= STATUS_OK;
                        rsp_valid             <= 1'b1;
                        state                 <= ST_RESP;
                    end else begin
                        clear_addr <= clear_addr + 8'd1;
                    end
                end

                ST_PEEK: begin
                    case (active_peek_sp)
                        2'd0: begin
                            sp0a_en   <= 1'b1;
                            sp0a_addr <= peek_addr;
                        end
                        2'd1: begin
                            sp1a_en   <= 1'b1;
                            sp1a_addr <= peek_addr;
                        end
                        default: begin
                            sp2a_en   <= 1'b1;
                            sp2a_addr <= peek_addr;
                        end
                    endcase
                    state    <= ST_PEEK_WAIT;
                end

                ST_PEEK_WAIT: begin
                    // Allow one cycle for RAM to present data.
                    state <= ST_PEEK_RESP;
                end

                ST_PEEK_RESP: begin
                    case (active_peek_sp)
                        2'd0: rsp_payload_outputs_0 <= sp0a_do;
                        2'd1: rsp_payload_outputs_0 <= sp1a_do;
                        default: rsp_payload_outputs_0 <= sp2a_do;
                    endcase
                    rsp_valid             <= 1'b1;
                    state                 <= ST_RESP;
                end

                ST_RESP: begin
                    // Response stays valid until host accepts (handled above).
                end

                default: state <= ST_IDLE;
            endcase
        end
    end

endmodule

`default_nettype wire
