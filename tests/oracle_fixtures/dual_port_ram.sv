module dual_port_ram #(
    parameter int ADDR_WIDTH = 8,
    parameter int DATA_WIDTH = 32
)(
    input  logic                     clk, rst_n,
    input  logic                     a_wr_en, a_rd_en,
    input  logic [ADDR_WIDTH-1:0]    a_addr,
    input  logic [DATA_WIDTH-1:0]    a_wdata,
    input  logic [DATA_WIDTH/8-1:0]  a_be,
    output logic [DATA_WIDTH-1:0]    a_rdata,
    output logic                     a_valid,
    input  logic                     b_wr_en, b_rd_en,
    input  logic [ADDR_WIDTH-1:0]    b_addr,
    input  logic [DATA_WIDTH-1:0]    b_wdata,
    input  logic [DATA_WIDTH/8-1:0]  b_be,
    output logic [DATA_WIDTH-1:0]    b_rdata,
    output logic                     b_valid,
    output logic                     collision
);
localparam int DEPTH = (1 << ADDR_WIDTH);
logic [DATA_WIDTH-1:0] mem [0:DEPTH-1];
integer i;
always_comb begin
    collision = 1'b0;
    if ((a_wr_en || a_rd_en) && (b_wr_en || b_rd_en) && (a_addr == b_addr))
        collision = 1'b1;
end
always_ff @(posedge clk or negedge rst_n) begin
    if(!rst_n) begin a_valid <= 1'b0; a_rdata <= '0; end
    else begin
        a_valid <= 1'b0;
        if(a_wr_en) begin
            for(i=0;i<DATA_WIDTH/8;i=i+1)
                if(a_be[i]) mem[a_addr][8*i +:8] <= a_wdata[8*i +:8];
        end
        if(a_rd_en) begin a_rdata <= mem[a_addr]; a_valid <= 1'b1; end
    end
end
always_ff @(posedge clk or negedge rst_n) begin
    if(!rst_n) begin b_valid <= 1'b0; b_rdata <= '0; end
    else begin
        b_valid <= 1'b0;
        if(b_wr_en) begin
            for(i=0;i<DATA_WIDTH/8;i=i+1)
                if(b_be[i]) mem[b_addr][8*i +:8] <= b_wdata[8*i +:8];
        end
        if(b_rd_en) begin b_rdata <= mem[b_addr]; b_valid <= 1'b1; end
    end
end
endmodule
