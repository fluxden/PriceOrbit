/* PriceOrbit price-history chart — dependency-free SVG.
   Reads JSON from #chart-data and renders into #price-chart. */
(function () {
  const host = document.getElementById('price-chart');
  const dataEl = document.getElementById('chart-data');
  if (!host || !dataEl) return;

  let data;
  try { data = JSON.parse(dataEl.textContent); } catch (e) { return; }
  const series = (data.series || []).filter(s => s.points && s.points.length);
  if (!series.length) { host.innerHTML = '<p class="chart-empty">No price history yet. Use “Check now” to record the first point.</p>'; return; }

  const SYM = { USD: '$', CAD: '$', AUD: '$', EUR: '€', GBP: '£', JPY: '¥', INR: '₹' };
  const cur = data.currency || '';
  const sym = SYM[cur] || '';
  const fmt = v => sym ? sym + v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
                       : (cur ? cur + ' ' : '') + v.toFixed(2);
  const DAY = 864e5;
  // Point times carry an explicit UTC offset, so Date.parse fixes the instant;
  // we render every label in the app-configured timezone (data.tz) rather than
  // the viewer's browser timezone. Invalid/blank tz falls back to browser local.
  const TZ = data.tz || undefined;
  const dtOpts = o => { const x = Object.assign({}, o); if (TZ) x.timeZone = TZ; return x; };
  const fmtDate = ms => new Date(ms).toLocaleDateString(undefined, dtOpts({ month: 'short', day: 'numeric' }));
  const fmtTime = ms => new Date(ms).toLocaleTimeString(undefined, dtOpts({ hour: 'numeric', minute: '2-digit' }));
  const dayKeyOf = ms => new Date(ms).toLocaleDateString('en-CA', dtOpts({}));  // tz-aware YYYY-MM-DD

  // Pick black or white text for a colored background by its sRGB luminance.
  function textOn(color) {
    const m = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(color || '');
    if (!m) return '#fff';
    let h = m[1];
    if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
    const r = parseInt(h.slice(0, 2), 16), g = parseInt(h.slice(2, 4), 16), b = parseInt(h.slice(4, 6), 16);
    const lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
    return lum > 0.6 ? '#1a1a1a' : '#fff';
  }

  // Round a raw step up to a "nice" 1/2/5×10ⁿ value so gridlines land on tidy numbers.
  function niceStep(range, count) {
    const raw = (range || 1) / count;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    const step = norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10;
    return step * mag;
  }

  // Normalize point times to epoch ms.
  series.forEach(s => s.points.forEach(p => { p.ms = Date.parse(p.t); }));
  const oos = (data.oosSpans || []).map(([a, b]) => [Date.parse(a), Date.parse(b)]);
  const target = (typeof data.target === 'number') ? data.target : null;

  // Compact geometry. W is measured from the host each render so 1 SVG unit == 1px
  // (no upscaling), which keeps the chart small and the axis text at its true size.
  const H = 200, PADR = 16, PADT = 12, PADB = 34;
  let W = 720, PADL = 56;             // both recomputed per render
  const hidden = new Set();           // store ids toggled off
  let rangeDays = 0;                  // 0 = all

  const allMs = series.flatMap(s => s.points.map(p => p.ms));
  const maxMs = Math.max(...allMs);

  function visibleSeries() { return series.filter(s => !hidden.has(s.id)); }

  function render() {
    W = Math.max(320, Math.round(host.clientWidth || 720));
    const fromMs = rangeDays ? maxMs - rangeDays * DAY : -Infinity;
    const vis = visibleSeries().map(s => ({
      ...s, pts: s.points.filter(p => p.ms >= fromMs && p.p != null)
    })).filter(s => s.pts.length);

    const xs = vis.flatMap(s => s.pts.map(p => p.ms));
    const ys = vis.flatMap(s => s.pts.map(p => p.p));
    if (target != null) ys.push(target);
    if (!xs.length) { host.innerHTML = '<p class="chart-empty">No points in this range.</p>'; bindControls(); return; }

    let tMin = Math.min(...xs), tMax = Math.max(...xs);
    if (tMin === tMax) { tMin -= 36e5; tMax += 36e5; }  // ±1h so a lone point centers
    let pMin = Math.min(...ys), pMax = Math.max(...ys);
    const padP = (pMax - pMin) * 0.08 || pMax * 0.05 || 1;
    pMin -= padP; pMax += padP;

    // Snap the price axis to round numbers, then expand the domain to those bounds.
    const step = niceStep(pMax - pMin, 4);
    pMin = Math.floor(pMin / step) * step;
    pMax = Math.ceil(pMax / step) * step;
    const tickVals = [];
    for (let v = pMin; v <= pMax + step / 2; v += step) tickVals.push(Math.round(v / step) * step);
    const tickLabels = tickVals.map(fmt);

    // Size the left margin to the widest price label so nothing is clipped.
    const maxChars = Math.max(...tickLabels.map(s => s.length));
    PADL = Math.max(44, Math.round(maxChars * 6) + 14);

    const x = t => PADL + (t - tMin) / (tMax - tMin) * (W - PADL - PADR);
    const y = p => PADT + (1 - (p - pMin) / (pMax - pMin)) * (H - PADT - PADB);

    let svg = `<svg viewBox="0 0 ${W} ${H}" class="chart-svg" role="img" aria-label="Price history">`;

    // One top-down fade gradient per series for the area fill under its line.
    svg += '<defs>' + vis.map(s =>
      `<linearGradient id="cg${s.id}" x1="0" y1="0" x2="0" y2="1">` +
      `<stop offset="0%" stop-color="${s.color}" stop-opacity="0.22"/>` +
      `<stop offset="100%" stop-color="${s.color}" stop-opacity="0"/></linearGradient>`
    ).join('') + '</defs>';

    // Out-of-stock shading
    oos.forEach(([a, b]) => {
      const x1 = x(Math.max(a, tMin)), x2 = x(Math.min(b, tMax));
      if (x2 > x1) svg += `<rect x="${x1.toFixed(1)}" y="${PADT}" width="${(x2 - x1).toFixed(1)}" height="${H - PADT - PADB}" class="chart-oos"/>`;
    });

    // Horizontal gridlines + price labels (right-aligned inside the left margin)
    for (let i = 0; i < tickVals.length; i++) {
      const yy = y(tickVals[i]).toFixed(1);
      svg += `<line x1="${PADL}" y1="${yy}" x2="${W - PADR}" y2="${yy}" class="chart-grid"/>`;
      svg += `<text x="${PADL - 8}" y="${(+yy + 3.5).toFixed(1)}" class="chart-axis chart-axis--y">${tickLabels[i]}</text>`;
    }

    // Vertical time labels. When the visible span is under ~2 days the same date
    // repeats on every tick, so we switch to clock times and print the date only
    // once (and again whenever the day rolls over).
    const span = tMax - tMin;
    const intraday = span < 2 * DAY;
    const tTicks = 4;
    let lastDay = null;
    for (let i = 0; i <= tTicks; i++) {
      const tv = tMin + (tMax - tMin) * i / tTicks;
      const xx = x(tv).toFixed(1);
      const anchor = i === 0 ? 'start' : (i === tTicks ? 'end' : 'middle');
      if (intraday) {
        svg += `<text x="${xx}" y="${H - 18}" class="chart-axis chart-axis--x" style="text-anchor:${anchor}">${fmtTime(tv)}</text>`;
        const dayKey = dayKeyOf(tv);
        if (dayKey !== lastDay) {
          svg += `<text x="${xx}" y="${H - 5}" class="chart-axis chart-axis--date" style="text-anchor:${anchor}">${fmtDate(tv)}</text>`;
          lastDay = dayKey;
        }
      } else {
        svg += `<text x="${xx}" y="${H - 10}" class="chart-axis chart-axis--x" style="text-anchor:${anchor}">${fmtDate(tv)}</text>`;
      }
    }
    // Target line
    if (target != null && target >= pMin && target <= pMax) {
      const ty = y(target).toFixed(1);
      svg += `<line x1="${PADL}" y1="${ty}" x2="${W - PADR}" y2="${ty}" class="chart-target"/>`;
      svg += `<text x="${W - PADR}" y="${(+ty - 4).toFixed(1)}" class="chart-target-label" text-anchor="end">Target ${fmt(target)}</text>`;
    }
    // Area fill + line + dots per series.
    const baseY = (H - PADB).toFixed(1);
    vis.forEach(s => {
      const pts = s.pts.map(p => `${x(p.ms).toFixed(1)} ${y(p.p).toFixed(1)}`);
      const x0 = x(s.pts[0].ms).toFixed(1);
      const xN = x(s.pts[s.pts.length - 1].ms).toFixed(1);
      svg += `<path d="M${x0} ${baseY} L${pts.join(' L')} L${xN} ${baseY} Z" fill="url(#cg${s.id})" class="chart-area"/>`;
      svg += `<path d="M${pts.join(' L')}" fill="none" stroke="${s.color}" stroke-width="2" class="chart-line"/>`;
      s.pts.forEach(p => {
        svg += `<circle cx="${x(p.ms).toFixed(1)}" cy="${y(p.p).toFixed(1)}" r="${p.s ? 2.4 : 3.2}" fill="${p.s ? s.color : 'var(--surface)'}" stroke="${s.color}" stroke-width="1.4"/>`;
      });
    });
    // Last-price pill at the right end of each line.
    vis.forEach(s => {
      const lp = s.pts[s.pts.length - 1];
      const lx = x(lp.ms), ly = y(lp.p), txt = fmt(lp.p);
      const w = txt.length * 5.8 + 10, h = 15;
      let bx = lx + 7;
      if (bx + w > W - 2) bx = lx - 7 - w;          // flip left when no room at the edge
      const by = Math.min(H - PADB - h, Math.max(PADT, ly - h / 2));
      svg += `<g class="chart-endlabel"><rect x="${bx.toFixed(1)}" y="${by.toFixed(1)}" width="${w.toFixed(1)}" height="${h}" rx="4" fill="${s.color}"/>` +
             `<text x="${(bx + w / 2).toFixed(1)}" y="${(by + h - 4.5).toFixed(1)}" text-anchor="middle" class="chart-endlabel__t" fill="${textOn(s.color)}">${txt}</text></g>`;
    });
    svg += `<g id="chart-hl"></g>`;        // hover highlight rings, populated on mousemove
    svg += `<line id="chart-guide" x1="0" y1="${PADT}" x2="0" y2="${H - PADB}" class="chart-guide" style="display:none"/>`;
    svg += `</svg><div id="chart-tip" class="chart-tip" hidden></div>`;
    host.innerHTML = svg;

    bindControls();
    bindHover(vis, x, y, tMin, tMax, intraday);
  }

  function bindControls() {
    document.querySelectorAll('[data-range]').forEach(b => {
      b.classList.toggle('is-active', String(rangeDays) === b.dataset.range);
      b.onclick = () => { rangeDays = +b.dataset.range; render(); };
    });
    document.querySelectorAll('[data-series]').forEach(b => {
      const id = +b.dataset.series;
      b.classList.toggle('is-off', hidden.has(id));
      b.onclick = () => { hidden.has(id) ? hidden.delete(id) : hidden.add(id); render(); };
    });
  }

  function bindHover(vis, x, y, tMin, tMax, intraday) {
    const svg = host.querySelector('svg');
    const guide = host.querySelector('#chart-guide');
    const hl = host.querySelector('#chart-hl');
    const tip = host.querySelector('#chart-tip');
    if (!svg) return;
    const stamps = [...new Set(vis.flatMap(s => s.pts.map(p => p.ms)))].sort((a, b) => a - b);
    svg.addEventListener('mousemove', ev => {
      const rect = svg.getBoundingClientRect();
      const vx = (ev.clientX - rect.left) / rect.width * W;
      const t = tMin + (vx - PADL) / (W - PADL - PADR) * (tMax - tMin);
      // Snap to the nearest real data timestamp.
      let snap = stamps[0], bd = Infinity;
      stamps.forEach(m => { const d = Math.abs(m - t); if (d < bd) { bd = d; snap = m; } });
      let rows = '', rings = '';
      vis.forEach(s => {
        let nearest = null, best = Infinity;
        s.pts.forEach(p => { const dd = Math.abs(p.ms - snap); if (dd < best) { best = dd; nearest = p; } });
        if (nearest) {
          rows += `<div><span class="dot" style="background:${s.color}"></span>${s.store}: <strong>${fmt(nearest.p)}</strong>${nearest.s ? '' : ' <em>(OOS)</em>'}</div>`;
          rings += `<circle cx="${x(nearest.ms).toFixed(1)}" cy="${y(nearest.p).toFixed(1)}" r="5" fill="none" stroke="${s.color}" stroke-width="2" class="chart-hl-ring"/>`;
        }
      });
      const gx = x(snap);
      const stamp = intraday ? fmtDate(snap) + ' ' + fmtTime(snap) : fmtDate(snap);
      guide.setAttribute('x1', gx); guide.setAttribute('x2', gx); guide.style.display = '';
      hl.innerHTML = rings;
      tip.hidden = false;
      tip.innerHTML = `<div class="chart-tip__date">${stamp}</div>${rows}`;
      const px = gx / W * rect.width;
      tip.style.left = Math.min(rect.width - 160, Math.max(0, px + 12)) + 'px';
    });
    svg.addEventListener('mouseleave', () => { guide.style.display = 'none'; hl.innerHTML = ''; tip.hidden = true; });
  }

  // Re-render on resize so the pixel-accurate width stays correct.
  let rz; window.addEventListener('resize', () => { clearTimeout(rz); rz = setTimeout(render, 150); });

  render();
})();
