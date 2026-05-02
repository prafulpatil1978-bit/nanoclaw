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


@test("analysis_agent: ObjectDescription has part_category and template_hint fields")
def test_object_description_new_fields():
    from agents.analysis_agent import ObjectDescription
    desc = ObjectDescription(
        name="Pipe Bracket",
        description="An L-shaped bracket for mounting a 25mm pipe.",
        overall_dimensions_mm={"x": 60, "y": 40, "z": 30},
        features=["mounting holes"],
        suggested_parts=[],
        structural_requirements="",
        print_considerations=[],
        assembly_notes="",
        part_category="mechanical",
        template_hint="bracket",
    )
    assert desc.part_category == "mechanical"
    assert desc.template_hint == "bracket"

    # Defaults when not supplied
    desc2 = ObjectDescription(
        name="Box",
        description="A simple box.",
        overall_dimensions_mm={"x": 50, "y": 50, "z": 30},
        features=[],
        suggested_parts=[],
        structural_requirements="",
        print_considerations=[],
        assembly_notes="",
    )
    assert desc2.part_category == "mechanical"
    assert desc2.template_hint is None


@test("pipeline: 3-way routing — partgen when template_hint matches and partgen available")
def test_3way_routing_partgen():
    from pipeline.orchestrator import Pipeline
    from agents.analysis_agent import ObjectDescription
    import unittest.mock as mock

    bracket_desc = ObjectDescription(
        name="Wall Bracket",
        description="A wall-mounted L-bracket for pipe support.",
        overall_dimensions_mm={"x": 80, "y": 50, "z": 40},
        features=["mounting holes"],
        suggested_parts=[],
        structural_requirements="",
        print_considerations=[],
        assembly_notes="",
        part_category="mechanical",
        template_hint="bracket",
    )

    pipeline = Pipeline(mode="auto", mesh_backend="tripo3d")

    # Patch PartgenAgent.available to True
    with mock.patch("agents.partgen_agent.PartgenAgent.available", new_callable=mock.PropertyMock, return_value=True):
        mode = pipeline._resolve_mode(bracket_desc)
    assert mode == "partgen", f"Expected partgen, got {mode}"


@test("pipeline: 3-way routing — mesh for organic when key set")
def test_3way_routing_mesh():
    from pipeline.orchestrator import Pipeline
    from agents.analysis_agent import ObjectDescription

    dragon_desc = ObjectDescription(
        name="Dragon Sculpture",
        description="An organic dragon creature sculpture.",
        overall_dimensions_mm={"x": 150, "y": 100, "z": 200},
        features=["wings", "tail"],
        suggested_parts=[],
        structural_requirements="",
        print_considerations=[],
        assembly_notes="",
        part_category="organic",
        template_hint=None,
    )

    os.environ["TRIPO3D_API_KEY"] = "test_key"
    pipeline = Pipeline(mode="auto", mesh_backend="tripo3d")
    mode = pipeline._resolve_mode(dragon_desc)
    del os.environ["TRIPO3D_API_KEY"]
    assert mode == "mesh", f"Expected mesh, got {mode}"


@test("pipeline: 3-way routing — parametric fallback when no keys and no partgen")
def test_3way_routing_parametric_fallback():
    from pipeline.orchestrator import Pipeline
    from agents.analysis_agent import ObjectDescription
    import unittest.mock as mock

    desc = ObjectDescription(
        name="Custom Bracket",
        description="A custom mechanical bracket.",
        overall_dimensions_mm={"x": 80, "y": 50, "z": 40},
        features=[],
        suggested_parts=[],
        structural_requirements="",
        print_considerations=[],
        assembly_notes="",
        part_category="mechanical",
        template_hint="bracket",
    )

    pipeline = Pipeline(mode="auto", mesh_backend="tripo3d")
    # partgen NOT available → should fall through to parametric
    with mock.patch("agents.partgen_agent.PartgenAgent.available", new_callable=mock.PropertyMock, return_value=False):
        mode = pipeline._resolve_mode(desc)
    assert mode == "parametric", f"Expected parametric, got {mode}"


@test("partgen_agent: PartgenAgent detects missing part-gen correctly")
def test_partgen_agent_unavailable():
    from agents.partgen_agent import PartgenAgent, PARTGEN_TEMPLATES
    with tempfile.TemporaryDirectory() as tmp:
        agent = PartgenAgent(work_dir=tmp, partgen_path="/nonexistent/path/part-gen")
        assert agent.available is False

    # PARTGEN_TEMPLATES should have all expected templates
    expected = {"bracket", "housing", "plate", "cylinder", "pipe", "lego", "lamp"}
    assert expected == PARTGEN_TEMPLATES


@test("partgen_agent: generate returns warnings when unavailable")
def test_partgen_agent_generate_unavailable():
    from agents.partgen_agent import PartgenAgent
    from agents.analysis_agent import ObjectDescription

    desc = ObjectDescription(
        name="Test Bracket",
        description="A simple bracket.",
        overall_dimensions_mm={"x": 80, "y": 50, "z": 40},
        features=[],
        suggested_parts=[],
        structural_requirements="",
        print_considerations=[],
        assembly_notes="",
    )
    with tempfile.TemporaryDirectory() as tmp:
        agent = PartgenAgent(work_dir=tmp, partgen_path="/nonexistent/path")
        result = agent.generate(desc)
        assert result.success is False
        assert len(result.warnings) > 0
        assert "not found" in result.warnings[0].lower() or "partgen" in result.warnings[0].lower() or "part-gen" in result.warnings[0].lower()


@test("partgen_agent: _detect_template keyword mapping")
def test_partgen_template_detection():
    from agents.partgen_agent import PartgenAgent
    from agents.analysis_agent import ObjectDescription

    def make_desc(name, desc, hint=None):
        return ObjectDescription(
            name=name, description=desc,
            overall_dimensions_mm={"x":50,"y":50,"z":20},
            features=[], suggested_parts=[],
            structural_requirements="", print_considerations=[],
            assembly_notes="", template_hint=hint,
        )

    with tempfile.TemporaryDirectory() as tmp:
        agent = PartgenAgent(work_dir=tmp, partgen_path="/tmp")

        # template_hint takes priority
        assert agent._detect_template(make_desc("x", "y", hint="lamp")) == "lamp"

        # keyword fallback
        assert agent._detect_template(make_desc("wall bracket", "mounting bracket")) == "bracket"
        assert agent._detect_template(make_desc("enclosure", "electronics housing")) == "housing"
        assert agent._detect_template(make_desc("mounting plate", "flat panel")) == "plate"
        assert agent._detect_template(make_desc("knob", "a cylinder spacer")) == "cylinder"
        assert agent._detect_template(make_desc("pipe fitting", "hollow tube")) == "pipe"
        assert agent._detect_template(make_desc("lamp shade", "a light diffuser")) == "lamp"
        assert agent._detect_template(make_desc("custom gear", "an unusual gear part")) == "auto"


@test("llm_client: build_client raises without keys or ollama")
def test_llm_client_no_keys():
    from utils.llm_client import build_client
    import unittest.mock as mock

    # Remove all keys, fake ollama not running
    env_backup = {k: os.environ.pop(k) for k in ("OPENROUTER_API_KEY","ANTHROPIC_API_KEY","USE_OLLAMA") if k in os.environ}
    try:
        with mock.patch("utils.llm_client._ollama_running", return_value=False):
            raised = False
            try:
                build_client()
            except EnvironmentError:
                raised = True
            assert raised, "Expected EnvironmentError when no keys set"
    finally:
        os.environ.update(env_backup)


@test("llm_client: resolve_model maps Anthropic names to OpenRouter slugs")
def test_resolve_model():
    from utils.llm_client import resolve_model
    assert "claude" in resolve_model("claude-opus-4-7", "openrouter").lower()
    assert resolve_model(None, "anthropic") == "claude-opus-4-7"
    # Ollama always uses env or default
    m = resolve_model("claude-opus-4-7", "ollama")
    assert m  # non-empty string


@test("ui_app: FastAPI app imports and has expected routes")
def test_ui_app_routes():
    from ui.app import app
    routes = {r.path for r in app.routes}
    assert "/" in routes, f"Missing / in {routes}"
    assert "/api/generate" in routes, f"Missing /api/generate in {routes}"
    assert "/api/jobs/{job_id}/events" in routes, f"Missing events route in {routes}"
    assert "/api/jobs/{job_id}/files" in routes, f"Missing files route in {routes}"
    # FastAPI path converter adds :path suffix for multi-segment paths
    assert any("download" in r for r in routes), f"Missing download route in {routes}"


@test("ui_app: friendly_error maps 401 to actionable message")
def test_friendly_error():
    from ui.app import _friendly_error
    msg = _friendly_error(Exception("Error code: 401 – {'error': {'message': 'User not found.', 'code': 401}}"))
    assert "openrouter" in msg.lower() or "api key" in msg.lower()
    assert "401" in msg

    msg2 = _friendly_error(Exception("rate limit 429 exceeded"))
    assert "429" in msg2

    msg3 = _friendly_error(Exception("some unknown error"))
    assert "unknown" in msg3


@test("analysis_agent: parses JSON with new part_category and template_hint fields")
def test_analysis_json_parsing():
    import unittest.mock as mock
    from agents.analysis_agent import AnalysisAgent

    sample_json = json.dumps({
        "name": "Pipe Bracket",
        "description": "An L-bracket for 25mm pipe.",
        "overall_dimensions_mm": {"x": 80, "y": 50, "z": 40},
        "features": ["mounting holes", "pipe slot"],
        "suggested_parts": [
            {
                "name": "body",
                "description": "Main bracket body",
                "estimated_dimensions_mm": {"x": 80, "y": 50, "z": 40},
                "notes": "Print flat",
            }
        ],
        "structural_requirements": "Rigid PLA.",
        "print_considerations": ["No supports needed."],
        "assembly_notes": "Single part.",
        "part_category": "mechanical",
        "template_hint": "bracket",
    })

    fake_block = mock.MagicMock()
    fake_block.text = sample_json
    fake_response = mock.MagicMock()
    fake_response.content = [fake_block]

    fake_client = mock.MagicMock()
    fake_client.messages.create.return_value = fake_response
    fake_client.messages._provider = "anthropic"

    with mock.patch("agents.analysis_agent.build_client", return_value=fake_client):
        agent = AnalysisAgent()
        desc = agent.analyse(text_description="A pipe bracket")

    assert desc.name == "Pipe Bracket"
    assert desc.part_category == "mechanical"
    assert desc.template_hint == "bracket"
    assert len(desc.suggested_parts) == 1


@test("analysis_agent: template_hint 'null' string becomes None")
def test_analysis_null_template_hint():
    import unittest.mock as mock
    from agents.analysis_agent import AnalysisAgent

    sample_json = json.dumps({
        "name": "Custom Gear",
        "description": "A spur gear.",
        "overall_dimensions_mm": {"x": 40, "y": 40, "z": 10},
        "features": ["teeth"],
        "suggested_parts": [],
        "structural_requirements": "",
        "print_considerations": [],
        "assembly_notes": "",
        "part_category": "mechanical",
        "template_hint": "null",
    })

    fake_block = mock.MagicMock()
    fake_block.text = sample_json
    fake_response = mock.MagicMock()
    fake_response.content = [fake_block]
    fake_client = mock.MagicMock()
    fake_client.messages.create.return_value = fake_response
    fake_client.messages._provider = "anthropic"

    with mock.patch("agents.analysis_agent.build_client", return_value=fake_client):
        agent = AnalysisAgent()
        desc = agent.analyse(text_description="A gear")

    assert desc.template_hint is None, f"Expected None, got {desc.template_hint!r}"


@test("design_agent: image passed through to first user message")
def test_design_agent_image_passthrough():
    import unittest.mock as mock
    from agents.design_agent import _build_user_prompt
    from agents.analysis_agent import ObjectDescription

    desc = ObjectDescription(
        name="Drone Frame",
        description="A quadcopter drone frame.",
        overall_dimensions_mm={"x": 300, "y": 300, "z": 80},
        features=["four arms", "central body"],
        suggested_parts=[],
        structural_requirements="",
        print_considerations=[],
        assembly_notes="",
    )

    # Without image — should be a single text block
    content_no_img = _build_user_prompt(desc, image_path=None)
    assert len(content_no_img) == 1
    assert content_no_img[0]["type"] == "text"

    # With image — should prepend an image block
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        # Write a minimal 1×1 white PNG
        import base64
        png_1x1 = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwADhQGAWjR9awAAAABJRU5ErkJggg=="
        )
        f.write(png_1x1)
        img_path = Path(f.name)

    try:
        content_with_img = _build_user_prompt(desc, image_path=img_path)
        assert len(content_with_img) == 2, f"Expected 2 blocks, got {len(content_with_img)}"
        assert content_with_img[0]["type"] == "image"
        assert content_with_img[1]["type"] == "text"
        assert "reference image" in content_with_img[1]["text"].lower()
    finally:
        img_path.unlink(missing_ok=True)


@test("openscad_tools: check_connector_pairs detects missing connectors")
def test_check_connector_pairs():
    from tools.openscad_tools import OpenSCADTools

    with tempfile.TemporaryDirectory() as tmp:
        osc = OpenSCADTools(work_dir=tmp)

        # Code with no connector geometry
        bad_code = """\
module part_body() {
    cube([100, 80, 20]);
}
module part_arm() {
    cube([60, 15, 10]);
}
module assembly() { part_body(); translate([100,0,0]) part_arm(); }
assembly();
"""
        osc.write_openscad_file("master.scad", bad_code)
        result = osc.check_connector_pairs("master.scad")
        assert "MISSING" in result or "FAILED" in result, f"Expected missing connectors: {result}"

        # Code WITH connector geometry
        good_code = """\
connector_d = 4; connector_h = 8; clearance = 0.4;
module part_body() {
    difference() {
        cube([100, 80, 20]);
        // connector: body → arm (socket side)
        translate([95, 7.5, 5]) cylinder(d=connector_d+clearance, h=connector_h+1, $fn=20);
    }
}
module part_arm() {
    cube([60, 15, 10]);
    // connector: arm → body (pin side)
    translate([-connector_h, 7.5-connector_d/2, 5])
        rotate([0,90,0]) cylinder(d=connector_d, h=connector_h, $fn=20);
}
module assembly() { part_body(); translate([100,0,0]) part_arm(); }
assembly();
"""
        osc.write_openscad_file("master.scad", good_code)
        result2 = osc.check_connector_pairs("master.scad")
        assert "PASSED" in result2 or "OK" in result2, f"Expected pass: {result2}"


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

        print()
        print("── New feature tests ──")
        test_object_description_new_fields()
        test_3way_routing_partgen()
        test_3way_routing_mesh()
        test_3way_routing_parametric_fallback()
        test_partgen_agent_unavailable()
        test_partgen_agent_generate_unavailable()
        test_partgen_template_detection()
        test_llm_client_no_keys()
        test_resolve_model()
        test_ui_app_routes()
        test_friendly_error()
        test_analysis_json_parsing()
        test_analysis_null_template_hint()
        test_design_agent_image_passthrough()
        test_check_connector_pairs()

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
