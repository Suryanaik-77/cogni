module sync_fifo #(
    parameter int DATA_WIDTH = 32,
    parameter int DEPTH      = 16,                     // must be power of 2
    parameter int ADDR_WIDTH = $clog2(DEPTH)
) (
    input  logic                   clk,
    input  logic                   rst_n,      // active-low async reset

    // Write interface
    input  logic                   wr_en,
    input  logic [DATA_WIDTH-1:0]  wr_data,
    output logic                   full,

    // Read interface
    input  logic                   rd_en,
    output logic [DATA_WIDTH-1:0]  rd_data,
    output logic                   empty,

    // Status
    output logic [ADDR_WIDTH:0]    count
);

    // Memory array
    logic [DATA_WIDTH-1:0] mem [DEPTH-1:0];

    // Pointers (extra MSB for full/empty disambiguation)
    logic [ADDR_WIDTH:0] wr_ptr, rd_ptr;

    logic wr_valid, rd_valid;

    assign wr_valid = wr_en & ~full;
    assign rd_valid = rd_en & ~empty;

    // Write logic
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            wr_ptr <= '0;
        end else if (wr_valid) begin
            mem[wr_ptr[ADDR_WIDTH-1:0]] <= wr_data;
            wr_ptr <= wr_ptr + 1'b1;
        end
    end

    // Read logic
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            rd_ptr <= '0;
        end else if (rd_valid) begin
            rd_ptr <= rd_ptr + 1'b1;
        end
    end

    // Combinational read path for better timing
    assign rd_data = mem[rd_ptr[ADDR_WIDTH-1:0]];

    // Status flags
    assign count = wr_ptr - rd_ptr;
    assign full  = (count == DEPTH[ADDR_WIDTH:0]);
    assign empty = (count == 0);

endmodule
