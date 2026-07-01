module pwm_generator #(
    parameter int RES_BITS  = 8,
    parameter int NUM_CH    = 4
) (
    input  logic clk, rst_n, enable,
    input  logic [RES_BITS-1:0] duty [NUM_CH],
    output logic [NUM_CH-1:0]   pwm_out
);
    logic [RES_BITS-1:0] counter;
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) counter <= '0;
        else if (enable) counter <= counter + 1'b1;
    end
    genvar i;
    generate
        for (i = 0; i < NUM_CH; i++) begin : gen_ch
            always_ff @(posedge clk or negedge rst_n) begin
                if (!rst_n) pwm_out[i] <= 1'b0;
                else if (!enable) pwm_out[i] <= 1'b0;
                else pwm_out[i] <= (counter < duty[i]);
            end
        end
    endgenerate
endmodule : pwm_generator
