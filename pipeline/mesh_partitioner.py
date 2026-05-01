"""Split a large STL mesh into print-bed-sized parts using trimesh convex decomposition.

For organic mesh objects (from Meshy.ai), we can't use OpenSCAD module splitting.
Instead we use geometric subdivision: the mesh is cut along planes and each
sub-mesh is exported as a separate STL with a connector pin cavity/peg added.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from pipeline.partitioner import Part, PartitionResult

BED_X = float(os.environ.get("PRINT_BED_X", 220))
BED_Y = float(os.environ.get("PRINT_BED_Y", 220))
BED_Z = float(os.environ.get("PRINT_BED_Z", 250))

# Connector peg dimensions (mm)
PEG_RADIUS = 3.0
PEG_HEIGHT = 8.0
PEG_CLEARANCE = 0.3  # socket is peg_radius + clearance


@dataclass
class MeshPart:
    index: int
    stl_path: Path
    stl_filename: str
    dimensions_mm: dict
    printable: bool
    issues: list[str] = field(default_factory=list)
    split_axis: str = ""


@dataclass
class MeshPartitionResult:
    parts: list[MeshPart]
    warnings: list[str] = field(default_factory=list)

    def to_partition_result(self, work_dir: Path) -> PartitionResult:
        """Adapt into the generic PartitionResult used by Packager."""
        generic_parts = [
            Part(
                index=p.index,
                module_name=f"mesh_part_{p.index:02d}",
                scad_filename="N/A (mesh mode)",
                stl_filename=p.stl_filename,
                scad_path=work_dir / "N/A",
                stl_path=p.stl_path,
                dimensions_mm=p.dimensions_mm,
                printable=p.printable,
                issues=p.issues,
            )
            for p in self.parts
        ]
        return PartitionResult(
            parts=generic_parts,
            master_scad_path=work_dir / "N/A",
            warnings=self.warnings,
        )


class MeshPartitioner:
    """Slice a large mesh into print-bed-sized sub-meshes."""

    def __init__(self, work_dir: str | Path) -> None:
        self.work_dir = Path(work_dir)
        self.stl_dir = self.work_dir / "stl"
        self.stl_dir.mkdir(parents=True, exist_ok=True)

    def partition(self, stl_path: Path, object_name: str) -> MeshPartitionResult:
        try:
            import trimesh
        except ImportError:
            return MeshPartitionResult(
                parts=[],
                warnings=["trimesh not installed — cannot partition mesh. pip install trimesh"],
            )

        mesh = trimesh.load(str(stl_path), force="mesh")
        extents = mesh.bounding_box.extents  # (x, y, z)
        warnings: list[str] = []

        # Determine how many cuts are needed per axis
        cuts_x = max(1, math.ceil(extents[0] / BED_X))
        cuts_y = max(1, math.ceil(extents[1] / BED_Y))
        cuts_z = max(1, math.ceil(extents[2] / BED_Z))

        if cuts_x == 1 and cuts_y == 1 and cuts_z == 1:
            # Fits on bed as-is — single part
            dest = self.stl_dir / f"{object_name}_part_01.stl"
            mesh.export(str(dest))
            dims = {
                "x": round(float(extents[0]), 2),
                "y": round(float(extents[1]), 2),
                "z": round(float(extents[2]), 2),
            }
            return MeshPartitionResult(
                parts=[
                    MeshPart(
                        index=1,
                        stl_path=dest,
                        stl_filename=dest.name,
                        dimensions_mm=dims,
                        printable=True,
                    )
                ],
                warnings=warnings,
            )

        # Slice the mesh into a grid of sub-meshes
        parts: list[MeshPart] = []
        bounds_min = mesh.bounds[0]
        bounds_max = mesh.bounds[1]

        # Choose primary cut axis (longest dimension that exceeds bed)
        axis_needed = []
        if cuts_x > 1:
            axis_needed.append(("x", 0, cuts_x))
        if cuts_y > 1:
            axis_needed.append(("y", 1, cuts_y))
        if cuts_z > 1:
            axis_needed.append(("z", 2, cuts_z))

        # For simplicity, cut along the single longest oversize axis first
        axis_needed.sort(key=lambda a: -a[2])
        axis_label, axis_idx, n_cuts = axis_needed[0]

        segment_length = (bounds_max[axis_idx] - bounds_min[axis_idx]) / n_cuts
        part_idx = 1

        for i in range(n_cuts):
            lo = bounds_min[axis_idx] + i * segment_length
            hi = lo + segment_length

            # Slice mesh between lo and hi on chosen axis
            sub = self._slice_between(mesh, axis_idx, lo, hi)
            if sub is None or len(sub.faces) == 0:
                warnings.append(f"Slice {i+1}/{n_cuts} along {axis_label} produced empty mesh.")
                continue

            # Add connector peg/socket geometry
            sub = self._add_connectors(sub, axis_idx, lo, hi, bounds_min[axis_idx], bounds_max[axis_idx], i, n_cuts)

            # Orient flat on bed
            sub.apply_translation(-sub.bounds[0])

            fname = f"{object_name}_part_{part_idx:02d}_{axis_label}{i+1}.stl"
            dest = self.stl_dir / fname
            sub.export(str(dest))

            ex = sub.bounding_box.extents
            dims = {"x": round(float(ex[0]), 2), "y": round(float(ex[1]), 2), "z": round(float(ex[2]), 2)}
            issues = self._check_printability(dims)

            parts.append(
                MeshPart(
                    index=part_idx,
                    stl_path=dest,
                    stl_filename=fname,
                    dimensions_mm=dims,
                    printable=len(issues) == 0,
                    issues=issues,
                    split_axis=f"{axis_label}{i+1}",
                )
            )
            part_idx += 1

        if not parts:
            warnings.append("Mesh slicing produced no valid parts. Using full mesh as single part.")
            dest = self.stl_dir / f"{object_name}_part_01_full.stl"
            mesh.export(str(dest))
            ex = mesh.bounding_box.extents
            parts = [
                MeshPart(
                    index=1,
                    stl_path=dest,
                    stl_filename=dest.name,
                    dimensions_mm={"x": round(float(ex[0]), 2), "y": round(float(ex[1]), 2), "z": round(float(ex[2]), 2)},
                    printable=False,
                    issues=["Exceeds print bed — manual splitting required."],
                )
            ]

        return MeshPartitionResult(parts=parts, warnings=warnings)

    # ------------------------------------------------------------------
    # Geometry helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _slice_between(mesh, axis: int, lo: float, hi: float):
        """Cut mesh to the slab between lo and hi along axis."""
        try:
            import trimesh
            import numpy as np

            origin_lo = [0.0, 0.0, 0.0]
            normal_lo = [0.0, 0.0, 0.0]
            origin_lo[axis] = lo
            normal_lo[axis] = 1.0  # keep the hi side

            origin_hi = [0.0, 0.0, 0.0]
            normal_hi = [0.0, 0.0, 0.0]
            origin_hi[axis] = hi
            normal_hi[axis] = -1.0  # keep the lo side

            result = mesh.slice_plane(
                plane_origin=origin_lo, plane_normal=normal_lo, cap=True
            )
            if result is None or len(result.faces) == 0:
                return None
            result = result.slice_plane(
                plane_origin=origin_hi, plane_normal=normal_hi, cap=True
            )
            return result
        except Exception:
            return None

    @staticmethod
    def _add_connectors(mesh, axis: int, lo: float, hi: float,
                         global_lo: float, global_hi: float,
                         part_idx: int, total_parts: int):
        """Emboss connector pegs/sockets on cut faces between adjacent parts."""
        try:
            import trimesh
            import numpy as np
            from trimesh.creation import cylinder

            # Find centroid of the cut face to place the connector
            bounds_centre = (mesh.bounds[0] + mesh.bounds[1]) / 2.0
            centre_2d = bounds_centre.copy()

            peg = cylinder(radius=PEG_RADIUS, height=PEG_HEIGHT, sections=32)
            socket = cylinder(radius=PEG_RADIUS + PEG_CLEARANCE, height=PEG_HEIGHT, sections=32)

            result = mesh
            # Add peg on high-axis face (connecting to the next part)
            if part_idx < total_parts - 1:
                peg_c = peg.copy()
                pos = centre_2d.copy()
                pos[axis] = hi - PEG_HEIGHT / 2
                peg_c.apply_translation(pos - peg_c.centroid)
                result = trimesh.boolean.union([result, peg_c], engine="blender")

            # Cut socket on low-axis face (receiving peg from previous part)
            if part_idx > 0:
                sock_c = socket.copy()
                pos = centre_2d.copy()
                pos[axis] = lo + PEG_HEIGHT / 2
                sock_c.apply_translation(pos - sock_c.centroid)
                result = trimesh.boolean.difference([result, sock_c], engine="blender")

            return result if isinstance(result, trimesh.Trimesh) else mesh
        except Exception:
            # Connector boolean ops require blender backend; skip gracefully
            return mesh

    @staticmethod
    def _check_printability(dims: dict) -> list[str]:
        issues = []
        if dims["x"] > BED_X or dims["y"] > BED_Y:
            issues.append(
                f"Footprint {dims['x']:.0f}×{dims['y']:.0f} mm exceeds bed {BED_X:.0f}×{BED_Y:.0f} mm"
            )
        if dims["z"] > BED_Z:
            issues.append(f"Height {dims['z']:.0f} mm exceeds max {BED_Z:.0f} mm")
        return issues
