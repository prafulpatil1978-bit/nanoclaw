from .base import MeshBackend, RawMesh
from .shape_e import ShapeEBackend
from .tripo3d import Tripo3DBackend
from .meshy import MeshyBackend

BACKEND_MAP = {
    "shape-e": ShapeEBackend,
    "tripo3d": Tripo3DBackend,
    "meshy": MeshyBackend,
}

__all__ = [
    "MeshBackend", "RawMesh",
    "ShapeEBackend", "Tripo3DBackend", "MeshyBackend",
    "BACKEND_MAP",
]
