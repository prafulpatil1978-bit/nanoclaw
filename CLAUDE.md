# Nanoclaw — Project Reference for AI Sessions

## What This Is
Nanoclaw is an AI-powered pipeline that converts a photo, sketch, or text description
into a print-ready 3D model (STL files). It runs as a local web UI on port 7860.

**User's machine:** Mac mini, project at `~/Desktop/n8n-setup/nanoclaw`
**Python venv:** `~/Desktop/n8n-setup/nanoclaw/.venv`
**Branch:** `claude/ai-3d-printing-workflow-v6OD6`
**Repo:** `prafulpatil1978-bit/nanoclaw`

---

## How to Start the Server (Mac)

```bash
cd ~/Desktop/n8n-setup/nanoclaw
.venv/bin/python3 main.py serve
# Then open http://localhost:7860
```

Or double-click `Nanoclaw.command` on the Desktop (starts server + opens browser).

---

## Project Structure

```
nanoclaw/
├── main.py                      # CLI entry point (serve / generate commands)
├── .env                         # API keys (OPENROUTER_API_KEY, etc.)
├── .env.example                 # Documented template for all config options
├── create_desktop_app.sh        # Creates Nanoclaw.app on Mac Desktop (one-time)
├── agents/
│   ├── analysis_agent.py        # Stage 1: image/text → structured ObjectDescription (JSON)
│   ├── design_agent.py          # Stage 2: ObjectDescription → OpenSCAD code (agentic loop)
│   ├── mesh_agent.py            # Stage 2 alt: AI mesh generation (Tripo3D / Meshy / Shap-E)
│   └── partgen_agent.py         # Stage 2 alt: CadQuery template engine
├── pipeline/
│   ├── orchestrator.py          # Ties all stages together, mode selection logic
│   ├── partitioner.py           # Splits master.scad into per-part SCAD files
│   ├── mesh_partitioner.py      # Splits large meshes to fit print bed
│   └── packager.py              # Assembles final output directory
├── tools/
│   └── openscad_tools.py        # Tool schemas + dispatch for design agent
│       # Tools: write_openscad_file, validate_openscad_file, list_part_modules,
│       #        check_connector_pairs (mandatory), render_part_to_stl
├── utils/
│   ├── llm_client.py            # Unified LLM client (OpenRouter / Anthropic / Ollama)
│   └── file_utils.py            # Image loading, path helpers
├── ui/
│   ├── app.py                   # FastAPI web server + SSE job streaming
│   └── static/
│       ├── index.html           # Single-page UI
│       ├── style.css            # Dark theme
│       └── app.js               # Frontend logic (SSE, STL viewer, approval flow)
└── tests/
    ├── test_pipeline.py         # 29 unit tests (run: python tests/test_pipeline.py)
    └── test_integration_stl.py  # Integration tests (real STL generation, no API needed)
```

---

## Pipeline Modes

| Mode | When used | Technology |
|---|---|---|
| `partgen` | Standard shapes (bracket, housing, etc.) | CadQuery templates |
| `parametric` | Custom mechanical parts | OpenSCAD (agentic LLM loop) |
| `mesh` | Organic / decorative objects | Tripo3D / Meshy / Shap-E |
| `auto` | Default — pipeline decides | All of the above |

---

## LLM / Model Configuration

### Provider priority (set in `.env`)
1. `OPENROUTER_API_KEY` — recommended (access to all models, BYOK support)
2. `ANTHROPIC_API_KEY` — direct Anthropic
3. `USE_OLLAMA=1` — free local models (requires `ollama serve`)

### Smart model routing (auto mode — default)
The system scores each job's complexity and picks the cheapest capable model:

| Stage | Condition | Model | Approx cost |
|---|---|---|---|
| Analysis | text-only input | Haiku (`claude-3-5-haiku`) | ~$0.001 |
| Analysis | image input | Sonnet (`claude-sonnet-4-5`) | ~$0.005 |
| Design | low (≤2 parts) | Haiku | ~$0.01 |
| Design | medium (3–5 parts) | Sonnet | ~$0.09 |
| Design | high (6+ parts) | Sonnet | ~$0.09 |

**Haiku via OpenRouter routes to Amazon Bedrock which does NOT support vision.**
This is why analysis with an image always uses Sonnet.

### Ollama fallback
If the primary cloud provider returns a rate-limit or network error, the client
automatically retries via Ollama (if `ollama serve` is running locally). Auth
errors (wrong API key) bypass the fallback and surface immediately.

### OpenRouter BYOK (Bring Your Own Key)
To use your own Anthropic/Perplexity key through OpenRouter:
openrouter.ai → Settings → Integrations → Add provider key

### Per-agent overrides (in `.env`)
```
ANALYSIS_MODEL=claude-sonnet-4-6   # override analysis model
DESIGN_MODEL=claude-sonnet-4-6     # override design model
OPENROUTER_MODEL=anthropic/claude-sonnet-4-5  # force a specific model globally
```

---

## Key Design Decisions & Past Fixes

### Design fidelity
- The reference image is passed directly to the design agent (not just analysis)
  so OpenSCAD generation matches the visual input.
- `check_connector_pairs` tool is mandatory — blocks rendering if any part
  lacks explicit pin+socket connector geometry.

### Token cost reduction
- History compression: old `write_openscad_file` payloads in conversation history
  are truncated to 300 chars (keeps only the most recent full version).
- Two-stage mode: generate SCAD → pause for user approval → render STL.
  Enable via the "Two-stage" checkbox in the UI.

### JSON robustness
- Analysis agent strips markdown code fences (```json```) that some models add.
- Empty response detection with actionable error message.

### OpenRouter model IDs (correct format)
```
anthropic/claude-3-5-haiku     # NOT claude-haiku-3-5
anthropic/claude-sonnet-4-5    # Sonnet
anthropic/claude-opus-4-5      # Opus
```

---

## UI Features

- **Drag-and-drop image** or text description input
- **Pipeline mode** selector (Auto / Part-gen / Parametric / Mesh)
- **Mesh backend** selector (Tripo3D / Shap-E / Meshy)
- **Design model** dropdown: Auto (complexity-based) / Sonnet / Haiku / Opus
- **Two-stage toggle**: review SCAD before rendering STL
- **Live progress** via Server-Sent Events with stage pills
- **Approval flow**: approve SCAD → render STL, or discard and start over
- **3D STL viewer** (Three.js, mouse drag to rotate, scroll to zoom)
- **File download** list for all output files

---

## Running Tests

```bash
cd ~/Desktop/n8n-setup/nanoclaw
python tests/test_pipeline.py          # all 29 offline tests
python tests/test_pipeline.py --full   # includes live API tests (needs keys)
```

---

## Common Commands

```bash
# Pull latest changes
cd ~/Desktop/n8n-setup/nanoclaw && git pull origin claude/ai-3d-printing-workflow-v6OD6

# Start server
.venv/bin/python3 main.py serve

# Run tests
python tests/test_pipeline.py

# Recreate Desktop launcher (if needed)
bash create_desktop_app.sh
```

---

## Known Issues / Gotchas

- **Pasting commands from chat**: the chat UI renders filenames like `main.py` as
  hyperlinks. If you copy-paste a command, check it doesn't contain
  `[main.py](http://main.py)` — replace with just `main.py`.
- **Desktop app `.app` bundle**: macOS TCC blocks `.app` bundles from accessing
  `~/Desktop`. Use `Nanoclaw.command` (double-click opens Terminal) instead.
- **OpenRouter → Bedrock routing**: Haiku gets routed to Amazon Bedrock which
  has no vision support. Fixed: image analysis always uses Sonnet.
- **Server must be running**: keep the Terminal window with `main.py serve` open
  (or minimize it). Closing Terminal stops the server.
