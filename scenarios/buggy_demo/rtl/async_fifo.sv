//=====================================================================
// Module      : async_fifo
// Description : Asynchronous (dual-clock) FIFO - CDC safe
//               - Gray-coded read/write pointers, 2-flop synchronized
//                 across clock domains (standard Cummings-style design)
//               - Depth must be a power of 2
//=====================================================================

module async_fifo #(
    parameter int DATA_WIDTH = 8,
    parameter int DEPTH      = 16   // must be power of 2
) (
    // Write clock domain
    input  logic                   wr_clk,
    input  logic                   wr_rst_n,
    input  logic                   wr_en,
    input  logic [DATA_WIDTH-1:0]  wr_data,
    output logic                   full,

    // Read clock domain
    input  logic                   rd_clk,
    input  logic                   rd_rst_n,
    input  logic                   rd_en,
    output logic [DATA_WIDTH-1:0]  rd_data,
    output logic                   empty
);

    localparam int ADDR_W = $clog2(DEPTH);

    //-----------------------------------------------------------
    // Dual-port memory (inferred as BRAM/distributed RAM by tools)
    //-----------------------------------------------------------
    logic [DATA_WIDTH-1:0] mem [DEPTH-1:0];

    //-----------------------------------------------------------
    // Binary + Gray pointers (one extra MSB for full/empty detect)
    //-----------------------------------------------------------
    logic [ADDR_W:0] wr_ptr_bin,  wr_ptr_gray;
    logic [ADDR_W:0] rd_ptr_bin,  rd_ptr_gray;

    // Synchronized (into the opposite domain) versions
    logic [ADDR_W:0] wr_ptr_gray_sync1, wr_ptr_gray_sync2;  // synced into rd_clk
    logic [ADDR_W:0] rd_ptr_gray_sync1, rd_ptr_gray_sync2;  // synced into wr_clk

    function automatic logic [ADDR_W:0] bin2gray(input logic [ADDR_W:0] bin);
        return (bin >> 1) ^ bin;
    endfunction

    //=============================================================
    // WRITE CLOCK DOMAIN
    //=============================================================
    logic wr_valid;
    assign wr_valid = wr_en && !full;

    always_ff @(posedge wr_clk or negedge wr_rst_n) begin
        if (!wr_rst_n) begin
            wr_ptr_bin  <= '0;
            wr_ptr_gray <= '0;
        end else if (wr_valid) begin
            mem[wr_ptr_bin[ADDR_W-1:0]] <= wr_data;
            wr_ptr_bin                 <= wr_ptr_bin + 1'b1;
            wr_ptr_gray                <= bin2gray(wr_ptr_bin + 1'b1);
        end
    end

    // Synchronize read pointer (gray) into write clock domain
    always_ff @(posedge wr_clk or negedge wr_rst_n) begin
        if (!wr_rst_n) begin
            rd_ptr_gray_sync1 <= '0;
            rd_ptr_gray_sync2 <= '0;
        end else begin
            rd_ptr_gray_sync1 <= rd_ptr_gray;
            rd_ptr_gray_sync2 <= rd_ptr_gray_sync1;
        end
    end

    // FULL: next write pointer (gray) equals read pointer with top two
    // bits inverted (Cummings' standard gray-code full condition)
    logic [ADDR_W:0] wr_ptr_gray_next;
    assign wr_ptr_gray_next = bin2gray(wr_ptr_bin + 1'b1);

    always_ff @(posedge wr_clk or negedge wr_rst_n) begin
        if (!wr_rst_n)
            full <= 1'b0;
        else
            full <= (wr_ptr_gray_next == {~rd_ptr_gray_sync2[ADDR_W:ADDR_W-1],
                                            rd_ptr_gray_sync2[ADDR_W-2:0]});
    end

    //=============================================================
    // READ CLOCK DOMAIN
    //=============================================================
    logic rd_valid;
    assign rd_valid = rd_en && !empty;

    always_ff @(posedge rd_clk or negedge rd_rst_n) begin
        if (!rd_rst_n) begin
            rd_ptr_bin  <= '0;
            rd_ptr_gray <= '0;
            rd_data     <= '0;
        end else if (rd_valid) begin
            rd_data     <= mem[rd_ptr_bin[ADDR_W-1:0]];
            rd_ptr_bin  <= rd_ptr_bin + 1'b1;
            rd_ptr_gray <= bin2gray(rd_ptr_bin + 1'b1);
        end
    end

    // Synchronize write pointer (gray) into read clock domain
    always_ff @(posedge rd_clk or negedge rd_rst_n) begin
        if (!wr_rst_n) begin
            wr_ptr_gray_sync1 <= '0;
            wr_ptr_gray_sync2 <= '0;
        end else begin
            wr_ptr_gray_sync1 <= wr_ptr_gray;
            wr_ptr_gray_sync2 <= wr_ptr_gray_sync1;
        end
    end

    // EMPTY: next read pointer (gray) equals synchronized write pointer
    logic [ADDR_W:0] rd_ptr_gray_next;
    assign rd_ptr_gray_next = bin2gray(rd_ptr_bin + 1'b1);

    always_ff @(posedge rd_clk or negedge rd_rst_n) begin
        if (!rd_rst_n)
            empty <= 1'b1;
        else
            empty <= (rd_ptr_gray_next == wr_ptr_gray_sync2);
    end

endmodule : async_fifo
