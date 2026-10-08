"""End-to-end behavioral tests of the Pico CXXRTL backend."""

from apio.pico.cxxrtl import (
    GLOBAL_CLOCK,
    INTERNAL_PIN,
    generate_firmware,
    globalize_ffs,
)
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
    advance on every other step_once() call. The pin is high on the first
    call, which isn't an edge: as in hardware, a clock that is already high
    at power-up hasn't risen, so cnt after N calls is floor(N/2)."""
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
        ({}, {0: 0, 1: 0, 2: 0}),  # -- call 1: high at power-up -> cnt=0
        ({}, {0: 0, 1: 0, 2: 0}),  # -- call 2: no edge -> cnt=0
        ({}, {0: 1, 1: 0, 2: 0}),  # -- call 3: edge -> cnt=1
        ({}, {0: 1, 1: 0, 2: 0}),  # -- call 4: no edge -> cnt=1
        ({}, {0: 0, 1: 1, 2: 0}),  # -- call 5: edge -> cnt=2
    ]
    run_case(tmp_path, verilog, pcf, steps)


@requires_yosys
@requires_cxx
def test_flip_flop_driven_clock(tmp_path):
    """A register clocked by another register's output, as with a
    debounced button. Plain CXXRTL never sees an edge on such a clock;
    the global clock rewrite must make it behave like a simulator."""
    verilog = """
module main(input clk, input btn, output reg [1:0] cnt);
  reg btn_q = 0;
  always @(posedge clk)
    btn_q <= btn;
  initial cnt = 2'd2;
  always @(posedge btn_q)
    cnt <= cnt + 1;
endmodule
"""
    pcf = "set_io clk 0\nset_io btn 1\nset_io cnt[0] 2\nset_io cnt[1] 3\n"
    steps = [
        # -- The initial value survives the rewrite.
        ({0: 0, 1: 0}, {2: 0, 3: 1}),
        # -- btn goes high but is only registered on the next clk edge.
        ({0: 0, 1: 1}, {2: 0, 3: 1}),
        # -- clk edge -> btn_q rises -> cnt increments in the same step.
        ({0: 1, 1: 1}, {2: 1, 3: 1}),
        # -- More clk edges with btn held -> btn_q stays high, no change.
        ({0: 0, 1: 1}, {2: 1, 3: 1}),
        ({0: 1, 1: 1}, {2: 1, 3: 1}),
        # -- Release and press again -> one more increment, wrapping to 0.
        ({0: 0, 1: 0}, {2: 1, 3: 1}),
        ({0: 1, 1: 0}, {2: 1, 3: 1}),
        ({0: 0, 1: 1}, {2: 1, 3: 1}),
        ({0: 1, 1: 1}, {2: 0, 3: 0}),
    ]
    run_case(tmp_path, verilog, pcf, steps)


def test_globalize_ffs():
    rtlil = (
        "module \\main\n"
        "  cell $ff $sample$1\n"
        "    parameter \\WIDTH 1\n"
        "    connect \\D \\a\n"
        "    connect \\Q \\b\n"
        "  end\n"
        "  cell $dff $keep\n"
        "  end\n"
        "end\n"
    )
    assert globalize_ffs(rtlil) == (
        "module \\main\n"
        "  cell $dff $sample$1\n"
        "    parameter \\CLK_POLARITY 1\n"
        f"    connect \\CLK \\{GLOBAL_CLOCK}\n"
        "    parameter \\WIDTH 1\n"
        "    connect \\D \\a\n"
        "    connect \\Q \\b\n"
        "  end\n"
        "  cell $dff $keep\n"
        "  end\n"
        "end\n"
    )


def test_global_clock_needs_no_pin():
    """The global clock port is driven by the wrapper, so it must not
    demand a PCF entry, and each loop iteration must pulse it."""
    model = (
        "struct p_main : public module {\n"
        "  /*input*/ value<1> p_a;\n"
        "  /*input*/ value<1> p_apio__gclk;\n"
        "  /*output*/ value<1> p_y;\n"
        "}; // struct p_main\n"
    )
    source = generate_firmware(model, {"a": 0, "y": 1}, "main", target="host")
    assert (
        "  design.p_apio__gclk.set<bool>(false);\n"
        "  design.step();\n"
        "  design.p_apio__gclk.set<bool>(true);\n"
        "  design.step();\n"
    ) in source


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
