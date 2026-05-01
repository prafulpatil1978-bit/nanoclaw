"""Bridge to the part-gen CadQuery template engine.

part-gen lives at ~/Desktop/n8n-setup/part-gen (or PARTGEN_PATH env var).
This agent calls it as a subprocess — zero code duplication.

Supported templates (from part-gen): bracket, housing, plate, cylinder,
pipe, lego, lamp.  Falls back to nanoclaw's OpenSCAD agent if unavailable.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from agents.analysis_agent import ObjectDescription

_DEFAULT_PATH = os.path.expanduser("~/Desktop/n8n-setup/part-gen")
PARTGEN_TEMPLATES = {
    "bracket", "housing", "plate", "cylinder", "pipe", "lego", "lamp",
}


@dataclass
class PartgenResult:
    stl_path: Path | None
    step_path: Path | None
    template_used: str
    raw_description: str
    warnings: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.stl_path is not None and self.stl_path.exists()


class PartgenAgent:
    """Calls the part-gen CLI to generate CadQuery/OpenCASCADE parts."""

    def __init__(self, work_dir: str | Path, partgen_path: str | None = None) -> None:
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.partgen_path = Path(
            partgen_path or os.environ.get("PARTGEN_PATH", _DEFAULT_PATH)
        )

    @property
    def available(self) -> bool:
        return (self.partgen_path / "main.py").exists()

    def generate(
        self,
        description: ObjectDescription,
        extra_prompt: str = "",
        formats: tuple[str, ...] = ("stl", "step"),
    ) -> PartgenResult:
        if not self.available:
            return PartgenResult(
                stl_path=None, step_path=None,
                template_used="unavailable",
                raw_description="",
                warnings=[
                    f"part-gen not found at {self.partgen_path}. "
                    "Set PARTGEN_PATH in .env or install at the default location."
                ],
            )

        prompt = self._build_prompt(description, extra_prompt)
        warnings: list[str] = []
        stl_path: Path | None = None
        step_path: Path | None = None

        for fmt in formats:
            out_stem = self.work_dir / f"partgen_{fmt}"
            result = self._run(prompt, str(out_stem), fmt)
            if result.returncode == 0:
                candidate = out_stem.with_suffix(f".{fmt}")
                if not candidate.exists():
                    # part-gen appends extension automatically
                    candidate = Path(str(out_stem) + f".{fmt}")
                if candidate.exists():
                    if fmt == "stl":
                        stl_path = candidate
                    elif fmt == "step":
                        step_path = candidate
                else:
                    warnings.append(f"part-gen ran but {fmt} output not found.")
            else:
                stderr = result.stderr.strip()
                warnings.append(f"part-gen {fmt} generation failed: {stderr[:300]}")

        template = self._detect_template(description)
        return PartgenResult(
            stl_path=stl_path,
            step_path=step_path,
            template_used=template,
            raw_description=prompt,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    def _run(self, prompt: str, out_stem: str, fmt: str) -> subprocess.CompletedProcess:
        cmd = [
            sys.executable,
            str(self.partgen_path / "main.py"),
            "generate", prompt,
            "--output", out_stem,
            "--format", fmt,
            "--provider", self._provider_flag(),
        ]
        api_key = self._api_key()
        if api_key:
            cmd += ["--api-key", api_key]

        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(self.partgen_path),
        )

    @staticmethod
    def _provider_flag() -> str:
        if os.environ.get("OPENROUTER_API_KEY"):
            return "openrouter"
        if os.environ.get("ANTHROPIC_API_KEY"):
            return "claude"
        return "ollama"  # free local default

    @staticmethod
    def _api_key() -> str:
        return (
            os.environ.get("OPENROUTER_API_KEY")
            or os.environ.get("ANTHROPIC_API_KEY")
            or ""
        )

    @staticmethod
    def _build_prompt(desc: ObjectDescription, extra: str) -> str:
        dims = desc.overall_dimensions_mm
        prompt = f"{desc.description}"
        if dims:
            prompt += (
                f", approximately {dims['x']:.0f}mm wide "
                f"× {dims['y']:.0f}mm deep "
                f"× {dims['z']:.0f}mm tall"
            )
        if desc.structural_requirements:
            prompt += f". {desc.structural_requirements}"
        if extra:
            prompt += f". {extra}"
        return prompt

    @staticmethod
    def _detect_template(desc: ObjectDescription) -> str:
        hint = getattr(desc, "template_hint", None)
        if hint and hint in PARTGEN_TEMPLATES:
            return hint
        name = (desc.name + " " + desc.description).lower()
        for kw, tmpl in [
            ("bracket", "bracket"), ("enclosure", "housing"), ("box", "housing"),
            ("housing", "housing"), ("plate", "plate"), ("panel", "plate"),
            ("cylinder", "cylinder"), ("spacer", "cylinder"), ("knob", "cylinder"),
            ("pipe", "pipe"), ("tube", "pipe"), ("lego", "lego"),
            ("lamp", "lamp"), ("light", "lamp"),
        ]:
            if kw in name:
                return tmpl
        return "auto"
