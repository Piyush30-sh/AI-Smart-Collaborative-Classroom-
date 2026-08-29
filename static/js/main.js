/**
 * main.js — Shared utilities for AI Study Planner
 * Handles: dark-mode toggle, reminders, motivational toasts
 */

/* ── Dark-mode toggle ─────────────────────────────────────────────────────── */
(function initDarkMode() {
  const root = document.documentElement;
  const toggle = document.getElementById('darkToggle');
  const prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;

  function safeGet(key) {
    try {
      return localStorage.getItem(key);
    } catch (e) {
      return null;
    }
  }

  function normalizeTheme(value) {
    return value === 'dark' || value === 'light' ? value : null;
  }

  function persistTheme(theme) {
    root.setAttribute('data-bs-theme', theme);
    try {
      localStorage.setItem('theme', theme);
      // Keep legacy key in sync so older pages/scripts do not flip the theme.
      localStorage.setItem('siteTheme', theme);
    } catch (e) {
      // Ignore storage write errors and still apply theme to the page.
    }
  }

  const stored = normalizeTheme(safeGet('theme'))
    || normalizeTheme(safeGet('siteTheme'));
  const initialTheme = stored || (prefersDark ? 'dark' : 'light');

  persistTheme(initialTheme);

  if (!toggle) return;

  function syncToggle() {
    toggle.checked = root.getAttribute('data-bs-theme') === 'dark';
  }

  syncToggle();
  toggle.addEventListener('change', () => {
    persistTheme(toggle.checked ? 'dark' : 'light');
    syncToggle();
  });

  document.addEventListener('DOMContentLoaded', syncToggle);
  window.addEventListener('pageshow', syncToggle);
  window.addEventListener('storage', (event) => {
    if (event.key === 'theme' || event.key === 'siteTheme') {
      const nextTheme = normalizeTheme(event.newValue);
      if (nextTheme) {
        root.setAttribute('data-bs-theme', nextTheme);
        syncToggle();
      }
    }
  });
})();


/* ── Auto-dismiss flash alerts ────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  setTimeout(() => {
    document.querySelectorAll('#flashContainer .alert').forEach(el => {
      const bsAlert = bootstrap.Alert.getOrCreateInstance(el);
      bsAlert.close();
    });
  }, 5000);
});


/* ── Reminder toasts ──────────────────────────────────────────────────────── */
async function loadReminders() {
  try {
    const res = await fetch('/api/reminders');
    if (!res.ok) return;
    const data = await res.json();

    if (!data.reminders || data.reminders.length === 0) return;

    const area = document.getElementById('toastArea');
    if (!area) return;

    data.reminders.forEach((msg, i) => {
      const id = `reminder-toast-${Date.now()}-${i}`;
      area.insertAdjacentHTML('beforeend', `
        <div id="${id}" class="toast align-items-center border-0 shadow" role="alert"
             data-bs-delay="6000">
          <div class="d-flex">
            <div class="toast-body small">${msg}</div>
            <button type="button" class="btn-close btn-close me-2 m-auto"
                    data-bs-dismiss="toast"></button>
          </div>
        </div>`);
      const el = document.getElementById(id);
      if (el) new bootstrap.Toast(el).show();
    });
  } catch (e) { /* silently ignore */ }
}

// Load reminders on every protected page (base.html includes this file)
if (document.getElementById('toastArea')) {
  setTimeout(loadReminders, 2000);  // slight delay after page load
}


/* ── Motivational toast after task completion ────────────────────────────── */
function showMotivation(msg) {
  const area = document.getElementById('toastArea');
  if (!area || !msg) return;
  const id = `mot-${Date.now()}`;
  area.insertAdjacentHTML('beforeend', `
    <div id="${id}" class="toast align-items-center bg-success text-white border-0 shadow"
         role="status" data-bs-delay="4000">
      <div class="d-flex">
        <div class="toast-body fw-semibold">${msg}</div>
        <button type="button" class="btn-close btn-close-white me-2 m-auto"
                data-bs-dismiss="toast"></button>
      </div>
    </div>`);
  const el = document.getElementById(id);
  if (el) new bootstrap.Toast(el).show();
}


/* ── Notification permission request ────────────────────────────────────── */
function requestNotifPermission() {
  if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission();
  }
}

document.addEventListener('DOMContentLoaded', requestNotifPermission);


/* ── Login/signup page transition ───────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  const authBody = document.querySelector('.auth-body');
  if (!authBody || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

  document.querySelectorAll('[data-auth-transition]').forEach((link) => {
    link.addEventListener('click', (event) => {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;

      event.preventDefault();
      authBody.classList.add('auth-is-leaving');
      window.setTimeout(() => {
        window.location.href = link.href;
      }, 220);
    });
  });
});


/* ── Browser notification helper ────────────────────────────────────────── */
function sendBrowserNotif(title, body) {
  if ('Notification' in window && Notification.permission === 'granted') {
    new Notification(title, { body, icon: '/static/images/icon.png' });
  }
}
