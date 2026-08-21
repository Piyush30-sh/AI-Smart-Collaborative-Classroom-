/**
 * main.js — Shared utilities for AI Study Planner
 * Handles: dark-mode toggle, reminders, motivational toasts
 */

/* ── Dark-mode toggle ─────────────────────────────────────────────────────── */
(function initDarkMode() {
  const root   = document.documentElement;
  const toggle = document.getElementById('darkToggle');
  if (!toggle) return;

  // Load saved preference
  const saved = localStorage.getItem('theme') || 'light';
  root.setAttribute('data-bs-theme', saved);
  toggle.checked = (saved === 'dark');

  toggle.addEventListener('change', () => {
    const theme = toggle.checked ? 'dark' : 'light';
    root.setAttribute('data-bs-theme', theme);
    localStorage.setItem('theme', theme);
  });
})();


/* ──second change 8-8-26 Dark-mode toggle ─────────────────────────────────────────────────────── */
const darkToggle = document.getElementById("darkToggle");
const htmlEl = document.documentElement;

// On toggle change
darkToggle.addEventListener("change", function () {
  if (this.checked) {
    htmlEl.setAttribute("data-bs-theme", "dark");
    localStorage.setItem("siteTheme", "dark");
  } else {
    htmlEl.setAttribute("data-bs-theme", "light");
    localStorage.setItem("siteTheme", "light");
  }
});


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
    const res  = await fetch('/api/reminders');
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


/* ── Browser notification helper ────────────────────────────────────────── */
function sendBrowserNotif(title, body) {
  if ('Notification' in window && Notification.permission === 'granted') {
    new Notification(title, { body, icon: '/static/images/icon.png' });
  }
}
