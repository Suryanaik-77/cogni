//=====================================================================
// Module      : alu
// Description : Parameterized Arithmetic Logic Unit
//=====================================================================

module alu #(
    parameter int WIDTH = 32
) (
    input  logic [WIDTH-1:0] a,
    input  logic [WIDTH-1:0] b,
    input  logic [3:0]       op_sel,
    output logic [WIDTH-1:0] result,
    output logic             zero_flag,
    output logic             carry_flag,
    output logic             overflow_flag
);

    // Operation encoding
    typedef enum logic [3:0] {
        ALU_ADD  = 4'b0000,
        ALU_SUB  = 4'b0001,
        ALU_AND  = 4'b0010,
        ALU_OR   = 4'b0011,
        ALU_XOR  = 4'b0100,
        ALU_NOT  = 4'b0101,
        ALU_SLL  = 4'b0110,  // shift left logical
        ALU_SRL  = 4'b0111,  // shift right logical
        ALU_SRA  = 4'b1000,  // shift right arithmetic
        ALU_SLT  = 4'b1001,  // set less than (signed)
        ALU_SLTU = 4'b1010   // set less than (unsigned)
    } alu_op_e;

    logic [WIDTH:0] add_sub_result; // extra bit for carry

    always_comb begin
        // Defaults
        result          = '0;
        add_sub_result  = '0;
        carry_flag      = 1'b0;
        overflow_flag   = 1'b0;

        case (op_sel)
            ALU_ADD: begin
                add_sub_result = {1'b0, a} + {1'b0, b};
                result         = add_sub_result[WIDTH-1:0];
                carry_flag     = add_sub_result[WIDTH];
                overflow_flag  = (a[WIDTH-1] == b[WIDTH-1]) &&
                                  (result[WIDTH-1] != a[WIDTH-1]);
            end

            ALU_SUB: begin
                add_sub_result = {1'b0, a} - {1'b0, b};
                result         = add_sub_result[WIDTH-1:0];
                carry_flag     = add_sub_result[WIDTH];
                overflow_flag  = (a[WIDTH-1] != b[WIDTH-1]) &&
                                  (result[WIDTH-1] != a[WIDTH-1]);
            end

            ALU_AND:  result = a & b;
            ALU_OR:   result = a | b;
            ALU_XOR:  result = a ^ b;
            ALU_NOT:  result = ~a;
            ALU_SLL:  result = a << b[$clog2(WIDTH)-1:0];
            ALU_SRL:  result = a >> b[$clog2(WIDTH)-1:0];
            ALU_SRA:  result = $signed(a) >>> b[$clog2(WIDTH)-1:0];
            ALU_SLT:  result = ($signed(a) < $signed(b)) ? {{(WIDTH-1){1'b0}}, 1'b1}
                                                           : '0;
            ALU_SLTU: result = (a < b) ? {{(WIDTH-1){1'b0}}, 1'b1} : '0;

            default:  result = '0;
        endcase
    end

    assign zero_flag = (result == '0);

endmodule
