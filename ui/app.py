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
_jobs: dict[str, dict] = {}   # job_id → {status, progress_events, result_dir, error}
_queues: dict[str, asyncio.Queue] = {}   # job_id → SSE queue


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


@app.post("/api/generate")
async def generate(
    description: str = Form(""),
    mode: str = Form("auto"),
    backend: str = Form("tripo3d"),
    image: UploadFile | None = File(None),
):
    """Start a pipeline job. Returns {job_id}."""
    if not description and (image is None or image.filename == ""):
        raise HTTPException(400, "Provide a description or upload an image.")

    job_id = str(uuid.uuid4())
    _jobs[job_id] = {"status": "queued", "progress": [], "result_dir": None, "error": None}
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
        _run_pipeline_async(job_id, description or None, image_path, mode, backend)
    )

    return {"job_id": job_id}


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
) -> None:
    _jobs[job_id]["status"] = "running"
    loop = asyncio.get_event_loop()
    try:
        result_dir = await loop.run_in_executor(
            None,
            _run_pipeline_sync,
            job_id, description, image_path, mode, backend,
        )
        _jobs[job_id]["status"] = "done"
        _jobs[job_id]["result_dir"] = result_dir
        _emit(job_id, "done", f"Package ready — {result_dir.name}")
    except Exception as exc:
        _jobs[job_id]["status"] = "error"
        _jobs[job_id]["error"] = str(exc)
        _emit(job_id, "error", str(exc))


def _run_pipeline_sync(
    job_id: str,
    description: str | None,
    image_path: Path | None,
    mode: str,
    backend: str,
) -> Path:
    from dotenv import load_dotenv
    load_dotenv()

    from pipeline.orchestrator import Pipeline

    def cb(stage: str, message: str) -> None:
        _emit(job_id, stage, message)

    output_root = Path(os.environ.get("OUTPUT_DIR", "./output"))
    pipeline = Pipeline(
        output_root=output_root,
        mode=mode,           # type: ignore[arg-type]
        mesh_backend=backend, # type: ignore[arg-type]
    )
    result = pipeline.run(
        text_description=description,
        image_path=image_path,
        progress_callback=cb,
    )
    return result.package.output_dir


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("UI_PORT", 7860))
    uvicorn.run("ui.app:app", host="0.0.0.0", port=port, reload=False)
