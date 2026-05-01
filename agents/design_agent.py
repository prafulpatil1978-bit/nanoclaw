"""Stage 2: Agentic 3D design generator — produces OpenSCAD from an ObjectDescription."""

from __future__ import annotations

import os
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agents.analysis_agent import ObjectDescription
from tools.openscad_tools import OpenSCADTools
from utils.llm_client import build_client, resolve_model


@dataclass
class DesignResult:
    master_scad_path: Path
    part_modules: list[str]
    stl_paths: list[Path] = field(default_factory=list)
    agent_messages: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


_SYSTEM_PROMPT = """\
You are an expert OpenSCAD programmer and mechanical engineer specialising in FDM 3D printing. \
You will design a complete, parametric 3D model using the tools available to you.

## Workflow
1. Write the complete OpenSCAD model using `write_openscad_file` (filename: master.scad).
2. Validate it with `validate_openscad_file`. Fix ALL errors.
3. Confirm parts exist with `list_part_modules`.
4. Render each part to STL with `render_part_to_stl`.
5. When all parts are rendered (or skipped due to missing OpenSCAD), stop.

## OpenSCAD code requirements
- Parameters section at the top (wall_thickness, layer_height, connector_d, etc.)
- One `module part_<name>()` per printable part.
- Each part must be self-contained and printable flat on the build plate.
- Parts that mate together MUST have matching connector geometry:
    - Alignment pin: cylinder(d=4, h=6) on one side
    - Matching hole: cylinder(d=4.3, h=6) on the other (0.3 mm clearance)
- An `assembly()` module that shows the full object assembled.
- Final line: `assembly();` (for preview), with individual part calls commented out.
- wall_thickness >= 1.2 mm (3 perimeters at 0.4 mm nozzle).
- No part larger than 210×210×240 mm (leave 5 mm safety margin on each axis).
- All geometry must be valid (watertight manifold).

## Print-quality guidelines
- Prefer flat base faces to avoid supports.
- Chamfer or fillet sharp bottom edges: `minkowski()` or `hull()`.
- For hollow parts use `difference()` with inner offset = wall_thickness.
- Label connectors in comments so the assembler knows which faces mate.

## Communication
Think step-by-step. After each tool call, state what you did and what comes next.\
"""


def _build_user_prompt(desc: ObjectDescription) -> str:
    parts_text = "\n".join(
        f"  - {p.name}: {p.description} "
        f"(~{p.estimated_dimensions_mm['x']:.0f}×"
        f"{p.estimated_dimensions_mm['y']:.0f}×"
        f"{p.estimated_dimensions_mm['z']:.0f} mm) {p.notes}"
        for p in desc.suggested_parts
    )
    features_text = "\n".join(f"  - {f}" for f in desc.features)
    considerations_text = "\n".join(f"  - {c}" for c in desc.print_considerations)

    return textwrap.dedent(f"""\
        Design the following object as a multi-part 3D printable model.

        ## Object
        Name: {desc.name}
        Description: {desc.description}

        ## Overall dimensions
        {desc.overall_dimensions_mm['x']:.0f} × \
{desc.overall_dimensions_mm['y']:.0f} × \
{desc.overall_dimensions_mm['z']:.0f} mm (X × Y × Z)

        ## Key features
{features_text}

        ## Structural requirements
        {desc.structural_requirements}

        ## Suggested parts ({len(desc.suggested_parts)} total)
{parts_text}

        ## Assembly
        {desc.assembly_notes}

        ## Print considerations
{considerations_text}

        Begin designing now. Write master.scad first.
    """)


class DesignAgent:
    MAX_ITERATIONS = 20

    def __init__(self, work_dir: str | Path, model: str | None = None) -> None:
        self._unified = build_client()
        self.client = self._unified
        _requested = model or os.environ.get("DESIGN_MODEL", "claude-opus-4-7")
        self.model = resolve_model(_requested, self._unified.messages._provider)
        self.work_dir = Path(work_dir)
        self.osc = OpenSCADTools(work_dir=work_dir)

    def design(self, description: ObjectDescription) -> DesignResult:
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": _build_user_prompt(description)}
        ]
        result = DesignResult(
            master_scad_path=self.work_dir / "master.scad",
            part_modules=[],
            agent_messages=messages,
        )

        for iteration in range(self.MAX_ITERATIONS):
            response = self.client.messages.create(
                model=self.model,
                max_tokens=8192,
                system=[
                    {
                        "type": "text",
                        "text": _SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=OpenSCADTools.TOOL_SCHEMAS,
                messages=messages,
            )

            assistant_content = response.content
            messages.append({"role": "assistant", "content": assistant_content})

            if response.stop_reason == "end_turn":
                break

            if response.stop_reason != "tool_use":
                result.warnings.append(f"Unexpected stop_reason: {response.stop_reason}")
                break

            tool_results: list[dict[str, Any]] = []
            for block in assistant_content:
                if block.type != "tool_use":
                    continue
                tool_output = self.osc.dispatch(block.name, block.input)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": tool_output,
                    }
                )

                # Track rendered STL paths
                if block.name == "render_part_to_stl" and tool_output.startswith("OK"):
                    stl_path = self.work_dir / block.input["stl_filename"]
                    if stl_path.exists():
                        result.stl_paths.append(stl_path)

            messages.append({"role": "user", "content": tool_results})

        # Collect part modules from the written master.scad
        master = self.work_dir / "master.scad"
        if master.exists():
            modules_output = self.osc.list_part_modules("master.scad")
            result.part_modules = [
                m for m in modules_output.splitlines() if m.startswith("part_")
            ]
        else:
            result.warnings.append("master.scad was not created by the agent.")

        result.agent_messages = messages
        return result
