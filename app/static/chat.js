/* ── State ───────────────────────────────────────────────────── */
const state = {
  sessionId: null,
  activeTab: 'files',
  files: [],
  isProcessing: false,
  isReady: false,
};

/* ── Init ────────────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  setupTabs();
  setupDragDrop();
  setupFileInput();
  setupHeadersToggle();
});

/* ── Tabs ────────────────────────────────────────────────────── */
function setupTabs() {
  document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
      const name = tab.dataset.tab;
      document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
      tab.classList.add('active');
      document.getElementById(`panel-${name}`).classList.add('active');
      state.activeTab = name;
    });
  });
}

/* ── Drag & Drop ─────────────────────────────────────────────── */
function setupDragDrop() {
  const zone = document.getElementById('drop-zone');

  zone.addEventListener('dragover', e => {
    e.preventDefault();
    zone.classList.add('dragover');
  });

  zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));

  zone.addEventListener('drop', e => {
    e.preventDefault();
    zone.classList.remove('dragover');
    addFiles([...e.dataTransfer.files]);
  });

  zone.addEventListener('click', (e) => {
    if (e.target.closest('.browse-btn')) return;
    document.getElementById('file-input').click();
  });
}

function setupFileInput() {
  document.getElementById('file-input').addEventListener('change', e => {
    addFiles([...e.target.files]);
    e.target.value = '';
  });
}

/* ── File management ─────────────────────────────────────────── */
const MAX_FILES = parseInt(getComputedEnv('MAX_FILES_PER_SESSION', 3));
const MAX_MB    = parseInt(getComputedEnv('MAX_FILE_SIZE_MB', 15));
const ALLOWED   = ['.pdf', '.doc', '.docx', '.txt'];

function getComputedEnv(key, fallback) {
  return fallback;
}

function addFiles(newFiles) {
  const allowed = newFiles.filter(f => {
    const ext = '.' + f.name.split('.').pop().toLowerCase();
    if (!ALLOWED.includes(ext)) {
      toast(`${f.name}: unsupported format (allowed: PDF, DOC, DOCX, TXT)`, 'error');
      return false;
    }
    if (f.size > MAX_MB * 1024 * 1024) {
      toast(`${f.name}: exceeds ${MAX_MB}MB limit`, 'error');
      return false;
    }
    return true;
  });

  const combined = [...state.files, ...allowed];
  if (combined.length > MAX_FILES) {
    toast(`Maximum ${MAX_FILES} files per session`, 'error');
    state.files = combined.slice(0, MAX_FILES);
  } else {
    state.files = combined;
  }

  renderFileList();
}

function removeFile(index) {
  state.files.splice(index, 1);
  renderFileList();
}

function renderFileList() {
  const list = document.getElementById('file-list');
  list.innerHTML = '';
  state.files.forEach((file, i) => {
    const ext = file.name.split('.').pop().toUpperCase();
    const size = formatBytes(file.size);
    list.innerHTML += `
      <div class="file-chip">
        <div class="file-chip-icon">${ext}</div>
        <div class="file-chip-info">
          <div class="file-chip-name">${escHtml(file.name)}</div>
          <div class="file-chip-size">${size}</div>
        </div>
        <button class="file-chip-remove" onclick="removeFile(${i})">×</button>
      </div>`;
  });
}

/* ── Headers ─────────────────────────────────────────────────── */
function setupHeadersToggle() {
  const btn   = document.getElementById('toggle-headers');
  const panel = document.getElementById('headers-panel');
  btn.addEventListener('click', () => {
    const open = !panel.classList.contains('hidden');
    panel.classList.toggle('hidden', open);
    btn.classList.toggle('open', !open);
  });
}

function addHeaderRow() {
  const row = document.createElement('div');
  row.className = 'header-row';
  row.innerHTML = `
    <input type="text" class="header-key" placeholder="Key (e.g. Authorization)" />
    <input type="text" class="header-val" placeholder="Value" />
    <button class="remove-row" onclick="removeHeaderRow(this)">×</button>`;
  document.getElementById('headers-list').appendChild(row);
}

function removeHeaderRow(btn) {
  btn.closest('.header-row').remove();
}

function collectHeaders() {
  const headers = {};
  document.querySelectorAll('.header-row').forEach(row => {
    const key = row.querySelector('.header-key').value.trim();
    const val = row.querySelector('.header-val').value.trim();
    if (key && val) headers[key] = val;
  });
  return Object.keys(headers).length ? headers : null;
}

/* ── Progress Card ───────────────────────────────────────────── */
class ProgressCard {
  constructor(title) {
    this._steps = {};
    const div = document.createElement('div');
    div.className = 'message bot';
    div.innerHTML = `
      <div class="bot-avatar">AI</div>
      <div>
        <div class="bubble progress-card">
          <div class="progress-title">${escHtml(title)}</div>
          <div class="progress-steps"></div>
          <div class="progress-total hidden"></div>
        </div>
      </div>`;
    clearWelcome();
    document.getElementById('messages').appendChild(div);
    scrollToBottom();
    this._titleEl  = div.querySelector('.progress-title');
    this._stepsEl  = div.querySelector('.progress-steps');
    this._totalEl  = div.querySelector('.progress-total');
  }

  setTitle(title) {
    this._titleEl.textContent = title;
  }

  addStep(stage, message) {
    if (this._steps[stage]) return;
    const row = document.createElement('div');
    row.className = 'progress-step running';
    row.innerHTML = `<span class="ps-icon"></span><span class="ps-label">${escHtml(message)}</span><span class="ps-timer">0.0s</span>`;
    this._stepsEl.appendChild(row);
    scrollToBottom();
    const startTime = Date.now();
    const timerEl = row.querySelector('.ps-timer');
    const interval = setInterval(() => {
      timerEl.textContent = ((Date.now() - startTime) / 1000).toFixed(1) + 's';
    }, 100);
    this._steps[stage] = { row, interval };
  }

  doneStep(stage, message, elapsed) {
    const s = this._steps[stage];
    if (!s) return;
    clearInterval(s.interval);
    s.row.className = 'progress-step done';
    s.row.querySelector('.ps-label').textContent = message;
    s.row.querySelector('.ps-timer').textContent = elapsed.toFixed(2) + 's';
    scrollToBottom();
  }

  errorStep(stage, message) {
    const s = this._steps[stage];
    if (!s) return;
    clearInterval(s.interval);
    s.row.className = 'progress-step error';
    s.row.querySelector('.ps-label').textContent = message;
    s.row.querySelector('.ps-timer').textContent = '';
  }

  complete(totalElapsed, message = 'Document processed') {
    Object.values(this._steps).forEach(s => clearInterval(s.interval));
    this._totalEl.classList.remove('hidden');
    this._totalEl.textContent = `${message} — ${totalElapsed.toFixed(2)}s`;
    scrollToBottom();
  }

  fail(message) {
    Object.values(this._steps).forEach(s => clearInterval(s.interval));
    this._totalEl.classList.remove('hidden');
    this._totalEl.classList.add('error');
    this._totalEl.textContent = `Failed: ${message}`;
    scrollToBottom();
  }
}

/* ── SSE stream reader ───────────────────────────────────────── */
async function readSSEStream(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop();
    for (const line of lines) {
      if (line.startsWith('data: ')) {
        try { onEvent(JSON.parse(line.slice(6))); } catch (_) {}
      }
    }
  }
}

function handleProgressEvent(card, ev) {
  if (ev.type === 'step') {
    if (ev.title) card.setTitle(ev.title);
    if (ev.status === 'start') card.addStep(ev.stage, ev.message);
    else if (ev.status === 'done') card.doneStep(ev.stage, ev.message, ev.elapsed ?? 0);
    else if (ev.status === 'error') card.errorStep(ev.stage, ev.message);
  } else if (ev.type === 'complete') {
    card.setTitle('Process done');
    card.complete(ev.total_elapsed ?? 0, ev.completion_message || 'Document processed');
    onProcessed(ev.session_id);
    toast('Ready to chat!', 'success');
  } else if (ev.type === 'error') {
    card.fail(ev.message);
    toast(ev.message, 'error');
  }
}

/* ── Process source ──────────────────────────────────────────── */
async function processSource() {
  if (state.isProcessing) return;
  const tab = state.activeTab;

  if (tab === 'files') {
    if (state.files.length === 0) return toast('Please select at least one file', 'error');
    return processFiles();
  }

  if (tab === 'url') {
    const url = document.getElementById('url-input').value.trim();
    if (!url) return toast('Please enter a URL', 'error');
    return processUrl(url);
  }

  if (tab === 'api') {
    const url = document.getElementById('api-url-input').value.trim();
    if (!url) return toast('Please enter an API URL', 'error');
    const apiHeaders = collectHeaders();
    setProcessing(true);
    try {
      const res  = await fetch('/upload/api', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url, headers: apiHeaders }) });
      const data = await safeJson(res);
      if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);
      onProcessed(data.session_id);
      toast(data.message || 'Ready to chat!', 'success');
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setProcessing(false);
    }
  }
}

async function processFiles() {
  setProcessing(true);
  const card = new ProgressCard(`Processing ${state.files.length} file(s)`);
  const form = new FormData();
  state.files.forEach(f => form.append('files', f));

  try {
    const res = await fetch('/upload/files', { method: 'POST', body: form });
    if (!res.ok) {
      const data = await safeJson(res);
      throw new Error(data?.detail || `Server error ${res.status}`);
    }
    await readSSEStream(res, (ev) => handleProgressEvent(card, ev));
  } catch (err) {
    card.fail(err.message);
    toast(err.message, 'error');
  } finally {
    setProcessing(false);
  }
}

async function processUrl(url) {
  setProcessing(true);
  const card = new ProgressCard('Processing URL');
  try {
    const res = await fetch('/upload/url', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    if (!res.ok) {
      const data = await safeJson(res);
      throw new Error(data?.detail || `Server error ${res.status}`);
    }
    await readSSEStream(res, (ev) => handleProgressEvent(card, ev));
  } catch (err) {
    card.fail(err.message);
    toast(err.message, 'error');
  } finally {
    setProcessing(false);
  }
}

function onProcessed(sessionId) {
  state.sessionId = sessionId;
  state.isReady = true;

  document.getElementById('session-badge').classList.remove('hidden');
  document.getElementById('session-label').textContent = 'Session Active';
  document.getElementById('reset-btn').classList.remove('hidden');

  const input  = document.getElementById('chat-input');
  const sendBtn = document.getElementById('send-btn');
  input.disabled  = false;
  input.placeholder = 'Ask anything about your data...';
  sendBtn.disabled = false;
  input.focus();

  clearWelcome();
  appendMessage('bot', 'Your data is ready! Ask me anything about it.');
}

/* ── Chat ────────────────────────────────────────────────────── */
function handleKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
}

async function sendMessage() {
  const input = document.getElementById('chat-input');
  const question = input.value.trim();
  if (!question || !state.isReady || state.isProcessing) return;

  input.value = '';
  autoResize(input);
  appendMessage('user', question);
  showTyping(true);

  try {
    const res  = await fetch('/chat/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: state.sessionId, question }),
    });
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);
    appendMessage('bot', data.answer, data.elapsed_ms, data.citations || []);
  } catch (err) {
    appendMessage('bot', `Error: ${err.message}`);
    toast(err.message, 'error');
  } finally {
    showTyping(false);
  }
}

/* ── Message rendering ───────────────────────────────────────── */
function appendMessage(role, text, elapsedMs = null, citations = []) {
  const messages = document.getElementById('messages');

  const div = document.createElement('div');
  div.className = `message ${role}`;

  const avatarHtml = role === 'bot'
    ? `<div class="bot-avatar">AI</div>`
    : `<div class="user-avatar">👤</div>`;

  const content = role === 'bot'
    ? marked.parse(text)
    : `<p>${escHtml(text)}</p>`;

  const timingHtml = (role === 'bot' && elapsedMs != null)
    ? `<div class="answer-timing">⏱ ${elapsedMs.toLocaleString()} ms</div>`
    : '';

  let citationsHtml = '';
  if (role === 'bot' && citations.length > 0) {
    const id = 'cit-' + Math.random().toString(36).slice(2, 8);
    const rows = citations.map(c => {
      const src = escHtml(c.source || 'unknown');
      const chunk = c.chunk_index ?? '?';
      const preview = escHtml((c.preview || '').replace(/\n+/g, ' '));
      const pageHtml = c.page_number != null ? `<span class="cit-sep">—</span><span class="cit-page">Page ${c.page_number}</span>` : '';
      const confHtml = c.confidence != null ? `<span class="cit-sep">—</span><span class="cit-conf">Confidence ${(c.confidence * 100).toFixed(1)}%</span>` : '';
      return `<div class="citation-item">
        <span class="cit-arrow">▶</span>
        <span class="cit-source">${src}</span>
        <span class="cit-sep">—</span>
        <span class="cit-chunk">Chunk #${chunk}</span>
        ${pageHtml}${confHtml}
        <span class="cit-sep">—</span>
        <span class="cit-preview">"${preview}"</span>
      </div>`;
    }).join('');
    citationsHtml = `
      <div class="citations-block">
        <button class="citations-toggle" onclick="toggleCitations('${id}')">
          <span class="cit-toggle-icon">▶</span> ${citations.length} source${citations.length > 1 ? 's' : ''} used
        </button>
        <div class="citations-list hidden" id="${id}">${rows}</div>
      </div>`;
  }

  const copyBtn = role === 'bot'
    ? `<div class="bubble-actions">
        <button class="copy-btn" onclick="copyText(this, ${JSON.stringify(text)})">
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
          Copy
        </button>
      </div>`
    : '';

  div.innerHTML = `
    ${avatarHtml}
    <div>
      <div class="bubble">${content}${timingHtml}${citationsHtml}</div>
      ${copyBtn}
    </div>`;

  messages.appendChild(div);
  scrollToBottom();
}

function toggleCitations(id) {
  const list = document.getElementById(id);
  const btn  = list.previousElementSibling;
  const icon = btn.querySelector('.cit-toggle-icon');
  const open = list.classList.toggle('hidden');
  icon.textContent = open ? '▶' : '▼';
}

function clearWelcome() {
  const w = document.querySelector('.welcome');
  if (w) w.remove();
}

function showTyping(show) {
  document.getElementById('typing').classList.toggle('hidden', !show);
  if (show) scrollToBottom();
}

function scrollToBottom() {
  const el = document.getElementById('messages');
  el.scrollTop = el.scrollHeight;
}

function copyText(btn, text) {
  navigator.clipboard.writeText(text).then(() => {
    btn.innerHTML = `✓ Copied`;
    setTimeout(() => {
      btn.innerHTML = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg> Copy`;
    }, 2000);
  });
}

/* ── Reset ───────────────────────────────────────────────────── */
function resetSession() {
  state.sessionId  = null;
  state.isReady    = false;
  state.files      = [];
  renderFileList();

  document.getElementById('session-badge').classList.add('hidden');
  document.getElementById('reset-btn').classList.add('hidden');

  const input   = document.getElementById('chat-input');
  const sendBtn = document.getElementById('send-btn');
  input.disabled   = true;
  input.placeholder = 'Process a data source first to start chatting...';
  sendBtn.disabled = true;

  const messages = document.getElementById('messages');
  messages.innerHTML = `
    <div class="welcome">
      <div class="welcome-icon">
        <svg width="40" height="40" viewBox="0 0 24 24" fill="none"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5" stroke="url(#g3)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><defs><linearGradient id="g3" x1="0" y1="0" x2="24" y2="24" gradientUnits="userSpaceOnUse"><stop stop-color="#667eea"/><stop offset="1" stop-color="#764ba2"/></linearGradient></defs></svg>
      </div>
      <h2 class="welcome-title">Welcome to RAG Assistant</h2>
      <p class="welcome-sub">Upload files, paste a URL, or connect an API — then ask anything about your data.</p>
      <div class="welcome-steps">
        <div class="step"><span class="step-num">1</span> Choose a data source on the left</div>
        <div class="step"><span class="step-num">2</span> Click "Process & Start Chat"</div>
        <div class="step"><span class="step-num">3</span> Ask any question about your data</div>
      </div>
    </div>`;
}

/* ── Processing state ────────────────────────────────────────── */
function setProcessing(on) {
  state.isProcessing = on;
  const btn     = document.getElementById('process-btn');
  const btnText = btn.querySelector('.btn-text');
  const spinner = btn.querySelector('.btn-spinner');
  btn.disabled = on;
  btnText.classList.toggle('hidden', on);
  spinner.classList.toggle('hidden', !on);
}

/* ── Auto-resize textarea ────────────────────────────────────── */
function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 160) + 'px';
}

/* ── Toast ───────────────────────────────────────────────────── */
function toast(msg, type = 'info') {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  document.getElementById('toast-container').appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

/* ── Helpers ─────────────────────────────────────────────────── */
function escHtml(str) {
  return str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function formatBytes(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

async function safeJson(res) {
  try {
    return await res.json();
  } catch {
    return { detail: `Server error ${res.status}` };
  }
}
