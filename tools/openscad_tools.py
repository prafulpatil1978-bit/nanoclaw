"""OpenSCAD file I/O and validation tools used by the design agent."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


class OpenSCADTools:
    def __init__(self, work_dir: str | Path, openscad_bin: str | None = None) -> None:
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.openscad_bin = openscad_bin or os.environ.get("OPENSCAD_PATH", "openscad")

    # ------------------------------------------------------------------
    # Tool definitions (schema for Claude tool_use)
    # ------------------------------------------------------------------
    TOOL_SCHEMAS: list[dict] = [
        {
            "name": "write_openscad_file",
            "description": (
                "Write OpenSCAD source code to a .scad file in the working directory. "
                "Use this to create or overwrite the 3D model. Always include: "
                "(1) a header comment with object name and part list, "
                "(2) a module per printable part prefixed with 'part_', "
                "(3) a final 'assembly()' module that positions all parts, "
                "(4) individual renders at the bottom (commented out). "
                "Follow OpenSCAD best practices: parametric variables at the top, "
                "wall_thickness >= 1.2, connector pins between mating parts."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Filename ending in .scad, e.g. 'master.scad'",
                    },
                    "code": {
                        "type": "string",
                        "description": "Complete valid OpenSCAD source code",
                    },
                },
                "required": ["filename", "code"],
            },
        },
        {
            "name": "validate_openscad_file",
            "description": (
                "Validate an OpenSCAD file for syntax and geometry errors without rendering. "
                "Returns 'OK' on success or the error message. Fix all errors before proceeding."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Filename of a .scad file in the working directory",
                    }
                },
                "required": ["filename"],
            },
        },
        {
            "name": "list_part_modules",
            "description": (
                "List all module names that start with 'part_' in an OpenSCAD file. "
                "Use this to confirm the design has the expected parts before rendering."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Filename of a .scad file",
                    }
                },
                "required": ["filename"],
            },
        },
        {
            "name": "render_part_to_stl",
            "description": (
                "Render a single part module from an OpenSCAD file to an STL file. "
                "Requires OpenSCAD to be installed on the system."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "scad_filename": {
                        "type": "string",
                        "description": "Source .scad file",
                    },
                    "module_name": {
                        "type": "string",
                        "description": "Name of the module to render, e.g. 'part_base'",
                    },
                    "stl_filename": {
                        "type": "string",
                        "description": "Output STL filename",
                    },
                },
                "required": ["scad_filename", "module_name", "stl_filename"],
            },
        },
    ]

    # ------------------------------------------------------------------
    # Implementations
    # ------------------------------------------------------------------
    def write_openscad_file(self, filename: str, code: str) -> str:
        path = self.work_dir / filename
        path.write_text(code, encoding="utf-8")
        return f"Written {len(code)} bytes to {path}"

    def validate_openscad_file(self, filename: str) -> str:
        path = self.work_dir / filename
        if not path.exists():
            return f"ERROR: File not found: {path}"
        if not self._openscad_available():
            # Fallback: basic syntax check via regex heuristics
            return self._heuristic_validate(path.read_text())
        try:
            result = subprocess.run(
                [self.openscad_bin, "--check-parameters", "true", str(path)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            stderr = result.stderr.strip()
            if result.returncode == 0 and "ERROR" not in stderr.upper():
                return "OK"
            return stderr or "Unknown validation error"
        except subprocess.TimeoutExpired:
            return "ERROR: OpenSCAD validation timed out"
        except Exception as exc:
            return f"ERROR: {exc}"

    def list_part_modules(self, filename: str) -> str:
        path = self.work_dir / filename
        if not path.exists():
            return f"ERROR: File not found: {path}"
        code = path.read_text(encoding="utf-8")
        modules = re.findall(r"^module\s+(part_\w+)\s*\(", code, re.MULTILINE)
        if not modules:
            return "No 'part_*' modules found."
        return "\n".join(modules)

    def render_part_to_stl(
        self, scad_filename: str, module_name: str, stl_filename: str
    ) -> str:
        scad_path = self.work_dir / scad_filename
        stl_path = self.work_dir / stl_filename
        if not scad_path.exists():
            return f"ERROR: SCAD file not found: {scad_path}"
        if not self._openscad_available():
            return "SKIPPED: OpenSCAD not installed — .scad file is ready for manual rendering."

        # Create a temporary wrapper that calls only the requested module
        wrapper_path = self.work_dir / f"_render_{module_name}.scad"
        wrapper_path.write_text(
            f'use <{scad_filename}>;\n{module_name}();\n', encoding="utf-8"
        )
        try:
            result = subprocess.run(
                [self.openscad_bin, "-o", str(stl_path), str(wrapper_path)],
                capture_output=True,
                text=True,
                timeout=120,
            )
            wrapper_path.unlink(missing_ok=True)
            if result.returncode == 0:
                size = stl_path.stat().st_size
                return f"OK: Rendered {stl_filename} ({size} bytes)"
            return f"ERROR: {result.stderr.strip()}"
        except subprocess.TimeoutExpired:
            wrapper_path.unlink(missing_ok=True)
            return "ERROR: Rendering timed out (120 s)"
        except Exception as exc:
            wrapper_path.unlink(missing_ok=True)
            return f"ERROR: {exc}"

    def dispatch(self, tool_name: str, tool_input: dict) -> str:
        if tool_name == "write_openscad_file":
            return self.write_openscad_file(**tool_input)
        if tool_name == "validate_openscad_file":
            return self.validate_openscad_file(**tool_input)
        if tool_name == "list_part_modules":
            return self.list_part_modules(**tool_input)
        if tool_name == "render_part_to_stl":
            return self.render_part_to_stl(**tool_input)
        return f"ERROR: Unknown tool '{tool_name}'"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _openscad_available(self) -> bool:
        try:
            subprocess.run(
                [self.openscad_bin, "--version"],
                capture_output=True,
                timeout=5,
            )
            return True
        except Exception:
            return False

    @staticmethod
    def _heuristic_validate(code: str) -> str:
        opens = code.count("{")
        closes = code.count("}")
        if opens != closes:
            return f"ERROR: Mismatched braces ({opens} open, {closes} close)"
        if "module" not in code:
            return "WARNING: No modules defined"
        return "OK (heuristic — install OpenSCAD for full validation)"
