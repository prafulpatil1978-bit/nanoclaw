"""Stage 1: Analyse an image, sketch, or text description into a structured object spec."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic

from utils.file_utils import load_image_as_base64


@dataclass
class SuggestedPart:
    name: str
    description: str
    estimated_dimensions_mm: dict[str, float]  # x, y, z
    notes: str = ""


@dataclass
class ObjectDescription:
    name: str
    description: str
    overall_dimensions_mm: dict[str, float]      # x, y, z bounding box
    features: list[str]
    suggested_parts: list[SuggestedPart]
    structural_requirements: str
    print_considerations: list[str]
    assembly_notes: str
    raw_analysis: str = ""


_SYSTEM_PROMPT = """\
You are an expert mechanical engineer and 3D printing specialist. Your job is to analyse \
an object — provided as an image, sketch, and/or text description — and produce a precise, \
structured specification that will guide an AI agent to create a 3D model.

Return ONLY a valid JSON object with exactly these keys:
{
  "name": "short object name",
  "description": "1-3 sentence accurate description",
  "overall_dimensions_mm": {"x": <float>, "y": <float>, "z": <float>},
  "features": ["list", "of", "notable", "geometric", "features"],
  "suggested_parts": [
    {
      "name": "part_name_snake_case",
      "description": "what this part is and does",
      "estimated_dimensions_mm": {"x": <float>, "y": <float>, "z": <float>},
      "notes": "print orientation, material, or structural notes"
    }
  ],
  "structural_requirements": "load-bearing, flexibility, or rigidity requirements",
  "print_considerations": [
    "each consideration as a string, e.g. supports needed, minimum wall thickness"
  ],
  "assembly_notes": "how parts connect and assemble in order"
}

Rules for suggested_parts:
- Each part must fit within 220×220×250 mm (standard FDM print bed).
- If the object is small enough, it can be a single part.
- Prefer 2-6 parts joined with snap-fit, press-fit pins, or M3 bolts.
- Name parts descriptively: base, body, lid, bracket, arm_left, etc.
- Assume PLA filament, 0.2 mm layer height, 20% infill unless otherwise specified.

Do not include markdown, code fences, or any text outside the JSON.\
"""


class AnalysisAgent:
    def __init__(self, model: str | None = None) -> None:
        self.client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self.model = model or os.environ.get("ANALYSIS_MODEL", "claude-opus-4-7")

    def analyse(
        self,
        text_description: str | None = None,
        image_path: str | Path | None = None,
    ) -> ObjectDescription:
        if not text_description and not image_path:
            raise ValueError("Provide at least a text description or an image path.")

        content: list[dict[str, Any]] = []

        if image_path:
            data, media_type = load_image_as_base64(image_path)
            content.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": data},
                }
            )

        user_text = text_description or "Analyse the object shown in the image."
        if image_path and text_description:
            user_text = f"Image provided above.\n\nAdditional description: {text_description}"
        content.append({"type": "text", "text": user_text})

        response = self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": content}],
        )

        raw = response.content[0].text.strip()
        data = json.loads(raw)

        parts = [
            SuggestedPart(
                name=p["name"],
                description=p["description"],
                estimated_dimensions_mm=p["estimated_dimensions_mm"],
                notes=p.get("notes", ""),
            )
            for p in data.get("suggested_parts", [])
        ]

        return ObjectDescription(
            name=data["name"],
            description=data["description"],
            overall_dimensions_mm=data["overall_dimensions_mm"],
            features=data.get("features", []),
            suggested_parts=parts,
            structural_requirements=data.get("structural_requirements", ""),
            print_considerations=data.get("print_considerations", []),
            assembly_notes=data.get("assembly_notes", ""),
            raw_analysis=raw,
        )
