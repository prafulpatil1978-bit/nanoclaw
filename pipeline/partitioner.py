"""Stage 3: Split master.scad into individual print-ready part files."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from agents.design_agent import DesignResult
from tools.stl_tools import STLTools


@dataclass
class Part:
    index: int
    module_name: str
    scad_filename: str
    stl_filename: str
    scad_path: Path
    stl_path: Path | None
    dimensions_mm: dict | None = None
    printable: bool = True
    issues: list[str] = field(default_factory=list)


@dataclass
class PartitionResult:
    parts: list[Part]
    master_scad_path: Path
    warnings: list[str] = field(default_factory=list)


# Print settings header injected into every per-part SCAD file
_PRINT_SETTINGS_COMMENT = """\
// === RECOMMENDED PRINT SETTINGS ===
// Filament  : PLA (or PETG for higher strength)
// Layer height: 0.20 mm
// Infill    : 20% Gyroid
// Perimeters: 3 (wall_thickness >= 1.2 mm)
// Supports  : See part notes below
// Bed temp  : 60°C (PLA) / 70°C (PETG)
// Nozzle    : 200°C (PLA) / 235°C (PETG)
// ====================================
"""


class Partitioner:
    """
    Reads master.scad produced by DesignAgent and writes one .scad file
    per part_* module, oriented for printing (flat on Z=0 plane).
    Also runs STL analysis if STLs were already rendered.
    """

    def __init__(self, work_dir: str | Path) -> None:
        self.work_dir = Path(work_dir)
        self.scad_dir = self.work_dir / "scad"
        self.stl_dir = self.work_dir / "stl"
        self.scad_dir.mkdir(parents=True, exist_ok=True)
        self.stl_dir.mkdir(parents=True, exist_ok=True)

    def partition(self, design: DesignResult) -> PartitionResult:
        master = design.master_scad_path
        if not master.exists():
            return PartitionResult(
                parts=[],
                master_scad_path=master,
                warnings=["master.scad not found — skipping partitioning."],
            )

        # Copy master into scad/ dir for the package
        dest_master = self.scad_dir / "master.scad"
        dest_master.write_text(master.read_text(encoding="utf-8"), encoding="utf-8")

        parts: list[Part] = []
        warnings: list[str] = []

        for idx, module_name in enumerate(design.part_modules, start=1):
            part = self._create_part_scad(idx, module_name, master)
            parts.append(part)

            # Analyse STL if it was already rendered by the design agent
            matching_stl = self._find_stl(design.stl_paths, module_name)
            if matching_stl and matching_stl.exists():
                analysis = STLTools.analyse(matching_stl)
                part.dimensions_mm = analysis.get("dimensions_mm")
                part.printable = analysis.get("printable", True)
                part.issues = analysis.get("issues", [])
                # Copy STL to stl/ subdir
                dest_stl = self.stl_dir / matching_stl.name
                dest_stl.write_bytes(matching_stl.read_bytes())
                part.stl_path = dest_stl

        if not parts:
            warnings.append(
                "No part_* modules found in master.scad. "
                "The model was not split into individual parts."
            )

        return PartitionResult(
            parts=parts,
            master_scad_path=dest_master,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _create_part_scad(self, idx: int, module_name: str, master: Path) -> Part:
        scad_filename = f"part_{idx:02d}_{module_name.removeprefix('part_')}.scad"
        stl_filename = scad_filename.replace(".scad", ".stl")
        scad_path = self.scad_dir / scad_filename
        stl_path = self.stl_dir / stl_filename

        orientation_note = self._orientation_hint(module_name)
        scad_content = (
            f"{_PRINT_SETTINGS_COMMENT}\n"
            f"// Part {idx}: {module_name}\n"
            f"{orientation_note}\n"
            f"// To render: openscad -o {stl_filename} {scad_filename}\n\n"
            f'use <master.scad>;\n\n'
            f"// Place part flat on build plate\n"
            f"{module_name}();\n"
        )
        scad_path.write_text(scad_content, encoding="utf-8")

        return Part(
            index=idx,
            module_name=module_name,
            scad_filename=scad_filename,
            stl_filename=stl_filename,
            scad_path=scad_path,
            stl_path=stl_path if stl_path.exists() else None,
        )

    @staticmethod
    def _orientation_hint(module_name: str) -> str:
        name = module_name.lower()
        if any(w in name for w in ("lid", "cover", "top", "cap")):
            return "// Print orientation: upside-down (flat face on bed) — no supports needed"
        if any(w in name for w in ("base", "bottom", "plate", "tray")):
            return "// Print orientation: right-side up (flat base on bed)"
        if any(w in name for w in ("wall", "panel", "side")):
            return "// Print orientation: standing upright — may need brim for adhesion"
        if any(w in name for w in ("bracket", "mount", "arm")):
            return "// Print orientation: flat on longest face — check for overhangs > 45°"
        return "// Print orientation: choose flattest face as bed contact"

    @staticmethod
    def _find_stl(stl_paths: list[Path], module_name: str) -> Path | None:
        bare = module_name.removeprefix("part_")
        for p in stl_paths:
            if bare in p.stem or module_name in p.stem:
                return p
        return None
