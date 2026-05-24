"""Stage 2: Agentic 3D design generator — produces OpenSCAD from an ObjectDescription."""

from __future__ import annotations

import os
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agents.analysis_agent import ObjectDescription
from tools.openscad_tools import OpenSCADTools
from utils.file_utils import load_image_as_base64
from utils.llm_client import build_client, resolve_model, score_complexity


@dataclass
class DesignResult:
    master_scad_path: Path
    part_modules: list[str]
    stl_paths: list[Path] = field(default_factory=list)
    agent_messages: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


_SYSTEM_PROMPT = """\
You are an expert OpenSCAD programmer and mechanical engineer specialising in FDM 3D printing.
You will design a complete, parametric 3D model that closely matches the reference image \
(if provided) AND the written specification.

## Workflow — follow every step in order
1. Study the reference image carefully (if provided). Note the overall shape, proportions,
   arm/body junctions, surface details, and any visible connection points.
2. Write the complete OpenSCAD model with `write_openscad_file` (filename: master.scad).
3. Validate with `validate_openscad_file`. Fix ALL errors before continuing.
4. Confirm parts with `list_part_modules`.
5. **Check connectors** with `check_connector_pairs`. If any part is flagged MISSING,
   rewrite master.scad to add connectors, then re-validate. Do NOT skip this step.
6. Render each part to STL with `render_part_to_stl`.
7. Stop only when all parts are rendered (or OpenSCAD is unavailable).

## Design fidelity (critical)
- Match the silhouette and proportions of the reference image as closely as possible.
- Reproduce distinctive shape features: swept arms, tapered profiles, cutouts, ribs,
  slots, camera mounts, motor housings — whatever is visible in the image.
- If the image shows curved or swept geometry, use `hull()` or `minkowski()` to approximate.
- Prefer shape accuracy over simplicity.

## Connector requirements (mandatory — enforced by check_connector_pairs)
Every pair of mating parts MUST have explicit connector geometry on BOTH sides:
- **Pin side**: `cylinder(d=4, h=8, $fn=20)` protruding 8 mm from the mating face.
- **Socket side**: `difference()` with `cylinder(d=4.4, h=8.5, $fn=20)` subtracted
  (0.4 mm diameter clearance + 0.5 mm depth clearance).
- Label every connector with a comment: `// connector: arm_left → body (pin side)`
- Drone-style designs: each arm must have a rectangular or dovetail slot that keys
  into a matching recess in the central body — do NOT just butt-join with no geometry.

## OpenSCAD code requirements
- Parameters block at top: wall_t, layer_h, connector_d=4, connector_h=8, clearance=0.4
- One `module part_<name>()` per printable part — self-contained, flat on build plate.
- `assembly()` module showing all parts in assembled position.
- Final line: `assembly();`
- wall_t >= 1.6 mm for structural parts (arms, body).
- No part larger than 210×210×240 mm.
- All geometry must be watertight (avoid `*` and `!` in final code).

## Print-quality guidelines
- Flat base faces on bed — no supports if possible.
- Chamfer bottom edges with `hull()` over slightly offset slabs.
- Hollow bodies: `difference()` with inner shell offset by wall_t.

## Communication
Think step-by-step. After each tool call, state what you found and what you will do next.\
"""


def _build_user_prompt(
    desc: ObjectDescription,
    image_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Return a content list (image block + text block) for the first user message."""
    parts_text = "\n".join(
        f"  - {p.name}: {p.description} "
        f"(~{p.estimated_dimensions_mm['x']:.0f}×"
        f"{p.estimated_dimensions_mm['y']:.0f}×"
        f"{p.estimated_dimensions_mm['z']:.0f} mm) {p.notes}"
        for p in desc.suggested_parts
    )
    features_text  = "\n".join(f"  - {f}" for f in desc.features)
    considerations = "\n".join(f"  - {c}" for c in desc.print_considerations)

    image_note = (
        "\nA reference image is attached above. "
        "Use it as the primary design guide for shape, proportions, and joint details.\n"
        if image_path else ""
    )

    text = textwrap.dedent(f"""\
        Design the following object as a multi-part 3D printable model.{image_note}
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
{considerations}

        IMPORTANT: After listing part modules, run check_connector_pairs and fix \
any MISSING connectors before rendering.

        Begin now — study the image first, then write master.scad.
    """)

    content: list[dict[str, Any]] = []
    if image_path and Path(image_path).exists():
        img_data, media_type = load_image_as_base64(image_path)
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": img_data},
        })
    content.append({"type": "text", "text": text})
    return content


_SCAD_KEEP_CHARS = 300   # keep first N chars of code when truncating old writes


def _compress_history(messages: list[dict]) -> None:
    """Truncate large write_openscad_file code payloads in older assistant turns.

    The most recent write is kept in full so the model still knows the current
    code.  All earlier writes are replaced with a stub to avoid resending
    thousands of tokens on every iteration.
    """
    # Find all assistant messages that contain write_openscad_file tool_use blocks
    write_indices: list[tuple[int, int]] = []   # (message_idx, block_idx)
    for mi, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for bi, block in enumerate(content):
            btype = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
            bname = getattr(block, "name", None) or (block.get("name") if isinstance(block, dict) else None)
            if btype == "tool_use" and bname == "write_openscad_file":
                write_indices.append((mi, bi))

    # Keep the last write intact; truncate all earlier ones
    for mi, bi in write_indices[:-1]:
        block = messages[mi]["content"][bi]
        inp = getattr(block, "input", None) or (block.get("input") if isinstance(block, dict) else {})
        code = inp.get("code", "") if isinstance(inp, dict) else ""
        if len(code) > _SCAD_KEEP_CHARS:
            stub = code[:_SCAD_KEEP_CHARS] + f"\n// ... [{len(code) - _SCAD_KEEP_CHARS} chars truncated to save tokens] ..."
            if isinstance(block, dict):
                block["input"] = {**inp, "code": stub}
            else:
                try:
                    block.input = {**inp, "code": stub}
                except AttributeError:
                    pass


class DesignAgent:
    MAX_ITERATIONS = 24   # extra headroom for connector re-write iteration

    def __init__(
        self,
        work_dir: str | Path,
        model: str | None = None,
        complexity: str = "medium",
    ) -> None:
        self._unified = build_client()
        self.client = self._unified
        # "auto" → complexity-based routing; explicit model → use as-is
        _requested = model or os.environ.get("DESIGN_MODEL", "auto")
        self.model = resolve_model(_requested, self._unified.messages._provider, complexity=complexity)
        self.work_dir = Path(work_dir)
        self.osc = OpenSCADTools(work_dir=work_dir)

    def design(
        self,
        description: ObjectDescription,
        image_path: str | Path | None = None,
    ) -> DesignResult:
        image_path = Path(image_path) if image_path else None
        first_content = _build_user_prompt(description, image_path)

        messages: list[dict[str, Any]] = [
            {"role": "user", "content": first_content}
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
                # If the model returned only text (no tool calls) before writing
                # master.scad, it gave an architecture/planning response instead of
                # acting. Nudge it to actually call write_openscad_file.
                master = self.work_dir / "master.scad"
                if not master.exists() and iteration < self.MAX_ITERATIONS - 2:
                    messages.append({
                        "role": "user",
                        "content": [{
                            "type": "text",
                            "text": (
                                "You have not called any tools yet — master.scad does not exist. "
                                "Do not describe your plan further. "
                                "Call write_openscad_file RIGHT NOW to create master.scad."
                            ),
                        }],
                    })
                    continue
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

                if block.name == "render_part_to_stl" and tool_output.startswith("OK"):
                    stl_path = self.work_dir / block.input["stl_filename"]
                    if stl_path.exists():
                        result.stl_paths.append(stl_path)

            messages.append({"role": "user", "content": tool_results})
            _compress_history(messages)

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
