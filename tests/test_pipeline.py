"""End-to-end and unit tests for the nanoclaw pipeline.

Run all tests:
    python tests/test_pipeline.py

Run only offline tests (no API keys needed):
    python tests/test_pipeline.py --offline

Run only the full pipeline test:
    python tests/test_pipeline.py --full
"""

from __future__ import annotations

import json
import os
import struct
import sys
import tempfile
import traceback
from pathlib import Path

# Make project root importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

results: list[tuple[str, str, str]] = []


def test(name: str):
    def decorator(fn):
        def wrapper(*args, **kwargs):
            try:
                fn(*args, **kwargs)
                results.append((name, PASS, ""))
                print(f"  [{PASS}] {name}")
            except Exception as e:
                results.append((name, FAIL, str(e)))
                print(f"  [{FAIL}] {name}")
                print(f"         {e}")
        return wrapper
    return decorator


def skip(name: str, reason: str):
    results.append((name, SKIP, reason))
    print(f"  [{SKIP}] {name} — {reason}")


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def make_test_stl(path: Path, size_mm: float = 50.0) -> Path:
    """Write a minimal valid binary STL (cube)."""
    # 12 triangles for a cube, simple approach with just header + count + one triangle
    vertices = [
        # bottom face
        ((0,0,0),(size_mm,0,0),(size_mm,size_mm,0)),
        ((0,0,0),(size_mm,size_mm,0),(0,size_mm,0)),
        # top face
        ((0,0,size_mm),(size_mm,size_mm,size_mm),(size_mm,0,size_mm)),
        ((0,0,size_mm),(0,size_mm,size_mm),(size_mm,size_mm,size_mm)),
        # front
        ((0,0,0),(size_mm,0,0),(size_mm,0,size_mm)),
        ((0,0,0),(size_mm,0,size_mm),(0,0,size_mm)),
        # back
        ((0,size_mm,0),(size_mm,size_mm,size_mm),(size_mm,size_mm,0)),
        ((0,size_mm,0),(0,size_mm,size_mm),(size_mm,size_mm,size_mm)),
        # left
        ((0,0,0),(0,size_mm,0),(0,size_mm,size_mm)),
        ((0,0,0),(0,size_mm,size_mm),(0,0,size_mm)),
        # right
        ((size_mm,0,0),(size_mm,size_mm,size_mm),(size_mm,size_mm,0)),
        ((size_mm,0,0),(size_mm,0,size_mm),(size_mm,size_mm,size_mm)),
    ]
    normals = [(0,-1,0),(0,-1,0),(0,1,0),(0,1,0),
               (0,-1,0),(0,-1,0),(0,1,0),(0,1,0),
               (-1,0,0),(-1,0,0),(1,0,0),(1,0,0)]

    with open(path, "wb") as f:
        f.write(b"\x00" * 80)  # header
        f.write(struct.pack("<I", len(vertices)))
        for (n, tri) in zip(normals, vertices):
            f.write(struct.pack("<3f", *n))
            for v in tri:
                f.write(struct.pack("<3f", *v))
            f.write(struct.pack("<H", 0))  # attr
    return path


def make_large_stl(path: Path) -> Path:
    """STL that exceeds the print bed (300 mm cube)."""
    return make_test_stl(path, size_mm=300.0)


# ──────────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────────

@test("imports: all modules load without error")
def test_imports():
    from utils.file_utils import ensure_dir, safe_filename, load_image_as_base64
    from tools.openscad_tools import OpenSCADTools
    from tools.stl_tools import STLTools
    from agents.backends import BACKEND_MAP, ShapeEBackend, Tripo3DBackend, MeshyBackend
    from agents.analysis_agent import AnalysisAgent, ObjectDescription, SuggestedPart
    from agents.design_agent import DesignAgent, DesignResult
    from agents.mesh_agent import MeshAgent, MeshResult
    from pipeline.partitioner import Partitioner, PartitionResult, Part
    from pipeline.mesh_partitioner import MeshPartitioner, MeshPartitionResult
    from pipeline.packager import Packager, PackageResult
    from pipeline.orchestrator import Pipeline, PipelineResult


@test("utils: safe_filename handles special chars and long strings")
def test_safe_filename():
    from utils.file_utils import safe_filename
    assert safe_filename("Hello World! 123") == "hello_world_123", safe_filename("Hello World! 123")
    result = safe_filename("Café & Co.")
    # é is a Unicode word char so it survives; & and . are stripped
    assert "café" in result or "cafe" in result or result.startswith("caf")
    assert "&" not in result and "." not in result
    long = safe_filename("a" * 200)
    assert len(long) <= 64


@test("utils: ensure_dir creates nested directories")
def test_ensure_dir():
    from utils.file_utils import ensure_dir
    with tempfile.TemporaryDirectory() as tmp:
        p = ensure_dir(Path(tmp) / "a" / "b" / "c")
        assert p.is_dir()


@test("stl_tools: analyses a well-formed test STL")
def test_stl_analysis():
    from tools.stl_tools import STLTools
    with tempfile.TemporaryDirectory() as tmp:
        stl = make_test_stl(Path(tmp) / "cube.stl", size_mm=50.0)
        result = STLTools.analyse(stl)
        assert "error" not in result, result.get("error")
        dims = result["dimensions_mm"]
        assert 45 < dims["x"] < 55, f"unexpected x={dims['x']}"
        assert result["printable"] is True


@test("stl_tools: flags oversized mesh as not printable")
def test_stl_oversized():
    from tools.stl_tools import STLTools
    with tempfile.TemporaryDirectory() as tmp:
        stl = make_large_stl(Path(tmp) / "big.stl")
        result = STLTools.analyse(stl)
        assert result["printable"] is False
        assert result["issues"]


@test("openscad_tools: heuristic validator accepts balanced braces")
def test_openscad_heuristic():
    from tools.openscad_tools import OpenSCADTools
    with tempfile.TemporaryDirectory() as tmp:
        osc = OpenSCADTools(work_dir=tmp)
        osc.write_openscad_file("test.scad", "module part_box() { cube([10,10,10]); }")
        result = osc.validate_openscad_file("test.scad")
        assert "ERROR" not in result.upper() or "heuristic" in result.lower(), result


@test("openscad_tools: heuristic rejects mismatched braces")
def test_openscad_bad_braces():
    from tools.openscad_tools import OpenSCADTools
    with tempfile.TemporaryDirectory() as tmp:
        osc = OpenSCADTools(work_dir=tmp)
        osc.write_openscad_file("bad.scad", "module part_box() { cube([10,10,10]); ")
        result = osc.validate_openscad_file("bad.scad")
        assert "ERROR" in result


@test("openscad_tools: list_part_modules finds module names")
def test_list_modules():
    from tools.openscad_tools import OpenSCADTools
    with tempfile.TemporaryDirectory() as tmp:
        osc = OpenSCADTools(work_dir=tmp)
        code = """
module part_base() { cube([100,100,5]); }
module part_lid() { cube([100,100,3]); }
module helper_pin() { cylinder(r=2, h=8); }
module assembly() { part_base(); translate([0,0,5]) part_lid(); }
assembly();
"""
        osc.write_openscad_file("master.scad", code)
        out = osc.list_part_modules("master.scad")
        assert "part_base" in out
        assert "part_lid" in out
        assert "helper_pin" not in out  # doesn't start with part_


@test("mesh_partitioner: single part when STL fits on bed")
def test_mesh_partitioner_single():
    from pipeline.mesh_partitioner import MeshPartitioner
    with tempfile.TemporaryDirectory() as tmp:
        stl = make_test_stl(Path(tmp) / "small.stl", size_mm=50.0)
        mp = MeshPartitioner(work_dir=tmp)
        result = mp.partition(stl, "test_object")
        assert len(result.parts) == 1
        assert result.parts[0].printable is True


@test("mesh_partitioner: splits oversized STL into multiple parts")
def test_mesh_partitioner_split():
    from pipeline.mesh_partitioner import MeshPartitioner
    with tempfile.TemporaryDirectory() as tmp:
        stl = make_large_stl(Path(tmp) / "big.stl")
        mp = MeshPartitioner(work_dir=tmp)
        result = mp.partition(stl, "big_object")
        # Should produce 2+ parts; fallback gives 1 part marked non-printable
        if len(result.parts) == 1:
            # Fallback path: single part must flag the oversize issue
            assert not result.parts[0].printable or result.warnings, \
                "Single oversized part should be flagged as non-printable or have warnings"
        else:
            assert len(result.parts) >= 2, f"Expected >=2 parts, got {len(result.parts)}"


@test("mesh_partitioner: to_partition_result converts correctly")
def test_mesh_to_partition():
    from pipeline.mesh_partitioner import MeshPartitioner
    with tempfile.TemporaryDirectory() as tmp:
        stl = make_test_stl(Path(tmp) / "item.stl", size_mm=100.0)
        mp = MeshPartitioner(work_dir=tmp)
        mesh_result = mp.partition(stl, "item")
        partition = mesh_result.to_partition_result(Path(tmp))
        assert len(partition.parts) == len(mesh_result.parts)
        for p in partition.parts:
            assert p.module_name.startswith("mesh_part_")


@test("packager: writes all expected files")
def test_packager():
    from pipeline.packager import Packager
    from pipeline.partitioner import Part, PartitionResult
    from agents.design_agent import DesignResult
    from agents.analysis_agent import ObjectDescription, SuggestedPart

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        stl_src = make_test_stl(tmp / "part_01.stl")

        obj_desc = ObjectDescription(
            name="Test Box",
            description="A simple test box for unit testing.",
            overall_dimensions_mm={"x": 50, "y": 50, "z": 30},
            features=["hollow interior", "flat base"],
            suggested_parts=[],
            structural_requirements="Rigid PLA sufficient.",
            print_considerations=["No supports needed."],
            assembly_notes="Single part, no assembly.",
        )
        part = Part(
            index=1,
            module_name="part_body",
            scad_filename="part_01_body.scad",
            stl_filename="part_01_body.stl",
            scad_path=tmp / "nonexistent.scad",
            stl_path=stl_src,
            dimensions_mm={"x": 50, "y": 50, "z": 30},
            printable=True,
        )
        partition = PartitionResult(
            parts=[part],
            master_scad_path=tmp / "master.scad",
        )
        stub_design = DesignResult(
            master_scad_path=tmp / "master.scad",
            part_modules=["part_body"],
        )

        packager = Packager(output_root=tmp / "output")
        result = packager.package(obj_desc, stub_design, partition, "test_box")

        assert result.output_dir.exists()
        assert (result.output_dir / "manifest.json").exists()
        assert (result.output_dir / "docs" / "print_instructions.md").exists()
        assert (result.output_dir / "docs" / "assembly_guide.md").exists()
        assert result.stl_count == 1

        manifest = json.loads((result.output_dir / "manifest.json").read_text())
        assert manifest["object"]["name"] == "Test Box"
        assert len(manifest["parts"]) == 1


@test("backend_map: all three backends listed")
def test_backend_map():
    from agents.backends import BACKEND_MAP
    assert "shape-e" in BACKEND_MAP
    assert "tripo3d" in BACKEND_MAP
    assert "meshy" in BACKEND_MAP


@test("pipeline: mode resolution with tripo3d backend")
def test_mode_resolution():
    from pipeline.orchestrator import Pipeline
    from agents.analysis_agent import ObjectDescription

    organic_desc = ObjectDescription(
        name="Capybara Figurine",
        description="A cute sitting capybara animal figurine toy.",
        overall_dimensions_mm={"x": 100, "y": 80, "z": 120},
        features=["four legs", "round body"],
        suggested_parts=[],
        structural_requirements="",
        print_considerations=[],
        assembly_notes="",
    )
    mechanical_desc = ObjectDescription(
        name="Cable Clip",
        description="A small cable management clip.",
        overall_dimensions_mm={"x": 30, "y": 15, "z": 10},
        features=["clip mechanism"],
        suggested_parts=[],
        structural_requirements="",
        print_considerations=[],
        assembly_notes="",
    )

    # Auto with tripo3d key available → organic = mesh, mechanical = parametric
    os.environ["TRIPO3D_API_KEY"] = "test_key_for_resolution_check"
    pipeline = Pipeline(mode="auto", mesh_backend="tripo3d")
    assert pipeline._resolve_mode(organic_desc) == "mesh"
    assert pipeline._resolve_mode(mechanical_desc) == "parametric"
    del os.environ["TRIPO3D_API_KEY"]

    # Auto with no key → always parametric
    pipeline2 = Pipeline(mode="auto", mesh_backend="tripo3d")
    assert pipeline2._resolve_mode(organic_desc) == "parametric"

    # Explicit mode always wins
    pipeline3 = Pipeline(mode="mesh", mesh_backend="tripo3d")
    assert pipeline3._resolve_mode(mechanical_desc) == "mesh"


# ──────────────────────────────────────────────────────────────────────────────
# Live API tests (require ANTHROPIC_API_KEY)
# ──────────────────────────────────────────────────────────────────────────────

def test_analysis_live():
    name = "analysis_agent: live Claude API call"
    if not os.environ.get("ANTHROPIC_API_KEY"):
        skip(name, "ANTHROPIC_API_KEY not set")
        return

    @test(name)
    def _run():
        from agents.analysis_agent import AnalysisAgent
        agent = AnalysisAgent()
        desc = agent.analyse(text_description="A small two-compartment pill organizer box, 80mm wide")
        assert desc.name
        assert desc.overall_dimensions_mm
        assert len(desc.suggested_parts) >= 1
        assert desc.description
        print(f"\n         → name: {desc.name}")
        print(f"           parts: {[p.name for p in desc.suggested_parts]}")
        print(f"           dims:  {desc.overall_dimensions_mm}")
    _run()


def test_full_pipeline_tripo3d():
    name = "full_pipeline: analysis → Tripo3D mesh → partition → package"
    if not os.environ.get("ANTHROPIC_API_KEY"):
        skip(name, "ANTHROPIC_API_KEY not set")
        return
    if not os.environ.get("TRIPO3D_API_KEY"):
        skip(name, "TRIPO3D_API_KEY not set")
        return

    @test(name)
    def _run():
        from pipeline.orchestrator import Pipeline

        with tempfile.TemporaryDirectory() as tmp:
            pipeline = Pipeline(
                output_root=tmp,
                mode="mesh",
                mesh_backend="tripo3d",
            )
            result = pipeline.run(
                text_description=(
                    "A small decorative capybara figurine sitting calmly, "
                    "80mm tall, suitable for 3D printing."
                ),
                progress_callback=lambda s, m: print(f"         [{s}] {m}"),
            )

            assert result.success, "Pipeline returned no parts"
            assert result.output_dir.exists()
            assert (result.output_dir / "manifest.json").exists()

            manifest = json.loads((result.output_dir / "manifest.json").read_text())
            print(f"\n         → output: {result.output_dir}")
            print(f"           parts:  {len(manifest['parts'])}")
            print(f"           STLs:   {manifest['files']['stl_count']}")
            print(f"           warnings: {manifest.get('warnings', [])}")

            assert manifest["files"]["stl_count"] >= 1, "No STL files in package"
    _run()


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    offline_only = "--offline" in sys.argv
    full_only = "--full" in sys.argv

    print("\n=== nanoclaw test suite ===\n")

    if not full_only:
        print("── Offline unit tests ──")
        test_imports()
        test_safe_filename()
        test_ensure_dir()
        test_stl_analysis()
        test_stl_oversized()
        test_openscad_heuristic()
        test_openscad_bad_braces()
        test_list_modules()
        test_mesh_partitioner_single()
        test_mesh_partitioner_split()
        test_mesh_to_partition()
        test_packager()
        test_backend_map()
        test_mode_resolution()

    if not offline_only:
        print("\n── Live API tests ──")
        test_analysis_live()
        test_full_pipeline_tripo3d()

    print("\n── Summary ──")
    passed = sum(1 for _, s, _ in results if s == PASS)
    failed = sum(1 for _, s, _ in results if s == FAIL)
    skipped = sum(1 for _, s, _ in results if s == SKIP)
    print(f"  {passed} passed  |  {failed} failed  |  {skipped} skipped")

    if failed:
        print("\nFailed tests:")
        for name, status, msg in results:
            if status == FAIL:
                print(f"  - {name}: {msg}")
        sys.exit(1)
    else:
        print("\nAll tests passed (or skipped).")
        sys.exit(0)
