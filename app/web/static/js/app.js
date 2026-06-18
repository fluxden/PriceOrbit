/* PriceOrbit — light client behavior (no framework, no build step). */
(function () {
  "use strict";

  var THEME_KEY = "priceorbit-theme";
  var VIEW_KEY = "priceorbit-view";
  var VIEWS = ["list", "grid", "compact"];

  /* ---- Theme toggle ---- */
  var themeBtn = document.getElementById("theme-toggle");
  if (themeBtn) {
    themeBtn.addEventListener("click", function () {
      var current = document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
      var next = current === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
    });
  }

  /* ---- View switching (list / grid / compact), persisted ---- */
  var container = document.querySelector(".products");
  var viewBtns = Array.prototype.slice.call(document.querySelectorAll(".viewbtn"));

  function applyView(view) {
    if (VIEWS.indexOf(view) === -1) view = "list";
    if (container) {
      VIEWS.forEach(function (v) { container.classList.remove("view-" + v); });
      container.classList.add("view-" + view);
    }
    viewBtns.forEach(function (b) {
      var on = b.getAttribute("data-view") === view;
      b.classList.toggle("is-active", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  var saved = "list";
  try { saved = localStorage.getItem(VIEW_KEY) || "list"; } catch (e) {}
  applyView(saved);

  viewBtns.forEach(function (b) {
    b.addEventListener("click", function () {
      var view = b.getAttribute("data-view");
      applyView(view);
      try { localStorage.setItem(VIEW_KEY, view); } catch (e) {}
    });
  });

  /* ---- Row action menus (kebab) ---- */
  function closeAllMenus(except) {
    document.querySelectorAll(".menu.is-open").forEach(function (m) {
      if (m !== except) {
        m.classList.remove("is-open");
        var btn = m.parentElement.querySelector(".kebab");
        if (btn) btn.setAttribute("aria-expanded", "false");
      }
    });
  }

  document.querySelectorAll(".kebab").forEach(function (btn) {
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      var menu = btn.parentElement.querySelector(".menu");
      if (!menu) return;
      var isOpen = menu.classList.contains("is-open");
      closeAllMenus(isOpen ? null : menu);
      menu.classList.toggle("is-open", !isOpen);
      btn.setAttribute("aria-expanded", !isOpen ? "true" : "false");
    });
  });

  document.addEventListener("click", function () { closeAllMenus(null); });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeAllMenus(null);
  });

  /* ---- Confirm destructive actions ---- */
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.getAttribute("data-confirm"))) e.preventDefault();
    });
  });

  /* ---- Auto-dismiss toasts ---- */
  document.querySelectorAll(".toast").forEach(function (t) {
    setTimeout(function () {
      t.style.transition = "opacity .4s ease";
      t.style.opacity = "0";
      setTimeout(function () { t.remove(); }, 400);
    }, 4000);
  });
})();

/* ---- Add Product page: reveal conditional fields ---- */
(function () {
  const freq = document.getElementById('frequency');
  const rowCustom = document.getElementById('row-custom');
  const rowDaily = document.getElementById('row-daily');
  const cond = document.getElementById('condition');
  const rowPercent = document.getElementById('row-percent');

  function syncFreq() {
    if (!freq) return;
    if (rowCustom) rowCustom.hidden = freq.value !== 'custom';
    if (rowDaily) rowDaily.hidden = freq.value !== 'daily';
  }
  function syncCond() {
    if (cond && rowPercent) rowPercent.hidden = cond.value !== 'drop_percent';
  }

  if (freq) { freq.addEventListener('change', syncFreq); syncFreq(); }
  if (cond) { cond.addEventListener('change', syncCond); syncCond(); }
})();

/* ---- Product page: show alert threshold only when the trigger needs one ---- */
(function () {
  const trig = document.getElementById('rule-trigger');
  const wrap = document.getElementById('rule-thresh-wrap');
  if (!trig || !wrap) return;
  function sync() {
    const opt = trig.options[trig.selectedIndex];
    wrap.hidden = !(opt && opt.dataset.thresh);
  }
  trig.addEventListener('change', sync); sync();
})();

/* ---- Alerts page: SMTP vs API field groups ---- */
(function () {
  const sel = document.getElementById('email-method');
  if (!sel) return;
  const smtp = document.querySelector('.email-smtp'), api = document.querySelector('.email-api');
  function sync() { const m = sel.value; if (smtp) smtp.hidden = m !== 'smtp'; if (api) api.hidden = m !== 'api'; }
  sel.addEventListener('change', sync); sync();
})();

/* ---- Alerts page: click-to-insert placeholder chips ---- */
(function () {
  const chips = document.querySelectorAll('.chip[data-token]');
  if (!chips.length) return;
  let active = null;
  document.querySelectorAll('.tpl-field').forEach(f => f.addEventListener('focus', () => { active = f; }));
  chips.forEach(c => c.addEventListener('click', () => {
    const f = active || document.querySelector('.tpl-field');
    if (!f) return;
    const token = '{' + c.dataset.token + '}';
    const s = f.selectionStart ?? f.value.length, e = f.selectionEnd ?? f.value.length;
    f.value = f.value.slice(0, s) + token + f.value.slice(e);
    const pos = s + token.length;
    f.focus(); try { f.setSelectionRange(pos, pos); } catch (_) {}
    active = f;
  }));
})();

/* ---- Settings page: live theme preview, presets, theme export/import ---- */
(function () {
  const form = document.getElementById('theme-form');
  if (!form) return;
  const root = document.documentElement;
  function applyVar(name, val) {
    if (val) {
      root.style.setProperty(name, val);
      if (name === '--accent') {
        root.style.setProperty('--accent-hover', 'color-mix(in srgb, ' + val + ' 84%, #000)');
        root.style.setProperty('--accent-weak', 'color-mix(in srgb, ' + val + ' 16%, var(--surface))');
      }
    } else {
      root.style.removeProperty(name);
      if (name === '--accent') { root.style.removeProperty('--accent-hover'); root.style.removeProperty('--accent-weak'); }
    }
  }
  form.querySelectorAll('.hex').forEach(inp => {
    const pick = inp.parentElement.querySelector('.swatch');
    inp.addEventListener('input', () => {
      const v = inp.value.trim();
      applyVar(inp.dataset.var, v);
      if (pick && /^#?[0-9a-fA-F]{6}$/.test(v)) pick.value = v.startsWith('#') ? v : '#' + v;
    });
    if (pick) pick.addEventListener('input', () => { inp.value = pick.value; applyVar(inp.dataset.var, pick.value); });
  });
  const base = document.getElementById('theme-base');
  if (base) base.addEventListener('change', () => {
    const v = base.value;
    root.setAttribute('data-theme', (v === 'light' || v === 'dark') ? v
      : (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'));
  });
  document.querySelectorAll('.preset').forEach(b => b.addEventListener('click', () => {
    const map = { theme_accent: b.dataset.accent, theme_sidebar_bg: b.dataset.sidebar, theme_topbar_accent: b.dataset.topbar, theme_link: b.dataset.link };
    form.querySelectorAll('.hex').forEach(inp => {
      const val = map[inp.name] || '';
      inp.value = val; applyVar(inp.dataset.var, val);
      const pick = inp.parentElement.querySelector('.swatch'); if (pick && val) pick.value = val;
    });
  }));
  const ex = document.getElementById('theme-export');
  if (ex) ex.addEventListener('click', () => {
    const data = { theme_base: base ? base.value : '' };
    form.querySelectorAll('.hex').forEach(inp => data[inp.name] = inp.value.trim());
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
    a.download = 'priceorbit-theme.json'; a.click(); URL.revokeObjectURL(a.href);
  });
  const ib = document.getElementById('theme-import-btn'), ta = document.getElementById('theme-import');
  if (ib && ta) ib.addEventListener('click', () => {
    if (ta.hidden) { ta.hidden = false; ta.focus(); return; }
    try {
      const data = JSON.parse(ta.value);
      if (base && typeof data.theme_base === 'string') {
        base.value = ['', 'light', 'dark'].includes(data.theme_base) ? data.theme_base : '';
        base.dispatchEvent(new Event('change'));
      }
      form.querySelectorAll('.hex').forEach(inp => {
        if (typeof data[inp.name] === 'string') {
          inp.value = data[inp.name]; applyVar(inp.dataset.var, inp.value.trim());
          const pick = inp.parentElement.querySelector('.swatch'); if (pick && inp.value.trim()) pick.value = inp.value.trim();
        }
      });
      ta.hidden = true; ta.style.borderColor = '';
    } catch (e) { ta.style.borderColor = 'var(--bad)'; }
  });
})();

/* ---- Browser-sound price alerts: poll, beep, toast (step 20) ---- */
(function () {
  var POLL_MS = 30000;

  function beep() {
    try {
      var Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      var ctx = new Ctx();
      var o = ctx.createOscillator(), g = ctx.createGain();
      o.connect(g); g.connect(ctx.destination);
      o.type = "sine"; o.frequency.value = 880;
      g.gain.setValueAtTime(0.0001, ctx.currentTime);
      g.gain.exponentialRampToValueAtTime(0.18, ctx.currentTime + 0.01);
      g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.45);
      o.start(); o.stop(ctx.currentTime + 0.45);
      o.onended = function () { try { ctx.close(); } catch (e) {} };
    } catch (e) {}
  }

  function stack() {
    var el = document.getElementById("po-toasts");
    if (!el) {
      el = document.createElement("div");
      el.id = "po-toasts";
      el.style.cssText = "position:fixed;right:18px;bottom:18px;z-index:9999;display:flex;flex-direction:column;gap:8px;max-width:340px;";
      document.body.appendChild(el);
    }
    return el;
  }

  function toast(text) {
    var t = document.createElement("div");
    t.className = "toast toast--good";
    t.setAttribute("role", "status");
    t.textContent = text;
    stack().appendChild(t);
    setTimeout(function () {
      t.style.opacity = "0";
      setTimeout(function () { t.remove(); }, 400);
    }, 6000);
  }

  function poll() {
    fetch("/api/alerts/unseen", { headers: { "Accept": "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data || !data.count) return;
        beep();
        data.items.slice(0, 4).forEach(function (it) { toast(it.subject || "Price alert"); });
        if (data.count > 4) toast("+" + (data.count - 4) + " more alert(s)");
        fetch("/api/alerts/seen", { method: "POST", headers: { "Accept": "application/json" } })
          .catch(function () {});
      })
      .catch(function () {});
  }

  setTimeout(poll, 3000);
  setInterval(poll, POLL_MS);
})();

/* ---- Password / token fields: show-hide toggle ---- */
(function () {
  var inputs = document.querySelectorAll('.form-card input[type="password"]');
  Array.prototype.forEach.call(inputs, function (inp) {
    var wrap = document.createElement("span");
    wrap.className = "pw-wrap";
    inp.parentNode.insertBefore(wrap, inp);
    wrap.appendChild(inp);
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "pw-toggle";
    btn.textContent = "Show";
    btn.setAttribute("aria-label", "Show value");
    wrap.appendChild(btn);
    btn.addEventListener("click", function () {
      var reveal = inp.type === "password";
      inp.type = reveal ? "text" : "password";
      btn.textContent = reveal ? "Hide" : "Show";
      btn.setAttribute("aria-label", reveal ? "Hide value" : "Show value");
    });
  });
})();

/* ---- Time pickers honoring the app's 12/24-hour setting ----
   Native <input type="time"> renders in the browser locale (ignoring the
   Settings choice) and its spinner loops endlessly. Replace each one with
   plain <select> dropdowns: hours 01–12 + AM/PM in 12h mode, or hours 00–23
   in 24h mode; minutes 00–59. A hidden input keeps the original name and an
   "HH:MM" (24-hour) value so the server side is unchanged. */
(function () {
  var fmt = (document.body && document.body.dataset.timeFormat) === "12" ? "12" : "24";
  var inputs = document.querySelectorAll('input[type="time"]');
  if (!inputs.length) return;

  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function opt(value, label, selected) {
    var o = document.createElement("option");
    o.value = String(value); o.textContent = label;
    if (selected) o.selected = true;
    return o;
  }

  Array.prototype.forEach.call(inputs, function (inp) {
    var parts = (inp.value || "").split(":");
    var h = parseInt(parts[0], 10); if (isNaN(h)) h = 0;
    var m = parseInt(parts[1], 10); if (isNaN(m)) m = 0;

    var wrap = document.createElement("span");
    wrap.className = "timepick";

    var hidden = document.createElement("input");
    hidden.type = "hidden";
    hidden.name = inp.name;
    hidden.value = pad(h) + ":" + pad(m);

    var hourSel = document.createElement("select");
    hourSel.className = "timepick__h";
    hourSel.setAttribute("aria-label", "Hour");
    var minSel = document.createElement("select");
    minSel.className = "timepick__m";
    minSel.setAttribute("aria-label", "Minute");
    var ampmSel = null;

    var hh;
    if (fmt === "12") {
      var displayH = (h % 12) || 12;
      for (hh = 1; hh <= 12; hh++) hourSel.appendChild(opt(hh, pad(hh), hh === displayH));
      ampmSel = document.createElement("select");
      ampmSel.className = "timepick__ampm";
      ampmSel.setAttribute("aria-label", "AM or PM");
      ampmSel.appendChild(opt("AM", "AM", h < 12));
      ampmSel.appendChild(opt("PM", "PM", h >= 12));
    } else {
      for (hh = 0; hh <= 23; hh++) hourSel.appendChild(opt(hh, pad(hh), hh === h));
    }
    for (var mm = 0; mm <= 59; mm++) minSel.appendChild(opt(mm, pad(mm), mm === m));

    function sync() {
      var H, M = parseInt(minSel.value, 10);
      if (fmt === "12") {
        H = parseInt(hourSel.value, 10) % 12;
        if (ampmSel.value === "PM") H += 12;
      } else {
        H = parseInt(hourSel.value, 10);
      }
      hidden.value = pad(H) + ":" + pad(M);
    }
    hourSel.addEventListener("change", sync);
    minSel.addEventListener("change", sync);
    if (ampmSel) ampmSel.addEventListener("change", sync);

    var sep = document.createElement("span");
    sep.className = "timepick__sep"; sep.textContent = ":";
    wrap.appendChild(hidden);
    wrap.appendChild(hourSel);
    wrap.appendChild(sep);
    wrap.appendChild(minSel);
    if (ampmSel) wrap.appendChild(ampmSel);

    inp.parentNode.replaceChild(wrap, inp);
  });
})();

/* ---- Bulk actions on Price Tracking (6d) ---- */
(function () {
  var form = document.getElementById("bulk-form");
  if (!form) return;
  var bar = document.getElementById("bulkbar");
  var countEl = document.getElementById("bulk-count");
  var all = document.getElementById("bulk-all");
  function boxes() { return Array.prototype.slice.call(form.querySelectorAll('input[name="ids"]')); }
  function checked() { return boxes().filter(function (b) { return b.checked; }); }
  function update() {
    var n = checked().length;
    if (countEl) countEl.textContent = n + " selected";
    if (bar) bar.hidden = n === 0;
    if (all) all.checked = n > 0 && n === boxes().length;
  }
  form.addEventListener("change", function (e) {
    if (e.target && e.target.name === "ids") update();
  });
  if (all) all.addEventListener("change", function () {
    boxes().forEach(function (b) { b.checked = all.checked; });
    update();
  });
  form.addEventListener("submit", function (e) {
    if (checked().length === 0) { e.preventDefault(); return; }
    var btn = e.submitter;
    if (btn && btn.value === "delete" &&
        !confirm("Delete the selected products and their history? This cannot be undone.")) {
      e.preventDefault();
    }
  });
  update();
})();
