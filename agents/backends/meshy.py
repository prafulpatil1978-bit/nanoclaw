"""Meshy.ai backend.

Free tier lets you preview meshes in-browser but does NOT allow downloads.
Use paid tier or upgrade for production use.

For free local experimentation use shape-e instead.
Set MESHY_API_KEY in .env — https://www.meshy.ai
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import httpx

from agents.analysis_agent import ObjectDescription
from agents.backends.base import MeshBackend, RawMesh

_BASE = "https://api.meshy.ai"
_POLL_S = 5
_TIMEOUT_S = 600


class MeshyBackend(MeshBackend):
    @property
    def name(self) -> str:
        return "meshy"

    def __init__(self, art_style: str = "realistic") -> None:
        key = os.environ.get("MESHY_API_KEY", "")
        if not key:
            raise EnvironmentError(
                "MESHY_API_KEY not set. "
                "Note: Meshy free tier does not allow downloads — "
                "use --backend shape-e (local/free) or --backend tripo3d instead."
            )
        self._headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        self.art_style = art_style

    def generate(
        self,
        description: ObjectDescription,
        image_path: Path | None,
        work_dir: Path,
    ) -> RawMesh:
        if image_path and image_path.exists():
            task_id = self._image_to_3d(image_path)
            base = f"{_BASE}/v1/image-to-3d"
        else:
            task_id = self._text_to_3d(description)
            base = f"{_BASE}/v2/text-to-3d"

        task_data = self._poll(task_id, base)
        return self._download(task_id, task_data, work_dir)

    # ------------------------------------------------------------------
    def _text_to_3d(self, desc: ObjectDescription) -> str:
        dims = desc.overall_dimensions_mm
        prompt = (
            f"{desc.description} "
            f"{dims['x']:.0f}x{dims['y']:.0f}x{dims['z']:.0f}mm "
            "3D printable manifold mesh"
        )
        payload = {
            "mode": "preview",
            "prompt": prompt,
            "negative_prompt": "low quality, broken, non-manifold, floating parts",
            "art_style": self.art_style,
            "should_remesh": True,
        }
        with httpx.Client(timeout=30) as c:
            r = c.post(f"{_BASE}/v2/text-to-3d", headers=self._headers, json=payload)
            r.raise_for_status()
            return r.json()["result"]

    def _image_to_3d(self, image_path: Path) -> str:
        import base64
        data = base64.standard_b64encode(image_path.read_bytes()).decode()
        suffix = image_path.suffix.lower().lstrip(".")
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}.get(suffix, "image/jpeg")
        payload = {
            "image_url": f"data:{mime};base64,{data}",
            "enable_pbr": True,
            "should_remesh": True,
        }
        with httpx.Client(timeout=60) as c:
            r = c.post(f"{_BASE}/v1/image-to-3d", headers=self._headers, json=payload)
            r.raise_for_status()
            return r.json()["result"]

    def _poll(self, task_id: str, endpoint_base: str) -> dict:
        deadline = time.time() + _TIMEOUT_S
        with httpx.Client(timeout=30) as c:
            while time.time() < deadline:
                r = c.get(f"{endpoint_base}/{task_id}", headers=self._headers)
                r.raise_for_status()
                data = r.json()
                status = data.get("status", "PENDING")
                if status == "SUCCEEDED":
                    return data
                if status in ("FAILED", "EXPIRED"):
                    raise RuntimeError(f"Meshy task {task_id} status: {status}")
                time.sleep(_POLL_S)
        raise TimeoutError(f"Meshy task {task_id} timed out after {_TIMEOUT_S}s")

    def _download(self, task_id: str, task_data: dict, work_dir: Path) -> RawMesh:
        urls = task_data.get("model_urls", {})
        warnings: list[str] = []
        stl_path: Path | None = None
        obj_path: Path | None = None

        if urls.get("stl"):
            stl_path = self._fetch(urls["stl"], work_dir / f"meshy_{task_id}.stl")
        elif urls.get("obj"):
            obj_path = self._fetch(urls["obj"], work_dir / f"meshy_{task_id}.obj")
            stl_path = self._obj_to_stl(obj_path)
        else:
            warnings.append(
                "Meshy returned no downloadable URL. "
                "This typically means you are on the free tier — upgrade or use --backend shape-e."
            )

        return RawMesh(
            stl_path=stl_path,
            obj_path=obj_path,
            backend_name=self.name,
            task_id=task_id,
            warnings=warnings,
        )

    @staticmethod
    def _fetch(url: str, dest: Path) -> Path:
        with httpx.Client(timeout=120, follow_redirects=True) as c:
            with c.stream("GET", url) as s:
                s.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in s.iter_bytes(65536):
                        f.write(chunk)
        return dest

    @staticmethod
    def _obj_to_stl(obj_path: Path) -> Path | None:
        try:
            import trimesh
            mesh = trimesh.load(str(obj_path), force="mesh")
            stl_path = obj_path.with_suffix(".stl")
            mesh.export(str(stl_path))
            return stl_path
        except Exception:
            return None
