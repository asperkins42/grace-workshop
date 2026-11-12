// Dual-port scratchpad BRAM (read-first on both ports)
module rams_tdp_rf_rf #(
    parameter integer DATA_WIDTH = 32,
    parameter integer ADDR_WIDTH = 8
) (
    input                       clka,
    input                       clkb,
    input                       ena,
    input                       enb,
    input                       wea,
    input                       web,
    input      [ADDR_WIDTH-1:0] addra,
    input      [ADDR_WIDTH-1:0] addrb,
    input      [DATA_WIDTH-1:0] dia,
    input      [DATA_WIDTH-1:0] dib,
    output reg [DATA_WIDTH-1:0] doa,
    output reg [DATA_WIDTH-1:0] dob
);

    localparam integer DEPTH = (1 << ADDR_WIDTH);

    (* ram_style = "block" *)
    reg [DATA_WIDTH-1:0] ram [0:DEPTH-1];

    always @(posedge clka) begin
        if (ena) begin
`ifndef SYNTHESIS
            if (wea && web && enb && (addra == addrb))
                $fatal(1, "rams_tdp_rf_rf: simultaneous same-address write on port A/B (%0d)", addra);
`endif
            if (wea)
                ram[addra] <= dia;
            doa <= ram[addra];
        end
    end

    always @(posedge clkb) begin
        if (enb) begin
`ifndef SYNTHESIS
            if (web && wea && ena && (addra == addrb))
                $fatal(1, "rams_tdp_rf_rf: simultaneous same-address write on port B/A (%0d)", addrb);
`endif
            if (web)
                ram[addrb] <= dib;
            dob <= ram[addrb];
        end
    end

endmodule
