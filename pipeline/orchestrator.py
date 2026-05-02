"""Ties all pipeline stages together. Supports three modes:

  partgen    — CadQuery template engine via part-gen subprocess (best for standard shapes)
  parametric — OpenSCAD-based agentic design (best for custom mechanical/functional objects)
  mesh       — AI mesh generation via a pluggable backend (best for organic/artistic objects)

Mesh backends (--backend flag):
  shape-e   Local, free, no API key  (default)
  tripo3d   Tripo3D cloud API, free tier supports downloads
  meshy     Meshy.ai cloud API, paid tier required for downloads
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from agents.analysis_agent import AnalysisAgent, ObjectDescription
from pipeline.packager import PackageResult, Packager
from pipeline.partitioner import PartitionResult
from utils.file_utils import ensure_dir, safe_filename

PipelineMode = Literal["partgen", "parametric", "mesh", "auto"]
MeshBackendName = Literal["shape-e", "tripo3d", "meshy"]


@dataclass
class PipelineResult:
    object_description: ObjectDescription
    partition: PartitionResult
    package: PackageResult
    mode_used: str
    scad_only: bool = False      # True when two_stage stopped after SCAD
    scad_dir: Path | None = None

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
        mode: PipelineMode = "auto",
        mesh_backend: MeshBackendName = "shape-e",
        analysis_model: str | None = None,
        design_model: str | None = None,
        two_stage: bool = False,
    ) -> None:
        self.output_root = Path(output_root)
        self.mode = mode
        self.mesh_backend = mesh_backend
        self.analysis_model = analysis_model
        self.design_model = design_model
        self.two_stage = two_stage

    def run(
        self,
        text_description: str | None = None,
        image_path: str | Path | None = None,
        progress_callback=None,
    ) -> PipelineResult:
        def _progress(stage: str, message: str) -> None:
            if progress_callback:
                progress_callback(stage, message)

        image_path = Path(image_path) if image_path else None

        # ── Stage 1: Analyse ────────────────────────────────────────────
        _progress("analyse", "Analysing input…")
        analysis_agent = AnalysisAgent(model=self.analysis_model)
        obj_desc = analysis_agent.analyse(
            text_description=text_description,
            image_path=image_path,
        )
        _progress(
            "analyse",
            f"Identified: {obj_desc.name} ({len(obj_desc.suggested_parts)} suggested parts)",
        )

        slug = safe_filename(obj_desc.name)
        work_dir = ensure_dir(self.output_root / f"_work_{slug}")

        # ── Mode selection ───────────────────────────────────────────────
        mode = self._resolve_mode(obj_desc)
        _progress("mode", f"Pipeline mode: {mode}")

        if mode == "partgen":
            partition = self._run_partgen_mode(obj_desc, work_dir, _progress)
        elif mode == "mesh":
            partition = self._run_mesh_mode(obj_desc, image_path, work_dir, slug, _progress)
        else:
            result = self._run_parametric_mode(
                obj_desc, work_dir, _progress, image_path=image_path
            )
            if isinstance(result, dict) and result.get("scad_only"):
                # Two-stage: return early with SCAD path, no package yet
                empty_partition = __import__(
                    "pipeline.partitioner", fromlist=["PartitionResult"]
                ).PartitionResult(parts=[], master_scad_path=work_dir / "master.scad")
                from agents.design_agent import DesignResult
                stub = DesignResult(master_scad_path=work_dir / "master.scad", part_modules=[])
                from pipeline.packager import PackageResult
                empty_pkg = PackageResult(output_dir=work_dir, stl_count=0, scad_count=0)
                return PipelineResult(
                    object_description=obj_desc,
                    partition=empty_partition,
                    package=empty_pkg,
                    mode_used=mode,
                    scad_only=True,
                    scad_dir=work_dir,
                )
            partition = result

        # ── Stage 4: Package ─────────────────────────────────────────────
        _progress("package", "Assembling delivery package…")
        # Build a stub DesignResult for the packager (only warnings matter)
        from agents.design_agent import DesignResult
        stub_design = DesignResult(
            master_scad_path=work_dir / "master.scad",
            part_modules=[p.module_name for p in partition.parts],
        )
        packager = Packager(output_root=self.output_root)
        package = packager.package(obj_desc, stub_design, partition, object_slug=slug)
        _progress("package", f"Package ready: {package.output_dir}")

        return PipelineResult(
            object_description=obj_desc,
            partition=partition,
            package=package,
            mode_used=mode,
        )

    # ------------------------------------------------------------------
    # Mode runners
    # ------------------------------------------------------------------
    def _run_partgen_mode(self, obj_desc, work_dir, _progress) -> PartitionResult:
        from agents.partgen_agent import PartgenAgent
        from pipeline.partitioner import Part, PartitionResult as PR

        _progress("design", "Generating CadQuery model via part-gen templates…")
        agent = PartgenAgent(work_dir=work_dir)
        if not agent.available:
            _progress("design", "part-gen not found — falling back to parametric (OpenSCAD)")
            return self._run_parametric_mode(obj_desc, work_dir, _progress)

        result = agent.generate(obj_desc)
        for w in result.warnings:
            _progress("design", f"[warn] {w}")

        if not result.success:
            _progress("design", "part-gen did not produce STL — falling back to parametric")
            return self._run_parametric_mode(obj_desc, work_dir, _progress)

        _progress("design", f"part-gen complete — template: {result.template_used}")

        parts: list[Part] = []
        if result.stl_path and result.stl_path.exists():
            parts.append(
                Part(
                    index=1,
                    module_name="part_main",
                    scad_filename="",
                    stl_filename=result.stl_path.name,
                    scad_path=work_dir / "master.scad",
                    stl_path=result.stl_path,
                    printable=True,
                )
            )
        if result.step_path and result.step_path.exists():
            # STEP is bonus — add as a second "part" entry so packager can copy it
            parts.append(
                Part(
                    index=2,
                    module_name="part_main_step",
                    scad_filename="",
                    stl_filename=result.step_path.name,
                    scad_path=work_dir / "master.scad",
                    stl_path=result.step_path,
                    printable=False,
                    issues=["STEP file — not an STL, for CAD import only"],
                )
            )

        _progress("partition", f"{len(parts)} output file(s) from part-gen")
        return PR(parts=parts, master_scad_path=work_dir / "master.scad", warnings=result.warnings)

    def _run_parametric_mode(self, obj_desc, work_dir, _progress, image_path=None):
        from agents.design_agent import DesignAgent
        from pipeline.partitioner import Partitioner

        _progress("design", "Generating parametric 3D design (OpenSCAD)…")
        if image_path:
            _progress("design", "Reference image attached — agent will match visual design")
        design_agent = DesignAgent(work_dir=work_dir, model=self.design_model)
        design = design_agent.design(obj_desc, image_path=image_path)
        _progress("design", f"Design complete — {len(design.part_modules)} modules")

        if self.two_stage:
            _progress("design", "Two-stage mode — SCAD ready. Awaiting approval before rendering.")
            return {"scad_only": True, "scad_dir": str(work_dir)}

        _progress("partition", "Creating per-part SCAD files…")
        partitioner = Partitioner(work_dir=work_dir)
        partition = partitioner.partition(design)
        _progress("partition", f"{len(partition.parts)} parts ready")
        return partition

    def _run_mesh_mode(
        self, obj_desc, image_path, work_dir, slug, _progress
    ) -> PartitionResult:
        from agents.mesh_agent import MeshAgent
        from pipeline.mesh_partitioner import MeshPartitioner

        backend = self.mesh_backend
        _progress("design", f"Generating 3D mesh via {backend}…")
        mesh_agent = MeshAgent(work_dir=work_dir, backend=backend)
        mesh_result = mesh_agent.generate(obj_desc, image_path=image_path)

        if not mesh_result.success:
            raise RuntimeError(
                f"{backend} mesh generation failed — no STL produced. "
                f"Warnings: {'; '.join(mesh_result.warnings)}"
            )

        _progress("design", f"Mesh downloaded: {mesh_result.mesh_stl_path}")

        _progress("partition", "Splitting mesh into print-bed-sized parts…")
        mesh_partitioner = MeshPartitioner(work_dir=work_dir)
        mesh_partition = mesh_partitioner.partition(mesh_result.mesh_stl_path, slug)
        _progress("partition", f"{len(mesh_partition.parts)} parts ready")

        partition = mesh_partition.to_partition_result(work_dir)
        partition.warnings.extend(mesh_result.warnings)
        return partition

    # ------------------------------------------------------------------
    # Mode selection
    # ------------------------------------------------------------------
    def _resolve_mode(self, obj_desc: ObjectDescription) -> str:
        if self.mode != "auto":
            return self.mode

        import os
        from agents.partgen_agent import PARTGEN_TEMPLATES, PartgenAgent

        # ── 1. partgen: template match + partgen available ──────────────
        template_hint = getattr(obj_desc, "template_hint", None)
        if template_hint and template_hint in PARTGEN_TEMPLATES:
            agent = PartgenAgent(work_dir="/tmp/_partgen_probe")
            if agent.available:
                return "partgen"

        # ── 2. mesh: organic/decorative + backend available ─────────────
        backend_available = {
            "shape-e": True,
            "tripo3d": bool(os.environ.get("TRIPO3D_API_KEY", "").strip()),
            "meshy":   bool(os.environ.get("MESHY_API_KEY", "").strip()),
        }.get(self.mesh_backend, False)

        category = getattr(obj_desc, "part_category", "mechanical")
        is_organic = category in ("organic", "decorative")

        if not is_organic:
            organic_keywords = {
                "animal", "character", "creature", "face", "figure", "organic",
                "sculpture", "figurine", "bust", "toy", "cartoon", "dragon",
                "robot", "miniature", "model", "statue", "person", "human",
            }
            desc_lower = (obj_desc.description + " " + obj_desc.name).lower()
            is_organic = any(kw in desc_lower for kw in organic_keywords)

        if is_organic and backend_available:
            return "mesh"

        # ── 3. parametric: default (OpenSCAD) ───────────────────────────
        return "parametric"
