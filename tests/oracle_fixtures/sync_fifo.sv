module sync_fifo #(
    parameter int DATA_WIDTH = 32,
    parameter int DEPTH      = 16
)(
    input  logic                  clk, rst_n, wr_en, rd_en,
    input  logic [DATA_WIDTH-1:0] din,
    output logic [DATA_WIDTH-1:0] dout,
    output logic                  full, empty, almost_full, almost_empty, overflow, underflow
);
localparam int ADDR_W = $clog2(DEPTH);
logic [DATA_WIDTH-1:0] mem [0:DEPTH-1];
logic [ADDR_W-1:0] wr_ptr, rd_ptr;
logic [ADDR_W:0]   count;
always_ff @(posedge clk or negedge rst_n) begin
    if(!rst_n) begin wr_ptr <= '0; overflow <= 1'b0; end
    else begin
        overflow <= 1'b0;
        if(wr_en) begin
            if(!full) begin
                mem[wr_ptr] <= din;
                if(wr_ptr == DEPTH-1) wr_ptr <= '0;
                else wr_ptr <= wr_ptr + 1'b1;
            end else overflow <= 1'b1;
        end
    end
end
always_ff @(posedge clk or negedge rst_n) begin
    if(!rst_n) begin rd_ptr <= '0; dout <= '0; underflow <= 1'b0; end
    else begin
        underflow <= 1'b0;
        if(rd_en) begin
            if(!empty) begin
                dout <= mem[rd_ptr];
                if(rd_ptr == DEPTH-1) rd_ptr <= '0;
                else rd_ptr <= rd_ptr + 1'b1;
            end else underflow <= 1'b1;
        end
    end
end
always_ff @(posedge clk or negedge rst_n) begin
    if(!rst_n) count <= '0;
    else begin
        case({wr_en && !full, rd_en && !empty})
            2'b10: count <= count + 1'b1;
            2'b01: count <= count - 1'b1;
            default: count <= count;
        endcase
    end
end
always_comb begin
    full  = (count == DEPTH);
    empty = (count == 0);
    almost_full  = (count >= DEPTH-1);
    almost_empty = (count <= 1);
end
endmodule
