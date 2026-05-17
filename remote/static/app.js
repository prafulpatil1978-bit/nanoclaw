'use strict';

// ── State ──────────────────────────────────────────────────────────────
let ws = null;
let token = sessionStorage.getItem('ncr_token') || '';
let frameW = 0, frameH = 0;
let displayMode = 'fit';
let lastMoveMs = 0;
let cursorNX = 0.5, cursorNY = 0.5;  // normalised cursor position (0-1)
const THROTTLE_MS = 16;

// ── Elements ───────────────────────────────────────────────────────────
const loginPage    = document.getElementById('login-page');
const deskPage     = document.getElementById('desktop-page');
const loginForm    = document.getElementById('login-form');
const loginBtn     = document.getElementById('login-btn');
const errMsg       = document.getElementById('error-msg');
const canvas       = document.getElementById('screen');
const cursorCanvas = document.getElementById('cursor');
const statusDot    = document.getElementById('status-dot');
const kbPanel      = document.getElementById('kb-panel');
const kbInput      = document.getElementById('kb-input');
const ctx          = canvas.getContext('2d');
const cursorCtx    = cursorCanvas.getContext('2d');

// ── Auto-connect if we have a saved token ──────────────────────────────
if (token) connectWS();

// ── Login ──────────────────────────────────────────────────────────────
loginForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const password = document.getElementById('password').value;
  const totpCode = document.getElementById('totp').value.replace(/\s/g, '');
  if (!password || totpCode.length < 6) return;

  loginBtn.disabled = true;
  loginBtn.textContent = 'Connecting…';
  hideError();

  try {
    const res  = await fetch('/api/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({password, totp_code: totpCode}),
    });
    const data = await res.json();
    if (!res.ok) {
      showError(data.detail || 'Login failed.');
      resetLoginBtn();
      return;
    }
    token = data.token;
    sessionStorage.setItem('ncr_token', token);
    connectWS();
  } catch {
    showError('Cannot reach server. Is Nanoclaw Remote running?');
    resetLoginBtn();
  }
});

// ── WebSocket ──────────────────────────────────────────────────────────
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/ws?token=${encodeURIComponent(token)}`);
  ws.binaryType = 'arraybuffer';

  ws.onopen = () => { showDesktop(); setStatus(true); };

  ws.onmessage = (e) => {
    if (typeof e.data === 'string') {
      const msg = JSON.parse(e.data);
      if (msg.type === 'info') {
        frameW = msg.width;
        frameH = msg.height;
        canvas.width        = frameW;
        canvas.height       = frameH;
        cursorCanvas.width  = frameW;
        cursorCanvas.height = frameH;
        applyMode();
      }
    } else {
      // Binary = JPEG frame — draw it then redraw cursor on top
      createImageBitmap(new Blob([e.data], {type: 'image/jpeg'})).then(bmp => {
        ctx.drawImage(bmp, 0, 0);
        bmp.close();
        drawCursor(cursorNX, cursorNY);
      });
    }
  };

  ws.onclose = (e) => {
    setStatus(false);
    if (e.code === 4401) {
      token = '';
      sessionStorage.removeItem('ncr_token');
      showLogin();
      showError('Session expired. Please log in again.');
    }
  };

  ws.onerror = () => setStatus(false);
}

// ── Cursor overlay ─────────────────────────────────────────────────────
function drawCursor(nx, ny) {
  if (!frameW || !frameH) return;
  cursorNX = nx;
  cursorNY = ny;
  const x = nx * frameW;
  const y = ny * frameH;

  cursorCtx.clearRect(0, 0, frameW, frameH);
  cursorCtx.save();
  cursorCtx.translate(x, y);

  // Classic arrow cursor (tip at 0,0 pointing top-left)
  const s = 18;
  cursorCtx.beginPath();
  cursorCtx.moveTo(0,        0);
  cursorCtx.lineTo(0,        s * 0.78);
  cursorCtx.lineTo(s * 0.22, s * 0.57);
  cursorCtx.lineTo(s * 0.39, s * 1.05);
  cursorCtx.lineTo(s * 0.54, s * 0.98);
  cursorCtx.lineTo(s * 0.37, s * 0.52);
  cursorCtx.lineTo(s * 0.65, s * 0.52);
  cursorCtx.closePath();

  cursorCtx.strokeStyle = 'rgba(0,0,0,0.85)';
  cursorCtx.lineWidth   = 2;
  cursorCtx.lineJoin    = 'round';
  cursorCtx.stroke();
  cursorCtx.fillStyle   = 'white';
  cursorCtx.fill();

  cursorCtx.restore();
}

// ── Mouse ──────────────────────────────────────────────────────────────
canvas.addEventListener('mousemove', (e) => {
  const now = Date.now();
  if (now - lastMoveMs < THROTTLE_MS) return;
  lastMoveMs = now;
  const p = pos(e);
  drawCursor(p.x, p.y);
  send({type: 'mousemove', ...p});
});

canvas.addEventListener('mousedown', (e) => {
  e.preventDefault();
  canvas.focus();
  send({type: 'mousedown', button: e.button, ...pos(e)});
});

canvas.addEventListener('mouseup',  (e) => send({type: 'mouseup',  button: e.button, ...pos(e)}));
canvas.addEventListener('dblclick', (e) => send({type: 'dblclick', ...pos(e)}));
canvas.addEventListener('contextmenu', (e) => e.preventDefault());

canvas.addEventListener('wheel', (e) => {
  e.preventDefault();
  send({type: 'scroll', dx: e.deltaX / 120, dy: e.deltaY / 120});
}, {passive: false});

// ── Keyboard (laptop/desktop) ──────────────────────────────────────────
const PASS_KEYS = new Set([
  'F1','F2','F3','F4','F5','F6','F7','F8','F9','F10','F11','F12',
  'Tab','Escape','ArrowLeft','ArrowRight','ArrowUp','ArrowDown',
  'Home','End','PageUp','PageDown','Backspace','Delete','Enter',
  'Control','Shift','Alt','Meta','CapsLock',
]);

canvas.addEventListener('keydown', (e) => {
  if (PASS_KEYS.has(e.key) || e.ctrlKey || e.metaKey) e.preventDefault();
  send({type: 'keydown', key: e.key});
});

canvas.addEventListener('keyup', (e) => {
  send({type: 'keyup', key: e.key});
});

// ── Touch → Mouse ──────────────────────────────────────────────────────
let touchT0 = 0;
let longPress = null;

canvas.addEventListener('touchstart', (e) => {
  e.preventDefault();
  canvas.focus();
  touchT0 = Date.now();
  const t = e.touches[0];
  const p = touchPos(t);
  drawCursor(p.x, p.y);
  send({type: 'mousemove',  ...p});
  send({type: 'mousedown', button: 0, ...p});
  longPress = setTimeout(() => {
    send({type: 'mouseup',   button: 0, ...p});
    send({type: 'mousedown', button: 2, ...p});
    send({type: 'mouseup',   button: 2, ...p});
  }, 600);
}, {passive: false});

canvas.addEventListener('touchmove', (e) => {
  e.preventDefault();
  clearTimeout(longPress);
  const now = Date.now();
  if (now - lastMoveMs >= THROTTLE_MS) {
    lastMoveMs = now;
    const p = touchPos(e.touches[0]);
    drawCursor(p.x, p.y);
    send({type: 'mousemove', ...p});
  }
}, {passive: false});

canvas.addEventListener('touchend', (e) => {
  e.preventDefault();
  clearTimeout(longPress);
  send({type: 'mouseup', button: 0, ...touchPos(e.changedTouches[0])});
}, {passive: false});

// ── Mobile keyboard panel ──────────────────────────────────────────────
const kbBtn = document.getElementById('kb-btn');

kbBtn.addEventListener('click', () => {
  const open = kbPanel.classList.toggle('hidden') === false;
  kbBtn.classList.toggle('active', !kbPanel.classList.contains('hidden'));
  if (open) {
    kbInput.value = '';
    kbInput.focus();
  }
});

document.getElementById('kb-close').addEventListener('click', () => {
  kbPanel.classList.add('hidden');
  kbBtn.classList.remove('active');
});

// Track value changes — more reliable than keydown on iOS virtual keyboard
let kbPrev = '';

kbInput.addEventListener('focus', () => { kbInput.value = ''; kbPrev = ''; });

kbInput.addEventListener('input', () => {
  const cur = kbInput.value;

  if (cur.length > kbPrev.length) {
    // Characters added
    const added = cur.slice(kbPrev.length);
    for (const ch of added) {
      send({type: 'keydown', key: ch});
      send({type: 'keyup',   key: ch});
    }
  } else if (cur.length < kbPrev.length) {
    // Backspace(s)
    const n = kbPrev.length - cur.length;
    for (let i = 0; i < n; i++) {
      send({type: 'keydown', key: 'Backspace'});
      send({type: 'keyup',   key: 'Backspace'});
    }
  }
  kbPrev = cur;
});

// Capture Enter and Tab which may not fire as input events on all keyboards
kbInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    e.preventDefault();
    send({type: 'keydown', key: 'Enter'});
    send({type: 'keyup',   key: 'Enter'});
  } else if (e.key === 'Tab') {
    e.preventDefault();
    send({type: 'keydown', key: 'Tab'});
    send({type: 'keyup',   key: 'Tab'});
  }
});

// ── Display mode ───────────────────────────────────────────────────────
document.querySelectorAll('.mode-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.mode-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    displayMode = btn.dataset.mode;
    applyMode();
  });
});

function applyMode() {
  if (!frameW || !frameH) return;
  const wrap = document.getElementById('canvas-wrap');
  let w, h;
  if (displayMode === 'fit') {
    const scale = Math.min(wrap.clientWidth / frameW, wrap.clientHeight / frameH);
    w = Math.round(frameW * scale);
    h = Math.round(frameH * scale);
  } else {
    w = frameW;
    h = frameH;
  }
  canvas.style.width        = w + 'px';
  canvas.style.height       = h + 'px';
  cursorCanvas.style.width  = w + 'px';
  cursorCanvas.style.height = h + 'px';
}

window.addEventListener('resize', applyMode);

// ── Disconnect ─────────────────────────────────────────────────────────
document.getElementById('disconnect-btn').addEventListener('click', () => {
  if (ws) ws.close();
  token = '';
  sessionStorage.removeItem('ncr_token');
  showLogin();
});

// ── Helpers ────────────────────────────────────────────────────────────
function pos(e) {
  const r = canvas.getBoundingClientRect();
  return {
    x: Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)),
    y: Math.max(0, Math.min(1, (e.clientY - r.top)  / r.height)),
  };
}

function touchPos(t) {
  const r = canvas.getBoundingClientRect();
  return {
    x: Math.max(0, Math.min(1, (t.clientX - r.left) / r.width)),
    y: Math.max(0, Math.min(1, (t.clientY - r.top)  / r.height)),
  };
}

function send(data) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(data));
}

function showError(msg) { errMsg.textContent = msg; errMsg.classList.remove('hidden'); }
function hideError()    { errMsg.classList.add('hidden'); }

function setStatus(ok) {
  statusDot.className = 'status-dot ' + (ok ? 'connected' : 'disconnected');
  statusDot.title = ok ? 'Connected' : 'Disconnected';
}

function showDesktop() {
  loginPage.classList.add('hidden');
  deskPage.classList.remove('hidden');
  canvas.focus();
}

function showLogin() {
  deskPage.classList.add('hidden');
  loginPage.classList.remove('hidden');
  kbPanel.classList.add('hidden');
  resetLoginBtn();
}

function resetLoginBtn() {
  loginBtn.disabled = false;
  loginBtn.textContent = 'Connect';
}
