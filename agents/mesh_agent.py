"""Stage 2 (mesh mode): AI mesh generation via Meshy.ai for organic / artistic objects.

Meshy.ai converts a text prompt or image into a textured 3D mesh (OBJ/GLB/STL).
The generated mesh is downloaded, repaired, scaled to the requested size,
and saved as an STL ready for the partitioner.

Docs: https://docs.meshy.ai
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from agents.analysis_agent import ObjectDescription


_MESHY_BASE = "https://api.meshy.ai"
_POLL_INTERVAL_S = 5
_TIMEOUT_S = 600  # Meshy can take up to 10 min for high-quality renders


@dataclass
class MeshResult:
    mesh_stl_path: Path | None          # Downloaded + repaired STL
    mesh_obj_path: Path | None          # Raw OBJ from Meshy (for reference)
    task_id: str
    art_style: str
    warnings: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.mesh_stl_path is not None and self.mesh_stl_path.exists()


class MeshAgent:
    """Calls Meshy.ai to generate an organic 3D mesh from description or image."""

    def __init__(self, work_dir: str | Path) -> None:
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        api_key = os.environ.get("MESHY_API_KEY", "")
        if not api_key:
            raise EnvironmentError(
                "MESHY_API_KEY is not set. "
                "Get a free key at https://www.meshy.ai and add it to .env"
            )
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------
    def generate(
        self,
        description: ObjectDescription,
        image_path: Path | None = None,
        art_style: str = "realistic",
    ) -> MeshResult:
        """Run text-to-3D or image-to-3D and return the downloaded STL."""
        if image_path and image_path.exists():
            task_id = self._image_to_3d(description, image_path)
            endpoint = f"{_MESHY_BASE}/v1/image-to-3d"
        else:
            task_id = self._text_to_3d(description, art_style)
            endpoint = f"{_MESHY_BASE}/v2/text-to-3d"

        task_data = self._poll(task_id, endpoint)
        return self._download(task_id, task_data, description, art_style)

    # ------------------------------------------------------------------
    # Meshy API calls
    # ------------------------------------------------------------------
    def _text_to_3d(self, desc: ObjectDescription, art_style: str) -> str:
        prompt = self._build_prompt(desc)
        negative = "low quality, broken geometry, non-manifold, floating parts"

        payload = {
            "mode": "preview",          # fast; use "refine" for final quality
            "prompt": prompt,
            "negative_prompt": negative,
            "art_style": art_style,     # realistic | sculpture | pbr
            "should_remesh": True,
        }
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"{_MESHY_BASE}/v2/text-to-3d",
                headers=self._headers,
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()["result"]

    def _image_to_3d(self, desc: ObjectDescription, image_path: Path) -> str:
        import base64

        with open(image_path, "rb") as f:
            img_b64 = base64.standard_b64encode(f.read()).decode()

        suffix = image_path.suffix.lower()
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}.get(
            suffix.lstrip("."), "image/jpeg"
        )

        payload = {
            "image_url": f"data:{mime};base64,{img_b64}",
            "enable_pbr": True,
            "should_remesh": True,
        }
        with httpx.Client(timeout=60) as client:
            resp = client.post(
                f"{_MESHY_BASE}/v1/image-to-3d",
                headers=self._headers,
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()["result"]

    def _poll(self, task_id: str, base_endpoint: str) -> dict:
        deadline = time.time() + _TIMEOUT_S
        with httpx.Client(timeout=30) as client:
            while time.time() < deadline:
                resp = client.get(
                    f"{base_endpoint}/{task_id}",
                    headers=self._headers,
                )
                resp.raise_for_status()
                data = resp.json()
                status = data.get("status", "PENDING")
                if status == "SUCCEEDED":
                    return data
                if status in ("FAILED", "EXPIRED"):
                    raise RuntimeError(
                        f"Meshy task {task_id} ended with status {status}: "
                        f"{data.get('task_error', {}).get('message', 'unknown error')}"
                    )
                time.sleep(_POLL_INTERVAL_S)
        raise TimeoutError(f"Meshy task {task_id} did not complete within {_TIMEOUT_S}s")

    # ------------------------------------------------------------------
    # Download + repair
    # ------------------------------------------------------------------
    def _download(
        self, task_id: str, task_data: dict, desc: ObjectDescription, art_style: str
    ) -> MeshResult:
        warnings: list[str] = []
        model_urls: dict = task_data.get("model_urls", {})

        obj_path: Path | None = None
        stl_path: Path | None = None

        # Prefer STL directly, fall back to OBJ
        if "stl" in model_urls and model_urls["stl"]:
            stl_path = self._fetch_file(model_urls["stl"], f"{task_id}.stl")
        elif "obj" in model_urls and model_urls["obj"]:
            obj_path = self._fetch_file(model_urls["obj"], f"{task_id}.obj")
            stl_path = self._convert_obj_to_stl(obj_path, desc)
        else:
            raise RuntimeError("Meshy returned no downloadable model URL (stl or obj).")

        if stl_path and stl_path.exists():
            stl_path, repair_warnings = self._repair_and_scale(stl_path, desc)
            warnings.extend(repair_warnings)

        return MeshResult(
            mesh_stl_path=stl_path,
            mesh_obj_path=obj_path,
            task_id=task_id,
            art_style=art_style,
            warnings=warnings,
        )

    def _fetch_file(self, url: str, filename: str) -> Path:
        dest = self.work_dir / filename
        with httpx.Client(timeout=120, follow_redirects=True) as client:
            with client.stream("GET", url) as stream:
                stream.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in stream.iter_bytes(chunk_size=65536):
                        f.write(chunk)
        return dest

    @staticmethod
    def _convert_obj_to_stl(obj_path: Path, desc: ObjectDescription) -> Path:
        try:
            import trimesh
            mesh = trimesh.load(str(obj_path), force="mesh")
            stl_path = obj_path.with_suffix(".stl")
            mesh.export(str(stl_path))
            return stl_path
        except ImportError:
            raise RuntimeError("trimesh is required to convert OBJ → STL. pip install trimesh")

    @staticmethod
    def _repair_and_scale(
        stl_path: Path, desc: ObjectDescription
    ) -> tuple[Path, list[str]]:
        """Repair mesh, scale to requested dimensions, re-export."""
        warnings: list[str] = []
        try:
            import trimesh
            import numpy as np

            mesh = trimesh.load(str(stl_path), force="mesh")

            # Repair
            if not mesh.is_watertight:
                trimesh.repair.fill_holes(mesh)
                trimesh.repair.fix_normals(mesh)
                if not mesh.is_watertight:
                    warnings.append("Mesh is not fully watertight after repair — check in slicer.")

            # Scale to requested bounding box
            target = np.array([
                desc.overall_dimensions_mm["x"],
                desc.overall_dimensions_mm["y"],
                desc.overall_dimensions_mm["z"],
            ])
            current = mesh.bounding_box.extents
            if current.max() > 0:
                # Uniform scale to fit the largest axis
                scale = (target / current).min()
                mesh.apply_scale(scale)

            # Centre on Z=0 plate
            mesh.apply_translation(-mesh.bounds[0])

            repaired_path = stl_path.parent / f"repaired_{stl_path.name}"
            mesh.export(str(repaired_path))
            return repaired_path, warnings

        except ImportError:
            warnings.append("trimesh not installed — skipping mesh repair/scaling.")
            return stl_path, warnings
        except Exception as exc:
            warnings.append(f"Mesh repair failed: {exc}")
            return stl_path, warnings

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _build_prompt(desc: ObjectDescription) -> str:
        dims = desc.overall_dimensions_mm
        features = "; ".join(desc.features[:5]) if desc.features else ""
        prompt = (
            f"{desc.description}. "
            f"Overall size approximately {dims['x']:.0f}×{dims['y']:.0f}×{dims['z']:.0f}mm. "
        )
        if features:
            prompt += f"Key features: {features}. "
        prompt += "3D printable, clean topology, manifold mesh, no floating geometry."
        return prompt
