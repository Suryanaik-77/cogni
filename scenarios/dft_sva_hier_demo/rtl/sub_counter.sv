// Sub-module: 8-bit counter with scan and assertions
module sub_counter (
    input  logic       clk,
    input  logic       rst_n,
    input  logic       enable,
    input  logic       scan_en,
    input  logic       scan_in,
    output logic       scan_out,
    output logic [7:0] count
);

    // =====================================================
    // BUG 1 (DFT): FF without scan mux — not scannable
    // =====================================================
    logic [7:0] shadow_reg;
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            shadow_reg <= 8'd0;
        else
            shadow_reg <= count;  // No scan_en mux — DFT violation
    end

    // CORRECT: FF with scan mux (scannable)
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            count <= 8'd0;
        else if (scan_en)
            count <= {count[6:0], scan_in};  // Scan path
        else if (enable)
            count <= count + 1'b1;
    end

    assign scan_out = count[7];

    // =====================================================
    // SVA: Assertion WITHOUT clock — will not be sampled
    // =====================================================
    no_clk_check: assert property (count < 8'd200);

    // =====================================================
    // SVA: Assertion without disable iff — fires during reset
    // =====================================================
    count_range: assert property (
        @(posedge clk) count <= 8'd255
    );

    // =====================================================
    // SVA: Vacuous implication — antecedent is constant false
    // =====================================================
    dead_check: assert property (
        @(posedge clk) disable iff (!rst_n)
        1'b0 |-> count == 8'd0
    );

    // =====================================================
    // SVA: Unbounded liveness — cannot complete in sim
    // =====================================================
    liveness_check: assert property (
        @(posedge clk) disable iff (!rst_n)
        enable |-> ##[0:$] (count == 8'hFF)
    );

    // =====================================================
    // SVA: Assume in RTL design (should be in testbench)
    // =====================================================
    input_assume: assume property (
        @(posedge clk) !scan_en |-> enable
    );

    // =====================================================
    // SVA: Assert without else action — silent failure
    // =====================================================
    range_check: assert property (
        @(posedge clk) disable iff (!rst_n)
        count < 8'd250
    );

    // CORRECT: Assertion with proper else
    overflow_check: assert property (
        @(posedge clk) disable iff (!rst_n)
        count != 8'hFF
    ) else $error("Counter overflow!");

    // No cover properties at all — SVA_no_cover
    // (assertions exist but no covers)

endmodule
