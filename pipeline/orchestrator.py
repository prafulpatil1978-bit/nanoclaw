"""Ties all four pipeline stages together."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from agents.analysis_agent import AnalysisAgent, ObjectDescription
from agents.design_agent import DesignAgent, DesignResult
from pipeline.partitioner import Partitioner, PartitionResult
from pipeline.packager import Packager, PackageResult
from utils.file_utils import ensure_dir, safe_filename


@dataclass
class PipelineResult:
    object_description: ObjectDescription
    design: DesignResult
    partition: PartitionResult
    package: PackageResult

    @property
    def output_dir(self) -> Path:
        return self.package.output_dir

    @property
    def success(self) -> bool:
        return bool(self.partition.parts)


class Pipeline:
    def __init__(
        self,
        output_root: str | Path = "./output",
        analysis_model: str | None = None,
        design_model: str | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.analysis_model = analysis_model
        self.design_model = design_model

    def run(
        self,
        text_description: str | None = None,
        image_path: str | Path | None = None,
        progress_callback=None,
    ) -> PipelineResult:
        def _progress(stage: str, message: str) -> None:
            if progress_callback:
                progress_callback(stage, message)

        # ── Stage 1: Analyse ────────────────────────────────────────────
        _progress("analyse", "Analysing input…")
        agent = AnalysisAgent(model=self.analysis_model)
        obj_desc = agent.analyse(
            text_description=text_description,
            image_path=image_path,
        )
        _progress("analyse", f"Identified: {obj_desc.name} ({len(obj_desc.suggested_parts)} parts)")

        # ── Stage 2: Design ──────────────────────────────────────────────
        slug = safe_filename(obj_desc.name)
        work_dir = ensure_dir(self.output_root / f"_work_{slug}")

        _progress("design", "Generating 3D design (OpenSCAD)…")
        design_agent = DesignAgent(work_dir=work_dir, model=self.design_model)
        design = design_agent.design(obj_desc)
        _progress("design", f"Design complete — {len(design.part_modules)} modules found")

        # ── Stage 3: Partition ───────────────────────────────────────────
        _progress("partition", "Creating per-part SCAD files…")
        partitioner = Partitioner(work_dir=work_dir)
        partition = partitioner.partition(design)
        _progress("partition", f"{len(partition.parts)} parts partitioned")

        # ── Stage 4: Package ─────────────────────────────────────────────
        _progress("package", "Assembling delivery package…")
        packager = Packager(output_root=self.output_root)
        package = packager.package(obj_desc, design, partition, object_slug=slug)
        _progress("package", f"Package ready at: {package.output_dir}")

        return PipelineResult(
            object_description=obj_desc,
            design=design,
            partition=partition,
            package=package,
        )
