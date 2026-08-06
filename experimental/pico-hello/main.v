// Minimal example: a combinational AND gate plus a clocked toggle
// register, to exercise both the codegen's combinational and sequential
// paths. Timing is intentionally trivial -- see pipico.md.
module main(
  input  clk,
  input  rst,
  input  a,
  input  b,
  output led_and,
  output reg led_toggle
);

  assign led_and = a & b;

  always @(posedge clk) begin
    if (rst)
      led_toggle <= 1'b0;
    else if (a)
      led_toggle <= ~led_toggle;
  end

endmodule
