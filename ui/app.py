"""FastAPI localhost UI for Nanoclaw — 3D printing pipeline.

Start with:  python -m ui.app   (or via main.py serve command)
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Nanoclaw 3D Print Pipeline", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Static files ─────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent
_STATIC = _HERE / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

# ── Job store (in-memory, per process) ───────────────────────────────────────
_jobs: dict[str, dict] = {}   # job_id → {status, progress, result_dir, error, scad_dir}
_queues: dict[str, asyncio.Queue] = {}   # job_id → SSE queue

# Model cost table ($ per 1M tokens, OpenRouter)
MODEL_COSTS = {
    "claude-sonnet-4-6": {"label": "Sonnet 4.5 (recommended)",  "in": 3.00,  "out": 15.00},
    "claude-haiku-4-5":  {"label": "Haiku 3.5 (cheapest)",      "in": 0.80,  "out":  4.00},
    "claude-opus-4-7":   {"label": "Opus 4.5 (best quality)",   "in": 15.00, "out": 75.00},
    "ollama":            {"label": "Ollama local (free)",        "in": 0,     "out":  0},
}

def _estimate_cost(model: str, iterations: int = 10) -> str:
    c = MODEL_COSTS.get(model, MODEL_COSTS["claude-sonnet-4-6"])
    if c["in"] == 0:
        return "Free (local)"
    inp_tok  = iterations * 8_000   # rough: growing history
    out_tok  = iterations * 2_500
    cost = (inp_tok * c["in"] + out_tok * c["out"]) / 1_000_000
    return f"~${cost:.2f}"


def _emit(job_id: str, stage: str, message: str) -> None:
    """Push a progress event to the SSE queue."""
    evt = {"stage": stage, "message": message}
    _jobs[job_id]["progress"].append(evt)
    q = _queues.get(job_id)
    if q:
        try:
            q.put_nowait(evt)
        except asyncio.QueueFull:
            pass


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse(str(_STATIC / "index.html"))


@app.get("/api/models")
async def list_models():
    """Return available models with cost estimates."""
    return {
        model: {**info, "cost_per_run": _estimate_cost(model)}
        for model, info in MODEL_COSTS.items()
    }


@app.post("/api/generate")
async def generate(
    description: str = Form(""),
    mode: str = Form("auto"),
    backend: str = Form("tripo3d"),
    design_model: str = Form("claude-sonnet-4-6"),
    two_stage: str = Form("false"),   # "true" → stop after SCAD, wait for approval
    image: UploadFile | None = File(None),
):
    """Start a pipeline job. Returns {job_id}."""
    if not description and (image is None or image.filename == ""):
        raise HTTPException(400, "Provide a description or upload an image.")

    job_id = str(uuid.uuid4())
    _jobs[job_id] = {
        "status": "queued", "progress": [], "result_dir": None,
        "error": None, "scad_dir": None, "meta": {
            "mode": mode, "backend": backend, "design_model": design_model,
            "two_stage": two_stage == "true",
        }
    }
    _queues[job_id] = asyncio.Queue(maxsize=256)

    # Save uploaded image
    image_path: Path | None = None
    if image and image.filename:
        upload_dir = Path("./output/_uploads")
        upload_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(image.filename).suffix or ".jpg"
        image_path = upload_dir / f"{job_id}{suffix}"
        content = await image.read()
        image_path.write_bytes(content)

    # Run pipeline in a background thread (it's blocking / sync)
    asyncio.create_task(
        _run_pipeline_async(
            job_id, description or None, image_path, mode, backend,
            design_model=design_model,
            two_stage=(two_stage == "true"),
        )
    )

    return {"job_id": job_id}


@app.post("/api/jobs/{job_id}/approve-render")
async def approve_render(job_id: str):
    """Stage-2 trigger: approve the SCAD design and start STL rendering."""
    if job_id not in _jobs:
        raise HTTPException(404, "Job not found")
    j = _jobs[job_id]
    if j["status"] != "awaiting_approval":
        raise HTTPException(400, f"Job is in status '{j['status']}', not awaiting_approval")

    j["status"] = "rendering"
    _emit(job_id, "design", "Approval received — starting STL rendering…")

    loop = asyncio.get_event_loop()
    asyncio.create_task(
        _run_render_stage_async(job_id)
    )
    return {"ok": True}


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str):
    """SSE stream for real-time progress."""
    if job_id not in _jobs:
        raise HTTPException(404, "Job not found")

    async def event_stream() -> AsyncGenerator[str, None]:
        # Replay already-arrived events
        for evt in _jobs[job_id]["progress"]:
            yield _sse(evt)

        q = _queues.get(job_id)
        if q is None:
            return

        while True:
            try:
                evt = await asyncio.wait_for(q.get(), timeout=30.0)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue

            yield _sse(evt)

            if evt.get("stage") in ("done", "error"):
                break

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/api/jobs/{job_id}/status")
async def job_status(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(404, "Job not found")
    j = _jobs[job_id]
    return {
        "status": j["status"],
        "result_dir": str(j["result_dir"]) if j["result_dir"] else None,
        "error": j["error"],
    }


@app.get("/api/jobs/{job_id}/files")
async def job_files(job_id: str):
    """List downloadable files produced by the job."""
    if job_id not in _jobs:
        raise HTTPException(404, "Job not found")
    result_dir: Path | None = _jobs[job_id]["result_dir"]
    if not result_dir or not result_dir.exists():
        return {"files": []}

    files = []
    for f in sorted(result_dir.rglob("*")):
        if f.is_file():
            files.append({
                "name": f.name,
                "path": str(f.relative_to(result_dir)),
                "size_kb": round(f.stat().st_size / 1024, 1),
                "url": f"/api/jobs/{job_id}/download/{f.relative_to(result_dir)}",
            })
    return {"files": files}


@app.get("/api/jobs/{job_id}/download/{file_path:path}")
async def download_file(job_id: str, file_path: str):
    if job_id not in _jobs:
        raise HTTPException(404, "Job not found")
    result_dir: Path | None = _jobs[job_id]["result_dir"]
    if not result_dir:
        raise HTTPException(404, "No output yet")
    target = (result_dir / file_path).resolve()
    if not str(target).startswith(str(result_dir.resolve())):
        raise HTTPException(403, "Access denied")
    if not target.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(str(target), filename=target.name)


# ── Pipeline runner (async wrapper around sync code) ─────────────────────────

async def _run_pipeline_async(
    job_id: str,
    description: str | None,
    image_path: Path | None,
    mode: str,
    backend: str,
    design_model: str = "claude-sonnet-4-6",
    two_stage: bool = False,
) -> None:
    _jobs[job_id]["status"] = "running"
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None, _run_pipeline_sync,
            job_id, description, image_path, mode, backend, design_model, two_stage,
        )
        if two_stage and isinstance(result, dict) and result.get("awaiting_approval"):
            _jobs[job_id]["status"] = "awaiting_approval"
            _jobs[job_id]["scad_dir"] = result["scad_dir"]
            _emit(job_id, "approval", (
                "Design draft ready — review the SCAD files below, then click "
                "'Approve & Render STL' to continue."
            ))
        else:
            _jobs[job_id]["status"] = "done"
            _jobs[job_id]["result_dir"] = result
            _emit(job_id, "done", f"Package ready — {Path(result).name}")
    except Exception as exc:
        msg = _friendly_error(exc)
        _jobs[job_id]["status"] = "error"
        _jobs[job_id]["error"] = msg
        _emit(job_id, "error", msg)


async def _run_render_stage_async(job_id: str) -> None:
    loop = asyncio.get_event_loop()
    try:
        result_dir = await loop.run_in_executor(None, _run_render_stage_sync, job_id)
        _jobs[job_id]["status"] = "done"
        _jobs[job_id]["result_dir"] = result_dir
        _emit(job_id, "done", f"STL rendering complete — {result_dir.name}")
    except Exception as exc:
        msg = _friendly_error(exc)
        _jobs[job_id]["status"] = "error"
        _jobs[job_id]["error"] = msg
        _emit(job_id, "error", msg)


def _run_render_stage_sync(job_id: str) -> Path:
    """Second stage: render SCAD files that were already written."""
    from dotenv import load_dotenv
    load_dotenv()
    from tools.openscad_tools import OpenSCADTools
    from pipeline.partitioner import Partitioner, PartitionResult

    scad_dir = _jobs[job_id].get("scad_dir")
    if not scad_dir or not Path(scad_dir).exists():
        raise RuntimeError("No SCAD directory from stage 1 — cannot render")

    _emit(job_id, "partition", "Rendering SCAD parts to STL…")
    osc = OpenSCADTools(work_dir=scad_dir)
    master = Path(scad_dir) / "master.scad"
    if not master.exists():
        raise RuntimeError(f"master.scad not found in {scad_dir}")

    modules_out = osc.list_part_modules("master.scad")
    modules = [m for m in modules_out.splitlines() if m.startswith("part_")]
    stl_paths = []
    for mod in modules:
        stl_name = f"{mod}.stl"
        out = osc.render_part_to_stl("master.scad", mod, stl_name)
        _emit(job_id, "partition", f"{mod}: {out[:80]}")
        stl_p = Path(scad_dir) / stl_name
        if stl_p.exists():
            stl_paths.append(stl_p)

    # Return scad_dir as result so file listing works
    _jobs[job_id]["result_dir"] = Path(scad_dir)
    return Path(scad_dir)


def _run_pipeline_sync(
    job_id: str,
    description: str | None,
    image_path: Path | None,
    mode: str,
    backend: str,
    design_model: str = "claude-sonnet-4-6",
    two_stage: bool = False,
):
    from dotenv import load_dotenv
    load_dotenv()

    from pipeline.orchestrator import Pipeline

    def cb(stage: str, message: str) -> None:
        _emit(job_id, stage, message)

    output_root = Path(os.environ.get("OUTPUT_DIR", "./output"))
    pipeline = Pipeline(
        output_root=output_root,
        mode=mode,                 # type: ignore[arg-type]
        mesh_backend=backend,      # type: ignore[arg-type]
        design_model=design_model,
        two_stage=two_stage,
    )
    result = pipeline.run(
        text_description=description,
        image_path=image_path,
        progress_callback=cb,
    )
    if two_stage and hasattr(result, "scad_only") and result.scad_only:
        return {"awaiting_approval": True, "scad_dir": str(result.scad_dir)}
    return result.package.output_dir


def _friendly_error(exc: Exception) -> str:
    msg = str(exc)
    if "401" in msg or "User not found" in msg or "authentication" in msg.lower():
        return (
            "API key rejected (401 Unauthorized). "
            "Open .env in the nanoclaw folder and check OPENROUTER_API_KEY — "
            "it should start with sk-or-v1-. Get yours at openrouter.ai → Settings → API Keys."
        )
    if "403" in msg:
        return "Access denied (403). Check that your API key has credits at openrouter.ai."
    if "429" in msg:
        return "Rate limit hit (429). Wait a moment and try again."
    if "TRIPO3D" in msg or "tripo" in msg.lower():
        return f"Tripo3D error: {msg}. Check TRIPO3D_API_KEY in .env."
    return msg


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("UI_PORT", 7860))
    uvicorn.run("ui.app:app", host="0.0.0.0", port=port, reload=False)
