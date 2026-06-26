// -----------------------------------------------------------------------------
// spi_master.sv
//
// A small SPI master that LOOKS clean and disciplined (always_ff/always_comb
// split, parameterized, reset handled) but carries several real, Verilator-
// catchable bugs hidden inside good-looking style. The point is to surprise a
// rulebook: a careful reader skimming the structure would predict "clean," but
// the tool proves otherwise -> the agent learns.
//
// Hidden bugs (do NOT fix these — they are the test):
//   1. WIDTH      : bit_cnt (3-bit) compared against the int parameter
//                   DATA_BITS, and shift_cnt arithmetic mixes widths.
//   2. CASE       : the state decode `case (state)` has no default arm.
//   3. LATCH      : in the always_comb, `next_state` and `sclk_en` are not
//                   assigned on every path -> inferred latch.
//   4. BLKSEQ     : blocking '=' used inside the sequential always_ff.
// -----------------------------------------------------------------------------
module spi_master #(
    parameter int DATA_BITS = 8,
    parameter int CLK_DIV   = 4
) (
    input  logic                   clk,
    input  logic                   rst_n,

    // Control
    input  logic                   start,
    input  logic [DATA_BITS-1:0]   tx_data,
    output logic [DATA_BITS-1:0]   rx_data,
    output logic                   busy,
    output logic                   done,

    // SPI pins
    output logic                   sclk,
    output logic                   mosi,
    input  logic                   miso,
    output logic                   cs_n
);

    typedef enum logic [1:0] {
        IDLE   = 2'b00,
        LOAD   = 2'b01,
        SHIFT  = 2'b10,
        FINISH = 2'b11
    } state_t;

    state_t state, next_state;

    logic [DATA_BITS-1:0] tx_shift, rx_shift;
    logic [2:0]           bit_cnt;        // BUG 1: only 3 bits, but counts to DATA_BITS (8)
    logic [7:0]           clk_cnt;
    logic                 sclk_en;

    // ----------------------------------------------------------------
    // Clock divider for SCLK
    // ----------------------------------------------------------------
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            clk_cnt <= 8'd0;
            sclk    <= 1'b0;
        end else if (sclk_en) begin
            if (clk_cnt == CLK_DIV - 1) begin
                clk_cnt = 8'd0;            // BUG 4: blocking '=' inside always_ff
                sclk    <= ~sclk;
            end else begin
                clk_cnt <= clk_cnt + 1'b1;
            end
        end
    end

    // ----------------------------------------------------------------
    // Next-state logic (combinational)
    // ----------------------------------------------------------------
    always_comb begin
        // BUG 3: next_state and sclk_en are NOT given a default here, so the
        // paths below that omit them infer a latch.
        case (state)                       // BUG 2: no default arm
            IDLE: begin
                sclk_en    = 1'b0;
                if (start) next_state = LOAD;
                else       next_state = IDLE;
            end
            LOAD: begin
                sclk_en    = 1'b0;
                next_state = SHIFT;
            end
            SHIFT: begin
                sclk_en = 1'b1;
                // BUG 1: bit_cnt is 3 bits, DATA_BITS is a 32-bit int
                if (bit_cnt == DATA_BITS) next_state = FINISH;
                else                      next_state = SHIFT;
            end
            FINISH: begin
                next_state = IDLE;
                // sclk_en omitted on this arm -> reinforces the latch on sclk_en
            end
        endcase
    end

    // ----------------------------------------------------------------
    // State register + datapath
    // ----------------------------------------------------------------
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state    <= IDLE;
            bit_cnt  <= 3'd0;
            tx_shift <= '0;
            rx_shift <= '0;
        end else begin
            state <= next_state;
            case (state)
                LOAD: begin
                    tx_shift <= tx_data;
                    bit_cnt  <= 3'd0;
                end
                SHIFT: begin
                    tx_shift <= {tx_shift[DATA_BITS-2:0], 1'b0};
                    rx_shift <= {rx_shift[DATA_BITS-2:0], miso};
                    bit_cnt  <= bit_cnt + 1'b1;
                end
                default: ; // no-op
            endcase
        end
    end

    // ----------------------------------------------------------------
    // Outputs
    // ----------------------------------------------------------------
    assign busy    = (state != IDLE);
    assign done    = (state == FINISH);
    assign cs_n    = (state == IDLE);
    assign mosi    = tx_shift[DATA_BITS-1];
    assign rx_data = rx_shift;

endmodule
