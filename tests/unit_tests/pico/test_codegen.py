"""End-to-end tests of apio/pico/codegen.py: real yosys synthesis ->
codegen -> native compile -> run, checking actual truth-table/sequential
behavior rather than generated source text."""

from apio.pico.codegen import INTERNAL_PIN
from tests.unit_tests.pico.testing import requires_cc, requires_yosys, run_case


@requires_yosys
@requires_cc
def test_and_gate(tmp_path):
    verilog = """
module main(input a, input b, output y);
  assign y = a & b;
endmodule
"""
    pcf = "set_io a 0\nset_io b 1\nset_io y 2\n"
    steps = [
        ({0: 0, 1: 0}, {2: 0}),
        ({0: 1, 1: 0}, {2: 0}),
        ({0: 0, 1: 1}, {2: 0}),
        ({0: 1, 1: 1}, {2: 1}),
    ]
    run_case(tmp_path, verilog, pcf, steps)


@requires_yosys
@requires_cc
def test_mux(tmp_path):
    verilog = """
module main(input sel, input a, input b, output y);
  assign y = sel ? b : a;
endmodule
"""
    pcf = "set_io sel 0\nset_io a 1\nset_io b 2\nset_io y 3\n"
    steps = [
        ({0: 0, 1: 1, 2: 0}, {3: 1}),
        ({0: 1, 1: 1, 2: 0}, {3: 0}),
        ({0: 1, 1: 0, 2: 1}, {3: 1}),
    ]
    run_case(tmp_path, verilog, pcf, steps)


@requires_yosys
@requires_cc
def test_multibit_bus(tmp_path):
    verilog = """
module main(input [2:0] a, input [2:0] b, output [2:0] y);
  assign y = a | b;
endmodule
"""
    pcf = (
        "set_io a[0] 0\nset_io a[1] 1\nset_io a[2] 2\n"
        "set_io b[0] 3\nset_io b[1] 4\nset_io b[2] 5\n"
        "set_io y[0] 6\nset_io y[1] 7\nset_io y[2] 8\n"
    )
    # -- a=0b101 (5), b=0b010 (2) -> y = 0b111 (7)
    steps = [
        (
            {0: 1, 1: 0, 2: 1, 3: 0, 4: 1, 5: 0},
            {6: 1, 7: 1, 8: 1},
        ),
    ]
    run_case(tmp_path, verilog, pcf, steps)


@requires_yosys
@requires_cc
def test_sync_reset_dff(tmp_path):
    verilog = """
module main(input clk, input rst, input d, output reg q);
  always @(posedge clk) begin
    if (rst)
      q <= 0;
    else
      q <= d;
  end
endmodule
"""
    pcf = "set_io clk 0\nset_io rst 1\nset_io d 2\nset_io q 3\n"
    steps = [
        # -- Establish prev_clk = 0, no edge yet.
        ({0: 0, 1: 1, 2: 0}, {3: 0}),
        # -- Rising edge with rst=1 -> synchronous reset -> q=0.
        ({0: 1, 1: 1, 2: 0}, {3: 0}),
        # -- Fall clk, set d=1, rst=0. No edge -> q holds at 0.
        ({0: 0, 1: 0, 2: 1}, {3: 0}),
        # -- Rising edge, rst=0 -> q <= d -> q=1.
        ({0: 1, 1: 0, 2: 1}, {3: 1}),
        # -- No new edge (clk already 1) -> q holds at 1 even if d changes.
        ({0: 1, 1: 0, 2: 0}, {3: 1}),
    ]
    run_case(tmp_path, verilog, pcf, steps)


@requires_yosys
@requires_cc
def test_async_reset_dff(tmp_path):
    verilog = """
module main(input clk, input rst, input d, output reg q);
  always @(posedge clk or posedge rst) begin
    if (rst)
      q <= 0;
    else
      q <= d;
  end
endmodule
"""
    pcf = "set_io clk 0\nset_io rst 1\nset_io d 2\nset_io q 3\n"
    steps = [
        # -- Rising edge, rst=0, d=1 -> q <= 1.
        ({0: 0, 1: 0, 2: 1}, {3: 0}),
        ({0: 1, 1: 0, 2: 1}, {3: 1}),
        # -- Assert rst with NO clk edge -> async reset fires immediately.
        ({0: 1, 1: 1, 2: 1}, {3: 0}),
    ]
    run_case(tmp_path, verilog, pcf, steps)


@requires_yosys
@requires_cc
def test_internal_clock_pin(tmp_path):
    """clk mapped to INTERNAL_PIN gets no real GPIO -- it's driven by a
    value toggled once per step_once() call, fed through the same
    edge-detect logic as any external pin. That halves the achievable
    edge rate relative to the loop rate (a rising edge needs a low
    sample followed by a high sample), so a free-running counter should
    advance on every other step_once() call, i.e. cnt after N calls is
    ceil(N/2)."""
    verilog = """
module main(input clk, output reg [2:0] cnt);
  always @(posedge clk)
    cnt <= cnt + 1;
endmodule
"""
    pcf = f"set_io clk {INTERNAL_PIN}\nset_io cnt[0] 0\nset_io cnt[1] 1\nset_io cnt[2] 2\n"
    steps = [
        ({}, {0: 1, 1: 0, 2: 0}),  # -- call 1: edge -> cnt=1
        ({}, {0: 1, 1: 0, 2: 0}),  # -- call 2: no edge -> cnt=1
        ({}, {0: 0, 1: 1, 2: 0}),  # -- call 3: edge -> cnt=2
        ({}, {0: 0, 1: 1, 2: 0}),  # -- call 4: no edge -> cnt=2
        ({}, {0: 1, 1: 1, 2: 0}),  # -- call 5: edge -> cnt=3
    ]
    run_case(tmp_path, verilog, pcf, steps)
