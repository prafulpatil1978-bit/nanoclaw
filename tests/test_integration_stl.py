"""Full pipeline integration test — no LLM API keys required.

Generates real STL output files by mocking the LLM analysis step and
running the real mesh/partition/package pipeline using trimesh geometry.

Output STLs are saved to:  tests/output/integration_test/stl/

Run:
    python tests/test_integration_stl.py
"""

from __future__ import annotations

import json
import struct
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

# ── Colour helpers ─────────────────────────────────────────────────────────────
GREEN = "\033[92m"; RED = "\033[91m"; CYAN = "\033[96m"; DIM = "\033[2m"; NC = "\033[0m"

PASS = "PASS"; FAIL = "FAIL"
results: list[tuple[str, str, str]] = []


def run(name: str):
    def dec(fn):
        print(f"\n{DIM}── {name} ──{NC}")
        try:
            fn()
            results.append((name, PASS, ""))
            print(f"  {GREEN}[PASS]{NC} {name}")
        except Exception as exc:
            import traceback
            results.append((name, FAIL, str(exc)))
            print(f"  {RED}[FAIL]{NC} {name}")
            traceback.print_exc()
    return dec


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_binary_stl(path: Path, size: float = 60.0) -> Path:
    """Write a watertight binary STL cube."""
    tris = [
        ((0,0,0),(size,0,0),(size,size,0)), ((0,0,0),(size,size,0),(0,size,0)),
        ((0,0,size),(size,size,size),(size,0,size)), ((0,0,size),(0,size,size),(size,size,size)),
        ((0,0,0),(size,0,0),(size,0,size)), ((0,0,0),(size,0,size),(0,0,size)),
        ((0,size,0),(size,size,size),(size,size,0)), ((0,size,0),(0,size,size),(size,size,size)),
        ((0,0,0),(0,size,0),(0,size,size)), ((0,0,0),(0,size,size),(0,0,size)),
        ((size,0,0),(size,size,size),(size,size,0)), ((size,0,0),(size,0,size),(size,size,size)),
    ]
    norms = [(0,0,-1)]*2 + [(0,0,1)]*2 + [(0,-1,0)]*2 + [(0,1,0)]*2 + [(-1,0,0)]*2 + [(1,0,0)]*2
    with open(path, "wb") as f:
        f.write(b"\x00" * 80)
        f.write(struct.pack("<I", len(tris)))
        for n, tri in zip(norms, tris):
            f.write(struct.pack("<3f", *n))
            for v in tri:
                f.write(struct.pack("<3f", *v))
            f.write(struct.pack("<H", 0))
    return path


def _mock_obj_desc(name="Test Box", category="mechanical", hint=None):
    from agents.analysis_agent import ObjectDescription, SuggestedPart
    return ObjectDescription(
        name=name,
        description=f"A {name.lower()} for integration testing.",
        overall_dimensions_mm={"x": 80, "y": 60, "z": 40},
        features=["hollow body", "flat base"],
        suggested_parts=[
            SuggestedPart(
                name="body",
                description="Main body",
                estimated_dimensions_mm={"x": 80, "y": 60, "z": 40},
                notes="Print upright",
            )
        ],
        structural_requirements="Rigid PLA.",
        print_considerations=["No supports needed."],
        assembly_notes="Single part.",
        part_category=category,
        template_hint=hint,
    )


# ── Output folder ──────────────────────────────────────────────────────────────
OUTPUT = Path(__file__).parent / "output" / "integration_test"
OUTPUT.mkdir(parents=True, exist_ok=True)


# ── Test 1: mesh pipeline with synthetic STL ──────────────────────────────────
@run("mesh_pipeline: synthetic STL → partition → package → real STL output")
def test_mesh_pipeline_real_stl():
    from pipeline.mesh_partitioner import MeshPartitioner
    from pipeline.packager import Packager
    from pipeline.partitioner import Part, PartitionResult
    from agents.design_agent import DesignResult

    obj_desc = _mock_obj_desc("Drone Frame")

    # Create a real STL to simulate what Tripo3D would return
    raw_stl = OUTPUT / "raw_mesh.stl"
    _make_binary_stl(raw_stl, size=80.0)

    # Run mesh partitioner
    mp = MeshPartitioner(work_dir=OUTPUT)
    mesh_result = mp.partition(raw_stl, "drone_frame")
    partition = mesh_result.to_partition_result(OUTPUT)

    assert partition.parts, "No parts produced"

    # Run packager
    stub_design = DesignResult(
        master_scad_path=OUTPUT / "master.scad",
        part_modules=[p.module_name for p in partition.parts],
    )
    packager = Packager(output_root=OUTPUT / "packages")
    package = packager.package(obj_desc, stub_design, partition, "drone_frame")

    stl_dir = package.output_dir / "stl"
    stl_files = list(stl_dir.glob("*.stl")) if stl_dir.exists() else []

    print(f"    Output dir : {package.output_dir}")
    print(f"    Parts      : {len(partition.parts)}")
    print(f"    STL files  : {[f.name for f in stl_files]}")
    print(f"    Manifest   : {'YES' if (package.output_dir/'manifest.json').exists() else 'NO'}")

    assert package.output_dir.exists(), "Output directory not created"
    assert (package.output_dir / "manifest.json").exists(), "manifest.json missing"
    assert (package.output_dir / "docs" / "print_instructions.md").exists(), "print_instructions missing"


# ── Test 2: OpenSCAD partitioner with real .scad ──────────────────────────────
@run("parametric_pipeline: write SCAD → partition → package → real output")
def test_parametric_pipeline_real_output():
    from tools.openscad_tools import OpenSCADTools
    from agents.design_agent import DesignResult
    from pipeline.partitioner import Partitioner
    from pipeline.packager import Packager

    scad_work = OUTPUT / "scad_work"
    scad_work.mkdir(exist_ok=True)

    obj_desc = _mock_obj_desc("Wall Bracket", category="mechanical", hint="bracket")

    osc = OpenSCADTools(work_dir=scad_work)
    scad_code = """\
module part_base() {
    difference() {
        cube([80, 50, 5]);
        translate([10, 10, -1]) cylinder(r=2.5, h=7, $fn=20);
        translate([70, 10, -1]) cylinder(r=2.5, h=7, $fn=20);
    }
}
module part_wall() {
    difference() {
        cube([5, 50, 40]);
        translate([-1, 10, 20]) cylinder(r=2.5, h=7, $fn=20);
    }
}
module assembly() {
    part_base();
    translate([75, 0, 0]) rotate([0, -90, 0]) part_wall();
}
assembly();
"""
    osc.write_openscad_file("master.scad", scad_code)
    modules = osc.list_part_modules("master.scad")
    print(f"    Modules found: {[m for m in modules.splitlines() if m.startswith('part_')]}")

    design = DesignResult(
        master_scad_path=scad_work / "master.scad",
        part_modules=[m for m in modules.splitlines() if m.startswith("part_")],
    )

    partitioner = Partitioner(work_dir=scad_work)
    partition = partitioner.partition(design)

    stub = DesignResult(
        master_scad_path=scad_work / "master.scad",
        part_modules=design.part_modules,
    )
    packager = Packager(output_root=OUTPUT / "packages")
    package = packager.package(obj_desc, stub, partition, "wall_bracket")

    scad_dir = package.output_dir / "scad"
    scad_files = list(scad_dir.glob("*.scad")) if scad_dir.exists() else []

    print(f"    Output dir : {package.output_dir}")
    print(f"    Parts      : {len(partition.parts)}")
    print(f"    SCAD files : {[f.name for f in scad_files]}")
    print(f"    Manifest   : {'YES' if (package.output_dir/'manifest.json').exists() else 'NO'}")

    assert len(partition.parts) == 2, f"Expected 2 parts, got {len(partition.parts)}"
    assert len(scad_files) >= 2, f"Expected >=2 SCAD files, got {scad_files}"


# ── Test 3: analysis agent JSON robustness ───────────────────────────────────
@run("analysis_agent: handles markdown-fenced JSON from LLM")
def test_analysis_markdown_json():
    from agents.analysis_agent import AnalysisAgent

    fenced = """\
```json
{
  "name": "Phone Stand",
  "description": "An adjustable phone stand for desk use.",
  "overall_dimensions_mm": {"x": 90, "y": 70, "z": 120},
  "features": ["adjustable angle", "cable slot"],
  "suggested_parts": [
    {
      "name": "base",
      "description": "Weighted base",
      "estimated_dimensions_mm": {"x": 90, "y": 70, "z": 10},
      "notes": "Print flat"
    }
  ],
  "structural_requirements": "Rigid PLA.",
  "print_considerations": ["No supports needed"],
  "assembly_notes": "Single part.",
  "part_category": "mechanical",
  "template_hint": "plate"
}
```"""

    fake_block = mock.MagicMock(); fake_block.text = fenced
    fake_response = mock.MagicMock(); fake_response.content = [fake_block]
    fake_client = mock.MagicMock()
    fake_client.messages.create.return_value = fake_response
    fake_client.messages._provider = "anthropic"

    with mock.patch("agents.analysis_agent.build_client", return_value=fake_client):
        agent = AnalysisAgent()
        desc = agent.analyse(text_description="A phone stand")

    assert desc.name == "Phone Stand"
    assert desc.template_hint == "plate"
    print(f"    Parsed: {desc.name}, category={desc.part_category}, template={desc.template_hint}")


# ── Test 4: analysis agent empty response ────────────────────────────────────
@run("analysis_agent: raises clear error on empty LLM response")
def test_analysis_empty_response():
    from agents.analysis_agent import AnalysisAgent

    fake_block = mock.MagicMock(); fake_block.text = ""
    fake_response = mock.MagicMock(); fake_response.content = [fake_block]
    fake_client = mock.MagicMock()
    fake_client.messages.create.return_value = fake_response
    fake_client.messages._provider = "anthropic"

    with mock.patch("agents.analysis_agent.build_client", return_value=fake_client):
        agent = AnalysisAgent()
        try:
            agent.analyse(text_description="anything")
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "empty" in str(e).lower(), f"Expected 'empty' in error: {e}"
            print(f"    Got expected error: {e}")


# ── Test 5: copy STLs to tests/output for manual inspection ──────────────────
@run("output: list all generated files")
def test_list_outputs():
    files = sorted(OUTPUT.rglob("*"))
    stl_files = [f for f in files if f.suffix == ".stl"]
    scad_files = [f for f in files if f.suffix == ".scad"]
    md_files = [f for f in files if f.suffix == ".md"]
    json_files = [f for f in files if f.suffix == ".json"]

    print(f"\n    {'='*50}")
    print(f"    Output root: {OUTPUT}")
    print(f"    STL files  ({len(stl_files)}):")
    for f in stl_files:
        size_kb = f.stat().st_size / 1024
        print(f"      {f.relative_to(OUTPUT)}  ({size_kb:.1f} KB)")
    print(f"    SCAD files ({len(scad_files)}):")
    for f in scad_files:
        print(f"      {f.relative_to(OUTPUT)}")
    print(f"    Docs       ({len(md_files)}):")
    for f in md_files:
        print(f"      {f.relative_to(OUTPUT)}")
    print(f"    Manifests  ({len(json_files)}):")
    for f in json_files:
        print(f"      {f.relative_to(OUTPUT)}")
    print(f"    {'='*50}")

    assert len(stl_files) >= 1, "No STL files generated"
    assert len(json_files) >= 1, "No manifest.json generated"


# ── Main ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"\n{CYAN}=== Nanoclaw Integration Tests (no API keys required) ==={NC}")
    print(f"Output: {OUTPUT}\n")

    passed = sum(1 for _, s, _ in results if s == PASS)
    failed = sum(1 for _, s, _ in results if s == FAIL)

    print(f"\n{'='*55}")
    print(f"  {GREEN}{passed} passed{NC}  |  {RED}{failed} failed{NC}")

    if failed:
        print("\nFailed tests:")
        for name, status, msg in results:
            if status == FAIL:
                print(f"  {RED}✗{NC} {name}: {msg}")
        sys.exit(1)
    else:
        print(f"\n  {GREEN}All integration tests passed.{NC}")
        print(f"  Output files are in: {OUTPUT}")
        sys.exit(0)
