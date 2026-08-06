# -*- coding: utf-8 -*-
# -- This file is part of the Apio project
"""Apio scons plugin for the pico (Raspberry Pi Pico / RP2040) target.

Unlike the FPGA architectures (ice40/ecp5/gowin/xilinx), this target does
not synthesize a bitstream. It generates C firmware that *interprets* the
design's logic in a loop, running on the Pico's own Cortex-M0+ core. See
apio/pico/codegen.py for the interpretation semantics.

apio's build pipeline is hard-wired as synth -> pnr -> bitstream
(scons_handler.py:_register_common_targets). This plugin reuses those three
stages for a different purpose:
  - synth_builder:     yosys generic synthesis  (.v  -> .json netlist)
  - pnr_builder:       netlist -> C codegen      (.json + .pcf -> .c)
  - bitstream_builder: native compile            (.c -> .uf2, via pico-sdk)
"""

from pathlib import Path
from SCons.Script import Builder
from SCons.Builder import BuilderBase, CompositeBuilder
from SCons.Action import Action
from apio.common.common_util import SRC_SUFFIXES
from apio.scons.apio_env import ApioEnv
from apio.scons.plugin_base import PluginBase, ArchPluginInfo
from apio.scons.plugin_util import get_define_flags
from apio.common.apio_console import cerror
from apio.pico.netlist import parse_yosys_json, NetlistError
from apio.pico.pcf import parse_pcf, PcfError
from apio.pico.codegen import generate_c
from apio.pico import runtime as pico_runtime


class PluginPico(PluginBase):
    """Apio scons plugin for the pico architecture."""

    def plugin_info(self) -> ArchPluginInfo:
        """Return plugin specific parameters."""
        return ArchPluginInfo(
            constrains_file_suffix=".pcf",
            pnr_file_suffix=".c",
            bitstream_file_suffix=".uf2",
            clk_name_index=0,
        )

    # @overrides
    def synth_builder(self) -> BuilderBase | CompositeBuilder:
        """Creates and returns the synth builder. Runs a *generic* yosys
        synthesis pass (no FPGA tech mapping) so the JSON netlist contains
        only yosys's documented internal cell types ($and, $mux, $dff,
        ...), which apio/pico/codegen.py knows how to translate to C."""

        apio_env = self.apio_env
        params = apio_env.params

        # -- Note: read_verilog must be called explicitly inside the -p
        # -- script, ahead of the other passes. Passing $SOURCES as
        # -- trailing positional args (as the ice40 plugin does for its
        # -- synth_ice40 pass) does NOT guarantee they're read before -p
        # -- runs -- confirmed by hand: that ordering silently produced
        # -- an empty, unsynthesized "$abstract\\main" module.
        return Builder(
            action=(
                'yosys -p "read_verilog -sv $SOURCES; proc; flatten; opt; '
                'write_json $TARGET" {0} -DSYNTHESIZE {1}'
            ).format(
                "" if params.verbosity.all or params.verbosity.synth else "-q",
                get_define_flags(apio_env),
            ),
            suffix=".json",
            src_suffix=SRC_SUFFIXES,
            source_scanner=self.verilog_src_scanner,
        )

    # @overrides
    def pnr_builder(self) -> BuilderBase | CompositeBuilder:
        """Repurposed as the netlist -> C codegen stage."""

        def codegen_action(target, source, env):
            _ = env
            json_path = str(source[0])
            pcf_path = self.constrain_file()
            try:
                netlist = parse_yosys_json(json_path)
                pin_map = parse_pcf(pcf_path)
                c_src = generate_c(
                    netlist, pin_map, target="pico", include_main=True
                )
            except (NetlistError, PcfError) as e:
                cerror(f"Pico codegen failed: {e}")
                return 1
            Path(str(target[0])).write_text(c_src, encoding="utf-8")
            return None

        return Builder(
            action=Action(
                codegen_action, "Generating pico C firmware from netlist"
            ),
            suffix=".c",
            src_suffix=".json",
        )

    # @overrides
    def bitstream_builder(self) -> BuilderBase | CompositeBuilder:
        """Repurposed as the native compile stage: compiles the generated
        C against pico-sdk with CMake + arm-none-eabi-gcc, producing a
        .uf2 ready for 'apio upload'.

        Requires the 'arm-none-eabi-gcc', 'cmake', and 'pico-sdk' packages
        to be installed (apio packages install) and PICO_SDK_PATH set --
        see apio/pico/runtime.py for the CMake project template used
        here. This stage is not yet exercised end-to-end against real
        hardware; see the pico plan doc for outstanding verification.
        """
        apio_env = self.apio_env

        def compile_action(target, source, env):
            _ = env
            generated_c = Path(str(source[0]))
            uf2_target = Path(str(target[0]))
            return pico_runtime.build_uf2(generated_c, uf2_target)

        return Builder(
            action=Action(compile_action, "Compiling pico firmware (.uf2)"),
            suffix=".uf2",
            src_suffix=".c",
        )
