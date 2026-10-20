/* ── Auth guard - redirect to login if no token ──────────────── */
(function() {
  if (!localStorage.getItem('rag_auth_token')) {
    window.location.replace('/auth.html');
  }
})();

/* ── State ───────────────────────────────────────────────────── */
const state = {
  sessionId:    null,
  clientToken:  null,
  activeTab:    'files',
  files:        [],
  isProcessing: false,
  isReady:      true,
  advancedMode: false,
  thinkingMode: false,
  authToken:    null,
  authUser:     null,
  connectorsLoaded: false,
  connectors:   [],
  connectorsUnlockToken: null,
};
window._ragState = state; // expose for testing

/* ── Init ────────────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  initTheme();
  initAuth();
  initClientToken();
  initAdvancedMode();
  initThinkingMode();
  setupTabs();
  setupDragDrop();
  setupFileInput();
  setupHeadersToggle();
});

function initTheme() {
  const dark = localStorage.getItem('aria_dark_mode') === 'true';
  if (dark) document.documentElement.classList.add('dark');

  const btn = document.createElement('button');
  btn.className = 'theme-toggle';
  btn.title = 'Toggle dark mode';
  btn.innerHTML = dark ? sunIcon() : moonIcon();
  btn.addEventListener('click', () => {
    const isDark = document.documentElement.classList.toggle('dark');
    localStorage.setItem('aria_dark_mode', isDark);
    btn.innerHTML = isDark ? sunIcon() : moonIcon();
  });
  document.querySelector('.header-right').appendChild(btn);
}

function moonIcon() {
  return `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>`;
}
function sunIcon() {
  return `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>`;
}

function initAuth() {
  state.authToken = localStorage.getItem('rag_auth_token');
  state.connectorsUnlockToken = localStorage.getItem('rag_connectors_unlock_token');
  try {
    state.authUser = JSON.parse(localStorage.getItem('rag_auth_user') || 'null');
  } catch (_) { state.authUser = null; }

  if (!state.authToken) { window.location.replace('/auth.html'); return; }

  const user = state.authUser;
  if (!user) return;

  // Inject user chip into header
  const headerRight = document.querySelector('.header-right');
  if (headerRight) {
    const isGuest = user.role === 'guest';
    const initials = (user.name || user.email || 'U')
      .split(' ').map(p => p[0]).join('').toUpperCase().slice(0, 2);

    const chip = document.createElement('div');
    chip.className = 'user-chip';
    chip.innerHTML = `
      <div class="user-avatar-sm">${initials}</div>
      <div class="user-chip-info">
        <span class="user-chip-name">${escHtml(user.name || user.email)}</span>
        ${isGuest ? '<span class="user-chip-role">Guest</span>' : ''}
      </div>
      <button class="logout-btn" onclick="logout()" title="Sign out">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>
          <polyline points="16 17 21 12 16 7"/>
          <line x1="21" y1="12" x2="9" y2="12"/>
        </svg>
      </button>`;
    headerRight.appendChild(chip);
  }
}

function logout() {
  localStorage.removeItem('rag_auth_token');
  localStorage.removeItem('rag_auth_user');
  window.location.replace('/auth.html');
}

/* ── Auth headers helper ─────────────────────────────────────── */
function authHeaders(extra = {}) {
  return {
    ...(state.authToken ? { 'Authorization': `Bearer ${state.authToken}` } : {}),
    ...extra,
  };
}

// Every /connectors/* call needs this on top of the normal auth header - the
// server rejects anything under that prefix without a valid unlock token,
// regardless of how valid the Bearer token is (see app/connectors/access_gate.py).
function connectorHeaders(extra = {}) {
  return authHeaders({
    ...(state.connectorsUnlockToken ? { 'X-Connectors-Unlock': state.connectorsUnlockToken } : {}),
    ...extra,
  });
}

function handleAuthError(res) {
  if (res.status === 401) {
    localStorage.removeItem('rag_auth_token');
    localStorage.removeItem('rag_auth_user');
    window.location.replace('/auth.html');
    return true;
  }
  return false;
}

function _uuid() {
  try { return crypto.randomUUID(); } catch (_) {}
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = Math.random() * 16 | 0;
    return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
  });
}

function initClientToken() {
  let token = localStorage.getItem('rag_client_token');
  if (!token) {
    try {
      token = crypto.randomUUID();
    } catch (_) {
      token = 'uid-' + Math.random().toString(36).slice(2) + Date.now().toString(36);
    }
    localStorage.setItem('rag_client_token', token);
  }
  state.clientToken = token;

  // Always have a valid UUID session ID - back-end falls back to sample if session is empty
  if (!state.sessionId) {
    state.sessionId = _uuid();
  }
}

function initAdvancedMode() {
  state.advancedMode = localStorage.getItem('rag_advanced_mode') === 'true';
  const toggle = document.getElementById('advanced-toggle');
  if (toggle) {
    toggle.checked = state.advancedMode;
    toggle.addEventListener('change', () => {
      state.advancedMode = toggle.checked;
      localStorage.setItem('rag_advanced_mode', state.advancedMode);
      const label = document.getElementById('advanced-label');
      if (label) label.textContent = state.advancedMode ? 'Advanced Mode: ON' : 'Advanced Mode: OFF';
    });
    const label = document.getElementById('advanced-label');
    if (label) label.textContent = state.advancedMode ? 'Advanced Mode: ON' : 'Advanced Mode: OFF';
  }
}

function initThinkingMode() {
  state.thinkingMode = localStorage.getItem('rag_thinking_mode') === 'true';
  const toggle = document.getElementById('thinking-toggle');
  if (toggle) {
    toggle.checked = state.thinkingMode;
    toggle.addEventListener('change', () => {
      state.thinkingMode = toggle.checked;
      localStorage.setItem('rag_thinking_mode', state.thinkingMode);
      const label = document.getElementById('thinking-label');
      if (label) label.textContent = state.thinkingMode ? 'Thinking Mode: ON' : 'Thinking Mode: OFF';
    });
    const label = document.getElementById('thinking-label');
    if (label) label.textContent = state.thinkingMode ? 'Thinking Mode: ON' : 'Thinking Mode: OFF';
  }
}

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

/* ── Connectors view toggle (swaps with the upload tabs) ────────── */
function toggleConnectorsView() {
  const uploadSection = document.getElementById('upload-section');
  const connectorsSection = document.getElementById('connectors-section');
  const processBtn = document.getElementById('process-btn');
  const toggleBtnText = document.querySelector('#connectors-toggle-btn .btn-text');

  const opening = connectorsSection.classList.contains('hidden');
  uploadSection.classList.toggle('hidden', opening);
  connectorsSection.classList.toggle('hidden', !opening);
  processBtn.classList.toggle('hidden', opening);
  toggleBtnText.textContent = opening ? 'Back to Upload' : 'Connect a Data Source';

  if (opening) {
    if (!state.connectorsUnlockToken) {
      showConnectorsUnlockPrompt();
    } else if (!state.connectorsLoaded) {
      state.connectorsLoaded = true;
      loadConnectors();
    }
  }
}

/* ── Connectors access gate ──────────────────────────────────── */
// A second secret beyond normal login, required before any /connectors/*
// call succeeds (enforced server-side in app/connectors/access_gate.py) -
// connecting Gmail/GitHub/Jira/etc. is real access to someone's real
// third-party accounts, so it gets its own separate lock.
function showConnectorsUnlockPrompt() {
  document.getElementById('connector-modal-title').textContent = 'Connectors are locked';
  const body = document.getElementById('connector-modal-body');
  body.innerHTML = `
    <div class="connector-browse-note">Enter the connectors access key to use Gmail, GitHub, Jira, and the other source connectors.</div>
    <input type="password" id="connectors-unlock-input" class="text-input" placeholder="Access key" autocomplete="off" />
    <button class="connector-action-btn" id="connectors-unlock-submit" onclick="submitConnectorsUnlock()">Unlock</button>
    <div class="connector-browse-note" id="connectors-unlock-error"></div>
  `;
  document.getElementById('connector-modal').classList.remove('hidden');
  document.addEventListener('keydown', _connectorModalEscHandler);
  const input = document.getElementById('connectors-unlock-input');
  input.focus();
  input.addEventListener('keydown', e => { if (e.key === 'Enter') submitConnectorsUnlock(); });
}

async function submitConnectorsUnlock() {
  const input = document.getElementById('connectors-unlock-input');
  const errEl = document.getElementById('connectors-unlock-error');
  const key = input.value.trim();
  if (!key) return;
  errEl.textContent = '';
  errEl.classList.remove('is-error');
  try {
    const res = await fetch('/connectors/unlock', {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ access_key: key }),
    });
    if (handleAuthError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);
    state.connectorsUnlockToken = data.unlock_token;
    localStorage.setItem('rag_connectors_unlock_token', data.unlock_token);
    closeConnectorModal();
    toast('Connectors unlocked', 'success');
    if (!state.connectorsLoaded) {
      state.connectorsLoaded = true;
      loadConnectors();
    }
  } catch (e) {
    errEl.textContent = e.message;
    errEl.classList.add('is-error');
  }
}

// Clears a stale/expired/wrong-user unlock token and re-prompts, used when a
// /connectors/* call comes back 403 even though the normal auth token is fine.
function handleConnectorsLockError(res) {
  if (res.status === 403) {
    state.connectorsUnlockToken = null;
    localStorage.removeItem('rag_connectors_unlock_token');
    state.connectorsLoaded = false;
    showConnectorsUnlockPrompt();
    return true;
  }
  return false;
}

/* ── Drag & Drop ─────────────────────────────────────────────── */
function setupDragDrop() {
  const zone    = document.getElementById('drop-zone');
  const overlay = document.getElementById('drag-overlay');
  let leaveTimer = null;

  const dropTitle = zone.querySelector('.drop-title');
  const origTitle = dropTitle ? dropTitle.textContent : '';

  function isFileDrag(e) {
    try { return [...e.dataTransfer.types].includes('Files'); } catch { return false; }
  }
  function processFiles(e) {
    const files = e.dataTransfer ? [...e.dataTransfer.files] : [];
    if (files.length > 0) addFiles(files);
  }
  function showDrag() {
    clearTimeout(leaveTimer);
    overlay.classList.remove('hidden');
    zone.classList.add('dragover');
    if (dropTitle) dropTitle.textContent = 'Release to drop';
  }
  function hideDrag() {
    clearTimeout(leaveTimer);
    overlay.classList.add('hidden');
    zone.classList.remove('dragover');
    if (dropTitle) dropTitle.textContent = origTitle;
  }

  // ── Single drop handler on document catches everything ───────
  document.addEventListener('dragenter', e => {
    if (!isFileDrag(e)) return;
    e.preventDefault();
    showDrag();
  });
  document.addEventListener('dragover', e => {
    if (!isFileDrag(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  });
  document.addEventListener('dragleave', e => {
    if (!isFileDrag(e)) return;
    leaveTimer = setTimeout(() => hideDrag(), 50);
  });
  document.addEventListener('drop', e => {
    if (!e.dataTransfer) return;
    e.preventDefault();
    e.stopPropagation();
    hideDrag();
    processFiles(e);
  });

  // ── Zone click → open file browser ───────────────────────────
  zone.addEventListener('click', e => {
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
const MAX_FILES      = 5;
const MAX_MB         = 15;
const MAX_SESSION_MB = 50;
const ALLOWED        = ['.pdf', '.doc', '.docx', '.txt', '.md', '.xlsx', '.xls', '.csv'];

function totalSizeBytes() {
  return state.files.reduce((sum, f) => sum + f.size, 0);
}

function addFiles(newFiles) {
  const allowed = newFiles.filter(f => {
    const ext = '.' + f.name.split('.').pop().toLowerCase();
    if (!ALLOWED.includes(ext)) {
      toast(`${f.name}: unsupported format (allowed: PDF, DOC, DOCX, TXT, MD, XLSX, XLS, CSV)`, 'error');
      return false;
    }
    if (f.size > MAX_MB * 1024 * 1024) {
      toast(`${f.name}: exceeds ${MAX_MB} MB per-file limit`, 'error');
      return false;
    }
    return true;
  });

  const combined = [...state.files, ...allowed];
  if (combined.length > MAX_FILES) {
    toast(`Maximum ${MAX_FILES} files per session - extra files skipped`, 'error');
    state.files = combined.slice(0, MAX_FILES);
  } else {
    state.files = combined;
  }

  const totalMB = totalSizeBytes() / (1024 * 1024);
  if (totalMB > MAX_SESSION_MB) {
    toast(`Total size ${totalMB.toFixed(1)} MB exceeds the ${MAX_SESSION_MB} MB session limit`, 'error');
    // Remove files until we're under the limit
    while (totalSizeBytes() > MAX_SESSION_MB * 1024 * 1024 && state.files.length > 0) {
      state.files.pop();
    }
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
    const ext  = file.name.split('.').pop().toUpperCase();
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

  if (state.files.length > 0) {
    const usedMB  = totalSizeBytes() / (1024 * 1024);
    const pct     = Math.min(100, (usedMB / MAX_SESSION_MB) * 100).toFixed(1);
    const fillCls = pct >= 90 ? 'danger' : pct >= 70 ? 'warn' : '';
    list.innerHTML += `
      <div class="session-size-bar">
        <span>${usedMB.toFixed(1)} MB of ${MAX_SESSION_MB} MB</span>
        <div class="bar-track"><div class="bar-fill ${fillCls}" style="width:${pct}%"></div></div>
      </div>`;
  }
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
    <input type="text" class="header-key" placeholder="Key (e.g. Authorization)" aria-label="Header key" />
    <input type="text" class="header-val" placeholder="Value" aria-label="Header value" />
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
      <div class="bot-avatar">AR</div>
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

  addWarning(message) {
    const row = document.createElement('div');
    row.className = 'progress-step skipped';
    row.innerHTML = `<span class="ps-icon"></span><span class="ps-label">${escHtml(message)}</span>`;
    this._stepsEl.appendChild(row);
    scrollToBottom();
  }

  complete(totalElapsed, message = 'Document processed') {
    Object.values(this._steps).forEach(s => clearInterval(s.interval));
    this._totalEl.classList.remove('hidden');
    this._totalEl.textContent = `${message} - ${totalElapsed.toFixed(2)}s`;
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
  let sawTerminalEvent = false;

  const wrappedOnEvent = (ev) => {
    if (ev.type === 'complete' || ev.type === 'error') sawTerminalEvent = true;
    onEvent(ev);
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop();
    for (const line of lines) {
      if (line.startsWith('data: ')) {
        try { wrappedOnEvent(JSON.parse(line.slice(6))); } catch (_) {}
      }
    }
  }

  if (!sawTerminalEvent) {
    // Connection closed mid-processing (e.g. an unhandled server error) without
    // ever sending a complete/error event - surface it instead of hanging silently.
    onEvent({ type: 'error', message: 'Connection lost while processing. Please try again.' });
  }
}

function handleProgressEvent(card, ev) {
  if (ev.type === 'step') {
    if (ev.title) card.setTitle(ev.title);
    if (ev.status === 'start') card.addStep(ev.stage, ev.message);
    else if (ev.status === 'done') card.doneStep(ev.stage, ev.message, ev.elapsed ?? 0);
    else if (ev.status === 'error') card.errorStep(ev.stage, ev.message);
  } else if (ev.type === 'dedup_confirm') {
    handleDedupConfirm(card, ev);
  } else if (ev.type === 'warning') {
    card.addWarning(ev.message);
    toast(ev.message, 'warning');
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

/* ── Dedup confirmation ──────────────────────────────────────── */
function handleDedupConfirm(card, ev) {
  // Stop the "Checking…" spinner for this stage
  const prev = card._steps[ev.stage];
  if (prev) {
    clearInterval(prev.interval);
    prev.row.className = 'progress-step done';
    prev.row.querySelector('.ps-label').textContent = `${escHtml(ev.filename)} - duplicate found`;
    prev.row.querySelector('.ps-timer').textContent = '';
  }

  // Confirm card uses its own class to avoid .progress-step grid conflict
  const row = document.createElement('div');
  row.className = 'dedup-confirm-card';
  row.dataset.confirmToken = ev.confirm_token;
  row.innerHTML = `
    <div class="dedup-title">${escHtml(ev.filename)}</div>
    <div class="dedup-meta">${escHtml(ev.file_size)} · ${ev.chunks_stored || 0} chunks already stored</div>
    <div class="dedup-buttons">
      <button class="dedup-btn reuse" onclick="sendDedupAction('${ev.confirm_token}', 'reuse', this)">Use existing</button>
      <button class="dedup-btn reprocess" onclick="sendDedupAction('${ev.confirm_token}', 'reprocess', this)">Re-upload</button>
    </div>
    <div class="dedup-countdown">Auto-reprocessing in <span class="countdown-num">60</span>s</div>`;
  card._stepsEl.appendChild(row);
  scrollToBottom();

  // Store separately - do NOT overwrite card._steps[ev.stage].
  // doneStep() must still operate on the original "✓ duplicate found" row.
  card._confirmCards = card._confirmCards || {};
  card._confirmCards[ev.stage] = row;

  let remaining = 60;
  const countdownEl = row.querySelector('.countdown-num');
  const interval = setInterval(() => {
    remaining--;
    if (countdownEl) countdownEl.textContent = remaining;
    if (remaining <= 0) {
      clearInterval(interval);
      const cd = row.querySelector('.dedup-countdown');
      if (cd) { cd.className = 'dedup-countdown expired'; cd.textContent = 'Timed out - reprocessing'; }
    }
  }, 1000);
  row._dedupInterval = interval;
}

async function sendDedupAction(confirmToken, action, btn) {
  const row = btn.closest('.dedup-confirm-card');
  row.querySelectorAll('.dedup-btn').forEach(b => { b.disabled = true; });
  clearInterval(row._dedupInterval);

  try {
    const res = await fetch('/upload/confirm', {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ confirm_token: confirmToken, action }),
    });
    if (!res.ok) {
      const data = await safeJson(res);
      toast(data?.detail || 'Confirmation failed', 'error');
    } else {
      row.remove();
    }
  } catch (e) {
    toast(e.message, 'error');
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
      const res  = await fetch('/upload/api', { method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }), body: JSON.stringify({ url, headers: apiHeaders, session_id: state.sessionId || undefined }) });
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
    const res = await fetch('/upload/files', {
      method: 'POST',
      body: form,
      headers: authHeaders({
        'X-Client-Token': state.clientToken || 'anonymous',
        ...(state.sessionId ? { 'X-Session-Id': state.sessionId } : {}),
      }),
    });
    if (!res.ok) {
      if (handleAuthError(res)) return;
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
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ url, session_id: state.sessionId || undefined }),
    });
    if (!res.ok) {
      if (handleAuthError(res)) return;
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
  appendMessage('bot', "Your data is ready! I'm ARIA - ask me anything about it.");
}

/* ── Connectors ──────────────────────────────────────────────── */
// Brand-flavored glyphs so each connector reads like an app-directory icon
// (own simplified recreations, not exact trademarked artwork).
function _connectorSvg(inner, viewBox = '0 0 24 24') {
  return `<svg width="18" height="18" viewBox="${viewBox}">${inner}</svg>`;
}

const CONNECTOR_ICON = {
  file_share: _connectorSvg('<path fill="#f59e0b" d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>'),

  lms: _connectorSvg('<path fill="#0d9488" d="M6.5 2A2.5 2.5 0 0 0 4 4.5v15A2.5 2.5 0 0 0 6.5 22H20V2H6.5zm0 2H18v14.09A2.49 2.49 0 0 0 17 17.9V4.1a2.5 2.5 0 0 0-10.5 0zM6 18h11v2H6.5a.5.5 0 0 1 0-1H6z"/>'),

  // Simplified Octocat silhouette (the near-universal open-source GitHub mark).
  github: _connectorSvg('<path fill="#181717" d="M12 .5C5.73.5.5 5.73.5 12c0 5.08 3.29 9.39 7.86 10.91.58.1.79-.25.79-.56 0-.27-.01-1.17-.02-2.12-3.2.7-3.88-1.36-3.88-1.36-.53-1.33-1.29-1.69-1.29-1.69-1.05-.72.08-.71.08-.71 1.16.08 1.78 1.19 1.78 1.19 1.03 1.77 2.7 1.26 3.36.96.1-.75.4-1.26.73-1.55-2.56-.29-5.26-1.28-5.26-5.69 0-1.26.45-2.29 1.19-3.09-.12-.29-.52-1.46.11-3.05 0 0 .97-.31 3.18 1.18a11.06 11.06 0 0 1 5.8 0c2.2-1.49 3.17-1.18 3.17-1.18.64 1.59.24 2.76.12 3.05.74.8 1.19 1.83 1.19 3.09 0 4.42-2.71 5.39-5.29 5.68.42.36.78 1.08.78 2.18 0 1.57-.01 2.83-.01 3.22 0 .31.21.67.8.56C19.72 21.39 23 17.08 23 12 23 5.73 17.77.5 12 .5z"/>'),

  gmail: _connectorSvg('<path fill="#EA4335" d="M2 6.5A1.5 1.5 0 0 1 3.5 5h17A1.5 1.5 0 0 1 22 6.5v11a1.5 1.5 0 0 1-1.5 1.5h-17A1.5 1.5 0 0 1 2 17.5v-11z"/><path fill="#fff" d="m4 6 8 6 8-6"/><path fill="#EA4335" d="M4 6v11.3L9.5 12 4 6zm16 0-5.5 6L20 17.3V6z" opacity=".55"/>'),

  google_drive: _connectorSvg('<path fill="#0066DA" d="M7.7 2 1 13.8l3.35 5.8L11 7.8z"/><path fill="#00AC47" d="m4.35 19.6 3.35 5.8h13.3l-3.35-5.8z" transform="translate(0 -5.8)"/><path fill="#EA4335" d="M11 7.8 17.65 19.6H24.65L17.95 2z"/><path fill="#FFBA00" d="M4.35 19.6h13.3l-3.35-5.8H7.7z"/>', '0 0 26 26'),

  sharepoint: _connectorSvg('<circle cx="9" cy="8" r="6" fill="#036C70"/><circle cx="16" cy="9" r="5" fill="#1A9BA1"/><circle cx="11" cy="16" r="5.5" fill="#37C6D0"/><path fill="#fff" d="M9.2 13.4h5.2a2 2 0 0 1 2 2v.3a4.1 4.1 0 0 1-4.1 4.1H9.2a1 1 0 0 1-1-1v-4.4a1 1 0 0 1 1-1z"/>'),

  teams: _connectorSvg('<path fill="#5059C9" d="M16.5 10.5a2.75 2.75 0 1 0 0-5.5 2.75 2.75 0 0 0 0 5.5zM20 11.5h-2.75a.75.75 0 0 0-.75.75v4.5A3.25 3.25 0 0 0 19.75 20 2.25 2.25 0 0 0 22 17.75V13.5a2 2 0 0 0-2-2z"/><circle cx="9.5" cy="7" r="3.5" fill="#7B83EB"/><path fill="#7B83EB" d="M14 12H5a2 2 0 0 0-2 2v3.5A4.5 4.5 0 0 0 7.5 22h1A5.5 5.5 0 0 0 14 16.5V13a1 1 0 0 0-1-1z"/>'),

  azure_devops: _connectorSvg('<path fill="#0078D4" d="M23 6.1 18 2v3l-9 2.3v7L4 16V9.3L1 10v7.2l5 2L18.1 15v3L23 13.9z"/>'),

  jira: _connectorSvg('<path fill="#2684FF" d="M23.2 11.3 12.9.9a1 1 0 0 0-1.4 0L1.2 11.3a1 1 0 0 0 0 1.4L11.5 23a1 1 0 0 0 1.4 0l10.3-10.3a1 1 0 0 0 0-1.4z" opacity="0"/><path fill="#2684FF" d="M12 1 6.3 6.7A1 1 0 0 0 6.3 8.1L12 13.8 17.7 8.1a1 1 0 0 0 0-1.4z"/><path fill="#2684FF" d="M12 10.2 6.3 15.9a1 1 0 0 0 0 1.4L12 23l5.7-5.7a1 1 0 0 0 0-1.4z" opacity=".55"/>'),

  servicenow: _connectorSvg('<circle cx="12" cy="12" r="11" fill="#62D84E"/><path fill="#fff" d="M7 14.2a1.4 1.4 0 0 1 2-1.9l2.3 2.1a1 1 0 0 0 1.4 0l2.3-2.1a1.4 1.4 0 1 1 1.9 2.1l-3 2.7a2.7 2.7 0 0 1-3.7 0z"/><path fill="#293e40" d="M7 10.6a1.4 1.4 0 0 1 1.9-2l2.3 2a1 1 0 0 0 1.4 0l2.3-2a1.4 1.4 0 0 1 1.9 2l-3 2.7a2.7 2.7 0 0 1-3.7 0z" opacity=".35"/>'),

  salesforce: _connectorSvg('<path fill="#00A1E0" d="M9.9 6.4a3.9 3.9 0 0 1 6.9 1.5 3.3 3.3 0 0 1 4.6 3.1 3.3 3.3 0 0 1-1.1 6.4H7a4.3 4.3 0 0 1-1-8.4A4.2 4.2 0 0 1 9.9 6.4z"/>'),
};

async function loadConnectors() {
  const list = document.getElementById('connector-list');
  try {
    const res = await fetch('/connectors/status', { headers: connectorHeaders() });
    if (handleAuthError(res)) return;
    if (handleConnectorsLockError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);
    state.connectors = data.sources || [];
    renderConnectors();
  } catch (e) {
    list.innerHTML = `<div class="connector-empty">Couldn't load connectors: ${escHtml(e.message)}</div>`;
  }
}

function renderConnectors() {
  document.getElementById('connector-list').innerHTML =
    state.connectors.map(connectorRowHtml).join('');
}

function connectorRowHtml(c) {
  const badge = c.connected
    ? '<span class="connector-badge connected">Connected</span>'
    : (c.configured
        ? (c.auth === 'oauth' ? '<span class="connector-badge not-connected">Not connected</span>' : '')
        : '<span class="connector-badge not-configured">Not configured</span>');

  const action = (c.auth === 'oauth')
    ? (c.connected
        ? `<button class="connector-action-btn" onclick="toggleBrowse('${c.id}')">Browse</button>`
        : `<button class="connector-action-btn" ${c.configured ? '' : 'disabled'} onclick="connectOAuth('${c.path}')">Connect</button>`)
    : `<button class="connector-action-btn" ${c.configured ? '' : 'disabled'} onclick="toggleBrowse('${c.id}')">Browse</button>`;

  return `
    <div class="connector-row">
      <div class="connector-row-main">
        <div class="connector-icon">${CONNECTOR_ICON[c.id] || escHtml(c.name.slice(0, 2).toUpperCase())}</div>
        <div class="connector-info">
          <div class="connector-name">${escHtml(c.name)}</div>
          <div class="connector-sub">${escHtml(c.type.replace(/_/g, ' '))}</div>
        </div>
        ${badge}
        ${action}
      </div>
    </div>`;
}

async function connectOAuth(path) {
  try {
    const res = await fetch(`/connectors/${path}/auth/login`, { headers: connectorHeaders() });
    if (handleAuthError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);
    window.location.href = data.auth_url;
  } catch (e) {
    toast(e.message, 'error');
  }
}

// Opens the browse popup for a connector. Each provider's existing browse
// functions are unchanged - they render into whatever element currently has
// id `connector-browse-<id>`, so we create that single container fresh inside
// the modal body each time it opens (only one connector is browsed at once).
function openConnectorModal(id) {
  const meta = state.connectors.find(c => c.id === id);
  document.getElementById('connector-modal-title').textContent = meta ? `Browse ${meta.name}` : 'Browse';

  const body = document.getElementById('connector-modal-body');
  body.innerHTML = `<div id="connector-browse-${id}"></div>`;

  document.getElementById('connector-modal').classList.remove('hidden');
  document.addEventListener('keydown', _connectorModalEscHandler);

  if (id === 'file_share') return browseFileShare('');
  if (id === 'lms') return browseLmsCourses();
  if (id === 'github') return browseGithubRepos();
  if (id === 'gmail') return browseGmailMessages();
  if (id === 'google_drive') return browseGoogleDriveRoot();
  if (id === 'jira') return browseJiraProjects();
  if (id === 'servicenow') return browseServiceNowIncidents();
  if (id === 'salesforce') return browseSalesforceCategories();
  if (id === 'sharepoint') return browseSharepointSites();
  if (id === 'teams') return browseTeamsList();
  if (id === 'azure_devops') return browseAzureDevOpsProjects();
  body.innerHTML = '<div class="connector-browse-note">Browsing UI for this source isn\'t built yet - ' +
    'once you add real credentials for it, ask to have the picker wired up next. The API itself already ' +
    'works (see <code>/docs</code>).</div>';
}
// Kept as the name used by existing onclick="toggleBrowse(...)" attributes.
function toggleBrowse(id) {
  openConnectorModal(id);
}

function _connectorModalEscHandler(e) {
  if (e.key === 'Escape') closeConnectorModal();
}

function closeConnectorModal() {
  document.getElementById('connector-modal').classList.add('hidden');
  document.getElementById('connector-modal-body').innerHTML = '';
  document.removeEventListener('keydown', _connectorModalEscHandler);
}

/* ── File Share browser ──────────────────────────────────────── */
async function browseFileShare(path) {
  const el = document.getElementById('connector-browse-file_share');
  el.innerHTML = '<div class="connector-browse-note">Loading…</div>';
  try {
    const res = await fetch(`/connectors/file-share/files?path=${encodeURIComponent(path)}`, { headers: connectorHeaders() });
    if (handleAuthError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);

    const crumbs = breadcrumbHtml(path, p => `browseFileShare('${escAttr(p)}')`);
    const rows = (data.items || []).map(item => item.type === 'folder'
      ? `<div class="connector-item-row is-folder" onclick="browseFileShare('${escAttr(item.path)}')">
           <span class="ci-name">📁 ${escHtml(item.name)}</span>
         </div>`
      : `<div class="connector-item-row" onclick="ingestFileShareFile('${escAttr(item.path)}', '${escAttr(item.name)}')">
           <span class="ci-name">📄 ${escHtml(item.name)}</span>
           <span class="ci-meta">${item.size != null ? formatBytes(item.size) : ''}</span>
         </div>`
    ).join('') || '<div class="connector-browse-note">Empty folder</div>';

    el.innerHTML = crumbs + rows;
  } catch (e) {
    el.innerHTML = `<div class="connector-browse-note">${escHtml(e.message)}</div>`;
  }
}

async function ingestFileShareFile(path, name) {
  await runConnectorIngest('file_share', { path }, name);
}

/* ── LMS browser ─────────────────────────────────────────────── */
async function browseLmsCourses() {
  const el = document.getElementById('connector-browse-lms');
  el.innerHTML = '<div class="connector-browse-note">Loading…</div>';
  try {
    const res = await fetch('/connectors/lms/courses', { headers: connectorHeaders() });
    if (handleAuthError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);
    el.innerHTML = (data.items || []).map(c => `
      <div class="connector-item-row is-folder" onclick="browseLmsFiles('${escAttr(c.id)}', '${escAttr(c.name)}')">
        <span class="ci-name">📁 ${escHtml(c.name)}</span>
        <span class="ci-meta">${c.module_count} modules</span>
      </div>`).join('') || '<div class="connector-browse-note">No courses</div>';
  } catch (e) {
    el.innerHTML = `<div class="connector-browse-note">${escHtml(e.message)}</div>`;
  }
}

async function browseLmsFiles(courseId, courseName) {
  const el = document.getElementById('connector-browse-lms');
  el.innerHTML = '<div class="connector-browse-note">Loading…</div>';
  try {
    const res = await fetch(`/connectors/lms/courses/${encodeURIComponent(courseId)}/files`, { headers: connectorHeaders() });
    if (handleAuthError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);
    const crumbs = `<div class="connector-breadcrumb"><a class="crumb" onclick="browseLmsCourses()">Courses</a> / ${escHtml(courseName)}</div>`;
    const rows = (data.items || []).map(f => `
      <div class="connector-item-row" onclick="ingestLmsFile('${escAttr(f.id)}', '${escAttr(f.name)}')">
        <span class="ci-name">📄 ${escHtml(f.name)}</span>
        <span class="ci-meta">${f.size != null ? formatBytes(f.size) : ''}</span>
      </div>`).join('') || '<div class="connector-browse-note">No files in this course</div>';
    el.innerHTML = crumbs + rows;
  } catch (e) {
    el.innerHTML = `<div class="connector-browse-note">${escHtml(e.message)}</div>`;
  }
}

async function ingestLmsFile(fileId, name) {
  await runConnectorIngest('lms', { file_id: fileId }, name);
}

/* ── GitHub browser ──────────────────────────────────────────── */
async function browseGithubRepos() {
  const el = document.getElementById('connector-browse-github');
  el.innerHTML = '<div class="connector-browse-note">Loading…</div>';
  try {
    const res = await fetch('/connectors/github/repos?per_page=100', { headers: connectorHeaders() });
    if (handleAuthError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);
    el.innerHTML = (data.items || []).map(r => `
      <div class="connector-item-row is-folder" onclick="browseGithubContents('${escAttr(r.owner)}', '${escAttr(r.name)}', '${escAttr(r.default_branch)}', '')">
        <span class="ci-name">📁 ${escHtml(r.full_name)}</span>
        <span class="ci-meta">${r.private ? 'Private' : 'Public'}</span>
      </div>`).join('') || '<div class="connector-browse-note">No repositories</div>';
  } catch (e) {
    el.innerHTML = `<div class="connector-browse-note">${escHtml(e.message)}</div>`;
  }
}

async function browseGithubContents(owner, repo, ref, path) {
  const el = document.getElementById('connector-browse-github');
  el.innerHTML = '<div class="connector-browse-note">Loading…</div>';
  try {
    const params = new URLSearchParams({ path });
    if (ref) params.set('ref', ref);
    const res = await fetch(
      `/connectors/github/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/contents?${params}`,
      { headers: connectorHeaders() },
    );
    if (handleAuthError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);

    const crumbs = [
      `<a class="crumb" onclick="browseGithubRepos()">Repos</a>`,
      `<a class="crumb" onclick="browseGithubContents('${escAttr(owner)}', '${escAttr(repo)}', '${escAttr(ref)}', '')">${escHtml(owner)}/${escHtml(repo)}</a>`,
    ];
    let acc = '';
    path.split('/').filter(Boolean).forEach(p => {
      acc = acc ? `${acc}/${p}` : p;
      crumbs.push(`<a class="crumb" onclick="browseGithubContents('${escAttr(owner)}', '${escAttr(repo)}', '${escAttr(ref)}', '${escAttr(acc)}')">${escHtml(p)}</a>`);
    });

    const rows = (data.items || []).map(item => item.type === 'dir'
      ? `<div class="connector-item-row is-folder" onclick="browseGithubContents('${escAttr(owner)}', '${escAttr(repo)}', '${escAttr(ref)}', '${escAttr(item.path)}')">
           <span class="ci-name">📁 ${escHtml(item.name)}</span>
         </div>`
      : `<div class="connector-item-row" onclick="ingestGithubFile('${escAttr(owner)}', '${escAttr(repo)}', '${escAttr(item.path)}', '${escAttr(ref)}', '${escAttr(item.name)}')">
           <span class="ci-name">📄 ${escHtml(item.name)}</span>
           <span class="ci-meta">${item.size != null ? formatBytes(item.size) : ''}</span>
         </div>`
    ).join('') || '<div class="connector-browse-note">Empty folder</div>';

    el.innerHTML = `<div class="connector-breadcrumb">${crumbs.join(' / ')}</div>` + rows;
  } catch (e) {
    el.innerHTML = `<div class="connector-browse-note">${escHtml(e.message)}</div>`;
  }
}

async function ingestGithubFile(owner, repo, path, ref, name) {
  await runConnectorIngest('github', { owner, repo, path, ref: ref || null }, name);
}

/* ── Gmail browser ────────────────────────────────────────────── */
let _gmailNextToken = null;

async function browseGmailMessages(pageToken = '', append = false) {
  const el = document.getElementById('connector-browse-gmail');
  if (!append) el.innerHTML = '<div class="connector-browse-note">Loading…</div>';
  try {
    const params = new URLSearchParams({ max_results: '50' });
    if (pageToken) params.set('page_token', pageToken);
    const res = await fetch(`/connectors/gmail/messages?${params}`, { headers: connectorHeaders() });
    if (handleAuthError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);

    const rows = (data.messages || []).map(m => `
      <div class="connector-item-row" onclick="browseGmailMessage('${escAttr(m.id)}', '${escAttr(m.subject)}')">
        <span class="ci-name">✉️ ${escHtml(m.subject || '(No Subject)')}</span>
        <span class="ci-meta">${escHtml(m.from || '')}</span>
      </div>`).join('');

    _gmailNextToken = data.next_page_token || null;
    const loadMore = _gmailNextToken
      ? `<button class="connector-load-more" onclick="browseGmailMessages('${escAttr(_gmailNextToken)}', true)">Load more messages</button>`
      : '';

    if (append) {
      const prevBtn = el.querySelector('.connector-load-more');
      if (prevBtn) prevBtn.remove();
      el.insertAdjacentHTML('beforeend', rows + loadMore);
    } else {
      el.innerHTML = (rows + loadMore) || '<div class="connector-browse-note">No messages</div>';
    }
  } catch (e) {
    el.innerHTML = `<div class="connector-browse-note">${escHtml(e.message)}</div>`;
  }
}

async function browseGmailMessage(messageId, subject) {
  const el = document.getElementById('connector-browse-gmail');
  el.innerHTML = '<div class="connector-browse-note">Loading…</div>';
  try {
    const res = await fetch(`/connectors/gmail/messages/${encodeURIComponent(messageId)}`, { headers: connectorHeaders() });
    if (handleAuthError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);

    const crumbs = `<div class="connector-breadcrumb"><a class="crumb" onclick="browseGmailMessages()">Inbox</a> / ${escHtml(subject)}</div>`;
    const rows = (data.attachments || []).map(a => `
      <div class="connector-item-row" onclick="ingestGmailAttachment('${escAttr(messageId)}', '${escAttr(a.attachment_id)}', '${escAttr(a.filename)}')">
        <span class="ci-name">📎 ${escHtml(a.filename)}</span>
        <span class="ci-meta">${a.size != null ? formatBytes(a.size) : ''}</span>
      </div>`).join('') || '<div class="connector-browse-note">No attachments in this email - only attachments can be added right now.</div>';

    el.innerHTML = crumbs + rows;
  } catch (e) {
    el.innerHTML = `<div class="connector-browse-note">${escHtml(e.message)}</div>`;
  }
}

async function ingestGmailAttachment(messageId, attachmentId, filename) {
  await runConnectorIngest('gmail', { message_id: messageId, attachment_id: attachmentId, filename }, filename);
}

/* ── Google Drive browser ────────────────────────────────────── */
let _gdriveTrail = [];
let _gdriveNextToken = null;

async function browseGoogleDriveRoot() {
  _gdriveTrail = [{ id: 'root', name: 'My Drive' }];
  await loadGoogleDriveFolder();
}

async function browseGoogleDriveCrumb(index) {
  _gdriveTrail = _gdriveTrail.slice(0, index + 1);
  await loadGoogleDriveFolder();
}

async function browseGoogleDriveOpen(id, name) {
  _gdriveTrail.push({ id, name });
  await loadGoogleDriveFolder();
}

async function loadGoogleDriveFolder(pageToken = '', append = false) {
  const el = document.getElementById('connector-browse-google_drive');
  if (!append) el.innerHTML = '<div class="connector-browse-note">Loading…</div>';
  const current = _gdriveTrail[_gdriveTrail.length - 1];
  try {
    const q = `'${current.id}' in parents and trashed = false`;
    const params = new URLSearchParams({ max_results: '100', q });
    if (pageToken) params.set('page_token', pageToken);
    const res = await fetch(`/connectors/google-drive/files?${params}`, { headers: connectorHeaders() });
    if (handleAuthError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);

    const crumbs = `<div class="connector-breadcrumb">${_gdriveTrail.map((t, i) =>
      `<a class="crumb" onclick="browseGoogleDriveCrumb(${i})">${escHtml(t.name)}</a>`).join(' / ')}</div>`;

    const rows = (data.files || []).map(f => f.mimeType === 'application/vnd.google-apps.folder'
      ? `<div class="connector-item-row is-folder" onclick="browseGoogleDriveOpen('${escAttr(f.id)}', '${escAttr(f.name)}')">
           <span class="ci-name">📁 ${escHtml(f.name)}</span>
         </div>`
      : `<div class="connector-item-row" onclick="ingestGoogleDriveFile('${escAttr(f.id)}', '${escAttr(f.name)}')">
           <span class="ci-name">📄 ${escHtml(f.name)}</span>
           <span class="ci-meta">${f.size != null ? formatBytes(f.size) : ''}</span>
         </div>`
    ).join('');

    _gdriveNextToken = data.next_page_token || null;
    const loadMore = _gdriveNextToken
      ? `<button class="connector-load-more" onclick="loadGoogleDriveFolder('${escAttr(_gdriveNextToken)}', true)">Load more</button>`
      : '';

    if (append) {
      const prevBtn = el.querySelector('.connector-load-more');
      if (prevBtn) prevBtn.remove();
      el.insertAdjacentHTML('beforeend', rows + loadMore);
    } else {
      el.innerHTML = crumbs + ((rows + loadMore) || '<div class="connector-browse-note">Empty folder</div>');
    }
  } catch (e) {
    el.innerHTML = `<div class="connector-browse-note">${escHtml(e.message)}</div>`;
  }
}

async function ingestGoogleDriveFile(fileId, name) {
  await runConnectorIngest('google_drive', { file_id: fileId }, name);
}

/* ── Jira browser ─────────────────────────────────────────────── */
async function browseJiraProjects() {
  const el = document.getElementById('connector-browse-jira');
  el.innerHTML = '<div class="connector-browse-note">Loading…</div>';
  try {
    const res = await fetch('/connectors/jira/projects', { headers: connectorHeaders() });
    if (handleAuthError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);
    el.innerHTML = (data.projects || []).map(p => `
      <div class="connector-item-row is-folder" onclick="browseJiraIssues('${escAttr(p.key)}', '${escAttr(p.name)}')">
        <span class="ci-name">📁 ${escHtml(p.name)}</span>
        <span class="ci-meta">${escHtml(p.key)}</span>
      </div>`).join('') || '<div class="connector-browse-note">No projects</div>';
  } catch (e) {
    el.innerHTML = `<div class="connector-browse-note">${escHtml(e.message)}</div>`;
  }
}

async function browseJiraIssues(projectKey, projectName) {
  const el = document.getElementById('connector-browse-jira');
  el.innerHTML = '<div class="connector-browse-note">Loading…</div>';
  try {
    const jql = `project = ${projectKey} order by created DESC`;
    const res = await fetch(`/connectors/jira/issues?jql=${encodeURIComponent(jql)}&max_results=20`, { headers: connectorHeaders() });
    if (handleAuthError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);

    const crumbs = `<div class="connector-breadcrumb"><a class="crumb" onclick="browseJiraProjects()">Projects</a> / ${escHtml(projectName)}</div>`;
    const rows = (data.issues || []).map(i => `
      <div class="connector-item-row" onclick="ingestJiraIssue('${escAttr(i.key)}', '${escAttr(i.summary)}')">
        <span class="ci-name">🎫 ${escHtml(i.key)} - ${escHtml(i.summary)}</span>
        <span class="ci-meta">${escHtml(i.status)}</span>
      </div>`).join('') || '<div class="connector-browse-note">No issues in this project</div>';

    el.innerHTML = crumbs + rows;
  } catch (e) {
    el.innerHTML = `<div class="connector-browse-note">${escHtml(e.message)}</div>`;
  }
}

async function ingestJiraIssue(issueKey, summary) {
  await runConnectorIngest('jira', { issue_key: issueKey }, `${issueKey} - ${summary}`);
}

/* ── ServiceNow browser ───────────────────────────────────────── */
// Flat list - no drill-down needed, pick an incident straight from the list.
async function browseServiceNowIncidents() {
  const el = document.getElementById('connector-browse-servicenow');
  el.innerHTML = '<div class="connector-browse-note">Loading…</div>';
  try {
    const res = await fetch('/connectors/servicenow/incidents?max_results=25', { headers: connectorHeaders() });
    if (handleAuthError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);
    el.innerHTML = (data.incidents || []).map(i => `
      <div class="connector-item-row" onclick="ingestServiceNowIncident('${escAttr(i.sys_id)}', '${escAttr(i.number)}', '${escAttr(i.short_description)}')">
        <span class="ci-name">🛠️ ${escHtml(i.number)} - ${escHtml(i.short_description)}</span>
        <span class="ci-meta">${escHtml(i.state)}${i.priority ? ' · ' + escHtml(i.priority) : ''}</span>
      </div>`).join('') || '<div class="connector-browse-note">No incidents</div>';
  } catch (e) {
    el.innerHTML = `<div class="connector-browse-note">${escHtml(e.message)}</div>`;
  }
}

async function ingestServiceNowIncident(sysId, number, shortDescription) {
  await runConnectorIngest('servicenow', { sys_id: sysId }, `${number} - ${shortDescription}`);
}

/* ── Salesforce browser ───────────────────────────────────────── */
const SF_CATEGORIES = [
  { key: 'accounts', sobject: 'Account', label: 'Accounts' },
  { key: 'leads', sobject: 'Lead', label: 'Leads' },
  { key: 'opportunities', sobject: 'Opportunity', label: 'Opportunities' },
];

function browseSalesforceCategories() {
  const el = document.getElementById('connector-browse-salesforce');
  el.innerHTML = SF_CATEGORIES.map(c => `
    <div class="connector-item-row is-folder" onclick="browseSalesforceRecords('${c.key}')">
      <span class="ci-name">📁 ${escHtml(c.label)}</span>
    </div>`).join('');
}

function sfRecordMeta(sobject, r) {
  if (sobject === 'Account') return r.Industry || r.Type || '';
  if (sobject === 'Lead') return [r.Company, r.Status].filter(Boolean).join(' - ');
  if (sobject === 'Opportunity') return [r.StageName, r.Amount != null ? `$${r.Amount.toLocaleString()}` : null].filter(Boolean).join(' - ');
  return '';
}

async function browseSalesforceRecords(categoryKey) {
  const cat = SF_CATEGORIES.find(c => c.key === categoryKey);
  const el = document.getElementById('connector-browse-salesforce');
  el.innerHTML = '<div class="connector-browse-note">Loading…</div>';
  try {
    const res = await fetch(`/connectors/salesforce/${cat.key}?max_results=20`, { headers: connectorHeaders() });
    if (handleAuthError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);

    const crumbs = `<div class="connector-breadcrumb"><a class="crumb" onclick="browseSalesforceCategories()">Salesforce</a> / ${escHtml(cat.label)}</div>`;
    const rows = (data.records || []).map(r => `
      <div class="connector-item-row" onclick="ingestSalesforceRecord('${escAttr(cat.sobject)}', '${escAttr(r.Id)}', '${escAttr(r.Name || r.Id)}')">
        <span class="ci-name">${escHtml(r.Name || r.Id)}</span>
        <span class="ci-meta">${escHtml(sfRecordMeta(cat.sobject, r))}</span>
      </div>`).join('') || `<div class="connector-browse-note">No ${escHtml(cat.label.toLowerCase())}</div>`;

    el.innerHTML = crumbs + rows;
  } catch (e) {
    el.innerHTML = `<div class="connector-browse-note">${escHtml(e.message)}</div>`;
  }
}

async function ingestSalesforceRecord(sobject, recordId, name) {
  await runConnectorIngest('salesforce', { sobject, record_id: recordId }, name);
}

/* ── SharePoint browser ───────────────────────────────────────── */
let _spState = { siteId: '', siteName: '', driveId: '', driveName: '', trail: [] };

async function browseSharepointSites() {
  const el = document.getElementById('connector-browse-sharepoint');
  el.innerHTML = '<div class="connector-browse-note">Loading…</div>';
  try {
    const res = await fetch('/connectors/sharepoint/sites', { headers: connectorHeaders() });
    if (handleAuthError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);
    el.innerHTML = (data.value || []).map(s => `
      <div class="connector-item-row is-folder" onclick="browseSharepointDrives('${escAttr(s.id)}', '${escAttr(s.displayName || s.name)}')">
        <span class="ci-name">📁 ${escHtml(s.displayName || s.name)}</span>
      </div>`).join('') || '<div class="connector-browse-note">No sites</div>';
  } catch (e) {
    el.innerHTML = `<div class="connector-browse-note">${escHtml(e.message)}</div>`;
  }
}

async function browseSharepointDrives(siteId, siteName) {
  _spState = { siteId, siteName, driveId: '', driveName: '', trail: [] };
  const el = document.getElementById('connector-browse-sharepoint');
  el.innerHTML = '<div class="connector-browse-note">Loading…</div>';
  try {
    const res = await fetch(`/connectors/sharepoint/sites/${encodeURIComponent(siteId)}/drives`, { headers: connectorHeaders() });
    if (handleAuthError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);

    const crumbs = `<div class="connector-breadcrumb"><a class="crumb" onclick="browseSharepointSites()">Sites</a> / ${escHtml(siteName)}</div>`;
    const rows = (data.value || []).map(d => `
      <div class="connector-item-row is-folder" onclick="browseSharepointOpenDrive('${escAttr(d.id)}', '${escAttr(d.name)}')">
        <span class="ci-name">📁 ${escHtml(d.name)}</span>
      </div>`).join('') || '<div class="connector-browse-note">No document libraries on this site</div>';

    el.innerHTML = crumbs + rows;
  } catch (e) {
    el.innerHTML = `<div class="connector-browse-note">${escHtml(e.message)}</div>`;
  }
}

async function browseSharepointOpenDrive(driveId, driveName) {
  _spState.driveId = driveId;
  _spState.driveName = driveName;
  _spState.trail = [{ id: 'root', name: driveName }];
  await loadSharepointItems();
}

async function browseSharepointOpenFolder(folderId, folderName) {
  _spState.trail.push({ id: folderId, name: folderName });
  await loadSharepointItems();
}

async function browseSharepointCrumb(index) {
  _spState.trail = _spState.trail.slice(0, index + 1);
  await loadSharepointItems();
}

async function loadSharepointItems() {
  const el = document.getElementById('connector-browse-sharepoint');
  el.innerHTML = '<div class="connector-browse-note">Loading…</div>';
  const current = _spState.trail[_spState.trail.length - 1];
  try {
    const res = await fetch(
      `/connectors/sharepoint/drives/${encodeURIComponent(_spState.driveId)}/items?folder_id=${encodeURIComponent(current.id)}`,
      { headers: connectorHeaders() },
    );
    if (handleAuthError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);

    const crumbs = [
      `<a class="crumb" onclick="browseSharepointSites()">Sites</a>`,
      `<a class="crumb" onclick="browseSharepointDrives('${escAttr(_spState.siteId)}', '${escAttr(_spState.siteName)}')">${escHtml(_spState.siteName)}</a>`,
      ..._spState.trail.map((t, i) => `<a class="crumb" onclick="browseSharepointCrumb(${i})">${escHtml(t.name)}</a>`),
    ];

    const rows = (data.value || []).map(item => item.folder
      ? `<div class="connector-item-row is-folder" onclick="browseSharepointOpenFolder('${escAttr(item.id)}', '${escAttr(item.name)}')">
           <span class="ci-name">📁 ${escHtml(item.name)}</span>
         </div>`
      : `<div class="connector-item-row" onclick="ingestSharepointFile('${escAttr(item.id)}', '${escAttr(item.name)}')">
           <span class="ci-name">📄 ${escHtml(item.name)}</span>
           <span class="ci-meta">${item.size != null ? formatBytes(item.size) : ''}</span>
         </div>`
    ).join('') || '<div class="connector-browse-note">Empty folder</div>';

    el.innerHTML = `<div class="connector-breadcrumb">${crumbs.join(' / ')}</div>` + rows;
  } catch (e) {
    el.innerHTML = `<div class="connector-browse-note">${escHtml(e.message)}</div>`;
  }
}

async function ingestSharepointFile(itemId, name) {
  await runConnectorIngest('sharepoint', { drive_id: _spState.driveId, item_id: itemId }, name);
}

/* ── Microsoft Teams browser ──────────────────────────────────── */
// Teams has no file drill-down - a channel's messages are themselves the
// ingestable unit, so each channel row carries an explicit "Add" action
// instead of opening a deeper level.
async function browseTeamsList() {
  const el = document.getElementById('connector-browse-teams');
  el.innerHTML = '<div class="connector-browse-note">Loading…</div>';
  try {
    const res = await fetch('/connectors/teams/list', { headers: connectorHeaders() });
    if (handleAuthError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);
    el.innerHTML = (data.value || []).map(t => `
      <div class="connector-item-row is-folder" onclick="browseTeamsChannels('${escAttr(t.id)}', '${escAttr(t.displayName)}')">
        <span class="ci-name">📁 ${escHtml(t.displayName)}</span>
      </div>`).join('') || '<div class="connector-browse-note">No teams</div>';
  } catch (e) {
    el.innerHTML = `<div class="connector-browse-note">${escHtml(e.message)}</div>`;
  }
}

async function browseTeamsChannels(teamId, teamName) {
  const el = document.getElementById('connector-browse-teams');
  el.innerHTML = '<div class="connector-browse-note">Loading…</div>';
  try {
    const res = await fetch(`/connectors/teams/${encodeURIComponent(teamId)}/channels`, { headers: connectorHeaders() });
    if (handleAuthError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);

    const crumbs = `<div class="connector-breadcrumb"><a class="crumb" onclick="browseTeamsList()">Teams</a> / ${escHtml(teamName)}</div>`;
    const rows = (data.value || []).map(c => `
      <div class="connector-item-row">
        <span class="ci-name">💬 ${escHtml(c.displayName)}</span>
        <span class="ci-meta">${escHtml(c.membershipType || '')}</span>
        <button class="connector-action-btn" onclick="event.stopPropagation(); ingestTeamsChannel('${escAttr(teamId)}', '${escAttr(c.id)}', '${escAttr(teamName)}', '${escAttr(c.displayName)}')">Add this channel</button>
      </div>`).join('') || '<div class="connector-browse-note">No channels in this team</div>';

    el.innerHTML = crumbs + rows;
  } catch (e) {
    el.innerHTML = `<div class="connector-browse-note">${escHtml(e.message)}</div>`;
  }
}

async function ingestTeamsChannel(teamId, channelId, teamName, channelName) {
  await runConnectorIngest('teams', { team_id: teamId, channel_id: channelId }, `${teamName} / ${channelName}`);
}

/* ── Azure DevOps Repos browser ──────────────────────────────── */
async function browseAzureDevOpsProjects() {
  const el = document.getElementById('connector-browse-azure_devops');
  el.innerHTML = '<div class="connector-browse-note">Loading…</div>';
  try {
    const res = await fetch('/connectors/azure-devops/projects', { headers: connectorHeaders() });
    if (handleAuthError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);
    el.innerHTML = (data.value || []).map(p => `
      <div class="connector-item-row is-folder" onclick="browseAzureDevOpsRepos('${escAttr(p.name)}')">
        <span class="ci-name">📁 ${escHtml(p.name)}</span>
      </div>`).join('') || '<div class="connector-browse-note">No projects</div>';
  } catch (e) {
    el.innerHTML = `<div class="connector-browse-note">${escHtml(e.message)}</div>`;
  }
}

async function browseAzureDevOpsRepos(project) {
  const el = document.getElementById('connector-browse-azure_devops');
  el.innerHTML = '<div class="connector-browse-note">Loading…</div>';
  try {
    const res = await fetch(`/connectors/azure-devops/projects/${encodeURIComponent(project)}/repos`, { headers: connectorHeaders() });
    if (handleAuthError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);

    const crumbs = `<div class="connector-breadcrumb"><a class="crumb" onclick="browseAzureDevOpsProjects()">Projects</a> / ${escHtml(project)}</div>`;
    const rows = (data.value || []).map(r => `
      <div class="connector-item-row is-folder" onclick="browseAzureDevOpsItems('${escAttr(project)}', '${escAttr(r.id)}', '${escAttr(r.name)}', '/')">
        <span class="ci-name">📁 ${escHtml(r.name)}</span>
      </div>`).join('') || '<div class="connector-browse-note">No repositories in this project</div>';

    el.innerHTML = crumbs + rows;
  } catch (e) {
    el.innerHTML = `<div class="connector-browse-note">${escHtml(e.message)}</div>`;
  }
}

async function browseAzureDevOpsItems(project, repoId, repoName, path) {
  const el = document.getElementById('connector-browse-azure_devops');
  el.innerHTML = '<div class="connector-browse-note">Loading…</div>';
  try {
    const params = new URLSearchParams({ path, recursion_level: 'OneLevel' });
    const res = await fetch(
      `/connectors/azure-devops/projects/${encodeURIComponent(project)}/repos/${encodeURIComponent(repoId)}/items?${params}`,
      { headers: connectorHeaders() },
    );
    if (handleAuthError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);

    const crumbs = [
      `<a class="crumb" onclick="browseAzureDevOpsProjects()">Projects</a>`,
      `<a class="crumb" onclick="browseAzureDevOpsRepos('${escAttr(project)}')">${escHtml(project)}</a>`,
      `<a class="crumb" onclick="browseAzureDevOpsItems('${escAttr(project)}', '${escAttr(repoId)}', '${escAttr(repoName)}', '/')">${escHtml(repoName)}</a>`,
    ];
    let acc = '';
    path.split('/').filter(Boolean).forEach(p => {
      acc = acc ? `${acc}/${p}` : `/${p}`;
      crumbs.push(`<a class="crumb" onclick="browseAzureDevOpsItems('${escAttr(project)}', '${escAttr(repoId)}', '${escAttr(repoName)}', '${escAttr(acc)}')">${escHtml(p)}</a>`);
    });

    // OneLevel + scopePath returns the folder itself alongside its children - drop the self-entry.
    const items = (data.value || []).filter(it => it.path !== path);
    const rows = items.map(it => it.isFolder
      ? `<div class="connector-item-row is-folder" onclick="browseAzureDevOpsItems('${escAttr(project)}', '${escAttr(repoId)}', '${escAttr(repoName)}', '${escAttr(it.path)}')">
           <span class="ci-name">📁 ${escHtml(it.path.split('/').pop())}</span>
         </div>`
      : `<div class="connector-item-row" onclick="ingestAzureDevOpsFile('${escAttr(project)}', '${escAttr(repoId)}', '${escAttr(it.path)}')">
           <span class="ci-name">📄 ${escHtml(it.path.split('/').pop())}</span>
         </div>`
    ).join('') || '<div class="connector-browse-note">Empty folder</div>';

    el.innerHTML = `<div class="connector-breadcrumb">${crumbs.join(' / ')}</div>` + rows;
  } catch (e) {
    el.innerHTML = `<div class="connector-browse-note">${escHtml(e.message)}</div>`;
  }
}

async function ingestAzureDevOpsFile(project, repoId, path) {
  await runConnectorIngest('azure_devops', { project, repo_id: repoId, path }, path.split('/').pop());
}

/* ── Shared ingest call ──────────────────────────────────────── */
async function runConnectorIngest(provider, body, filename) {
  toast(`Adding ${filename}…`, 'info');
  // The backend does dedup+parse+chunk+embed as one blocking call with no
  // progress events (see app/connectors/pipeline_bridge.py) - if it's still
  // running after a few seconds, reassure the user instead of going silent.
  const stillProcessingTimer = setTimeout(() => {
    toast(`Still processing ${filename}… parsing and embedding can take a moment`, 'info');
  }, 3000);
  try {
    const res = await fetch(`/connectors/${provider}/ingest`, {
      method: 'POST',
      headers: connectorHeaders({ 'Content-Type': 'application/json' }),
      // Attach the active session (if one exists) so this document joins it
      // instead of starting an isolated new session that orphans whatever
      // was added before - set centrally here so every ingestXFile() caller
      // gets it for free.
      body: JSON.stringify({ ...body, session_id: state.sessionId || undefined }),
    });
    clearTimeout(stillProcessingTimer);
    if (handleAuthError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);
    onProcessed(data.session_id);
    closeConnectorModal();
    toast(`${filename} added - ready to chat!`, 'success');
  } catch (e) {
    clearTimeout(stillProcessingTimer);
    toast(e.message, 'error');
  }
}

/* ── Small helpers ───────────────────────────────────────────── */
function escAttr(str) {
  return String(str ?? '').replace(/'/g, "\\'").replace(/"/g, '&quot;');
}
function breadcrumbHtml(path, onCrumbClick) {
  if (!path) return '';
  const parts = path.split('/').filter(Boolean);
  let acc = '';
  const crumbs = [`<a class="crumb" onclick="${onCrumbClick('')}">Root</a>`];
  parts.forEach(p => {
    acc = acc ? `${acc}/${p}` : p;
    crumbs.push(`<a class="crumb" onclick="${onCrumbClick(acc)}">${escHtml(p)}</a>`);
  });
  return `<div class="connector-breadcrumb">${crumbs.join(' / ')}</div>`;
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
      headers: authHeaders({
        'Content-Type':    'application/json',
        'X-Advanced-Mode':  state.advancedMode  ? 'true' : 'false',
        'X-Thinking-Mode':  state.thinkingMode  ? 'true' : 'false',
      }),
      body: JSON.stringify({ session_id: state.sessionId, question }),
    });
    if (handleAuthError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) {
      const detail = data?.detail;
      const msg = Array.isArray(detail) ? (detail[0]?.msg || 'Request error') : (detail || `Server error ${res.status}`);
      throw new Error(msg);
    }
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
    ? `<div class="bot-avatar">AR</div>`
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
      const pageHtml = c.page_number != null ? `<span class="cit-sep">-</span><span class="cit-page">Page ${c.page_number}</span>` : '';
      const confHtml = c.confidence != null ? `<span class="cit-sep">-</span><span class="cit-conf">Confidence ${(c.confidence * 100).toFixed(1)}%</span>` : '';
      return `<div class="citation-item">
        <span class="cit-arrow">▶</span>
        <span class="cit-source">${src}</span>
        <span class="cit-sep">-</span>
        <span class="cit-chunk">Chunk #${chunk}</span>
        ${pageHtml}${confHtml}
        <span class="cit-sep">-</span>
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
        <button class="copy-btn" onclick="copyText(this, ${escHtml(JSON.stringify(text))})">
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
      <h2 class="welcome-title">Hi, I'm ARIA ✦</h2>
      <p class="welcome-greeting">Upload your data and I'll answer anything about it.</p>
      <div class="welcome-steps">
        <div class="step"><span class="step-num">1</span>Upload a file, URL, or API</div>
        <div class="step"><span class="step-num">2</span>Or connect Gmail, GitHub &amp; more</div>
        <div class="step"><span class="step-num">3</span>Click Process &amp; Chat</div>
        <div class="step"><span class="step-num">4</span>Ask me anything</div>
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

/* ── Sidebar hamburger toggle (mobile) ──────────────────────── */
function toggleSidebar() {
  const sidebar = document.getElementById('sidebar');
  const btn     = document.getElementById('sidebar-hamburger');
  sidebar.classList.toggle('collapsed');
  btn.classList.toggle('collapsed');
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
