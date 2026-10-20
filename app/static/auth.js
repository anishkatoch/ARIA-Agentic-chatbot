/* ── Redirect if already logged in ──────────────────────────── */
(function() {
  if (localStorage.getItem('rag_auth_token')) window.location.replace('/');
  if (localStorage.getItem('aria_dark_mode') === 'true') document.documentElement.classList.add('dark');
})();

/* ── OTP state ───────────────────────────────────────────────── */
let _otpEmail     = '';
let _resendTimer  = null;

/* ── Tab switching ───────────────────────────────────────────── */
function showTab(name) {
  document.querySelectorAll('.auth-tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.auth-panel').forEach(p => p.classList.remove('active'));
  const tab = document.querySelector(`.auth-tab[data-tab="${name}"]`);
  const panel = document.getElementById(`panel-${name}`);
  if (tab) tab.classList.add('active');
  if (panel) panel.classList.add('active');
  clearAllErrors();
}

document.querySelectorAll('.auth-tab').forEach(btn => {
  btn.addEventListener('click', () => showTab(btn.dataset.tab));
});

function showForgot() {
  document.querySelectorAll('.auth-tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.auth-panel').forEach(p => p.classList.remove('active'));
  document.getElementById('panel-forgot').classList.add('active');
  clearAllErrors();
}

function showOtp(email) {
  _otpEmail = email;
  document.querySelectorAll('.auth-tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.auth-panel').forEach(p => p.classList.remove('active'));
  document.getElementById('panel-otp').classList.add('active');
  document.getElementById('otp-email-label').textContent = email;
  clearAllErrors();
  clearOtpBoxes();
  document.querySelectorAll('.otp-box')[0].focus();
  startResendTimer(60);
}

/* ── Password visibility toggle ─────────────────────────────── */
function togglePw(inputId, btn) {
  const inp = document.getElementById(inputId);
  if (!inp) return;
  const isText = inp.type === 'text';
  inp.type = isText ? 'password' : 'text';
  btn.innerHTML = isText
    ? `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>`
    : `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>`;
}

/* ── Password strength ───────────────────────────────────────── */
function checkPasswordStrength(val) {
  const wrap = document.getElementById('pw-strength');
  const fill = document.getElementById('pw-fill');
  const label = document.getElementById('pw-label');
  if (!wrap) return;

  if (!val) { wrap.classList.remove('visible'); return; }
  wrap.classList.add('visible');

  let score = 0;
  if (val.length >= 6) score++;
  if (val.length >= 10) score++;
  if (/[A-Z]/.test(val)) score++;
  if (/[0-9]/.test(val)) score++;
  if (/[^A-Za-z0-9]/.test(val)) score++;

  const levels = [
    { pct: '20%', color: '#ef4444', text: 'Very weak' },
    { pct: '40%', color: '#f97316', text: 'Weak' },
    { pct: '60%', color: '#eab308', text: 'Fair' },
    { pct: '80%', color: '#22c55e', text: 'Strong' },
    { pct: '100%', color: '#10b981', text: 'Very strong' },
  ];
  const l = levels[score - 1] || levels[0];
  fill.style.width = l.pct;
  fill.style.background = l.color;
  label.textContent = l.text;
  label.style.color = l.color;
}

/* ── Error helpers ───────────────────────────────────────────── */
function showError(id, msg) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = msg;
  el.classList.remove('hidden');
}
function showSuccess(id, msg) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = msg;
  el.classList.remove('hidden');
}
function clearAllErrors() {
  document.querySelectorAll('.form-error, .form-success').forEach(el => {
    el.classList.add('hidden');
    el.textContent = '';
  });
}

/* ── Button loading state ────────────────────────────────────── */
function setLoading(btnId, loading) {
  const btn = document.getElementById(btnId);
  if (!btn) return;
  btn.disabled = loading;
  btn.querySelector('.btn-label').style.opacity = loading ? '0' : '1';
  const spinner = btn.querySelector('.btn-spinner');
  spinner.classList.toggle('hidden', !loading);
}

/* ── Save auth & redirect ────────────────────────────────────── */
function saveAuthAndRedirect(data) {
  localStorage.setItem('rag_auth_token', data.token);
  localStorage.setItem('rag_auth_user', JSON.stringify(data.user));
  window.location.replace('/');
}

/* ── API caller ──────────────────────────────────────────────── */
async function callAuth(endpoint, body) {
  const res = await fetch(`/auth/${endpoint}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  let data;
  try { data = await res.json(); } catch (_) { data = {}; }
  if (!res.ok) {
    throw new Error(data.detail || data.message || `Server error ${res.status}`);
  }
  return data;
}

/* ── Sign In ─────────────────────────────────────────────────── */
async function handleSignin(e) {
  e.preventDefault();
  clearAllErrors();
  const email    = document.getElementById('signin-email').value.trim();
  const password = document.getElementById('signin-password').value;
  setLoading('btn-signin', true);
  try {
    const data = await callAuth('signin', { email, password });
    saveAuthAndRedirect(data);
  } catch (err) {
    showError('signin-error', err.message);
    setLoading('btn-signin', false);
  }
}

/* ── Sign Up ─────────────────────────────────────────────────── */
async function handleSignup(e) {
  e.preventDefault();
  clearAllErrors();
  const name     = document.getElementById('signup-name').value.trim();
  const email    = document.getElementById('signup-email').value.trim();
  const password = document.getElementById('signup-password').value;
  setLoading('btn-signup', true);
  try {
    const data = await callAuth('signup', { name, email, password });
    if (data.token) {
      saveAuthAndRedirect(data);
    } else if (data.needs_otp) {
      setLoading('btn-signup', false);
      showOtp(email);
    } else {
      showSuccess('signup-success', 'Account created! Check your email to confirm, then sign in.');
      setLoading('btn-signup', false);
    }
  } catch (err) {
    showError('signup-error', err.message);
    setLoading('btn-signup', false);
  }
}

/* ── OTP verification ────────────────────────────────────────── */
async function handleOtp() {
  clearAllErrors();
  const boxes = [...document.querySelectorAll('.otp-box')];
  const code  = boxes.map(b => b.value).join('');
  if (code.length < 6) {
    shakeOtp();
    showError('otp-error', 'Please enter all 6 digits.');
    return;
  }
  setLoading('btn-otp', true);
  try {
    const data = await callAuth('verify-otp', { email: _otpEmail, token: code });
    saveAuthAndRedirect(data);
  } catch (err) {
    showError('otp-error', err.message);
    shakeOtp();
    clearOtpBoxes();
    setLoading('btn-otp', false);
  }
}

async function resendOtp() {
  if (!_otpEmail) return;
  const btn = document.getElementById('resend-btn');
  btn.disabled = true;
  try {
    await callAuth('resend-otp', { email: _otpEmail });
    startResendTimer(60);
    document.getElementById('otp-error').classList.add('hidden');
  } catch (_) {
    startResendTimer(60); // still start timer to prevent spam
  }
}

function startResendTimer(seconds) {
  if (_resendTimer) clearInterval(_resendTimer);
  const btn   = document.getElementById('resend-btn');
  const timer = document.getElementById('resend-timer');
  btn.disabled = true;
  timer.textContent = `(${seconds}s)`;
  timer.classList.remove('hidden');
  let remaining = seconds;
  _resendTimer = setInterval(() => {
    remaining--;
    timer.textContent = `(${remaining}s)`;
    if (remaining <= 0) {
      clearInterval(_resendTimer);
      btn.disabled = false;
      timer.classList.add('hidden');
    }
  }, 1000);
}

function clearOtpBoxes() {
  document.querySelectorAll('.otp-box').forEach(b => {
    b.value = '';
    b.classList.remove('filled');
  });
}

function shakeOtp() {
  document.querySelectorAll('.otp-box').forEach(b => {
    b.classList.remove('shake');
    void b.offsetWidth; // reflow to restart animation
    b.classList.add('shake');
  });
}

/* ── Guest ───────────────────────────────────────────────────── */
async function handleGuest(e) {
  e.preventDefault();
  clearAllErrors();
  const name  = document.getElementById('guest-name').value.trim();
  const email = document.getElementById('guest-email').value.trim();
  setLoading('btn-guest', true);
  try {
    const data = await callAuth('guest', { name, email });
    saveAuthAndRedirect(data);
  } catch (err) {
    showError('guest-error', err.message);
    setLoading('btn-guest', false);
  }
}

/* ── Typing greeting animation ───────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  const messages = [
    "Hi there! I'm ARIA - ask me anything.",
    "Upload your data and I'll answer instantly.",
    "Powered by RAG. Ready when you are.",
  ];
  const el = document.getElementById('aria-greeting-text');
  if (!el) return;
  let msgIndex = 0;

  function typeMessage(text, cb) {
    el.textContent = '';
    let i = 0;
    const t = setInterval(() => {
      el.textContent += text[i++];
      if (i >= text.length) { clearInterval(t); setTimeout(cb, 2200); }
    }, 40);
  }

  function eraseMessage(cb) {
    const t = setInterval(() => {
      el.textContent = el.textContent.slice(0, -1);
      if (!el.textContent.length) { clearInterval(t); cb(); }
    }, 22);
  }

  function loop() {
    typeMessage(messages[msgIndex], () => {
      eraseMessage(() => {
        msgIndex = (msgIndex + 1) % messages.length;
        loop();
      });
    });
  }
  loop();
});

/* ── OTP box keyboard wiring ─────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  const boxes = [...document.querySelectorAll('.otp-box')];
  boxes.forEach((box, i) => {
    box.addEventListener('input', () => {
      const val = box.value.replace(/\D/g, '');
      box.value = val ? val[0] : '';
      box.classList.toggle('filled', !!box.value);
      if (box.value && i < boxes.length - 1) boxes[i + 1].focus();
      // Auto-submit when all filled
      if (boxes.every(b => b.value)) handleOtp();
    });
    box.addEventListener('keydown', e => {
      if (e.key === 'Backspace' && !box.value && i > 0) {
        boxes[i - 1].value = '';
        boxes[i - 1].classList.remove('filled');
        boxes[i - 1].focus();
      }
      if (e.key === 'ArrowLeft' && i > 0) boxes[i - 1].focus();
      if (e.key === 'ArrowRight' && i < boxes.length - 1) boxes[i + 1].focus();
    });
    box.addEventListener('paste', e => {
      e.preventDefault();
      const pasted = (e.clipboardData || window.clipboardData)
        .getData('text').replace(/\D/g, '').slice(0, 6);
      boxes.forEach((b, j) => {
        b.value = pasted[j] || '';
        b.classList.toggle('filled', !!b.value);
      });
      const next = Math.min(pasted.length, boxes.length - 1);
      boxes[next].focus();
      if (pasted.length === 6) handleOtp();
    });
    box.addEventListener('focus', () => box.select());
  });
});

/* ── Forgot Password ─────────────────────────────────────────── */
async function handleForgot(e) {
  e.preventDefault();
  clearAllErrors();
  const email = document.getElementById('forgot-email').value.trim();
  setLoading('btn-forgot', true);
  try {
    const data = await callAuth('forgot-password', { email });
    showSuccess('forgot-success', data.message);
  } catch (err) {
    showError('forgot-error', err.message);
  } finally {
    setLoading('btn-forgot', false);
  }
}
