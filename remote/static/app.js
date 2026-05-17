'use strict';

// ── State ──────────────────────────────────────────────────────────────
let ws = null;
let token = sessionStorage.getItem('ncr_token') || '';
let frameW = 0, frameH = 0;
let displayMode = 'fit';
let lastMoveMs = 0;
const THROTTLE_MS = 16; // ~60 fps mouse moves

// ── Elements ───────────────────────────────────────────────────────────
const loginPage  = document.getElementById('login-page');
const deskPage   = document.getElementById('desktop-page');
const loginForm  = document.getElementById('login-form');
const loginBtn   = document.getElementById('login-btn');
const errMsg     = document.getElementById('error-msg');
const canvas     = document.getElementById('screen');
const statusDot  = document.getElementById('status-dot');
const ctx        = canvas.getContext('2d');

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

  ws.onopen = () => {
    showDesktop();
    setStatus(true);
  };

  ws.onmessage = (e) => {
    if (typeof e.data === 'string') {
      const msg = JSON.parse(e.data);
      if (msg.type === 'info') {
        frameW = msg.width;
        frameH = msg.height;
        canvas.width  = frameW;
        canvas.height = frameH;
        applyMode();
      }
    } else {
      // Binary = JPEG frame
      createImageBitmap(new Blob([e.data], {type: 'image/jpeg'})).then(bmp => {
        ctx.drawImage(bmp, 0, 0);
        bmp.close();
      });
    }
  };

  ws.onclose = (e) => {
    setStatus(false);
    if (e.code === 4401) {
      // Token rejected — clear and re-login
      token = '';
      sessionStorage.removeItem('ncr_token');
      showLogin();
      showError('Session expired. Please log in again.');
    }
  };

  ws.onerror = () => setStatus(false);
}

// ── Mouse ──────────────────────────────────────────────────────────────
canvas.addEventListener('mousemove', (e) => {
  const now = Date.now();
  if (now - lastMoveMs < THROTTLE_MS) return;
  lastMoveMs = now;
  send({type: 'mousemove', ...pos(e)});
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

// ── Keyboard ───────────────────────────────────────────────────────────
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
    send({type: 'mousemove', ...touchPos(e.touches[0])});
  }
}, {passive: false});

canvas.addEventListener('touchend', (e) => {
  e.preventDefault();
  clearTimeout(longPress);
  const t = e.changedTouches[0];
  send({type: 'mouseup', button: 0, ...touchPos(t)});
}, {passive: false});

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
  if (displayMode === 'fit') {
    const scale = Math.min(wrap.clientWidth / frameW, wrap.clientHeight / frameH);
    canvas.style.width  = Math.round(frameW * scale) + 'px';
    canvas.style.height = Math.round(frameH * scale) + 'px';
  } else {
    canvas.style.width  = frameW + 'px';
    canvas.style.height = frameH + 'px';
  }
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
    x: Math.max(0, Math.min(1, (e.clientX - r.left)  / r.width)),
    y: Math.max(0, Math.min(1, (e.clientY - r.top)   / r.height)),
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

function showError(msg) {
  errMsg.textContent = msg;
  errMsg.classList.remove('hidden');
}

function hideError() { errMsg.classList.add('hidden'); }

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
  resetLoginBtn();
}

function resetLoginBtn() {
  loginBtn.disabled = false;
  loginBtn.textContent = 'Connect';
}
