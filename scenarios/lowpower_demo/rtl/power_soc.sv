//=====================================================================
// Module      : power_soc
// Description : Low-power subsystem with three power domains
//               - PD_AON : always-on power controller + wake logic
//               - PD_CPU : switchable compute core (power-gated)
//               - PD_MEM : switchable low-voltage scratchpad (0.8V rail)
//
//   Power intent lives in power_soc.upf. This RTL is written to be
//   power-aware: the AON controller sequences isolation / retention /
//   power-switch enables, and domain-crossing outputs are (mostly)
//   isolation-clamped.  A few crossings are intentionally left
//   unprotected so the UPF consistency checker has something to find.
//=====================================================================

module power_soc #(
    parameter int DATA_W = 32,
    parameter int ADDR_W = 6
) (
    input  logic              clk,
    input  logic              rst_n,

    // Power sequencing requests (AON)
    input  logic              sleep_req,
    input  logic              wake_req,

    // Command interface into the CPU domain (AON -> CPU)
    input  logic              cmd_valid,
    input  logic [DATA_W-1:0] cmd_data,

    // Results out of the CPU domain (CPU -> AON)
    output logic [DATA_W-1:0] result_data,
    output logic              result_valid,

    // Raw core activity flag (CPU -> AON)
    output logic              cpu_active,

    // Scratchpad read data (MEM 0.8V -> AON 1.0V)
    output logic [DATA_W-1:0] scratch_rdata,

    // Power status (AON)
    output logic              cpu_powered,
    output logic              mem_powered
);

    //=================================================================
    // PD_AON : power-control FSM (always powered)
    //=================================================================
    typedef enum logic [2:0] {
        PS_RUN,        // all domains powered
        PS_ISO,        // assert isolation clamps
        PS_SAVE,       // retention save
        PS_OFF,        // power switch off
        PS_RESTORE,    // power on + retention restore
        PS_DEISO       // release isolation
    } pstate_e;

    pstate_e pstate, pstate_n;

    // Power-control outputs (map to UPF switches / iso / retention nets)
    logic cpu_pwr_en;       // 1 = CPU domain powered
    logic cpu_iso_en;       // 1 = isolate CPU outputs
    logic cpu_ret_save;     // retention save pulse
    logic cpu_ret_restore;  // retention restore pulse
    logic mem_pwr_en;
    logic mem_iso_en;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) pstate <= PS_RUN;
        else        pstate <= pstate_n;
    end

    always_comb begin
        pstate_n = pstate;
        case (pstate)
            PS_RUN:     pstate_n = sleep_req ? PS_ISO     : PS_RUN;
            PS_ISO:     pstate_n = PS_SAVE;
            PS_SAVE:    pstate_n = PS_OFF;
            PS_OFF:     pstate_n = wake_req  ? PS_RESTORE : PS_OFF;
            PS_RESTORE: pstate_n = PS_DEISO;
            PS_DEISO:   pstate_n = PS_RUN;
            default:    pstate_n = PS_RUN;
        endcase
    end

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            cpu_pwr_en      <= 1'b1;
            cpu_iso_en      <= 1'b0;
            cpu_ret_save    <= 1'b0;
            cpu_ret_restore <= 1'b0;
            mem_pwr_en      <= 1'b1;
            mem_iso_en      <= 1'b0;
        end else begin
            cpu_ret_save    <= 1'b0;
            cpu_ret_restore <= 1'b0;
            case (pstate_n)
                PS_ISO:     begin cpu_iso_en <= 1'b1; mem_iso_en <= 1'b1; end
                PS_SAVE:    cpu_ret_save <= 1'b1;
                PS_OFF:     begin cpu_pwr_en <= 1'b0; mem_pwr_en <= 1'b0; end
                PS_RESTORE: begin cpu_pwr_en <= 1'b1; mem_pwr_en <= 1'b1;
                                  cpu_ret_restore <= 1'b1; end
                PS_DEISO:   begin cpu_iso_en <= 1'b0; mem_iso_en <= 1'b0; end
                default: ;
            endcase
        end
    end

    assign cpu_powered = cpu_pwr_en;
    assign mem_powered = mem_pwr_en;

    //=================================================================
    // PD_CPU : switchable compute core
    //=================================================================
    logic [DATA_W-1:0] cpu_acc;     // accumulator — MUST retain across sleep
    logic [DATA_W-1:0] cpu_result;
    logic              cpu_done;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            cpu_acc    <= '0;
            cpu_result <= '0;
            cpu_done   <= 1'b0;
        end else if (cmd_valid) begin
            cpu_acc    <= cpu_acc + cmd_data;
            cpu_result <= cpu_acc + cmd_data;
            cpu_done   <= 1'b1;
        end else begin
            cpu_done   <= 1'b0;
        end
    end

    //=================================================================
    // PD_MEM : low-voltage scratchpad (0.8V rail)
    //=================================================================
    logic [DATA_W-1:0] mem [0:(1<<ADDR_W)-1];
    logic [DATA_W-1:0] mem_rdata;

    always_ff @(posedge clk) begin
        if (cpu_done)
            mem[cpu_result[ADDR_W-1:0]] <= cpu_result;
        mem_rdata <= mem[cmd_data[ADDR_W-1:0]];
    end

    //=================================================================
    // Domain crossings (CPU / MEM -> AON output ports)
    //=================================================================
    // Correctly isolation-clamped crossings (match UPF cpu_iso strategy):
    assign result_data  = cpu_iso_en ? '0   : cpu_result;
    assign result_valid = cpu_iso_en ? 1'b0 : cpu_done;

    // BUG (seed #1): CPU->AON crossing with NO isolation clamp.
    //   UPF cpu_iso applies_to=outputs expects every PD_CPU output to be
    //   clamped; cpu_active is driven raw from the switchable domain.
    assign cpu_active = cpu_done;

    // BUG (seed #2): MEM(0.8V)->AON(1.0V) crossing.
    //   Requires a level shifter; UPF omits a shifter on this path.
    assign scratch_rdata = mem_rdata;

endmodule : power_soc
