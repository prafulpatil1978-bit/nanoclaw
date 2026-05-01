"""Stage 2 (mesh mode): dispatch to a mesh generation backend, then repair + scale.

Supported backends
──────────────────
shape-e   Runs locally, no API key, completely free.
          pip install shap-e  (~1 GB model download on first run)
          Best for: quick local experiments, no internet dependency.

tripo3d   Tripo3D cloud API — free tier allows mesh downloads.
          Set TRIPO3D_API_KEY in .env  https://platform.tripo3d.ai
          Best for: higher quality organic meshes with free credits.

meshy     Meshy.ai cloud API — paid tier required for downloads.
          Set MESHY_API_KEY in .env  https://www.meshy.ai
          Best for: production quality once you have a paid account.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agents.analysis_agent import ObjectDescription
from agents.backends import BACKEND_MAP, RawMesh

_DEFAULT_BACKEND = "shape-e"


@dataclass
class MeshResult:
    mesh_stl_path: Path | None
    mesh_obj_path: Path | None
    backend_name: str
    task_id: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.mesh_stl_path is not None and self.mesh_stl_path.exists()


class MeshAgent:
    def __init__(self, work_dir: str | Path, backend: str = _DEFAULT_BACKEND) -> None:
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.backend_name = backend

        if backend not in BACKEND_MAP:
            raise ValueError(
                f"Unknown backend '{backend}'. "
                f"Choose from: {', '.join(BACKEND_MAP)}"
            )
        self._backend = BACKEND_MAP[backend]()

    def generate(
        self,
        description: ObjectDescription,
        image_path: Path | None = None,
    ) -> MeshResult:
        raw: RawMesh = self._backend.generate(description, image_path, self.work_dir)

        if not raw.success:
            return MeshResult(
                mesh_stl_path=None,
                mesh_obj_path=raw.obj_path,
                backend_name=self.backend_name,
                task_id=raw.task_id,
                warnings=raw.warnings,
            )

        stl_path, repair_warnings = _repair_and_scale(raw.best_path, description)

        return MeshResult(
            mesh_stl_path=stl_path,
            mesh_obj_path=raw.obj_path,
            backend_name=self.backend_name,
            task_id=raw.task_id,
            warnings=raw.warnings + repair_warnings,
        )


# ------------------------------------------------------------------
# Shared post-processing (same for all backends)
# ------------------------------------------------------------------
def _repair_and_scale(
    mesh_path: Path, desc: ObjectDescription
) -> tuple[Path, list[str]]:
    warnings: list[str] = []
    try:
        import trimesh
        import numpy as np

        mesh = trimesh.load(str(mesh_path), force="mesh")

        if not mesh.is_watertight:
            trimesh.repair.fill_holes(mesh)
            trimesh.repair.fix_normals(mesh)
            if not mesh.is_watertight:
                warnings.append("Mesh not fully watertight after repair — check in slicer.")

        target = np.array([
            desc.overall_dimensions_mm["x"],
            desc.overall_dimensions_mm["y"],
            desc.overall_dimensions_mm["z"],
        ])
        current = mesh.bounding_box.extents
        if current.max() > 0:
            scale = (target / current).min()
            mesh.apply_scale(scale)

        # Place on Z=0 build plate
        mesh.apply_translation(-mesh.bounds[0])

        out = mesh_path.parent / f"repaired_{mesh_path.name}"
        if not out.suffix:
            out = out.with_suffix(".stl")
        mesh.export(str(out))
        return out, warnings

    except ImportError:
        warnings.append("trimesh not installed — skipping mesh repair. pip install trimesh")
        return mesh_path, warnings
    except Exception as exc:
        warnings.append(f"Mesh repair/scale failed: {exc}")
        return mesh_path, warnings
