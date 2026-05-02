/* Nanoclaw UI — frontend logic */

'use strict';

// ── State ─────────────────────────────────────────────────────────────────────
let currentJobId = null;
let eventSource = null;
let viewer = null;
let selectedFile = null;   // tracks file from either drop or file-input click

// ── DOM refs ─────────────────────────────────────────────────────────────────
const dropZone       = document.getElementById('drop-zone');
const fileInput      = document.getElementById('file-input');
const preview        = document.getElementById('preview');
const descriptionEl  = document.getElementById('description');
const modeEl         = document.getElementById('mode');
const backendEl      = document.getElementById('backend');
const designModelEl  = document.getElementById('design-model');
const twoStageEl     = document.getElementById('two-stage');
const generateBtn    = document.getElementById('generate-btn');
const approvalBox    = document.getElementById('approval-box');
const approveBtn     = document.getElementById('approve-btn');
const rejectBtn      = document.getElementById('reject-btn');
const inputPanel     = document.getElementById('input-panel');
const progressPanel  = document.getElementById('progress-panel');
const resultsPanel   = document.getElementById('results-panel');
const logEl          = document.getElementById('log');
const fileListEl     = document.getElementById('file-list');
const newJobBtn      = document.getElementById('new-job-btn');
const viewerCanvas   = document.getElementById('viewer-canvas');
const viewerHint     = document.getElementById('viewer-hint');
const viewerControls = document.getElementById('viewer-controls');
const resetCameraBtn = document.getElementById('reset-camera-btn');

// ── Image drop / select ───────────────────────────────────────────────────────
dropZone.addEventListener('click', () => fileInput.click());

dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('dragover'); });
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
dropZone.addEventListener('drop', e => {
  e.preventDefault();
  dropZone.classList.remove('dragover');
  const file = e.dataTransfer.files[0];
  if (file) { selectedFile = file; showImagePreview(file); }
});

fileInput.addEventListener('change', () => {
  if (fileInput.files[0]) { selectedFile = fileInput.files[0]; showImagePreview(fileInput.files[0]); }
});

function showImagePreview(file) {
  const url = URL.createObjectURL(file);
  preview.src = url;
  preview.hidden = false;
  dropZone.querySelector('p').hidden = true;
}

// ── Generate ──────────────────────────────────────────────────────────────────
generateBtn.addEventListener('click', startJob);

async function startJob() {
  const description = descriptionEl.value.trim();
  const imageFile = selectedFile || fileInput.files[0];

  if (!description && !imageFile) {
    alert('Please enter a description or upload an image.');
    return;
  }

  generateBtn.disabled = true;

  const formData = new FormData();
  formData.append('description', description);
  formData.append('mode', modeEl.value);
  formData.append('backend', backendEl.value);
  formData.append('design_model', designModelEl.value);
  formData.append('two_stage', twoStageEl.checked ? 'true' : 'false');
  if (imageFile) formData.append('image', imageFile);

  try {
    const res = await fetch('/api/generate', { method: 'POST', body: formData });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || res.statusText);
    }
    const { job_id } = await res.json();
    currentJobId = job_id;
    showProgressPanel();
    connectSSE(job_id);
  } catch (err) {
    alert('Failed to start job: ' + err.message);
    generateBtn.disabled = false;
  }
}

// ── UI transitions ────────────────────────────────────────────────────────────
function showProgressPanel() {
  inputPanel.classList.add('hidden');
  progressPanel.classList.remove('hidden');
  resultsPanel.classList.add('hidden');
  logEl.innerHTML = '';
  resetPills();
}

function showResultsPanel() {
  progressPanel.classList.add('hidden');
  resultsPanel.classList.remove('hidden');
  loadFileList(currentJobId);
}

// ── SSE ───────────────────────────────────────────────────────────────────────
function connectSSE(jobId) {
  if (eventSource) eventSource.close();
  eventSource = new EventSource(`/api/jobs/${jobId}/events`);

  eventSource.onmessage = e => {
    try {
      const evt = JSON.parse(e.data);
      handleEvent(evt);
    } catch (_) {}
  };

  eventSource.onerror = () => {
    appendLog('error', 'Connection lost — check server.');
    eventSource.close();
  };
}

function handleEvent(evt) {
  const { stage, message } = evt;
  appendLog(stage, message);
  activatePill(stage);

  if (stage === 'approval') {
    // Two-stage: show approval box and load SCAD files for preview
    approvalBox.classList.remove('hidden');
    loadFileList(currentJobId);
  }
  if (stage === 'done') {
    markPillDone('done');
    eventSource && eventSource.close();
    approvalBox.classList.add('hidden');
    showResultsPanel();
  }
  if (stage === 'error') {
    markPillError();
    eventSource && eventSource.close();
  }
}

// ── Approval flow ─────────────────────────────────────────────────────────────
approveBtn.addEventListener('click', async () => {
  approveBtn.disabled = true;
  approveBtn.textContent = 'Rendering…';
  try {
    const res = await fetch(`/api/jobs/${currentJobId}/approve-render`, { method: 'POST' });
    if (!res.ok) throw new Error((await res.json()).detail);
    approvalBox.classList.add('hidden');
  } catch (err) {
    appendLog('error', 'Approve failed: ' + err.message);
    approveBtn.disabled = false;
    approveBtn.textContent = 'Approve & Render STL';
  }
});

rejectBtn.addEventListener('click', () => {
  approvalBox.classList.add('hidden');
  if (eventSource) { eventSource.close(); eventSource = null; }
  newJobBtn.click();
});

// ── Log helpers ───────────────────────────────────────────────────────────────
function appendLog(stage, message) {
  const line = document.createElement('div');
  line.className = `log-line ${stage}`;
  const ts = new Date().toLocaleTimeString();
  line.textContent = `[${ts}] [${stage.toUpperCase()}] ${message}`;
  logEl.appendChild(line);
  logEl.scrollTop = logEl.scrollHeight;
}

// ── Stage pills ───────────────────────────────────────────────────────────────
const STAGE_ORDER = ['analyse', 'mode', 'design', 'partition', 'package', 'done'];
let activeStageSeen = new Set();

function resetPills() {
  activeStageSeen.clear();
  document.querySelectorAll('.pill').forEach(p => {
    p.classList.remove('active', 'done', 'error');
  });
}

function activatePill(stage) {
  if (activeStageSeen.has(stage)) return;
  activeStageSeen.add(stage);

  // Mark previous stages as done
  const idx = STAGE_ORDER.indexOf(stage);
  STAGE_ORDER.slice(0, idx).forEach(s => {
    const el = document.querySelector(`.pill[data-stage="${s}"]`);
    if (el && !el.classList.contains('error')) { el.classList.remove('active'); el.classList.add('done'); }
  });

  const el = document.querySelector(`.pill[data-stage="${stage}"]`);
  if (el) { el.classList.add('active'); el.classList.remove('done'); }
}

function markPillDone(stage) {
  STAGE_ORDER.forEach(s => {
    const el = document.querySelector(`.pill[data-stage="${s}"]`);
    if (el) { el.classList.remove('active'); el.classList.add('done'); }
  });
}

function markPillError() {
  document.querySelectorAll('.pill.active').forEach(p => {
    p.classList.remove('active'); p.classList.add('error');
  });
}

// ── File list ─────────────────────────────────────────────────────────────────
async function loadFileList(jobId) {
  fileListEl.innerHTML = '<p class="loading-text">Loading files…</p>';
  try {
    const res = await fetch(`/api/jobs/${jobId}/files`);
    const { files } = await res.json();
    renderFileList(files, jobId);
  } catch (err) {
    fileListEl.innerHTML = `<p class="loading-text" style="color:var(--error)">Error: ${err.message}</p>`;
  }
}

function renderFileList(files, jobId) {
  if (!files.length) {
    fileListEl.innerHTML = '<p class="loading-text">No output files found.</p>';
    return;
  }
  fileListEl.innerHTML = '';

  files.forEach(f => {
    const isStl = f.name.toLowerCase().endsWith('.stl');
    const icon = isStl ? '🧩' : f.name.endsWith('.scad') ? '📐' : f.name.endsWith('.step') ? '⚙️' : f.name.endsWith('.md') ? '📄' : '📦';

    const item = document.createElement('div');
    item.className = 'file-item';

    item.innerHTML = `
      <span class="file-icon">${icon}</span>
      <div class="file-info">
        <div class="file-name" title="${f.name}">${f.name}</div>
        <div class="file-size">${f.size_kb} KB</div>
      </div>
      <div class="file-actions">
        ${isStl ? `<button class="preview-btn" data-url="${f.url}">Preview</button>` : ''}
        <a href="${f.url}" download="${f.name}">↓</a>
      </div>
    `;

    fileListEl.appendChild(item);

    if (isStl) {
      item.querySelector('.preview-btn').addEventListener('click', () => {
        document.querySelectorAll('.file-item').forEach(i => i.classList.remove('active'));
        item.classList.add('active');
        loadSTL(f.url);
      });
      // Auto-preview first STL
      if (!fileListEl._firstStlPreviewed) {
        fileListEl._firstStlPreviewed = true;
        loadSTL(f.url);
        item.classList.add('active');
      }
    }
  });
}

// ── New job ───────────────────────────────────────────────────────────────────
newJobBtn.addEventListener('click', () => {
  currentJobId = null;
  selectedFile = null;
  if (eventSource) { eventSource.close(); eventSource = null; }
  if (viewer) { viewer.dispose(); viewer = null; }
  // Reset file input
  fileInput.value = '';
  preview.hidden = true;
  preview.src = '';
  dropZone.querySelector('p').hidden = false;
  descriptionEl.value = '';
  generateBtn.disabled = false;
  approvalBox.classList.add('hidden');
  approveBtn.disabled = false;
  approveBtn.textContent = 'Approve & Render STL';
  resultsPanel.classList.add('hidden');
  inputPanel.classList.remove('hidden');
  fileListEl._firstStlPreviewed = false;
});

resetCameraBtn.addEventListener('click', () => { if (viewer) viewer.resetCamera(); });

// ── Three.js STL Viewer ────────────────────────────────────────────────────────
class STLViewer {
  constructor(canvas) {
    const w = canvas.clientWidth || 500;
    const h = canvas.clientHeight || 375;

    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    this.renderer.setPixelRatio(window.devicePixelRatio);
    this.renderer.setSize(w, h);
    this.renderer.setClearColor(0x0b0d14, 1);

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(45, w / h, 0.1, 5000);

    // Lights
    const ambient = new THREE.AmbientLight(0xffffff, 0.5);
    const dir = new THREE.DirectionalLight(0xffffff, 1.0);
    dir.position.set(1, 2, 3);
    this.scene.add(ambient, dir);

    // Grid
    const grid = new THREE.GridHelper(200, 20, 0x2e3147, 0x1a1d27);
    this.scene.add(grid);

    this.mesh = null;
    this._animate = this._animate.bind(this);
    this._raf = requestAnimationFrame(this._animate);

    // Orbit via mouse drag
    this._drag = false;
    this._lastMouse = { x: 0, y: 0 };
    this._spherical = { theta: Math.PI / 4, phi: Math.PI / 3, r: 200 };

    canvas.addEventListener('mousedown', e => { this._drag = true; this._lastMouse = { x: e.clientX, y: e.clientY }; });
    canvas.addEventListener('mouseup', () => { this._drag = false; });
    canvas.addEventListener('mousemove', e => {
      if (!this._drag) return;
      const dx = e.clientX - this._lastMouse.x;
      const dy = e.clientY - this._lastMouse.y;
      this._spherical.theta -= dx * 0.01;
      this._spherical.phi = Math.max(0.1, Math.min(Math.PI - 0.1, this._spherical.phi - dy * 0.01));
      this._lastMouse = { x: e.clientX, y: e.clientY };
    });
    canvas.addEventListener('wheel', e => {
      this._spherical.r = Math.max(20, this._spherical.r + e.deltaY * 0.3);
    }, { passive: true });

    // Touch support
    canvas.addEventListener('touchstart', e => {
      if (e.touches.length === 1) { this._drag = true; this._lastMouse = { x: e.touches[0].clientX, y: e.touches[0].clientY }; }
    });
    canvas.addEventListener('touchend', () => { this._drag = false; });
    canvas.addEventListener('touchmove', e => {
      if (!this._drag || e.touches.length !== 1) return;
      const dx = e.touches[0].clientX - this._lastMouse.x;
      const dy = e.touches[0].clientY - this._lastMouse.y;
      this._spherical.theta -= dx * 0.01;
      this._spherical.phi = Math.max(0.1, Math.min(Math.PI - 0.1, this._spherical.phi - dy * 0.01));
      this._lastMouse = { x: e.touches[0].clientX, y: e.touches[0].clientY };
    });
  }

  loadGeometry(geometry) {
    if (this.mesh) { this.scene.remove(this.mesh); this.mesh.geometry.dispose(); }

    geometry.computeBoundingBox();
    geometry.computeVertexNormals();
    const bb = geometry.boundingBox;
    const size = new THREE.Vector3();
    bb.getSize(size);
    const center = new THREE.Vector3();
    bb.getCenter(center);

    // Centre at origin
    geometry.translate(-center.x, -center.y, -center.z);

    const maxDim = Math.max(size.x, size.y, size.z);
    this._spherical.r = maxDim * 2.2;

    const mat = new THREE.MeshPhongMaterial({ color: 0x6c63ff, specular: 0x444466, shininess: 40 });
    this.mesh = new THREE.Mesh(geometry, mat);
    this.scene.add(this.mesh);
    this.resetCamera();
  }

  resetCamera() {
    const { theta, phi, r } = this._spherical;
    this.camera.position.set(
      r * Math.sin(phi) * Math.sin(theta),
      r * Math.cos(phi),
      r * Math.sin(phi) * Math.cos(theta),
    );
    this.camera.lookAt(0, 0, 0);
  }

  _animate() {
    this._raf = requestAnimationFrame(this._animate);
    if (this._drag || this._lastRot !== this._spherical.theta + this._spherical.phi) {
      this._lastRot = this._spherical.theta + this._spherical.phi;
      this.resetCamera();
    }
    this.renderer.render(this.scene, this.camera);
  }

  dispose() {
    cancelAnimationFrame(this._raf);
    this.renderer.dispose();
  }
}

function loadSTL(url) {
  viewerHint.hidden = true;
  viewerControls.classList.remove('hidden');

  if (!viewer) {
    viewer = new STLViewer(viewerCanvas);
    window.addEventListener('resize', () => {
      const c = viewerCanvas;
      viewer.renderer.setSize(c.clientWidth, c.clientHeight);
      viewer.camera.aspect = c.clientWidth / c.clientHeight;
      viewer.camera.updateProjectionMatrix();
    });
  }

  const loader = new THREE.STLLoader();
  loader.load(url, geo => viewer.loadGeometry(geo));
}
