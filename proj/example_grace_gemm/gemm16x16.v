`timescale 1ns/1ps

// -----------------------------------------------------------------------------
// Parameterizable GEMM engine with optional K-dimension tiling.
// Computes C(MxN) = A(MxK) * B(KxN) assuming row-major storage.
// A single multiply-accumulate is issued per cycle; tile support allows
// the inner K loop to be chunked into smaller groups (`K_TILE`).
// -----------------------------------------------------------------------------
module gemm16x16 #(
    parameter integer DATA_WIDTH = 32,
    parameter integer ADDR_WIDTH = 8,
    parameter integer M_DIM      = 16,
    parameter integer N_DIM      = 16,
    parameter integer K_DIM      = 16,
    parameter integer K_TILE     = 16
) (
    input                       clk,
    input                       reset,
    input                       start,
    input      [ADDR_WIDTH-1:0] base_a,
    input      [ADDR_WIDTH-1:0] base_b,
    input      [ADDR_WIDTH-1:0] base_c,
    output reg                  busy,
    output reg                  done,

    // Scratchpad A (read-only)
    output reg                  a_en,
    output                      a_we,
    output reg [ADDR_WIDTH-1:0] a_addr,
    output     [DATA_WIDTH-1:0] a_di,
    input      [DATA_WIDTH-1:0] a_dout,

    // Scratchpad B (read-only)
    output reg                  b_en,
    output                      b_we,
    output reg [ADDR_WIDTH-1:0] b_addr,
    output     [DATA_WIDTH-1:0] b_di,
    input      [DATA_WIDTH-1:0] b_dout,

    // Scratchpad C (write-only while running)
    output reg                  c_en,
    output reg                  c_we,
    output reg [ADDR_WIDTH-1:0] c_addr,
    output reg [DATA_WIDTH-1:0] c_di,
    input      [DATA_WIDTH-1:0] c_dout   // unused
);

    // -------------------------------------------------------------------------
    // Helper to determine counter widths
    // -------------------------------------------------------------------------
    function integer idx_width;
        input integer value;
        integer v;
    begin
        if (value <= 1) begin
            idx_width = 1;
        end else begin
            v = value - 1;
            idx_width = 0;
            while (v > 0) begin
                v = v >> 1;
                idx_width = idx_width + 1;
            end
        end
    end
    endfunction

    // -------------------------------------------------------------------------
    // Local parameters / derived constants
    // -------------------------------------------------------------------------
    localparam integer ACC_WIDTH      = DATA_WIDTH * 2;
    localparam integer I_IDX_W        = idx_width(M_DIM);
    localparam integer J_IDX_W        = idx_width(N_DIM);
    localparam integer K_COUNT_W      = idx_width(K_TILE + 1);
    localparam integer NUM_K_TILES    = (K_DIM + K_TILE - 1) / K_TILE;
    localparam integer K_TILE_IDX_W   = idx_width((NUM_K_TILES > 0) ? NUM_K_TILES : 1);
    localparam integer LAST_TILE_IDX  = (NUM_K_TILES > 0) ? (NUM_K_TILES - 1) : 0;
    localparam integer LAST_TILE_K    = K_DIM - (K_TILE * LAST_TILE_IDX);

    localparam [K_COUNT_W-1:0] ONE_COUNT          = {{(K_COUNT_W-1){1'b0}}, 1'b1};
    localparam [K_TILE_IDX_W-1:0] ONE_TILE        = {{(K_TILE_IDX_W-1){1'b0}}, 1'b1};
    localparam [I_IDX_W-1:0] ONE_I                = {{(I_IDX_W-1){1'b0}}, 1'b1};
    localparam [J_IDX_W-1:0] ONE_J                = {{(J_IDX_W-1){1'b0}}, 1'b1};
    localparam [K_TILE_IDX_W-1:0] LAST_TILE_IDX_C = LAST_TILE_IDX;
    localparam [K_COUNT_W-1:0] TILE_K_CONST       = K_TILE;
    localparam [K_COUNT_W-1:0] LAST_TILE_K_CONST  = (NUM_K_TILES == 0) ? 0 : LAST_TILE_K;

    // -------------------------------------------------------------------------
    // State machine encoding
    // -------------------------------------------------------------------------
    localparam [2:0]
        S_IDLE        = 3'd0,
        S_MAC         = 3'd1,
        S_READ_C      = 3'd2,
        S_READ_C_WAIT = 3'd3,
        S_READ_C_WAIT2= 3'd4,
        S_WRITE       = 3'd5,
        S_ADVANCE     = 3'd6,
        S_DONE        = 3'd7;

    reg [2:0] state;

    // Loop indices / counters
    reg [I_IDX_W-1:0]      i_idx;
    reg [J_IDX_W-1:0]      j_idx;
    reg [K_TILE_IDX_W-1:0] tile_k_idx;
    reg [K_COUNT_W-1:0]    issue_cnt;
    reg [K_COUNT_W-1:0]    acc_cnt;

    // Accumulator
    reg signed [ACC_WIDTH-1:0] acc;

    // Pipeline tracking for synchronous RAM readback
    reg a_en_q, b_en_q;
    reg a_en_qq, b_en_qq;
    reg [DATA_WIDTH-1:0] c_dout_q;
    reg [DATA_WIDTH-1:0] a_dout_q, b_dout_q;

    // Address helpers
    reg [ADDR_WIDTH-1:0] addr_a_w;
    reg [ADDR_WIDTH-1:0] addr_b_w;
    reg [ADDR_WIDTH-1:0] addr_c_w;

    integer idx_helper;
    integer addr_tmp;

    always @* begin
        // Global K index for current request
        idx_helper = (tile_k_idx * K_TILE) + issue_cnt;

        addr_tmp = base_a + (i_idx * K_DIM) + idx_helper;
        addr_a_w = addr_tmp[ADDR_WIDTH-1:0];

        addr_tmp = base_b + (idx_helper * N_DIM) + j_idx;
        addr_b_w = addr_tmp[ADDR_WIDTH-1:0];

        addr_tmp = base_c + (i_idx * N_DIM) + j_idx;
        addr_c_w = addr_tmp[ADDR_WIDTH-1:0];
    end

    wire [K_COUNT_W-1:0] chunk_len =
        (tile_k_idx == LAST_TILE_IDX_C) ? LAST_TILE_K_CONST : TILE_K_CONST;

    wire req_valid  = (state == S_MAC) && (issue_cnt < chunk_len);
    wire mac_active   = (state == S_MAC);
    // RAM outputs are registered; align data_valid to MAC state so we only
    // accumulate when the MAC state is active.
    wire data_valid   = a_en_qq && b_en_qq;
    wire data_valid_mac = mac_active && data_valid;
    wire chunk_complete = data_valid_mac && (acc_cnt + ONE_COUNT == chunk_len);

    // Static ties for unused ports
    assign a_we = 1'b0;
    assign b_we = 1'b0;
    assign a_di = {DATA_WIDTH{1'b0}};
    assign b_di = {DATA_WIDTH{1'b0}};

    always @(posedge clk) begin
        if (reset) begin
            state      <= S_IDLE;
            busy       <= 1'b0;
            done       <= 1'b0;

            a_en       <= 1'b0;
            b_en       <= 1'b0;
            c_en       <= 1'b0;
            c_we       <= 1'b0;

            a_addr     <= {ADDR_WIDTH{1'b0}};
            b_addr     <= {ADDR_WIDTH{1'b0}};
            c_addr     <= {ADDR_WIDTH{1'b0}};
            c_di       <= {DATA_WIDTH{1'b0}};

            a_en_q     <= 1'b0;
            b_en_q     <= 1'b0;
            a_en_qq    <= 1'b0;
            b_en_qq    <= 1'b0;
            c_dout_q   <= {DATA_WIDTH{1'b0}};
            b_dout_q   <= {DATA_WIDTH{1'b0}};
            a_dout_q   <= {DATA_WIDTH{1'b0}};

            i_idx      <= {I_IDX_W{1'b0}};
            j_idx      <= {J_IDX_W{1'b0}};
            tile_k_idx <= {K_TILE_IDX_W{1'b0}};
            issue_cnt  <= {K_COUNT_W{1'b0}};
            acc_cnt    <= {K_COUNT_W{1'b0}};
            acc        <= {ACC_WIDTH{1'b0}};
        end else begin
            // Default outputs
            done <= 1'b0;
            c_en <= 1'b0;
            c_we <= 1'b0;

            // Drive read enables / pipeline registers
            a_en   <= req_valid;
            b_en   <= req_valid;
            if (state == S_IDLE && start) begin
                a_en_q   <= 1'b0;
                b_en_q   <= 1'b0;
                a_en_qq  <= 1'b0;
                b_en_qq  <= 1'b0;
                c_dout_q <= {DATA_WIDTH{1'b0}};
                a_dout_q <= {DATA_WIDTH{1'b0}};
                b_dout_q <= {DATA_WIDTH{1'b0}};
            end else begin
                a_en_q   <= a_en;
                b_en_q   <= b_en;
                a_en_qq  <= a_en_q;
                b_en_qq  <= b_en_q;
                c_dout_q <= c_dout;
                a_dout_q <= a_dout;
                b_dout_q <= b_dout;
            end

            if (req_valid) begin
                a_addr <= addr_a_w;
                b_addr <= addr_b_w;
                issue_cnt <= issue_cnt + ONE_COUNT;
            end

            if (data_valid_mac && !chunk_complete)
                acc_cnt <= acc_cnt + ONE_COUNT;

            if (data_valid_mac)
                acc <= acc + $signed(a_dout_q) * $signed(b_dout_q);

            case (state)
                S_IDLE: begin
                    busy <= 1'b0;
                    if (start) begin
                        busy       <= 1'b1;
                        i_idx      <= {I_IDX_W{1'b0}};
                        j_idx      <= {J_IDX_W{1'b0}};
                        tile_k_idx <= {K_TILE_IDX_W{1'b0}};
                        issue_cnt  <= {K_COUNT_W{1'b0}};
                        acc_cnt    <= {K_COUNT_W{1'b0}};
                        acc        <= {ACC_WIDTH{1'b0}};
                        state      <= S_MAC;
                    end
                end

                S_MAC: begin
                    busy <= 1'b1;

                    if (chunk_len == {K_COUNT_W{1'b0}}) begin
                        issue_cnt <= {K_COUNT_W{1'b0}};
                        acc_cnt   <= {K_COUNT_W{1'b0}};
                        if (tile_k_idx == LAST_TILE_IDX_C) begin
                            state <= S_READ_C;
                        end else begin
                            tile_k_idx <= tile_k_idx + ONE_TILE;
                        end
                    end else if (chunk_complete) begin
                        if (tile_k_idx == LAST_TILE_IDX_C) begin
                            state <= S_READ_C;
                        end else begin
                            tile_k_idx <= tile_k_idx + ONE_TILE;
                        end
                        issue_cnt <= {K_COUNT_W{1'b0}};
                        acc_cnt   <= {K_COUNT_W{1'b0}};
                    end
                end

                // Issue a read of C so we can accumulate the existing value.
                S_READ_C: begin
                    busy   <= 1'b1;
                    c_en   <= 1'b1;
                    c_we   <= 1'b0;
                    c_addr <= addr_c_w;
                    state  <= S_READ_C_WAIT;
                end

                // Allow one cycle for synchronous RAM read to present data.
                S_READ_C_WAIT: begin
                    busy <= 1'b1;
                    state <= S_READ_C_WAIT2;
                end

                // Allow one cycle for synchronous RAM read to present data.
                S_READ_C_WAIT2: begin
                    busy <= 1'b1;
                    state <= S_WRITE;
                end


                S_WRITE: begin
                    busy   <= 1'b1;
                    c_en   <= 1'b1;
                    c_we   <= 1'b1;
                    c_addr <= addr_c_w;
                    c_di   <= acc[DATA_WIDTH-1:0] + c_dout_q;
                    state  <= S_ADVANCE;
                end

                S_ADVANCE: begin
                    busy       <= 1'b1;
                    tile_k_idx <= {K_TILE_IDX_W{1'b0}};
                    issue_cnt  <= {K_COUNT_W{1'b0}};
                    acc_cnt    <= {K_COUNT_W{1'b0}};
                    acc        <= {ACC_WIDTH{1'b0}};

                    if (j_idx == (N_DIM-1)) begin
                        j_idx <= {J_IDX_W{1'b0}};
                        if (i_idx == (M_DIM-1)) begin
                            state <= S_DONE;
                        end else begin
                            i_idx <= i_idx + ONE_I;
                            state <= S_MAC;
                        end
                    end else begin
                        j_idx <= j_idx + ONE_J;
                        state <= S_MAC;
                    end
                end

                S_DONE: begin
                    busy <= 1'b0;
                    done <= 1'b1;
                    if (!start)
                        state <= S_IDLE;
                end

                default: state <= S_IDLE;
            endcase
        end
    end
endmodule
