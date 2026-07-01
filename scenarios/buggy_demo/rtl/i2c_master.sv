//=====================================================================
// Module      : i2c_master
// Description : I2C Master Controller
//               - Supports Standard (100kHz) / Fast (400kHz) modes
//               - Single-byte and multi-byte read/write via simple
//                 command interface
//               - Clock stretching support (monitors SCL line)
//               - Generates START, repeated START, and STOP conditions
//
// Interface   : Simple FIFO-less command/response handshake
//               Drive cmd_* + start_transfer, wait for busy=0 and
//               check ack_error / rx_valid.
//=====================================================================

module i2c_master #(
    parameter int CLK_FREQ_HZ  = 50_000_000,  // system clock frequency
    parameter int I2C_FREQ_HZ  = 400_000      // target SCL frequency
) (
    input  logic        clk,
    input  logic        rst_n,

    //-----------------------------------------------------------
    // Command interface
    //-----------------------------------------------------------
    input  logic        start_transfer,  // pulse to begin op
    input  logic [6:0]  slave_addr,      // 7-bit slave address
    input  logic        rw,              // 0 = write, 1 = read
    input  logic [7:0]  tx_data,         // byte to write
    input  logic         stop_after,     // issue STOP after this byte
    output logic [7:0]  rx_data,         // byte read from slave
    output logic         rx_valid,        // rx_data valid pulse
    output logic         ack_error,       // slave NACK'd
    output logic         busy,            // transaction in progress

    //-----------------------------------------------------------
    // I2C physical lines (open-drain, use tri-state at top level)
    //-----------------------------------------------------------
    inout  wire          scl,
    inout  wire          sda
);

    //-----------------------------------------------------------
    // Clock divider: generate quarter-period ticks of SCL
    //-----------------------------------------------------------
    localparam int DIVIDER      = CLK_FREQ_HZ / (I2C_FREQ_HZ * 4);
    localparam int DIV_CNT_W    = $clog2(DIVIDER);

    logic [DIV_CNT_W-1:0] clk_div_cnt;
    logic [1:0]           quadrant;   // 0..3 within an SCL period
    logic                  tick;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            clk_div_cnt <= '0;
            tick        <= 1'b0;
        end else if (clk_div_cnt == DIVIDER[DIV_CNT_W-1:0] - 1'b1) begin
            clk_div_cnt <= '0;
            tick        <= 1'b1;
        end else begin
            clk_div_cnt <= clk_div_cnt + 1'b1;
            tick        <= 1'b0;
        end
    end

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            quadrant <= 2'd0;
        else if (tick)
            quadrant <= quadrant + 1'b1;
    end

    //-----------------------------------------------------------
    // Open-drain line control
    //-----------------------------------------------------------
    logic scl_out_en;   // 1 = drive SCL low, 0 = release (pulled high)
    logic sda_out_en;   // 1 = drive SDA low, 0 = release (pulled high)
    logic sda_in, scl_in;

    assign scl     = scl_out_en ? 1'b0 : 1'bz;
    assign sda     = sda_out_en ? 1'b0 : 1'bz;
    assign scl_in  = scl;
    assign sda_in  = sda;

    //-----------------------------------------------------------
    // Clock stretching detect: slave holds SCL low
    //-----------------------------------------------------------
    logic scl_stretch;
    assign scl_stretch = (~scl_out_en) && (scl_in == 1'b0);

    //-----------------------------------------------------------
    // FSM state encoding
    //-----------------------------------------------------------
    typedef enum logic [3:0] {
        S_IDLE,
        S_START,
        S_ADDR,
        S_ADDR_ACK,
        S_WR_DATA,
        S_WR_ACK,
        S_RD_DATA,
        S_RD_ACK,
        S_STOP,
        S_DONE
    } state_e;

    state_e state, state_n;

    logic [2:0] bit_cnt;      // 0..7 bit index within byte
    logic [7:0] shift_reg;    // tx/rx shift register
    logic       addr_phase;   // currently shifting address byte
    logic       rw_latched;
    logic       stop_latched;

    //-----------------------------------------------------------
    // Sequential state register
    //-----------------------------------------------------------
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            state <= S_IDLE;
        else if (tick && !scl_stretch)
            state <= state_n;
    end

    //-----------------------------------------------------------
    // Main datapath + control
    //-----------------------------------------------------------
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            scl_out_en   <= 1'b0;
            sda_out_en   <= 1'b0;
            bit_cnt      <= 3'd0;
            shift_reg    <= 8'h00;
            rw_latched   <= 1'b0;
            stop_latched <= 1'b0;
            rx_data      <= 8'h00;
            rx_valid     <= 1'b0;
            ack_error    <= 1'b0;
            busy         <= 1'b0;
        end else begin
            rx_valid <= 1'b0;  // default: single-cycle pulse

            if (!tick || scl_stretch) begin
                // hold everything while waiting for tick or slave stretch
            end else begin
                case (state)

                    //---------------------------------------------
                    S_IDLE: begin
                        scl_out_en <= 1'b0;
                        sda_out_en <= 1'b0;
                        busy       <= 1'b0;
                        if (start_transfer) begin
                            busy         <= 1'b1;
                            rw_latched   <= rw;
                            stop_latched <= stop_after;
                            shift_reg    <= {slave_addr, rw};
                            bit_cnt      <= 3'd0;
                            ack_error    <= 1'b0;
                        end
                    end

                    //---------------------------------------------
                    // START condition: SDA falls while SCL is high
                    S_START: begin
                        case (quadrant)
                            2'd0: begin sda_out_en <= 1'b0; scl_out_en <= 1'b0; end
                            2'd1: begin sda_out_en <= 1'b1; scl_out_en <= 1'b0; end // SDA falls
                            2'd2: begin sda_out_en <= 1'b1; scl_out_en <= 1'b1; end // SCL falls
                            2'd3: begin sda_out_en <= 1'b1; scl_out_en <= 1'b1; end
                        endcase
                    end

                    //---------------------------------------------
                    S_ADDR: begin
                        case (quadrant)
                            2'd0: sda_out_en <= ~shift_reg[7];  // set up data, SCL low
                            2'd1: scl_out_en <= 1'b0;           // release SCL (rises)
                            2'd2: scl_out_en <= 1'b0;           // sample point (master side)
                            2'd3: begin
                                scl_out_en <= 1'b1;             // SCL falls
                                shift_reg  <= {shift_reg[6:0], 1'b0};
                                bit_cnt    <= bit_cnt + 1'b1;
                            end
                        endcase
                    end

                    //---------------------------------------------
                    S_ADDR_ACK: begin
                        case (quadrant)
                            2'd0: sda_out_en <= 1'b0;  // release SDA for slave ACK
                            2'd1: scl_out_en <= 1'b0;  // release SCL (rises)
                            2'd2: ack_error  <= sda_in; // sample ACK (0 = ACK)
                            2'd3: begin
                                scl_out_en <= 1'b1;
                                bit_cnt    <= 3'd0;
                                shift_reg  <= tx_data;
                            end
                        endcase
                    end

                    //---------------------------------------------
                    S_WR_DATA: begin
                        case (quadrant)
                            2'd0: sda_out_en <= ~shift_reg[7];
                            2'd1: scl_out_en <= 1'b0;
                            2'd2: scl_out_en <= 1'b0;
                            2'd3: begin
                                scl_out_en <= 1'b1;
                                shift_reg  <= {shift_reg[6:0], 1'b0};
                                bit_cnt    <= bit_cnt + 1'b1;
                            end
                        endcase
                    end

                    //---------------------------------------------
                    S_WR_ACK: begin
                        case (quadrant)
                            2'd0: sda_out_en <= 1'b0;
                            2'd1: scl_out_en <= 1'b0;
                            2'd2: ack_error  <= sda_in;
                            2'd3: scl_out_en <= 1'b1;
                        endcase
                    end

                    //---------------------------------------------
                    S_RD_DATA: begin
                        case (quadrant)
                            2'd0: sda_out_en <= 1'b0;  // release SDA, slave drives
                            2'd1: scl_out_en <= 1'b0;  // SCL rises
                            2'd2: shift_reg  <= {shift_reg[6:0], sda_in}; // sample bit
                            2'd3: begin
                                scl_out_en <= 1'b1;
                                bit_cnt    <= bit_cnt + 1'b1;
                            end
                        endcase
                    end

                    //---------------------------------------------
                    S_RD_ACK: begin
                        // Master sends ACK (continue) or NACK (stop_after=last byte)
                        case (quadrant)
                            2'd0: sda_out_en <= stop_latched ? 1'b0 : 1'b1; // NACK=release, ACK=drive low
                            2'd1: scl_out_en <= 1'b0;
                            2'd2: begin
                                rx_data  <= shift_reg;
                                rx_valid <= 1'b1;
                            end
                            2'd3: scl_out_en <= 1'b1;
                        endcase
                    end

                    //---------------------------------------------
                    // STOP condition: SDA rises while SCL is high
                    S_STOP: begin
                        case (quadrant)
                            2'd0: begin sda_out_en <= 1'b1; scl_out_en <= 1'b1; end
                            2'd1: begin sda_out_en <= 1'b1; scl_out_en <= 1'b0; end // SCL rises
                            2'd2: begin sda_out_en <= 1'b0; scl_out_en <= 1'b0; end // SDA rises
                            2'd3: begin sda_out_en <= 1'b0; scl_out_en <= 1'b0; end
                        endcase
                    end

                    //---------------------------------------------
                    S_DONE: begin
                        busy <= 1'b0;
                    end

                    default: ;
                endcase
            end
        end
    end

    //-----------------------------------------------------------
    // Next-state logic (combinational)
    //-----------------------------------------------------------
    always_comb begin
        state_n = state;
        case (state)
            S_IDLE:      state_n = start_transfer ? S_START : S_IDLE;
            S_START:     state_n = (quadrant == 2'd3) ? S_ADDR : S_START;
            S_ADDR:      state_n = (quadrant == 2'd3 && bit_cnt == 3'd7) ? S_ADDR_ACK : S_ADDR;
            S_ADDR_ACK:  state_n = (quadrant == 2'd3) ?
                                       (rw_latched ? S_RD_DATA : S_WR_DATA) : S_ADDR_ACK;
            S_WR_DATA:   state_n = (quadrant == 2'd3 && bit_cnt == 3'd7) ? S_WR_ACK : S_WR_DATA;
            S_WR_ACK:    state_n = (quadrant == 2'd3) ?
                                       (stop_latched ? S_STOP : S_DONE) : S_WR_ACK;
            S_RD_DATA:   state_n = (quadrant == 2'd3 && bit_cnt == 3'd7) ? S_RD_ACK : S_RD_DATA;
            S_RD_ACK:    state_n = (quadrant == 2'd3) ?
                                       (stop_latched ? S_STOP : S_DONE) : S_RD_ACK;
            S_STOP:      state_n = (quadrant == 2'd3) ? S_IDLE : S_STOP;
            S_DONE:      state_n = S_IDLE;
            default:     state_n = S_IDLE;
        endcase
    end

endmodule : i2c_master
