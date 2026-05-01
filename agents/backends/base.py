"""Abstract base class for 3D mesh generation backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from agents.analysis_agent import ObjectDescription


@dataclass
class RawMesh:
    """Result from any backend — a local STL (or OBJ) path plus metadata."""
    stl_path: Path | None
    obj_path: Path | None
    backend_name: str
    task_id: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return (self.stl_path is not None and self.stl_path.exists()) or \
               (self.obj_path is not None and self.obj_path.exists())

    @property
    def best_path(self) -> Path | None:
        """Return STL if available, otherwise OBJ."""
        if self.stl_path and self.stl_path.exists():
            return self.stl_path
        return self.obj_path


class MeshBackend(ABC):
    """Common interface every mesh generation backend must implement."""

    @abstractmethod
    def generate(
        self,
        description: ObjectDescription,
        image_path: Path | None,
        work_dir: Path,
    ) -> RawMesh:
        """Generate a mesh and return local file path(s)."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        ...
