// CDC Demo: Comprehensive clock domain crossing scenarios
// Covers all SpyGlass Ac_cdc01 through Ac_cdc10 rule equivalents
//
// Domain A: clk_a (fast clock)
// Domain B: clk_b (slow clock)

module cdc_example (
    input  logic       clk_a,
    input  logic       clk_b,
    input  logic       rst_n,
    input  logic       data_in,
    input  logic [7:0] bus_in,
    output logic       data_out,
    output logic [7:0] bus_out,
    output logic       pulse_out,
    output logic       handshake_done
);

    // =====================================================
    // Domain A signals (driven by clk_a)
    // =====================================================
    logic       flag_a;
    logic       pulse_a;
    logic [7:0] counter_a;
    logic       rst_from_a;

    always_ff @(posedge clk_a or negedge rst_n) begin
        if (!rst_n) begin
            flag_a    <= 1'b0;
            counter_a <= 8'd0;
            rst_from_a <= 1'b0;
        end else begin
            flag_a    <= data_in;
            counter_a <= counter_a + 1'b1;
            rst_from_a <= (counter_a == 8'hFF);
        end
    end

    // Pulse generator in domain A
    always_ff @(posedge clk_a or negedge rst_n) begin
        if (!rst_n)
            pulse_a <= 1'b0;
        else if (counter_a == 8'h80)
            pulse_a <= 1'b1;
        else
            pulse_a <= 1'b0;
    end

    // =====================================================
    // BUG 1 (Ac_cdc01): Missing synchronizer
    // flag_a read directly in clk_b domain
    // =====================================================
    always_ff @(posedge clk_b or negedge rst_n) begin
        if (!rst_n)
            data_out <= 1'b0;
        else
            data_out <= flag_a;  // CDC VIOLATION: direct use
    end

    // =====================================================
    // BUG 2 (Ac_cdc02): Multi-bit crossing without gray code
    // counter_a (8-bit) read in clk_b domain
    // =====================================================
    always_ff @(posedge clk_b or negedge rst_n) begin
        if (!rst_n)
            bus_out <= 8'd0;
        else
            bus_out <= counter_a;  // CDC VIOLATION: multi-bit
    end

    // =====================================================
    // BUG 3 (Ac_cdc03): Combinational logic before sync
    // flag_a goes through AND gate before entering clk_b
    // =====================================================
    logic combo_sig;
    assign combo_sig = flag_a & data_in;

    logic combo_synced;
    always_ff @(posedge clk_b or negedge rst_n) begin
        if (!rst_n)
            combo_synced <= 1'b0;
        else
            combo_synced <= combo_sig;  // CDC: glitch risk
    end

    // =====================================================
    // BUG 4 (Ac_cdc04): Reconvergence
    // flag_a synchronized through TWO independent paths
    // that reconverge in downstream logic
    // =====================================================
    logic flag_a_meta1, flag_a_sync1;
    logic flag_a_meta2, flag_a_sync2;

    always_ff @(posedge clk_b or negedge rst_n) begin
        if (!rst_n) begin
            flag_a_meta1 <= 1'b0;
            flag_a_sync1 <= 1'b0;
        end else begin
            flag_a_meta1 <= flag_a;       // Independent sync path 1
            flag_a_sync1 <= flag_a_meta1;
        end
    end

    always_ff @(posedge clk_b or negedge rst_n) begin
        if (!rst_n) begin
            flag_a_meta2 <= 1'b0;
            flag_a_sync2 <= 1'b0;
        end else begin
            flag_a_meta2 <= flag_a;       // Independent sync path 2
            flag_a_sync2 <= flag_a_meta2;
        end
    end

    // Reconvergence: both sync outputs used together
    logic reconverged;
    assign reconverged = flag_a_sync1 & flag_a_sync2;  // BUG: may see different values

    // =====================================================
    // BUG 5 (Ac_cdc05): Reset domain crossing
    // rst_from_a used in clk_b without reset synchronizer
    // =====================================================
    logic reg_b;
    always_ff @(posedge clk_b or negedge rst_from_a) begin
        if (!rst_from_a)
            reg_b <= 1'b0;
        else
            reg_b <= data_in;
    end

    // =====================================================
    // BUG 6 (Ac_cdc06): FIFO pointer crossing without gray
    // =====================================================
    logic [3:0] wr_ptr, rd_ptr;

    always_ff @(posedge clk_a or negedge rst_n) begin
        if (!rst_n)
            wr_ptr <= 4'd0;
        else
            wr_ptr <= wr_ptr + 1'b1;
    end

    always_ff @(posedge clk_b or negedge rst_n) begin
        if (!rst_n)
            rd_ptr <= 4'd0;
        else
            rd_ptr <= rd_ptr + 1'b1;
    end

    // BUG: binary wr_ptr read in clk_b domain (needs gray code)
    logic fifo_empty;
    always_ff @(posedge clk_b or negedge rst_n) begin
        if (!rst_n)
            fifo_empty <= 1'b1;
        else
            fifo_empty <= (wr_ptr == rd_ptr);  // CDC: binary ptr comparison
    end

    // =====================================================
    // BUG 7 (Ac_cdc08): Handshake without synchronizers
    // req from domain A, ack from domain B, neither synced
    // =====================================================
    logic req_a;
    logic ack_b;

    always_ff @(posedge clk_a or negedge rst_n) begin
        if (!rst_n)
            req_a <= 1'b0;
        else if (data_in && !req_a)
            req_a <= 1'b1;
        else if (ack_b)        // CDC BUG: reading ack_b in clk_a domain
            req_a <= 1'b0;
    end

    always_ff @(posedge clk_b or negedge rst_n) begin
        if (!rst_n)
            ack_b <= 1'b0;
        else if (req_a)        // CDC BUG: reading req_a in clk_b domain
            ack_b <= 1'b1;
        else
            ack_b <= 1'b0;
    end

    assign handshake_done = ack_b;

    // =====================================================
    // BUG 8 (Ac_cdc09): Pulse crossing
    // pulse_a may be missed by slower clk_b
    // =====================================================
    always_ff @(posedge clk_b or negedge rst_n) begin
        if (!rst_n)
            pulse_out <= 1'b0;
        else
            pulse_out <= pulse_a;  // CDC: pulse may be missed
    end

    // =====================================================
    // CORRECT: Proper 2-FF synchronizer (should NOT flag)
    // =====================================================
    logic flag_a_meta, flag_a_sync;
    always_ff @(posedge clk_b or negedge rst_n) begin
        if (!rst_n) begin
            flag_a_meta <= 1'b0;
            flag_a_sync <= 1'b0;
        end else begin
            flag_a_meta <= flag_a;      // Stage 1
            flag_a_sync <= flag_a_meta; // Stage 2
        end
    end

endmodule
