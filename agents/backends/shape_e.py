"""Shap-E backend — fully local, no API key, completely free.

Install:  pip install shap-e
Models are auto-downloaded (~1 GB) on first run to ~/.cache/shap_e_model_cache/

Text-to-3D and image-to-3D both supported.
Quality is lower than cloud APIs but sufficient for pipeline testing.

GitHub: https://github.com/openai/shap-e
"""

from __future__ import annotations

from pathlib import Path

from agents.analysis_agent import ObjectDescription
from agents.backends.base import MeshBackend, RawMesh

# Reasonable defaults — reduce steps for faster iteration
_TEXT_STEPS = 64
_IMAGE_STEPS = 75
_GUIDANCE_SCALE = 15.0


class ShapeEBackend(MeshBackend):
    """Runs OpenAI Shap-E inference locally (CPU or CUDA)."""

    @property
    def name(self) -> str:
        return "shape-e"

    def generate(
        self,
        description: ObjectDescription,
        image_path: Path | None,
        work_dir: Path,
    ) -> RawMesh:
        try:
            import torch
        except ImportError:
            return RawMesh(
                stl_path=None,
                obj_path=None,
                backend_name=self.name,
                warnings=["PyTorch not installed. Run: pip install torch"],
            )

        try:
            from shap_e.diffusion.sample import sample_latents
            from shap_e.diffusion.gaussian_diffusion import diffusion_from_config
            from shap_e.models.download import load_model, load_config
            from shap_e.util.notebooks import decode_latent_mesh
        except ImportError:
            return RawMesh(
                stl_path=None,
                obj_path=None,
                backend_name=self.name,
                warnings=["shap-e not installed. Run: pip install shap-e"],
            )

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        if image_path and image_path.exists():
            return self._image_to_3d(description, image_path, work_dir, device)
        return self._text_to_3d(description, work_dir, device)

    # ------------------------------------------------------------------
    def _text_to_3d(
        self, description: ObjectDescription, work_dir: Path, device
    ) -> RawMesh:
        import torch
        from shap_e.diffusion.sample import sample_latents
        from shap_e.diffusion.gaussian_diffusion import diffusion_from_config
        from shap_e.models.download import load_model, load_config
        from shap_e.util.notebooks import decode_latent_mesh

        xm = load_model("transmitter", device=device)
        model = load_model("text300M", device=device)
        diffusion = diffusion_from_config(load_config("diffusion"))

        prompt = self._build_prompt(description)

        latents = sample_latents(
            batch_size=1,
            model=model,
            diffusion=diffusion,
            guidance_scale=_GUIDANCE_SCALE,
            model_kwargs=dict(texts=[prompt]),
            progress=True,
            clip_denoised=True,
            use_fp16=True,
            use_karras=True,
            karras_steps=_TEXT_STEPS,
            sigma_min=1e-3,
            sigma_max=160,
            s_churn=0,
        )

        return self._export(latents[0], xm, description.name, work_dir)

    def _image_to_3d(
        self,
        description: ObjectDescription,
        image_path: Path,
        work_dir: Path,
        device,
    ) -> RawMesh:
        import torch
        from PIL import Image
        from shap_e.diffusion.sample import sample_latents
        from shap_e.diffusion.gaussian_diffusion import diffusion_from_config
        from shap_e.models.download import load_model, load_config
        from shap_e.util.notebooks import decode_latent_mesh

        xm = load_model("transmitter", device=device)
        model = load_model("image300M", device=device)
        diffusion = diffusion_from_config(load_config("diffusion"))

        image = Image.open(str(image_path)).convert("RGBA")

        latents = sample_latents(
            batch_size=1,
            model=model,
            diffusion=diffusion,
            guidance_scale=3.0,
            model_kwargs=dict(images=[image]),
            progress=True,
            clip_denoised=True,
            use_fp16=True,
            use_karras=True,
            karras_steps=_IMAGE_STEPS,
            sigma_min=1e-3,
            sigma_max=160,
            s_churn=0,
        )

        return self._export(latents[0], xm, description.name, work_dir)

    @staticmethod
    def _export(latent, xm, name: str, work_dir: Path) -> RawMesh:
        from shap_e.util.notebooks import decode_latent_mesh

        safe_name = name.lower().replace(" ", "_")[:40]
        stl_path = work_dir / f"shape_e_{safe_name}.stl"

        tri = decode_latent_mesh(xm, latent).tri_mesh()
        with open(stl_path, "wb") as f:
            tri.write_stl(f)

        return RawMesh(
            stl_path=stl_path,
            obj_path=None,
            backend_name="shape-e",
        )

    @staticmethod
    def _build_prompt(desc: ObjectDescription) -> str:
        dims = desc.overall_dimensions_mm
        return (
            f"{desc.description} "
            f"approximately {dims['x']:.0f}x{dims['y']:.0f}x{dims['z']:.0f}mm"
        )
