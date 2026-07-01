//=====================================================================
// Module      : sync_fifo
// Description : Synchronous (single-clock) FIFO
//               - Parameterized data width and depth (must be power of 2)
//               - Standard full/empty flags
//               - Optional almost-full / almost-empty thresholds
//=====================================================================

module sync_fifo #(
    parameter int DATA_WIDTH   = 8,
    parameter int DEPTH        = 16,               // must be power of 2
    parameter int ALMOST_FULL_THRESH  = DEPTH - 2,
    parameter int ALMOST_EMPTY_THRESH = 2
) (
    input  logic                   clk,
    input  logic                   rst_n,

    // Write port
    input  logic                   wr_en,
    input  logic [DATA_WIDTH-1:0]  wr_data,
    output logic                   full,
    output logic                   almost_full,

    // Read port
    input  logic                   rd_en,
    output logic [DATA_WIDTH-1:0]  rd_data,
    output logic                   empty,
    output logic                   almost_empty,

    output logic [$clog2(DEPTH):0] count   // occupancy, 0..DEPTH
);

    localparam int ADDR_W = $clog2(DEPTH);

    logic [DATA_WIDTH-1:0] mem [DEPTH-1:0];

    logic [ADDR_W-1:0] wr_ptr, rd_ptr;
    logic              wr_valid, rd_valid;

    assign wr_valid = wr_en && !full;
    assign rd_valid = rd_en && !empty;

    //-----------------------------------------------------------
    // Write pointer / memory write
    //-----------------------------------------------------------
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            wr_ptr <= '0;
        end else if (wr_valid) begin
            mem[wr_ptr] <= wr_data;
            wr_ptr      <= wr_ptr + 1'b1;
        end
    end

    //-----------------------------------------------------------
    // Read pointer / memory read (registered output)
    //-----------------------------------------------------------
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            rd_ptr  <= '0;
            rd_data <= '0;
        end else if (rd_valid) begin
            rd_data <= mem[rd_ptr];
            rd_ptr  <= rd_ptr + 1'b1;
        end
    end

    //-----------------------------------------------------------
    // Occupancy counter
    //-----------------------------------------------------------
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            count <= '0;
        end else begin
            case ({wr_valid, rd_valid})
                2'b10:   count <= count + 1'b1;
                2'b01:   count <= count - 1'b1;
                default: count <= count; // 00: no change, 11: net zero change
            endcase
        end
    end

    //-----------------------------------------------------------
    // Status flags
    //-----------------------------------------------------------
    assign full         = (count == DEPTH[$clog2(DEPTH):0]);
    assign empty        = (count == '0);
    assign almost_full   = (count >= ALMOST_FULL_THRESH[$clog2(DEPTH):0]);
    assign almost_empty  = (count <= ALMOST_EMPTY_THRESH[$clog2(DEPTH):0]);

endmodule : sync_fifo
