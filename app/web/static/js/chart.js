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
  const fmtDate = ms => new Date(ms).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });

  // Normalize point times to epoch ms.
  series.forEach(s => s.points.forEach(p => { p.ms = Date.parse(p.t); }));
  const oos = (data.oosSpans || []).map(([a, b]) => [Date.parse(a), Date.parse(b)]);
  const target = (typeof data.target === 'number') ? data.target : null;

  const W = 1000, H = 280, PADR = 28, PADT = 20, PADB = 42;
  let PADL = 84;                      // recomputed per render to fit price labels
  const hidden = new Set();           // store ids toggled off
  let rangeDays = 0;                  // 0 = all

  const allMs = series.flatMap(s => s.points.map(p => p.ms));
  const maxMs = Math.max(...allMs);

  function visibleSeries() { return series.filter(s => !hidden.has(s.id)); }

  function render() {
    const fromMs = rangeDays ? maxMs - rangeDays * 864e5 : -Infinity;
    const vis = visibleSeries().map(s => ({
      ...s, pts: s.points.filter(p => p.ms >= fromMs && p.p != null)
    })).filter(s => s.pts.length);

    const xs = vis.flatMap(s => s.pts.map(p => p.ms));
    const ys = vis.flatMap(s => s.pts.map(p => p.p));
    if (target != null) ys.push(target);
    if (!xs.length) { host.innerHTML = '<p class="chart-empty">No points in this range.</p>'; bindControls(); return; }

    let tMin = Math.min(...xs), tMax = Math.max(...xs);
    if (tMin === tMax) { tMin -= 864e5; tMax += 864e5; }
    let pMin = Math.min(...ys), pMax = Math.max(...ys);
    const padP = (pMax - pMin) * 0.08 || pMax * 0.05 || 1;
    pMin -= padP; pMax += padP;

    // Size the left margin to the widest price label so nothing is clipped.
    const ticks = 5;
    const tickVals = Array.from({ length: ticks + 1 }, (_, i) => pMin + (pMax - pMin) * i / ticks);
    const tickLabels = tickVals.map(fmt);
    const maxChars = Math.max(...tickLabels.map(s => s.length));
    PADL = Math.max(72, Math.round(maxChars * 7.2) + 18);

    const x = t => PADL + (t - tMin) / (tMax - tMin) * (W - PADL - PADR);
    const y = p => PADT + (1 - (p - pMin) / (pMax - pMin)) * (H - PADT - PADB);

    let svg = `<svg viewBox="0 0 ${W} ${H}" class="chart-svg" role="img" aria-label="Price history">`;

    // Out-of-stock shading
    oos.forEach(([a, b]) => {
      const x1 = x(Math.max(a, tMin)), x2 = x(Math.min(b, tMax));
      if (x2 > x1) svg += `<rect x="${x1.toFixed(1)}" y="${PADT}" width="${(x2 - x1).toFixed(1)}" height="${H - PADT - PADB}" class="chart-oos"/>`;
    });

    // Horizontal gridlines + price labels (right-aligned inside the left margin)
    for (let i = 0; i <= ticks; i++) {
      const yy = y(tickVals[i]).toFixed(1);
      svg += `<line x1="${PADL}" y1="${yy}" x2="${W - PADR}" y2="${yy}" class="chart-grid"/>`;
      svg += `<text x="${PADL - 10}" y="${(+yy + 4).toFixed(1)}" class="chart-axis chart-axis--y">${tickLabels[i]}</text>`;
    }
    // Vertical time labels (ends anchored inward so they don't clip)
    const tTicks = 5;
    for (let i = 0; i <= tTicks; i++) {
      const tv = tMin + (tMax - tMin) * i / tTicks;
      const xx = x(tv).toFixed(1);
      const anchor = i === 0 ? 'start' : (i === tTicks ? 'end' : 'middle');
      svg += `<text x="${xx}" y="${H - 13}" class="chart-axis chart-axis--x" style="text-anchor:${anchor}">${fmtDate(tv)}</text>`;
    }
    // Target line
    if (target != null && target >= pMin && target <= pMax) {
      const ty = y(target).toFixed(1);
      svg += `<line x1="${PADL}" y1="${ty}" x2="${W - PADR}" y2="${ty}" class="chart-target"/>`;
      svg += `<text x="${W - PADR}" y="${(+ty - 5).toFixed(1)}" class="chart-target-label" text-anchor="end">Target ${fmt(target)}</text>`;
    }
    // Series lines + dots
    vis.forEach(s => {
      const d = s.pts.map((p, i) => `${i ? 'L' : 'M'}${x(p.ms).toFixed(1)} ${y(p.p).toFixed(1)}`).join(' ');
      svg += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2.5" class="chart-line"/>`;
      s.pts.forEach(p => {
        svg += `<circle cx="${x(p.ms).toFixed(1)}" cy="${y(p.p).toFixed(1)}" r="${p.s ? 2.6 : 3.4}" fill="${p.s ? s.color : 'var(--surface)'}" stroke="${s.color}" stroke-width="1.4"/>`;
      });
    });
    svg += `<line id="chart-guide" x1="0" y1="${PADT}" x2="0" y2="${H - PADB}" class="chart-guide" style="display:none"/>`;
    svg += `</svg><div id="chart-tip" class="chart-tip" hidden></div>`;
    host.innerHTML = svg;

    bindControls();
    bindHover(vis, x, y, tMin, tMax);
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

  function bindHover(vis, x, y, tMin, tMax) {
    const svg = host.querySelector('svg');
    const guide = host.querySelector('#chart-guide');
    const tip = host.querySelector('#chart-tip');
    if (!svg) return;
    svg.addEventListener('mousemove', ev => {
      const rect = svg.getBoundingClientRect();
      const vx = (ev.clientX - rect.left) / rect.width * W;
      const t = tMin + (vx - PADL) / (W - PADL - PADR) * (tMax - tMin);
      let rows = '';
      vis.forEach(s => {
        let nearest = null, best = Infinity;
        s.pts.forEach(p => { const dd = Math.abs(p.ms - t); if (dd < best) { best = dd; nearest = p; } });
        if (nearest) rows += `<div><span class="dot" style="background:${s.color}"></span>${s.store}: <strong>${fmt(nearest.p)}</strong>${nearest.s ? '' : ' <em>(OOS)</em>'}</div>`;
      });
      const dateMs = Math.min(Math.max(t, tMin), tMax);
      guide.setAttribute('x1', x(dateMs)); guide.setAttribute('x2', x(dateMs)); guide.style.display = '';
      tip.hidden = false;
      tip.innerHTML = `<div class="chart-tip__date">${fmtDate(dateMs)}</div>${rows}`;
      const left = Math.min(rect.width - 160, Math.max(0, (ev.clientX - rect.left) + 12));
      tip.style.left = left + 'px';
    });
    svg.addEventListener('mouseleave', () => { guide.style.display = 'none'; tip.hidden = true; });
  }

  render();
})();
