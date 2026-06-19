// =====================================================================
// cmd_alu.sv  —  Hand-written FSM + ALU demo for cogni RTL-stage testing.
//
// This file is INTENTIONALLY imperfect. It is the test material for the
// RTL-stage prediction scenario. The agent reads the lint output and
// either correctly predicts every flagged issue (latch_count > 0,
// X-prop risk, blocking-in-seq) or fails honestly.
//
// Hazards on purpose (each is a real bug class from the RTL rule pack):
//   H1  always_comb missing else on `result_o`        -> infers latch
//   H2  case in always_comb without `default`         -> infers latch on `flag_o`
//   H3  blocking assignment used inside always_ff     -> sim/synth race risk
//   H4  width mismatch on add (32+8 -> 32 result)     -> implicit truncation
//
// Everything else is meant to be clean.
// =====================================================================
`default_nettype none

module cmd_alu #(
  parameter int unsigned WIDTH = 32
) (
  input  logic                   clk_i,
  input  logic                   rst_ni,        // async low

  input  logic                   start_i,
  input  logic [3:0]             opcode_i,
  input  logic [WIDTH-1:0]       a_i,
  input  logic [WIDTH-1:0]       b_i,
  input  logic [7:0]             k_i,           // small constant input

  output logic                   busy_o,
  output logic                   done_o,
  output logic [WIDTH-1:0]       result_o,
  output logic                   flag_o
);

  // -------------------------------------------------------------------
  // FSM state encoding (clean)
  // -------------------------------------------------------------------
  typedef enum logic [1:0] {
    S_IDLE  = 2'd0,
    S_LOAD  = 2'd1,
    S_EXEC  = 2'd2,
    S_DONE  = 2'd3
  } state_e;

  state_e state_q, state_d;

  // -------------------------------------------------------------------
  // Datapath registers
  // -------------------------------------------------------------------
  logic [WIDTH-1:0] op_a_q, op_b_q;
  logic [3:0]       opc_q;
  logic [WIDTH-1:0] acc_q;

  // ===================================================================
  // FSM next-state (combinational, clean — every path assigns state_d)
  // ===================================================================
  always_comb begin
    state_d = state_q;
    unique case (state_q)
      S_IDLE: if (start_i) state_d = S_LOAD;
      S_LOAD:              state_d = S_EXEC;
      S_EXEC:              state_d = S_DONE;
      S_DONE:              state_d = S_IDLE;
      default:             state_d = S_IDLE;
    endcase
  end

  // ===================================================================
  // FSM register
  // (HAZARD H3: blocking assignment in an always_ff body.
  //  This is `=` instead of `<=`, which races against same-clock readers.)
  // ===================================================================
  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      state_q  = S_IDLE;        // <-- H3 (blocking in seq)
      op_a_q   <= '0;
      op_b_q   <= '0;
      opc_q    <= '0;
      acc_q    <= '0;
    end else begin
      state_q  = state_d;       // <-- H3 (blocking in seq)
      if (state_q == S_LOAD) begin
        op_a_q <= a_i;
        op_b_q <= b_i;
        opc_q  <= opcode_i;
      end
      if (state_q == S_EXEC) begin
        acc_q  <= acc_q + a_i;  // simple accumulator path
      end
    end
  end

  // ===================================================================
  // Result combinational logic
  // (HAZARD H1: result_o is conditionally assigned. There is no
  //  pre-block default, and the `else` is missing for the start_i path.
  //  Synthesis will infer a latch on result_o.)
  // ===================================================================
  always_comb begin
    if (state_q == S_DONE) begin
      result_o = acc_q;
    end
    // <-- H1: no else, no top-of-block default => latch
  end

  // ===================================================================
  // Flag generation
  // (HAZARD H2: case statement in always_comb without a default branch.
  //  Some opcodes are unhandled; flag_o gets latched on those cycles.)
  // ===================================================================
  always_comb begin
    case (opc_q)
      4'h0: flag_o = (op_a_q == op_b_q);
      4'h1: flag_o = (op_a_q != op_b_q);
      4'h2: flag_o = (op_a_q  < op_b_q);
      4'h3: flag_o = (op_a_q  > op_b_q);
      // <-- H2: no default => unhandled opcodes latch flag_o
    endcase
  end

  // ===================================================================
  // Width-checked add path
  // (HAZARD H4: a_i is WIDTH=32 bits, k_i is 8 bits. The result is
  //  assigned back to a 32-bit signal. Most simulators allow this with
  //  zero-extension, but the rule "explicit width on arithmetic" is
  //  violated — there is no explicit width on k_i in the add.)
  // ===================================================================
  logic [WIDTH-1:0] sum_w;
  assign sum_w = a_i + k_i;   // <-- H4: implicit width-extension

  // ===================================================================
  // Status outputs (clean)
  // ===================================================================
  assign busy_o = (state_q != S_IDLE);
  assign done_o = (state_q == S_DONE);

  // Used to keep sum_w from being optimized away in lint mode.
  // (Real designs would hook this somewhere meaningful.)
  // synopsys translate_off
  // pragma keep
  // synopsys translate_on
  // verilator lint_off UNUSED
  wire _unused_sum = |sum_w;
  // verilator lint_on UNUSED

endmodule

`default_nettype wire
