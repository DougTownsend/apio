"""End-to-end behavioral tests of the Pico CXXRTL backend."""

from apio.pico.cxxrtl import INTERNAL_PIN
from tests.unit_tests.pico.testing import (
    requires_cxx,
    requires_yosys,
    run_case,
)


@requires_yosys
@requires_cxx
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
@requires_cxx
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
@requires_cxx
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
@requires_cxx
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
@requires_cxx
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
@requires_cxx
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
    pcf = (
        f"set_io clk {INTERNAL_PIN}\nset_io cnt[0] 0\n"
        "set_io cnt[1] 1\nset_io cnt[2] 2\n"
    )
    steps = [
        ({}, {0: 1, 1: 0, 2: 0}),  # -- call 1: edge -> cnt=1
        ({}, {0: 1, 1: 0, 2: 0}),  # -- call 2: no edge -> cnt=1
        ({}, {0: 0, 1: 1, 2: 0}),  # -- call 3: edge -> cnt=2
        ({}, {0: 0, 1: 1, 2: 0}),  # -- call 4: no edge -> cnt=2
        ({}, {0: 1, 1: 1, 2: 0}),  # -- call 5: edge -> cnt=3
    ]
    run_case(tmp_path, verilog, pcf, steps)


@requires_yosys
@requires_cxx
def test_multimodule_rom_and_variable_index(tmp_path):
    """Covers the constructs that the former hand-written IR translator
    rejected as $memrd_v2/$meminit and $shiftx cells."""

    verilog = """
module lookup(input [1:0] address, output [2:0] value);
  reg [2:0] table [0:3];
  initial begin
    table[0] = 3'b001;
    table[1] = 3'b010;
    table[2] = 3'b100;
    table[3] = 3'b111;
  end
  assign value = table[address];
endmodule

module selector(input [3:0] bits, input [1:0] index, output selected);
  assign selected = bits[index];
endmodule

module main(
    input [1:0] address,
    input [3:0] bits,
    output [2:0] value,
    output selected
);
  lookup lookup_instance(address, value);
  selector selector_instance(bits, address, selected);
endmodule
"""
    pcf = (
        "set_io address[0] 0\nset_io address[1] 1\n"
        "set_io bits[0] 2\nset_io bits[1] 3\n"
        "set_io bits[2] 4\nset_io bits[3] 5\n"
        "set_io value[0] 6\nset_io value[1] 7\n"
        "set_io value[2] 8\nset_io selected 9\n"
    )
    steps = [
        ({0: 0, 1: 0, 2: 1, 3: 0, 4: 0, 5: 0}, {6: 1, 7: 0, 8: 0, 9: 1}),
        ({0: 0, 1: 1, 2: 0, 3: 0, 4: 1, 5: 0}, {6: 0, 7: 0, 8: 1, 9: 1}),
        ({0: 1, 1: 1, 2: 0, 3: 0, 4: 0, 5: 1}, {6: 1, 7: 1, 8: 1, 9: 1}),
    ]
    run_case(tmp_path, verilog, pcf, steps)
