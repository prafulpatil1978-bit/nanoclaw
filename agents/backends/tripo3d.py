"""Tripo3D backend — free tier allows mesh downloads.

Sign up: https://platform.tripo3d.ai
Free tier gives credits that include OBJ/STL downloads.
Set TRIPO3D_API_KEY in .env

API docs: https://platform.tripo3d.ai/docs
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import httpx

from agents.analysis_agent import ObjectDescription
from agents.backends.base import MeshBackend, RawMesh

_BASE = "https://api.tripo3d.ai/v2/openapi"
_POLL_S = 4
_TIMEOUT_S = 300


class Tripo3DBackend(MeshBackend):
    @property
    def name(self) -> str:
        return "tripo3d"

    def __init__(self) -> None:
        key = os.environ.get("TRIPO3D_API_KEY", "")
        if not key:
            raise EnvironmentError(
                "TRIPO3D_API_KEY not set. "
                "Register free at https://platform.tripo3d.ai and add the key to .env"
            )
        self._headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    def generate(
        self,
        description: ObjectDescription,
        image_path: Path | None,
        work_dir: Path,
    ) -> RawMesh:
        if image_path and image_path.exists():
            task_id = self._image_to_model(image_path)
        else:
            task_id = self._text_to_model(description)

        task = self._poll(task_id)
        return self._download(task, task_id, work_dir)

    # ------------------------------------------------------------------
    def _text_to_model(self, desc: ObjectDescription) -> str:
        payload = {
            "type": "text_to_model",
            "prompt": self._prompt(desc),
        }
        with httpx.Client(timeout=30) as c:
            r = c.post(f"{_BASE}/task", headers=self._headers, json=payload)
            r.raise_for_status()
            return r.json()["data"]["task_id"]

    def _image_to_model(self, image_path: Path) -> str:
        # Step 1: upload the image to get a file token
        with open(image_path, "rb") as f:
            img_bytes = f.read()

        suffix = image_path.suffix.lower().lstrip(".")
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}.get(
            suffix, "image/jpeg"
        )

        with httpx.Client(timeout=60) as c:
            upload_resp = c.post(
                f"{_BASE}/upload",
                headers={k: v for k, v in self._headers.items() if k != "Content-Type"},
                files={"file": (image_path.name, img_bytes, mime)},
            )
            upload_resp.raise_for_status()
            file_token = upload_resp.json()["data"]["image_token"]

        # Step 2: create image-to-model task
        payload = {
            "type": "image_to_model",
            "file": {"type": "jpg", "file_token": file_token},
        }
        with httpx.Client(timeout=30) as c:
            r = c.post(f"{_BASE}/task", headers=self._headers, json=payload)
            r.raise_for_status()
            return r.json()["data"]["task_id"]

    def _poll(self, task_id: str) -> dict:
        deadline = time.time() + _TIMEOUT_S
        with httpx.Client(timeout=30) as c:
            while time.time() < deadline:
                r = c.get(f"{_BASE}/task/{task_id}", headers=self._headers)
                r.raise_for_status()
                data = r.json()["data"]
                status = data.get("status", "queued")
                if status == "success":
                    return data
                if status in ("failed", "cancelled"):
                    raise RuntimeError(f"Tripo3D task {task_id} failed: {data}")
                time.sleep(_POLL_S)
        raise TimeoutError(f"Tripo3D task {task_id} did not finish within {_TIMEOUT_S}s")

    def _download(self, task: dict, task_id: str, work_dir: Path) -> RawMesh:
        output = task.get("output", {})
        # Prefer GLB → convert, then OBJ, then STL
        url = output.get("model") or output.get("pbr_model") or ""
        if not url:
            return RawMesh(
                stl_path=None, obj_path=None, backend_name=self.name,
                task_id=task_id,
                warnings=["Tripo3D returned no downloadable model URL"],
            )

        ext = url.split("?")[0].rsplit(".", 1)[-1].lower()
        dest = work_dir / f"tripo3d_{task_id}.{ext}"

        with httpx.Client(timeout=120, follow_redirects=True) as c:
            with c.stream("GET", url) as stream:
                stream.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in stream.iter_bytes(65536):
                        f.write(chunk)

        stl_path: Path | None = None
        obj_path: Path | None = None
        warnings: list[str] = []

        if ext == "stl":
            stl_path = dest
        elif ext in ("obj", "glb", "gltf"):
            obj_path = dest
            stl_path = self._convert_to_stl(dest)
            if stl_path is None:
                warnings.append(f"Could not convert {ext} to STL — using {ext} directly")
        else:
            obj_path = dest
            warnings.append(f"Unexpected format '{ext}' — attempting STL conversion")
            stl_path = self._convert_to_stl(dest)

        return RawMesh(
            stl_path=stl_path,
            obj_path=obj_path,
            backend_name=self.name,
            task_id=task_id,
            warnings=warnings,
        )

    @staticmethod
    def _convert_to_stl(src: Path) -> Path | None:
        try:
            import trimesh
            mesh = trimesh.load(str(src), force="mesh")
            dest = src.with_suffix(".stl")
            mesh.export(str(dest))
            return dest
        except Exception:
            return None

    @staticmethod
    def _prompt(desc: ObjectDescription) -> str:
        dims = desc.overall_dimensions_mm
        return (
            f"{desc.description}, "
            f"clean 3D printable mesh, watertight, manifold, "
            f"size {dims['x']:.0f}x{dims['y']:.0f}x{dims['z']:.0f}mm"
        )
