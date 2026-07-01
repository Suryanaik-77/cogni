// Top-level: multi-clock system with hierarchy, DFT, and CDC
module top_system (
    input  logic        clk_core,
    input  logic        clk_io,
    input  logic        rst_n,
    input  logic        test_mode,
    input  logic        scan_en,
    input  logic        scan_in,
    output logic        scan_out,
    input  logic [7:0]  data_in,
    output logic [7:0]  data_out,
    output logic        valid_out,
    output logic        passthrough_out
);

    // =====================================================
    // DFT BUG: Gated clock without test bypass
    // =====================================================
    logic clk_gated;
    logic clk_enable;
    assign clk_gated = clk_core & clk_enable;  // No test_mode bypass

    // =====================================================
    // DFT BUG: Clock mux without glitch-free switching
    // =====================================================
    logic sel_io;
    logic clk_muxed;
    assign clk_muxed = sel_io ? clk_io : clk_core;

    // =====================================================
    // DFT BUG: Tristate not gated by test_mode
    // =====================================================
    logic tri_en;
    logic [7:0] tri_data;
    assign tri_data = tri_en ? data_in : 8'bz;

    // =====================================================
    // DFT BUG: Large memory without BIST
    // =====================================================
    logic [31:0] mem_array [0:255];  // 256x32 = 8192 bits, no BIST

    // =====================================================
    // Core domain logic (clk_core)
    // =====================================================
    logic [7:0] core_data;
    logic       core_valid;

    always_ff @(posedge clk_core or negedge rst_n) begin
        if (!rst_n) begin
            core_data  <= 8'd0;
            core_valid <= 1'b0;
            clk_enable <= 1'b0;
            sel_io     <= 1'b0;
            tri_en     <= 1'b0;
        end else begin
            core_data  <= data_in;
            core_valid <= |data_in;
            clk_enable <= 1'b1;
            sel_io     <= data_in[7];
            tri_en     <= data_in[6];
        end
    end

    // =====================================================
    // IO domain logic (clk_io) — reads core_data across CDC
    // =====================================================
    logic [7:0] io_data;
    logic       io_valid;

    always_ff @(posedge clk_io or negedge rst_n) begin
        if (!rst_n) begin
            io_data  <= 8'd0;
            io_valid <= 1'b0;
        end else begin
            io_data  <= core_data;   // CDC: multi-bit crossing
            io_valid <= core_valid;  // CDC: single-bit crossing
        end
    end

    assign data_out = io_data;
    assign valid_out = io_valid;

    // =====================================================
    // HIER BUG: Feedthrough signal
    // =====================================================
    assign passthrough_out = data_in[0];

    // =====================================================
    // HIER: Instance with unconnected ports
    // =====================================================
    logic [7:0] cnt_val;

    sub_counter u_counter (
        .clk      (clk_core),
        .rst_n    (),          // HIER BUG: reset unconnected!
        .enable   (core_valid),
        .scan_en  (scan_en),
        .scan_in  (scan_in),
        .scan_out (scan_out),
        .count    (cnt_val)
    );

    // =====================================================
    // DFT BUG: Async reset not gated during scan
    // =====================================================
    logic ext_rst_n;
    assign ext_rst_n = rst_n;

    logic [3:0] async_reg;
    always_ff @(posedge clk_core or negedge ext_rst_n) begin
        if (!ext_rst_n)
            async_reg <= 4'd0;
        else
            async_reg <= data_in[3:0];
    end

    // =====================================================
    // FSM without assertions (SVA_fsm_uncovered)
    // =====================================================
    typedef enum logic [1:0] {
        IDLE  = 2'b00,
        LOAD  = 2'b01,
        PROC  = 2'b10,
        DONE  = 2'b11
    } state_t;

    state_t state, next_state;

    always_ff @(posedge clk_core or negedge rst_n) begin
        if (!rst_n)
            state <= IDLE;
        else
            state <= next_state;
    end

    always_comb begin
        next_state = state;
        case (state)
            IDLE: if (core_valid) next_state = LOAD;
            LOAD: next_state = PROC;
            PROC: if (cnt_val == 8'hFF) next_state = DONE;
            DONE: next_state = IDLE;
        endcase
    end

endmodule
