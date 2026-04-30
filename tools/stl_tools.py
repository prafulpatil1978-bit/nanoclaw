"""STL file analysis utilities."""

from __future__ import annotations

from pathlib import Path


class STLTools:
    """Analyses STL files for print-readiness without requiring OpenSCAD."""

    PRINT_BED_X_MM = 220.0
    PRINT_BED_Y_MM = 220.0
    PRINT_BED_Z_MM = 250.0

    @staticmethod
    def analyse(stl_path: str | Path) -> dict:
        """Return bounding box and basic printability verdict."""
        path = Path(stl_path)
        if not path.exists():
            return {"error": f"File not found: {path}"}
        try:
            import trimesh

            mesh = trimesh.load(str(path), force="mesh")
            bounds = mesh.bounding_box.extents  # (x, y, z)
            volume_cm3 = float(mesh.volume) / 1000.0
            is_watertight = mesh.is_watertight
            verdict = STLTools._printability(bounds)
            return {
                "dimensions_mm": {
                    "x": round(float(bounds[0]), 2),
                    "y": round(float(bounds[1]), 2),
                    "z": round(float(bounds[2]), 2),
                },
                "volume_cm3": round(volume_cm3, 2),
                "is_watertight": is_watertight,
                "printable": verdict["printable"],
                "issues": verdict["issues"],
            }
        except ImportError:
            return {"error": "trimesh not installed — run: pip install trimesh"}
        except Exception as exc:
            return {"error": str(exc)}

    @staticmethod
    def _printability(bounds) -> dict:
        issues = []
        x, y, z = float(bounds[0]), float(bounds[1]), float(bounds[2])
        if x > STLTools.PRINT_BED_X_MM or y > STLTools.PRINT_BED_Y_MM:
            issues.append(
                f"Footprint {x:.0f}×{y:.0f} mm exceeds bed {STLTools.PRINT_BED_X_MM:.0f}×{STLTools.PRINT_BED_Y_MM:.0f} mm"
            )
        if z > STLTools.PRINT_BED_Z_MM:
            issues.append(f"Height {z:.0f} mm exceeds max {STLTools.PRINT_BED_Z_MM:.0f} mm")
        return {"printable": len(issues) == 0, "issues": issues}
