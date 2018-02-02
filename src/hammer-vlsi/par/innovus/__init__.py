#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  hammer-vlsi plugin for Cadence Innovus.
#
#  Copyright 2018 Edward Wang <edward.c.wang@compdigitec.com>

from typing import List, Dict

import os

from hammer_vlsi import HammerPlaceAndRouteTool, CadenceTool, HammerVLSILogging


# Notes: camelCase commands are the old syntax (deprecated)
# snake_case commands are the new/common UI syntax.
# This plugin should only use snake_case commands.

class Innovus(HammerPlaceAndRouteTool, CadenceTool):
    @property
    def env_vars(self) -> Dict[str, str]:
        v = dict(super().env_vars)
        v["INNOVUS_BIN"] = self.get_setting("par.innovus.innovus_bin")
        return v

    def do_run(self) -> bool:
        self.create_enter_script()

        output = []  # type: List[str]

        # Python doesn't have Scala's nice currying syntax (e.g. val newfunc = func(_, fixed_arg))
        def verbose_append(cmd: str) -> None:
            self.verbose_tcl_append(cmd, output)

        # Read LEF layouts.
        lef_files = self.read_libs([
            self.lef_filter
        ], self.to_plain_item)
        verbose_append("read_physical -lef {{ {files} }}".format(
            files=" ".join(lef_files)
        ))

        # Read timing libraries.
        mmmc_path = os.path.join(self.run_dir, "mmmc.tcl")
        with open(mmmc_path, "w") as f:
            f.write(self.generate_mmmc_script())
        verbose_append("read_mmmc {mmmc_path}".format(mmmc_path=mmmc_path))

        # Read netlist.
        # Innovus only supports structural Verilog for the netlist.
        if not self.check_input_files([".v"]):
            return False
        # We are switching working directories and Genus still needs to find paths.
        abspath_input_files = list(map(lambda name: os.path.join(os.getcwd(), name), self.input_files))
        verbose_append("read_netlist {{ {files} }} -top {top}".format(
            files=" ".join(abspath_input_files),
            top=self.top_module
        ))

        # Run init_design to validate data and start the Cadence place-and-route workflow.
        verbose_append("init_design")

        # Set design mode to express effort to increase turnaround speed.
        # TODO: make this a parameter
        verbose_append("set_db design_flow_effort express")

        floorplan_tcl = os.path.join(self.run_dir, "floorplan.tcl")
        with open(floorplan_tcl, "w") as f:
            f.write("\n".join(self.create_floorplan_tcl()))
        verbose_append("source -echo -verbose {}".format(floorplan_tcl))

        # Place the design and do pre-routing optimization.
        verbose_append("place_opt_design")

        # Route the design.
        verbose_append("route_design")

        # Post-route optimization and fix setup & hold time violations.
        verbose_append("opt_design -post_route -setup -hold")

        # Save the Innovus design.
        output_innovus_lib_name = "{top}_ENC".format(top=self.top_module)
        verbose_append("write_db {lib_name} -def -verilog".format(
            lib_name=output_innovus_lib_name
        ))

        # GDS streamout.
        verbose_append("write_stream -output_macros -mode ALL -unit 1000 gds_file")
        # extra junk: -map_file -attach_inst_name ... -attach_net_name ...

        # Quit Innovus.
        verbose_append("exit")

        # Create par script.
        par_tcl_filename = os.path.join(self.run_dir, "par.tcl")
        with open(par_tcl_filename, "w") as f:
            f.write("\n".join(output))

        # Make sure that generated-scripts exists.
        generated_scripts_dir = os.path.join(self.run_dir, "generated-scripts")
        os.makedirs(generated_scripts_dir, exist_ok=True)

        # Create open_chip script.
        with open(os.path.join(generated_scripts_dir, "open_chip.tcl"), "w") as f:
            f.write("""
read_db {name}
        """.format(name=output_innovus_lib_name))

        with open(os.path.join(generated_scripts_dir, "open_chip"), "w") as f:
            f.write("""
cd {run_dir}
source enter
$INNOVUS_BIN -common_ui -files generated-scripts/open_chip.tcl
        """.format(run_dir=self.run_dir))
        self.run_executable([
            "chmod", "+x", os.path.join(generated_scripts_dir, "open_chip")
        ])

        # Build args.
        args = [
            self.get_setting("par.innovus.innovus_bin"),
            "-nowin",  # Prevent the GUI popping up.
            "-common_ui",
            "-files", par_tcl_filename
        ]

        # Temporarily disable colours/tag to make run output more readable.
        # TODO: think of a more elegant way to do this?
        HammerVLSILogging.enable_colour = False
        HammerVLSILogging.enable_tag = False
        self.run_executable(args, cwd=self.run_dir)  # TODO: check for errors and deal with them
        HammerVLSILogging.enable_colour = True
        HammerVLSILogging.enable_tag = True

        # TODO: check that par run was successful

        return True

    def create_floorplan_tcl(self) -> List[str]:
        """
        Create a floorplan TCL depending on the floorplan mode.
        """
        output = []  # type: List[str]

        floorplan_mode = str(self.get_setting("par.innovus.floorplan_mode"))
        if floorplan_mode == "manual":
            floorplan_script_contents = str(self.get_setting("par.innovus.floorplan_script_contents"))
            # TODO(edwardw): proper source locators/SourceInfo
            output.append("# Floorplan manually specified from HAMMER")
            output.extend(floorplan_script_contents.split("\n"))
        elif floorplan_mode == "generate":
            output.extend(self.generate_floorplan_tcl())
        else:
            if floorplan_mode != "blank":
                self.logger.error("Invalid floorplan_mode {mode}. Using blank floorplan.".format(mode=floorplan_mode))
            # Write blank floorplan
            output.append("# Blank floorplan specified from HAMMER")
        return output

    def generate_mmmc_script(self) -> str:
        """
        Output for the mmmc.tcl script.
        Innovus (init_design) requires that the timing script be placed in a separate file.
        :return: Contents of the mmmc script.
        """
        mmmc_output = []  # type: List[str]

        def append_mmmc(cmd: str) -> None:
            self.verbose_tcl_append(cmd, mmmc_output)

        # First, create an Innovus library set.
        library_set_name = "my_lib_set"
        append_mmmc("create_library_set -name {name} -timing [list {list}]".format(
            name=library_set_name,
            list=self.get_liberty_libs()
        ))

        # Next, create an Innovus timing condition.
        timing_condition_name = "my_timing_condition"
        append_mmmc("create_timing_condition -name {name} -library_sets [list {list}]".format(
            name=timing_condition_name,
            list=library_set_name
        ))
        # extra junk: -opcond ...

        # Next, create an Innovus delay corner.
        delay_corner_name = "my_delay_corner"
        append_mmmc(
            "create_delay_corner -name {name} -timing_condition {timing_cond}".format(
                name=delay_corner_name,
                timing_cond=timing_condition_name
            ))
        # extra junk: -rc_corner my_rc_corner_maybe_worst

        # In parallel, create an Innovus constraint mode.
        constraint_mode = "my_constraint_mode"
        sdc_files = []  # type: List[str]

        # Add the post-synthesis SDC, if present.
        if self.post_synth_sdc != "":
            sdc_files.append(self.post_synth_sdc)
        # TODO: add floorplanning SDC
        if len(sdc_files) > 0:
            sdc_files_arg = "-sdc_files [list {sdc_files}]".format(
                sdc_files=" ".join(sdc_files)
            )
        else:
            blank_sdc = os.path.join(self.run_dir, "blank.sdc")
            self.run_executable(["touch", blank_sdc])
            sdc_files_arg = "-sdc_files {{ {} }}".format(blank_sdc)
        append_mmmc("create_constraint_mode -name {name} {sdc_files_arg}".format(
            name=constraint_mode,
            sdc_files_arg=sdc_files_arg
        ))

        # Next, create an Innovus analysis view.
        analysis_view_name = "my_view"
        append_mmmc("create_analysis_view -name {name} -delay_corner {corner} -constraint_mode {constraint}".format(
            name=analysis_view_name, corner=delay_corner_name, constraint=constraint_mode))
        # Finally, apply the analysis view.
        # TODO: introduce different views of setup/hold and true multi-corner
        append_mmmc("set_analysis_view -setup {{ {setup_view} }} -hold {{ {hold_view} }}".format(
            setup_view=analysis_view_name,
            hold_view=analysis_view_name
        ))

        return "\n".join(mmmc_output)

    def generate_floorplan_tcl(self) -> List[str]:
        """
        Generate a TCL floorplan for Innovus based on the input config/IR.
        Not to be confused with create_floorplan_tcl, which calls this function.
        """
        output = []  # type: List[str]

        # TODO(edwardw): proper source locators/SourceInfo
        output.append("# Floorplan automatically generated from HAMMER")

        # TODO: implement floorplan generation
        output.append("create_floorplan -core_margins_by die -die_size_by_io_height max -die_size {1100.0 400.0 100 100 100 100}")
        # extra junk: -site core

        return output


tool = Innovus()