# -*- coding: utf-8 -*-
# -- This file is part of the Apio project
"""Apio scons plugin for the pico (Raspberry Pi Pico / RP2040) target.

Unlike the FPGA architectures (ice40/ecp5/gowin/xilinx), this target does
not synthesize a bitstream. Yosys's CXXRTL backend turns the design into a
C++ model which is wrapped with Pico GPIO access and compiled as firmware.

apio's build pipeline is hard-wired as synth -> pnr -> bitstream
(scons_handler.py:_register_common_targets). This plugin reuses those three
stages for a different purpose:
  - synth_builder:     Yosys CXXRTL generation   (.v -> .cxxrtl.cc)
  - pnr_builder:       add Pico GPIO wrapper     (.cxxrtl.cc + .pcf -> .cc)
  - bitstream_builder: native compile            (.cc -> .uf2, via pico-sdk)
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
from apio.pico.pcf import parse_pcf, PcfError
from apio.pico.cxxrtl import generate_firmware, CxxrtlError
from apio.pico import runtime as pico_runtime


class PluginPico(PluginBase):
    """Apio scons plugin for the pico architecture."""

    def plugin_info(self) -> ArchPluginInfo:
        """Return plugin specific parameters."""
        return ArchPluginInfo(
            constrains_file_suffix=".pcf",
            pnr_file_suffix=".cc",
            bitstream_file_suffix=".uf2",
            clk_name_index=0,
        )

    # @overrides
    def synth_builder(self) -> BuilderBase | CompositeBuilder:
        """Creates a CXXRTL model of the selected top-level module."""

        apio_env = self.apio_env
        params = apio_env.params

        top_module = params.apio_env_params.top_module
        return Builder(
            action=(
                'yosys -p "read_verilog -sv $SOURCES; '
                'prep -top {0} -flatten; write_cxxrtl -O6 -g0 $TARGET" '
                '{1} -DSYNTHESIZE {2}'
            ).format(
                top_module,
                "" if params.verbosity.all or params.verbosity.synth else "-q",
                get_define_flags(apio_env),
            ),
            suffix=".cxxrtl.cc",
            src_suffix=SRC_SUFFIXES,
            source_scanner=self.verilog_src_scanner,
        )

    # @overrides
    def pnr_builder(self) -> BuilderBase | CompositeBuilder:
        """Appends a PCF-driven Pico GPIO wrapper to the CXXRTL model."""

        top_module = self.apio_env.params.apio_env_params.top_module

        def codegen_action(target, source, env):
            _ = env
            model_path = Path(str(source[0]))
            pcf_path = self.constrain_file()
            try:
                model_source = model_path.read_text(encoding="utf-8")
                pin_map = parse_pcf(pcf_path)
                firmware_source = generate_firmware(
                    model_source, pin_map, top_module, target="pico"
                )
            except (CxxrtlError, PcfError, OSError) as e:
                cerror(f"Pico CXXRTL wrapper generation failed: {e}")
                return 1
            Path(str(target[0])).write_text(
                firmware_source, encoding="utf-8"
            )
            return None

        return Builder(
            action=Action(
                codegen_action, "Adding Pico GPIO wrapper to CXXRTL model"
            ),
            suffix=".cc",
            src_suffix=".cxxrtl.cc",
        )

    # @overrides
    def bitstream_builder(self) -> BuilderBase | CompositeBuilder:
        """Repurposed as the native compile stage: compiles the generated
        C++ against pico-sdk with CMake + arm-none-eabi-g++, producing a
        .uf2 ready for 'apio upload'.

        Requires the Pico toolchain packages installed by
        'apio packages install'; see apio/pico/runtime.py for resolution
        rules and the CMake project template.
        """
        apio_env = self.apio_env

        def compile_action(target, source, env):
            _ = env
            generated_cpp = Path(str(source[0]))
            uf2_target = Path(str(target[0]))
            cxxrtl_runtime_dir = (
                Path(apio_env.params.environment.yosys_path)
                / "include"
                / "backends"
                / "cxxrtl"
                / "runtime"
            )
            return pico_runtime.build_uf2(
                generated_cpp, uf2_target, cxxrtl_runtime_dir
            )

        return Builder(
            action=Action(compile_action, "Compiling pico firmware (.uf2)"),
            suffix=".uf2",
            src_suffix=".cc",
        )
