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
            "name": "check_connector_pairs",
            "description": (
                "Scan master.scad and verify every part that mates with another part has "
                "matching connector geometry (pin on one side, hole on the other). "
                "Returns a report listing which pairs are correctly connected and which are MISSING connectors. "
                "Call this AFTER list_part_modules and BEFORE render_part_to_stl. "
                "If any part is missing connectors, rewrite the file to add them."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Filename of the .scad file to check",
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
        {
            "name": "fetch_url",
            "description": (
                "Fetch the plain-text content of any URL — web page, GitHub raw file, "
                "datasheet, or design specification. Use this whenever the user supplies "
                "a link as a reference or source material. Returns up to 4000 characters "
                "of readable text (HTML tags stripped)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Full http/https URL to fetch",
                    }
                },
                "required": ["url"],
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

    def check_connector_pairs(self, filename: str) -> str:
        path = self.work_dir / filename
        if not path.exists():
            return f"ERROR: File not found: {path}"
        code = path.read_text(encoding="utf-8")

        # Find all part_* modules
        modules = re.findall(r"^module\s+(part_\w+)\s*\(", code, re.MULTILINE)
        if len(modules) < 2:
            return "OK: Only one part — no connectors needed."

        # Extract per-module bodies
        report_lines = []
        missing = []
        for mod in modules:
            # Grab the module body between its braces
            pattern = rf"module\s+{re.escape(mod)}\s*\(\)[^{{]*\{{"
            m = re.search(pattern, code)
            if not m:
                continue
            start = m.end() - 1
            depth, body_start = 0, start
            body = ""
            for i, ch in enumerate(code[start:], start):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        body = code[body_start : i + 1]
                        break

            has_pin  = bool(re.search(r"cylinder\s*\(", body) and "connector" in body.lower()
                           or re.search(r"//.*pin", body, re.IGNORECASE)
                           or re.search(r"connector_pin|pin_h|pin_d|slot_w|dovetail", body, re.IGNORECASE))
            has_hole = bool(re.search(r"difference\s*\(\s*\)", body)
                           or "hole" in body.lower()
                           or re.search(r"connector_hole|slot|socket", body, re.IGNORECASE))
            has_any  = has_pin or has_hole or re.search(
                r"(cylinder|cube|slot|dovetail|snap|latch|pin|socket|hole)", body, re.IGNORECASE
            ) is not None and "difference" in body

            if has_pin and has_hole:
                report_lines.append(f"  OK  {mod}: has both pin and matching hole geometry")
            elif has_any:
                report_lines.append(f"  OK  {mod}: has connector-like geometry")
            else:
                report_lines.append(f"  MISSING  {mod}: no connector geometry detected")
                missing.append(mod)

        summary = "\n".join(report_lines)
        if missing:
            return (
                f"CONNECTOR CHECK FAILED — {len(missing)} part(s) missing connectors:\n"
                f"{summary}\n\n"
                f"You MUST rewrite master.scad to add matching pin+hole connectors "
                f"on all mating faces before rendering."
            )
        return f"CONNECTOR CHECK PASSED:\n{summary}"

    def fetch_url(self, url: str) -> str:
        try:
            import httpx
            r = httpx.get(
                url,
                follow_redirects=True,
                timeout=15,
                headers={"User-Agent": "nanoclaw/1.0"},
            )
            r.raise_for_status()
            text = r.text
            # Strip HTML tags and decode common entities
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"&amp;", "&", text)
            text = re.sub(r"&lt;", "<", text)
            text = re.sub(r"&gt;", ">", text)
            text = re.sub(r"&nbsp;", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) > 4000:
                return text[:4000] + f"\n... [truncated — {len(text) - 4000} more chars]"
            return text or "(empty response)"
        except Exception as exc:
            return f"ERROR fetching {url}: {exc}"

    def dispatch(self, tool_name: str, tool_input: dict) -> str:
        if tool_name == "write_openscad_file":
            return self.write_openscad_file(**tool_input)
        if tool_name == "validate_openscad_file":
            return self.validate_openscad_file(**tool_input)
        if tool_name == "list_part_modules":
            return self.list_part_modules(**tool_input)
        if tool_name == "check_connector_pairs":
            return self.check_connector_pairs(**tool_input)
        if tool_name == "render_part_to_stl":
            return self.render_part_to_stl(**tool_input)
        if tool_name == "fetch_url":
            return self.fetch_url(**tool_input)
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
