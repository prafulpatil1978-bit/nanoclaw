"""Stage 4: Assemble the final deliverable package folder."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agents.analysis_agent import ObjectDescription
from agents.design_agent import DesignResult
from pipeline.partitioner import Part, PartitionResult


@dataclass
class PackageResult:
    output_dir: Path
    manifest_path: Path
    stl_count: int
    scad_count: int


class Packager:
    def __init__(self, output_root: str | Path) -> None:
        self.output_root = Path(output_root)

    def package(
        self,
        obj_desc: ObjectDescription,
        design: DesignResult,
        partition: PartitionResult,
        object_slug: str,
    ) -> PackageResult:
        out_dir = self.output_root / object_slug
        out_dir.mkdir(parents=True, exist_ok=True)

        stl_dir = out_dir / "stl"
        scad_dir = out_dir / "scad"
        docs_dir = out_dir / "docs"
        for d in (stl_dir, scad_dir, docs_dir):
            d.mkdir(exist_ok=True)

        # Copy SCAD files
        scad_count = 0
        for src in partition.master_scad_path.parent.glob("*.scad"):
            dest = scad_dir / src.name
            shutil.copy2(src, dest)
            scad_count += 1

        # Copy STL files
        stl_count = 0
        for part in partition.parts:
            if part.stl_path and part.stl_path.exists():
                dest = stl_dir / part.stl_filename
                shutil.copy2(part.stl_path, dest)
                stl_count += 1

        # Write docs
        self._write_print_instructions(docs_dir, obj_desc, partition.parts)
        self._write_assembly_guide(docs_dir, obj_desc, partition.parts)

        # Write manifest
        manifest = self._build_manifest(obj_desc, design, partition, stl_count, scad_count)
        manifest_path = out_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        return PackageResult(
            output_dir=out_dir,
            manifest_path=manifest_path,
            stl_count=stl_count,
            scad_count=scad_count,
        )

    # ------------------------------------------------------------------
    # Document generators
    # ------------------------------------------------------------------
    def _write_print_instructions(
        self, docs_dir: Path, obj: ObjectDescription, parts: list[Part]
    ) -> None:
        lines = [
            f"# Print Instructions — {obj.name}",
            "",
            f"> {obj.description}",
            "",
            "## Global Settings",
            "",
            "| Setting | Value |",
            "|---------|-------|",
            "| Filament | PLA (or PETG for higher strength) |",
            "| Layer height | 0.20 mm |",
            "| Infill | 20% Gyroid |",
            "| Perimeters | 3 |",
            "| Nozzle temperature | 200°C (PLA) / 235°C (PETG) |",
            "| Bed temperature | 60°C (PLA) / 70°C (PETG) |",
            "",
            "## Parts",
            "",
        ]
        for part in parts:
            dims = part.dimensions_mm
            dim_str = (
                f"{dims['x']:.0f} × {dims['y']:.0f} × {dims['z']:.0f} mm"
                if dims
                else "see SCAD file"
            )
            stl_note = f"`stl/{part.stl_filename}`" if part.stl_path else "render from SCAD"
            lines += [
                f"### Part {part.index}: `{part.module_name}`",
                "",
                f"- **File:** {stl_note}",
                f"- **Dimensions:** {dim_str}",
                f"- **Printable:** {'Yes' if part.printable else 'No — see issues'}",
            ]
            if part.issues:
                lines += [f"- **Issues:** {'; '.join(part.issues)}"]
            scad = Path(part.scad_filename)
            orientation_hint = (part.scad_path.read_text(encoding="utf-8")
                                .split("\n")[8] if part.scad_path.exists() else "")
            if orientation_hint.startswith("//"):
                lines += [f"- **Orientation:** {orientation_hint.lstrip('/ ').strip()}"]
            lines.append("")

        if obj.print_considerations:
            lines += ["## Additional Considerations", ""]
            for c in obj.print_considerations:
                lines.append(f"- {c}")
            lines.append("")

        (docs_dir / "print_instructions.md").write_text("\n".join(lines), encoding="utf-8")

    def _write_assembly_guide(
        self, docs_dir: Path, obj: ObjectDescription, parts: list[Part]
    ) -> None:
        lines = [
            f"# Assembly Guide — {obj.name}",
            "",
            f"> {obj.description}",
            "",
            "## Parts List",
            "",
        ]
        for part in parts:
            lines.append(f"{part.index}. **{part.module_name}**")
        lines += [
            "",
            "## Assembly Steps",
            "",
            obj.assembly_notes,
            "",
            "## Structural Notes",
            "",
            obj.structural_requirements,
            "",
            "## Tips",
            "",
            "- Clean connector holes with a 4 mm drill bit before assembly.",
            "- A small drop of super glue on press-fit connectors adds permanence.",
            "- Test-fit all parts before applying adhesive.",
        ]
        (docs_dir / "assembly_guide.md").write_text("\n".join(lines), encoding="utf-8")

    @staticmethod
    def _build_manifest(
        obj: ObjectDescription,
        design: DesignResult,
        partition: PartitionResult,
        stl_count: int,
        scad_count: int,
    ) -> dict:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "object": {
                "name": obj.name,
                "description": obj.description,
                "overall_dimensions_mm": obj.overall_dimensions_mm,
            },
            "parts": [
                {
                    "index": p.index,
                    "module": p.module_name,
                    "scad_file": f"scad/{p.scad_filename}",
                    "stl_file": f"stl/{p.stl_filename}" if p.stl_path else None,
                    "dimensions_mm": p.dimensions_mm,
                    "printable": p.printable,
                    "issues": p.issues,
                }
                for p in partition.parts
            ],
            "files": {
                "stl_count": stl_count,
                "scad_count": scad_count,
            },
            "warnings": design.warnings + partition.warnings,
        }
