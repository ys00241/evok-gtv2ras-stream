/* ─── TV-STREAM Single-Page App ─── */

const API_BASE = '/api';
let hlsPlayers = {};
let pollInterval = null;
let streamUrl = '';
let rtspUrl = '';

// ─── Tab Navigation ───
document.querySelectorAll('.nav-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    const target = document.getElementById(`tab-${tab.dataset.tab}`);
    if (target) target.classList.add('active');
  });
});

// ─── API Helper ───
async function api(method, path, body = null) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body) opts.body = JSON.stringify(body);
  try {
    const r = await fetch(`${API_BASE}${path}`, opts);
    return await r.json();
  } catch (e) {
    console.error('API error:', e);
    return { status: 'error', message: e.message };
  }
}

function toast(msg, type = '') {
  const existing = document.querySelector('.toast');
  if (existing) existing.remove();
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3000);
}

// ─── HLS Player ───
function initPlayer(videoId, url) {
  const video = document.getElementById(videoId);
  if (!video) return;
  if (hlsPlayers[videoId]) { hlsPlayers[videoId].destroy(); delete hlsPlayers[videoId]; }
  if (!url) return;
  if (Hls.isSupported()) {
    const hls = new Hls({ liveDurationInfinity: true, lowLatencyMode: true });
    hls.loadSource(url);
    hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {}));
    hlsPlayers[videoId] = hls;
  } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
    video.src = url;
    video.play().catch(() => {});
  }
}

// ─── Dashboard ───
async function loadStatus() {
  const data = await api('GET', '/stream/status');
  if (data.status !== 'ok') return;
  const running = data.running;
  const el = document.getElementById('streamRunning');
  el.textContent = running ? '🔴 LIVE' : 'Offline';
  el.className = `badge ${running ? 'badge-on' : 'badge-off'}`;
  document.getElementById('hlsReady').textContent = data.hls_ready ? '✅ Yes' : '❌ No';
  document.getElementById('hlsReady').className = `badge ${data.hls_ready ? 'badge-on' : 'badge-off'}`;
  document.getElementById('currentPreset').textContent = data.current_preset || 'custom';
  document.getElementById('streamDot').className = `dot ${running ? 'online' : 'offline'}`;
  if (data.current_preset) {
    const radio = document.querySelector(`input[name="preset"][value="${data.current_preset}"]`);
    if (radio) radio.checked = true;
  }
}

async function loadChannels() {
  const data = await api('GET', '/channel/status');
  if (data.status !== 'ok') return;
  const ch = data.channels;
  document.getElementById('ch-hls').checked = ch.hls?.enabled || false;
  document.getElementById('ch-rtsp').checked = ch.rtsp?.enabled || false;
  document.getElementById('ch-teams').checked = ch.teams?.enabled || false;
  document.getElementById('ch-telegram').checked = ch.telegram?.enabled || false;
  if (ch.teams) {
    document.getElementById('teamsUrl').value = ch.teams.rtmp_url || '';
    document.getElementById('teamsKey').value = ch.teams.rtmp_key || '';
  }
  if (ch.telegram) {
    document.getElementById('tgUrl').value = ch.telegram.rtmp_url || '';
  }
}

async function loadCCStatus() {
  const data = await api('GET', '/cc/status');
  const el = document.getElementById('ccConnected');
  if (!el) return;
  // CC remote not deployed → hide the row
  if (data.cc_available === false || data.message === 'CC Remote service not deployed') {
    // Check if there's a parent stat-row to hide
    const row = el.closest('.stat-row');
    if (row) row.style.display = 'none';
    return;
  }
  if (data.status === 'ok' && data.connected) {
    el.textContent = `✅ ${data.host}`;
    el.className = 'badge badge-on';
  } else {
    el.textContent = '❌ Disconnected';
    el.className = 'badge badge-off';
  }
}

async function loadStreamUrl() {
  const base = window.location.host;
  streamUrl = `http://${base}/hls/stream.m3u8`;
  document.getElementById('streamUrl').textContent = streamUrl;
  initPlayer('playerVideo', streamUrl);
  const ev = document.getElementById('expandVideo');
  if (ev && !ev.src) initPlayer('expandVideo', streamUrl);
  // RTSP — mediamtx always runs on port 8554 independently
  const hostname = window.location.hostname;
  rtspUrl = `rtsp://${hostname}:8554/live`;
  document.getElementById('rtspUrl').textContent = rtspUrl;
  document.getElementById('rtspUrlRow').style.display = 'flex';
}

// ─── Buttons: Stream ───
document.getElementById('btnStreamStart').addEventListener('click', async () => {
  const r = await api('POST', '/stream/start');
  toast(r.message, r.status === 'ok' ? 'success' : 'error');
  loadStatus();
  setTimeout(loadStreamUrl, 1000);
});
document.getElementById('btnStreamStop').addEventListener('click', async () => {
  const r = await api('POST', '/stream/stop');
  toast(r.message, r.status === 'ok' ? 'success' : 'error');
  loadStatus();
});
document.getElementById('btnStreamRestart').addEventListener('click', async () => {
  const r = await api('POST', '/stream/restart');
  toast(r.message, 'success');
  loadStatus();
});

// ─── Buttons: Preset ───
document.getElementById('btnPresetApply').addEventListener('click', async () => {
  const selected = document.querySelector('input[name="preset"]:checked');
  if (!selected) return;
  const r = await api('PUT', '/stream/config', { preset: selected.value });
  toast(r.message, 'success');
  loadStatus();
});

// ─── Buttons: Channels ───
document.getElementById('btnChannelApply').addEventListener('click', async () => {
  const hls = document.getElementById('ch-hls').checked;
  const rtsp = document.getElementById('ch-rtsp').checked;
  const teams = document.getElementById('ch-teams').checked;
  const tg = document.getElementById('ch-telegram').checked;
  const teamsUrl = document.getElementById('teamsUrl').value;
  const teamsKey = document.getElementById('teamsKey').value;
  const tgUrl = document.getElementById('tgUrl').value;
  await api('PUT', '/channel/hls', { enabled: hls });
  await api('PUT', '/channel/rtsp', { enabled: rtsp });
  await api('PUT', '/channel/teams', { enabled: teams, rtmp_url: teamsUrl, rtmp_key: teamsKey });
  await api('PUT', '/channel/telegram', { enabled: tg, rtmp_url: tgUrl });
  toast('Channels updated', 'success');
  loadChannels();
  loadStatus();
  loadStreamUrl();
});

document.getElementById('ch-teams').addEventListener('change', toggleChannelConfig);
document.getElementById('ch-telegram').addEventListener('change', toggleChannelConfig);
function toggleChannelConfig() {
  const show = document.getElementById('ch-teams').checked || document.getElementById('ch-telegram').checked;
  document.getElementById('channelConfig').style.display = show ? 'block' : 'none';
}

// ─── Remote Control（CC remote optional — missing API returns gracefully）───

// Try connect on page load — no error if not deployed
setTimeout(async () => {
  const r = await api('POST', '/cc/connect', {});
  if (r.status === 'ok') toast('Chromecast connected!', 'success');
}, 1000);

// D-Pad
document.querySelectorAll('.btn-dpad').forEach(btn => {
  btn.addEventListener('click', () => api('POST', `/cc/nav/${btn.dataset.key}`));
  let timer = null;
  btn.addEventListener('mousedown', () => timer = setInterval(() => api('POST', `/cc/nav/${btn.dataset.key}`), 150));
  btn.addEventListener('touchstart', (e) => { e.preventDefault(); timer = setInterval(() => api('POST', `/cc/nav/${btn.dataset.key}`), 150); });
  const clear = () => { if (timer) { clearInterval(timer); timer = null; }};
  btn.addEventListener('mouseup', clear); btn.addEventListener('mouseleave', clear);
  btn.addEventListener('touchend', clear); btn.addEventListener('touchcancel', clear);
});

document.querySelectorAll('.btn-nav').forEach(btn => {
  btn.addEventListener('click', () => api('POST', `/cc/nav/${btn.dataset.key}`));
});

// Volume
document.querySelectorAll('.btn-vol, .btn-vol-mute').forEach(btn => {
  btn.addEventListener('click', () => {
    const action = btn.dataset.vol;
    api('POST', `/cc/vol/${action}`);
    const fill = document.getElementById('volFill');
    if (action === 'up') fill.style.width = Math.min(100, parseInt(fill.style.width) + 10) + '%';
    else if (action === 'down') fill.style.width = Math.max(0, parseInt(fill.style.width) - 10) + '%';
  });
});

// Apps
document.querySelectorAll('.btn-app').forEach(btn => {
  btn.addEventListener('click', () => {
    api('POST', `/cc/app/${btn.dataset.app}`);
    toast(`Launching ${btn.textContent.trim()}`, 'success');
  });
});

// Text
document.getElementById('btnCcText').addEventListener('click', () => {
  const text = document.getElementById('ccTextInput').value;
  if (!text) return;
  api('POST', '/cc/text', { text });
  document.getElementById('ccTextInput').value = '';
});

// Screenshot
document.getElementById('btnScreenshot').addEventListener('click', async () => {
  try {
    const r = await fetch(`${API_BASE}/cc/screenshot?_=${Date.now()}`);
    if (!r.ok) { toast('Screenshot failed', 'error'); return; }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const img = document.getElementById('screenshotImg');
    img.src = url;
    document.getElementById('screenPlaceholder').style.display = 'none';
    img.style.display = 'block';
  } catch (e) { toast('Screenshot error', 'error'); }
});

// Expand / Collapse
document.getElementById('btnExpandScreen').addEventListener('click', () => {
  document.getElementById('expandPreviewCard').style.display = 'block';
  document.getElementById('btnExpandScreen').textContent = '🔴 Live';
  initPlayer('expandVideo', streamUrl);
});
document.getElementById('btnCollapseScreen').addEventListener('click', () => {
  document.getElementById('expandPreviewCard').style.display = 'none';
  document.getElementById('btnExpandScreen').textContent = '⛶ Expand';
});

// ─── Player Tab ───
document.getElementById('btnCopyUrl').addEventListener('click', () => {
  navigator.clipboard.writeText(streamUrl).then(() => toast('URL copied!', 'success')).catch(() => {});
});
document.getElementById('btnToggleMute').addEventListener('click', () => {
  const v = document.getElementById('playerVideo');
  v.muted = !v.muted;
  document.getElementById('btnToggleMute').textContent = v.muted ? '🔇 Unmute' : '🔇 Mute';
});
document.getElementById('btnFullscreen').addEventListener('click', () => {
  const v = document.getElementById('playerVideo');
  if (v.requestFullscreen) v.requestFullscreen();
  else if (v.webkitRequestFullscreen) v.webkitRequestFullscreen();
});

// QR Code
function generateQR(text) {
  const container = document.getElementById('qrCode');
  container.innerHTML = '';
  const img = document.createElement('img');
  img.src = `https://api.qrserver.com/v1/create-qr-code/?size=120x120&data=${encodeURIComponent(text)}`;
  img.alt = 'QR';
  img.style.width = '120px';
  img.style.height = '120px';
  container.appendChild(img);
}
const qrCheck = setInterval(() => { if (streamUrl) { generateQR(streamUrl); clearInterval(qrCheck); } }, 500);

// ─── Recording ───
document.getElementById('btnRecStart').addEventListener('click', async () => {
  const config = {
    quality: document.getElementById('recQuality').value,
    mode: document.getElementById('recMode').value,
    segment_seconds: parseInt(document.getElementById('recSegment').value),
    destination: document.getElementById('recDest').value,
  };
  const r = await api('POST', '/record/start', config);
  toast(r.message, r.status === 'ok' ? 'success' : 'error');
  loadRecStatus();
});
document.getElementById('btnRecStop').addEventListener('click', async () => {
  const r = await api('POST', '/record/stop');
  toast(r.message, r.status === 'ok' ? 'success' : 'error');
  loadRecStatus();
});
document.getElementById('recMode').addEventListener('change', () => {
  document.getElementById('segmentOpts').style.display =
    document.getElementById('recMode').value === 'segment' ? 'block' : 'none';
});

async function loadRecStatus() {
  const data = await api('GET', '/record/status');
  if (data.status !== 'ok') return;
  const el = document.getElementById('recStatus');
  el.textContent = data.running ? '🔴 Recording' : 'Idle';
  el.className = `badge ${data.running ? 'badge-on' : 'badge-off'}`;
  document.getElementById('recDiskUsed').textContent = data.disk_used_mb ? `${Math.round(data.disk_used_mb)} MB` : '0 MB';
  const list = document.getElementById('recFileList');
  if (!data.files || data.files.length === 0) {
    list.innerHTML = '<div class="file-empty">No recordings yet</div>';
  } else {
    list.innerHTML = data.files.slice(0, 20).map(f =>
      `<div class="file-item"><span class="file-name">${f.name}</span><span class="file-size">${f.size_mb}MB</span><a class="file-dl" href="/api/record/files/${encodeURIComponent(f.name)}" download>⬇</a></div>`
    ).join('');
  }
}

// ─── Polling ───
async function pollAll() {
  await Promise.all([loadStatus(), loadCCStatus(), loadRecStatus()]);
}
pollInterval = setInterval(pollAll, 5000);
pollAll();
loadChannels();
loadStreamUrl();

// ─── Keyboard shortcuts ───
document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  const keyMap = {
    'ArrowUp': 'up', 'ArrowDown': 'down', 'ArrowLeft': 'left', 'ArrowRight': 'right',
    'Enter': 'ok', 'Escape': 'back', 'Backspace': 'back', 'h': 'home', ' ': 'ok',
  };
  const key = keyMap[e.key];
  if (key) { e.preventDefault(); api('POST', `/cc/nav/${key}`); }
});
