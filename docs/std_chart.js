/* StdChart: one standard chart for every page (the 'Terminal' look, with drawing tools).
   Candles, 10/21 EMA + 50 SMA, green/red volume with a 30-day average line, black dotted 52-week high/low,
   trade arrows that sit in empty space, and TradingView-style drawing tools saved per ticker. */
(function () {
  const KEY = 'std:', MEM = {};
  // Colour tokens: the hub pages and the published panels name them differently, so each has aliases.
  const ALIAS = { '--line': ['--line', '--grid-line', '--border'], '--dim': ['--dim', '--text-dim'], '--panel': ['--panel', '--panel-bg'], '--bg': ['--bg', '--panel-bg'] };
  const cv = n => { const cs = getComputedStyle(document.documentElement); for (const k of (ALIAS[n] || [n])) { const v = cs.getPropertyValue(k).trim(); if (v) return v; } return n === '--text' ? '#888' : '#8b93a3'; };
  const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const sma = (a, n) => a.map((_, i) => i < n - 1 ? null : a.slice(i - n + 1, i + 1).reduce((x, y) => x + y, 0) / n);
  const ema = (a, n) => { const k = 2 / (n + 1), o = []; let p = null; a.forEach(v => { p = p == null ? v : v * k + p * (1 - k); o.push(p); }); return o; };
  const fmtP = v => v >= 1000 ? v.toLocaleString(undefined, { maximumFractionDigits: 0 }) : v.toFixed(2);
  const fmtV = v => v >= 1e9 ? (v / 1e9).toFixed(2) + 'B' : v >= 1e6 ? (v / 1e6).toFixed(1) + 'M' : v >= 1e3 ? (v / 1e3).toFixed(0) + 'K' : String(v);

  function load(t) {
    let o = null; try { o = JSON.parse(localStorage.getItem(KEY + t) || 'null'); } catch (e) { o = MEM[t] || null; }
    o = o || { draw: [], layout: {} }; o.draw = o.draw || []; o.layout = o.layout || {}; return o;
  }
  function save(t, o) { MEM[t] = o; try { localStorage.setItem(KEY + t, JSON.stringify(o)); } catch (e) {} document.dispatchEvent(new CustomEvent('tv-saved', { detail: t })); }
  function listSaved() { const out = {}; try { for (let i = 0; i < localStorage.length; i++) { const k = localStorage.key(i); if (k.indexOf(KEY) === 0) { const o = JSON.parse(localStorage.getItem(k)); if (o && o.draw && o.draw.length) out[k.slice(KEY.length)] = o.draw.length; } } } catch (e) { Object.keys(MEM).forEach(t => { if (MEM[t].draw.length) out[t] = MEM[t].draw.length; }); } return out; }

  const CSS = '.tv{position:relative}.tv-bar{display:flex;flex-wrap:wrap;gap:6px 8px;align-items:center;margin-bottom:6px}.tv-seg{display:inline-flex;border:1px solid var(--line,var(--border,#d0d4dc));border-radius:8px;overflow:hidden;background:var(--bg,#fff)}' +
    '.tv-seg button,.tv-b{border:0;background:transparent;color:var(--dim,var(--text-dim,#667085));font-weight:600;font-size:11.5px;padding:5px 9px;cursor:pointer;white-space:nowrap;font-family:inherit}.tv-seg button.on,.tv-b.on{background:var(--accent);color:#fff}.tv-seg button:hover:not(.on),.tv-b:hover:not(.on){color:var(--text)}' +
    '.tv-b{border:1px solid var(--line,var(--border,#d0d4dc));border-radius:8px;background:var(--bg,#fff)}.tv-b.pri{background:var(--accent);color:#fff;border-color:var(--accent)}.tv-sw{width:16px;height:16px;border-radius:50%;border:2px solid var(--panel,var(--panel-bg,#fff));box-shadow:0 0 0 1px var(--line,var(--border,#d0d4dc));cursor:pointer;display:inline-block}.tv-sw.on{box-shadow:0 0 0 2px var(--text)}' +
    '.tv-leg{font:11.5px "IBM Plex Mono",ui-monospace,monospace;display:flex;flex-wrap:wrap;gap:3px 12px;min-height:18px;margin-bottom:2px}.tv-wrap{position:relative}.tv-ov{position:absolute;left:0;top:0;pointer-events:none}' +
    '.tv-in{position:absolute;z-index:5;border:1px solid var(--accent);background:var(--panel,var(--panel-bg,#fff));color:var(--text);padding:4px 7px;border-radius:6px;font-size:12px;min-width:150px}.tv-st{font-size:11px;color:var(--dim,var(--text-dim,#667085))}.tv-sep{width:1px;height:18px;background:var(--line,var(--border,#d0d4dc))}' +
    '.tv-modal{position:fixed;inset:0;z-index:80;background:rgba(10,12,16,.55);display:flex;align-items:center;justify-content:center;padding:3vh 3vw}.tv-modal>div{background:var(--panel,var(--panel-bg,#fff));border:1px solid var(--line,var(--border,#d0d4dc));border-radius:14px;width:min(1280px,96vw);max-height:94vh;overflow:auto;padding:14px 16px;box-shadow:var(--shadow)}';
  if (!document.getElementById('tv-css')) { const s = document.createElement('style'); s.id = 'tv-css'; s.textContent = CSS; document.head.appendChild(s); }

  const COLORS = ['#2563eb', '#f59e0b', '#16a34a', '#dc2626', '#8b5cf6', '#e5e7eb'];
  const TOOLS = [['cursor', 'Cursor'], ['trend', 'Trend line'], ['ray', 'Ray'], ['hline', 'Horizontal'], ['rect', 'Box'], ['fib', 'Fib'], ['text', 'Note']];
  const TFS = ['1M', '3M', '6M', '1Y', '2Y', 'All'], TFN = { '1M': 21, '3M': 63, '6M': 126, '1Y': 252, '2Y': 504 };

  function mount(el, S) {
    if (!S.v) { S.v = S.d.map(() => 0); S.novol = true; }
    if (S.entryIdx == null && S.entryDate) S.entryIdx = Math.max(0, S.d.findIndex(x => x >= S.entryDate));
    const n = S.d.length, compact = !!S.compact, H = S.ht || (compact ? 230 : 440), t = S.t;
    const st = load(t), L = Object.assign({ ma: true, vol: true, w52: true, log: false, magnet: true, tf: S.tf || (compact ? '6M' : '6M'), at: S.entryIdx != null ? 'entry' : 'today', dist: false, trades: true }, st.layout); if (S.novol) L.vol = false;
    if (S.tf && !st.layout.tf) L.tf = S.tf;
    const idxOf = {}; S.d.forEach((d, i) => idxOf[d] = i);
    let tool = 'cursor', color = COLORS[0], sel = null, pend = null, ver = 0, dead = false, hoverPt = null;
    el.innerHTML = '<div class="tv">' + (compact ? '' : '<div class="tv-bar" id="b1"></div>') + '<div class="tv-leg" id="lg"></div><div class="tv-wrap" id="wr" style="height:' + H + 'px"><div id="ch"></div><canvas class="tv-ov" id="ov"></canvas></div></div>';
    const q = id => el.querySelector('#' + id), wrap = q('wr'), cd = q('ch'), ov = q('ov'), ctx = ov.getContext('2d');
    const chart = LightweightCharts.createChart(cd, { width: wrap.clientWidth, height: H, layout: { background: { color: 'transparent' }, textColor: cv('--dim'), fontFamily: 'IBM Plex Mono, monospace', fontSize: 10 }, grid: { vertLines: { color: cv('--line') }, horzLines: { color: cv('--line') } }, rightPriceScale: { borderColor: cv('--line'), mode: L.log ? 1 : 0 }, timeScale: { borderColor: cv('--line'), rightOffset: 4 }, crosshair: { mode: 0 }, handleScale: { axisPressedMouseMove: true } });
    const hasTr = !!(S.trades && S.trades.length);
    const topM = hasTr ? .15 : .06, botM = hasTr ? .37 : (L.vol ? .26 : .06);
    // Autoscale to the candles on screen (plus your own lines). The 52-week high/low lines must not stretch
    // the scale: a 52w low far below today's price would squash the whole move into the top of the chart.
    const visRange = () => {
      const r = chart.timeScale().getVisibleLogicalRange(); if (!r) return null;
      const a = Math.max(0, Math.floor(r.from)), b = Math.min(S.d.length - 1, Math.ceil(r.to)); if (b < a) return null;
      let lo = Infinity, hi = -Infinity; for (let i = a; i <= b; i++) { if (S.l[i] < lo) lo = S.l[i]; if (S.h[i] > hi) hi = S.h[i]; }
      (S.lines || []).forEach(x => { if (x.p < lo) lo = x.p; if (x.p > hi) hi = x.p; });
      return { priceRange: { minValue: lo, maxValue: hi } };
    };
    const cs = chart.addCandlestickSeries({ autoscaleInfoProvider: visRange, upColor: cv('--up'), downColor: cv('--down'), borderUpColor: cv('--up'), borderDownColor: cv('--down'), wickUpColor: cv('--up'), wickDownColor: cv('--down'), priceLineVisible: false });
    cs.setData(S.d.map((d, i) => ({ time: d, open: S.o[i], high: S.h[i], low: S.l[i], close: S.c[i] })));
    const MAS = [['10', '#2962FF', ema(S.c, 10), 'EMA'], ['20', '#F23645', ema(S.c, 20), 'EMA'], ['50', '#FF9800', ema(S.c, 50), 'EMA']];
    const maS = MAS.map(([nm, col, v]) => { const l = chart.addLineSeries({ color: col, lineWidth: 1.3, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }); l.setData(S.d.map((d, i) => v[i] == null ? null : { time: d, value: v[i] }).filter(Boolean)); return l; });
    const avg30 = sma(S.v, 30);
    const vs = chart.addHistogramSeries({ priceScaleId: 'v', priceFormat: { type: 'volume' }, priceLineVisible: false, lastValueVisible: false });
    const vavg = chart.addLineSeries({ priceScaleId: 'v', color: cv('--text'), lineWidth: 1.5, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    vavg.setData(S.d.map((d, i) => avg30[i] == null ? null : { time: d, value: avg30[i] }).filter(Boolean));
    chart.priceScale('v').applyOptions({ scaleMargins: { top: .82, bottom: 0 }, visible: false });
    const paintVol = () => vs.setData(S.d.map((d, i) => { let col = S.c[i] >= S.o[i] ? 'rgba(34,197,94,.55)' : 'rgba(239,68,68,.55)';
      if (L.dist && i) { const dn = S.c[i] < S.c[i - 1] && S.v[i] > S.v[i - 1], up = S.c[i] > S.c[i - 1] && S.v[i] > S.v[i - 1]; col = dn ? 'rgba(185,28,28,.98)' : up ? 'rgba(21,128,61,.98)' : 'rgba(140,148,165,.4)'; } return { time: d, value: S.v[i], color: col }; }));
    // 52-week high / low: a plain horizontal line, measured on the chosen date (today, or the entry date on a journal chart)
    let hiL = null, loL = null;
    const w52 = () => { const e = L.at === 'entry' && S.entryIdx != null ? S.entryIdx : n - 1, a = Math.max(0, e - 251); return { hi: Math.max(...S.h.slice(a, e + 1)), lo: Math.min(...S.l.slice(a, e + 1)), e }; };
    function applyLines() {
      if (hiL) { cs.removePriceLine(hiL); hiL = null; } if (loL) { cs.removePriceLine(loL); loL = null; }
      if (L.w52) { const w = w52(), tag = L.at === 'entry' && S.entryIdx != null ? ' at entry' : '';
        hiL = cs.createPriceLine({ price: w.hi, color: cv('--text'), lineWidth: 1, lineStyle: 1, axisLabelVisible: true, title: '52w high' + tag }); loL = cs.createPriceLine({ price: w.lo, color: cv('--text'), lineWidth: 1, lineStyle: 1, axisLabelVisible: true, title: '52w low' + tag }); }
      maS.forEach(s => s.applyOptions({ visible: L.ma })); vs.applyOptions({ visible: L.vol }); vavg.applyOptions({ visible: L.vol });
      cs.priceScale().applyOptions({ mode: L.log ? 1 : 0, scaleMargins: { top: topM, bottom: L.vol || hasTr ? botM : .06 } });
    }
    (S.lines || []).forEach(x => cs.createPriceLine({ price: x.p, color: x.color, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: x.title }));
    paintVol(); applyLines();
    function setTF(k) {
      L.tf = k; const ts = chart.timeScale();
      if (k === 'All') ts.fitContent(); else if (k === 'Trade' && hasTr) { const ix = S.trades.map(x => idxNear(x.d)); ts.setVisibleLogicalRange({ from: Math.max(0, Math.min(...ix) - 30), to: Math.min(n - 1, Math.max(...ix) + 30) + 3 }); }
      else ts.setVisibleLogicalRange({ from: Math.max(-1, n - 1 - (TFN[k] || 126)), to: n + 3 });
    }
    const idxNear = d => { if (idxOf[d] != null) return idxOf[d]; let i = S.d.findIndex(x => x >= d); return i < 0 ? n - 1 : i; };
    setTF(L.tf);
    // ---- legend
    const lg = i => { const up = S.c[i] >= S.o[i]; const wide = wrap.clientWidth >= 640, nm = S.name && S.name !== t ? S.name : '';
      q('lg').innerHTML = (wide && S.logoUrl ? '<img src="' + esc(S.logoUrl) + '" alt="" style="width:18px;height:18px;border-radius:4px;vertical-align:middle" onerror="this.remove()">' : '') + '<b>' + esc(t) + '</b>' + (wide && nm ? '<span style="color:var(--dim,var(--text-dim,#667085))">' + esc(nm) + ' &middot; 1D</span>' : '') + '<span style="color:var(--dim,var(--text-dim,#667085))">' + S.d[i] + '</span><span>O <b>' + fmtP(S.o[i]) + '</b></span><span>H <b>' + fmtP(S.h[i]) + '</b></span><span>L <b>' + fmtP(S.l[i]) + '</b></span><span>C <b style="color:var(--' + (up ? 'up' : 'down') + ')">' + fmtP(S.c[i]) + '</b></span>' + (i ? '<span style="color:var(--' + (S.c[i] >= S.c[i - 1] ? 'up' : 'down') + ')">' + (S.c[i] >= S.c[i - 1] ? '+' : '') + ((S.c[i] / S.c[i - 1] - 1) * 100).toFixed(2) + '%</span>' : '') +
      (L.vol ? '<span>Vol <b>' + fmtV(S.v[i]) + '</b></span>' + (avg30[i] ? '<span>Vol avg 30 <b>' + fmtV(avg30[i]) + '</b></span>' : '') : '') + (L.ma ? MAS.map(m => m[2][i] == null ? '' : '<span style="color:' + m[1] + '">' + m[0] + m[3][0] + ' <b>' + fmtP(m[2][i]) + '</b></span>').join('') : ''); };
    lg(n - 1);
    chart.subscribeCrosshairMove(p => { if (p.time) { const i = idxOf[p.time]; if (i != null) lg(i); } else lg(n - 1); });
    // ---- overlay painting
    const X = i => chart.timeScale().logicalToCoordinate(i), Y = p => cs.priceToCoordinate(p);
    function pts(dr) { return dr.pts.map(pt => ({ x: X(idxOf[pt.d] != null ? idxOf[pt.d] : idxNear(pt.d)), y: Y(pt.p) })); }
    function drawOne(dr, selected, W) {
      const P = pts(dr); if (P.some(p => p.x == null || p.y == null)) return; ctx.save(); ctx.strokeStyle = dr.color; ctx.fillStyle = dr.color; ctx.lineWidth = selected ? 2.4 : 1.6; ctx.setLineDash([]);
      const a = P[0], b = P[1];
      if (dr.type === 'trend') { ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke(); }
      else if (dr.type === 'ray') { const dx = b.x - a.x, dy = b.y - a.y; const k = dx === 0 ? 0 : (W - a.x) / (dx || 1); ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(dx > 0 ? W : a.x + dx * 1000, dx > 0 ? a.y + dy * k : a.y + dy * 1000); ctx.stroke(); }
      else if (dr.type === 'hline') { ctx.beginPath(); ctx.moveTo(0, a.y); ctx.lineTo(W, a.y); ctx.stroke(); ctx.font = '10px IBM Plex Mono, monospace'; const tx = fmtP(dr.pts[0].p); ctx.fillStyle = dr.color; ctx.fillRect(W - 62, a.y - 8, 62, 16); ctx.fillStyle = '#fff'; ctx.fillText(tx, W - 58, a.y + 3.5); }
      else if (dr.type === 'rect') { ctx.globalAlpha = .12; ctx.fillRect(Math.min(a.x, b.x), Math.min(a.y, b.y), Math.abs(b.x - a.x), Math.abs(b.y - a.y)); ctx.globalAlpha = 1; ctx.strokeRect(Math.min(a.x, b.x), Math.min(a.y, b.y), Math.abs(b.x - a.x), Math.abs(b.y - a.y)); }
      else if (dr.type === 'fib') { const hi = dr.pts[0].p, lo = dr.pts[1].p, x0 = Math.min(a.x, b.x); ctx.font = '10px IBM Plex Mono, monospace';
        [0, .236, .382, .5, .618, .786, 1].forEach(r => { const pr = hi + (lo - hi) * r, y = Y(pr); if (y == null) return; ctx.globalAlpha = .75; ctx.beginPath(); ctx.moveTo(x0, y); ctx.lineTo(W, y); ctx.stroke(); ctx.globalAlpha = 1; ctx.fillText((r * 100).toFixed(1) + '%  ' + fmtP(pr), x0 + 4, y - 3); });
        ctx.globalAlpha = .1; ctx.fillRect(x0, Math.min(a.y, b.y), W - x0, Math.abs(b.y - a.y)); ctx.globalAlpha = 1; }
      else if (dr.type === 'text') { ctx.font = '600 12px Inter, sans-serif'; const w = ctx.measureText(dr.text || '').width + 12; ctx.fillStyle = dr.color; ctx.fillRect(a.x, a.y - 11, w, 22); ctx.fillStyle = '#fff'; ctx.fillText(dr.text || '', a.x + 6, a.y + 4); }
      if (selected) { ctx.fillStyle = '#fff'; ctx.strokeStyle = dr.color; P.forEach(p => { ctx.beginPath(); ctx.arc(p.x, p.y, 4, 0, 6.3); ctx.fill(); ctx.stroke(); }); }
      ctx.restore();
    }
    function drawTrades(W, Hh) {
      if (!hasTr || !L.trades) return;
      const items = S.trades.map(tr => { const i = idxNear(tr.d), x = X(i); return x == null ? null : { tr, i, x, buy: /buy/i.test(tr.a), label: tr.label || ((/buy/i.test(tr.a) ? 'B ' : 'S ') + (tr.q || '') + (tr.p ? ' @ ' + fmtP(tr.p) : '')) }; }).filter(Boolean).sort((a, b) => a.x - b.x);
      ctx.font = '600 10.5px IBM Plex Mono, monospace'; const inner = Hh - 28, endsB = [], endsS = [];
      items.forEach(it => {
        const w = ctx.measureText(it.label).width + 12, ends = it.buy ? endsB : endsS; let r = 0; while (ends[r] != null && ends[r] > it.x - w / 2 - 4) r++; ends[r] = it.x + w / 2;
        const ly = it.buy ? inner * (topM + (1 - topM - botM)) + 10 + r * 19 : inner * topM - 24 - r * 19, col = it.tr.color || (it.buy ? '#16a34a' : '#dc2626');
        const barY = it.buy ? Y(S.l[it.i]) : Y(S.h[it.i]); if (barY == null) return;
        const tipY = it.buy ? barY + 5 : barY - 5, fromY = it.buy ? ly : ly + 20;
        ctx.strokeStyle = col; ctx.fillStyle = col; ctx.lineWidth = 1.4; ctx.setLineDash([]); ctx.beginPath(); ctx.moveTo(it.x, fromY); ctx.lineTo(it.x, tipY + (it.buy ? 6 : -6)); ctx.stroke();
        ctx.beginPath(); if (it.buy) { ctx.moveTo(it.x, tipY); ctx.lineTo(it.x - 4.5, tipY + 8); ctx.lineTo(it.x + 4.5, tipY + 8); } else { ctx.moveTo(it.x, tipY); ctx.lineTo(it.x - 4.5, tipY - 8); ctx.lineTo(it.x + 4.5, tipY - 8); } ctx.closePath(); ctx.fill();
        const px = Math.max(2, Math.min(W - w - 2, it.x - w / 2)); ctx.fillStyle = col; ctx.beginPath(); ctx.roundRect ? ctx.roundRect(px, ly, w, 18, 5) : ctx.rect(px, ly, w, 18); ctx.fill(); ctx.fillStyle = '#fff'; ctx.fillText(it.label, px + 6, ly + 12.5);
        const fy = Y(it.tr.p); if (fy != null && it.tr.p) { ctx.beginPath(); ctx.arc(it.x, fy, 3, 0, 6.3); ctx.fillStyle = '#fff'; ctx.fill(); ctx.strokeStyle = col; ctx.lineWidth = 1.5; ctx.stroke(); }
      });
    }
    let lastSig = '';
    function paint(force) {
      if (dead) return; const W = wrap.clientWidth, Hh = H, dpr = window.devicePixelRatio || 1;
      const r = chart.timeScale().getVisibleLogicalRange(), sig = [W, r && r.from, r && r.to, Y(S.c[n - 1]), ver, sel, pend && pend.k, hoverPt && hoverPt.x + ':' + hoverPt.y, L.trades, L.log].join('|');
      if (!force && sig === lastSig) return; lastSig = sig;
      if (ov.width !== W * dpr || ov.height !== Hh * dpr) { ov.width = W * dpr; ov.height = Hh * dpr; ov.style.width = W + 'px'; ov.style.height = Hh + 'px'; }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, W, Hh);
      const cw = W - 60; // keep clear of the price axis
      ctx.save(); ctx.beginPath(); ctx.rect(0, 0, cw, Hh - 28); ctx.clip();
      st.draw.forEach(dr => drawOne(dr, dr.id === sel, cw));
      if (pend && hoverPt) { const tmp = { type: pend.type, color: color, pts: [pend.pt, hoverPt.pt] }; drawOne(tmp, false, cw); }
      ctx.restore(); drawTrades(cw, Hh);
    }
    let raf = 0; (function loop() { if (dead) return; paint(false); raf = requestAnimationFrame(loop); })();
    new ResizeObserver(en => { if (dead) return; try { chart.applyOptions({ width: en[0].contentRect.width }); } catch (e) {} paint(true); }).observe(wrap);
    // ---- drawing interaction
    const toPt = (cx, cy) => { let i = Math.round(chart.timeScale().coordinateToLogical(cx)); i = Math.max(0, Math.min(n - 1, i)); let p = cs.coordinateToPrice(cy); if (p == null) return null;
      if (L.magnet) { const c = [S.o[i], S.h[i], S.l[i], S.c[i]]; let best = null, bd = 9; c.forEach(v => { const y = Y(v); if (y != null && Math.abs(y - cy) < bd) { bd = Math.abs(y - cy); best = v; } }); if (best != null) p = best; } return { d: S.d[i], p: +p.toFixed(2) }; };
    const posIn = e => { const r = wrap.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
    const persist = () => { st.layout = Object.assign({}, L); save(t, st); ver++; flash('Saved'); };
    function flash(m) { const s = el.querySelector('#stt'); if (s) { s.textContent = m + ' · shows on every page'; clearTimeout(flash.t); flash.t = setTimeout(() => { const s2 = el.querySelector('#stt'); if (s2) s2.textContent = st.draw.length ? st.draw.length + ' annotation' + (st.draw.length > 1 ? 's' : '') + ' saved' : 'No annotations yet'; }, 1800); } }
    function setTool(k) { tool = k; pend = null; hoverPt = null; ov.style.pointerEvents = k === 'cursor' ? 'none' : 'auto'; ov.style.cursor = 'crosshair'; wrap.querySelectorAll('.tv-in').forEach(x => x.remove()); renderBar(); }
    function commit(dr) { dr.id = Date.now() + Math.random(); dr.color = color; st.draw.push(dr); sel = dr.id; persist(); setTool('cursor'); }
    ov.addEventListener('click', e => {
      const [x, y] = posIn(e), pt = toPt(x, y); if (!pt) return;
      if (tool === 'hline') { commit({ type: 'hline', pts: [pt] }); }
      else if (tool === 'text') { const inp = document.createElement('input'); inp.className = 'tv-in'; inp.placeholder = 'Note, then Enter'; inp.style.left = Math.min(x, wrap.clientWidth - 170) + 'px'; inp.style.top = y + 'px'; wrap.appendChild(inp); inp.focus(); inp.onkeydown = k => { if (k.key === 'Enter' && inp.value.trim()) { const v = inp.value.trim(); inp.remove(); commit({ type: 'text', pts: [pt], text: v }); } else if (k.key === 'Escape') { inp.remove(); setTool('cursor'); } }; }
      else if (!pend) { pend = { type: tool, pt, k: 1 }; }
      else { commit({ type: pend.type, pts: [pend.pt, pt] }); }
    });
    ov.addEventListener('mousemove', e => { if (!pend) return; const [x, y] = posIn(e), pt = toPt(x, y); hoverPt = pt ? { x, y, pt } : null; });
    function hit(x, y) {
      for (let k = st.draw.length - 1; k >= 0; k--) { const dr = st.draw[k], P = pts(dr); if (P.some(p => p.x == null || p.y == null)) continue; const a = P[0], b = P[1], W = wrap.clientWidth;
        const seg = (p, q) => { const dx = q.x - p.x, dy = q.y - p.y, l2 = dx * dx + dy * dy || 1; let u = ((x - p.x) * dx + (y - p.y) * dy) / l2; u = Math.max(0, Math.min(1, u)); return Math.hypot(x - (p.x + u * dx), y - (p.y + u * dy)); };
        let d = 99;
        if (dr.type === 'trend') d = seg(a, b); else if (dr.type === 'ray') d = seg(a, { x: a.x + (b.x - a.x) * 400, y: a.y + (b.y - a.y) * 400 }); else if (dr.type === 'hline') d = Math.abs(y - a.y);
        else if (dr.type === 'rect') { const inX = x >= Math.min(a.x, b.x) - 4 && x <= Math.max(a.x, b.x) + 4, inY = y >= Math.min(a.y, b.y) - 4 && y <= Math.max(a.y, b.y) + 4; d = inX && inY ? 0 : 99; }
        else if (dr.type === 'fib') { d = 99; [0, .236, .382, .5, .618, .786, 1].forEach(r => { const yy = Y(dr.pts[0].p + (dr.pts[1].p - dr.pts[0].p) * r); if (yy != null && x >= Math.min(a.x, b.x) - 4) d = Math.min(d, Math.abs(y - yy)); }); }
        else if (dr.type === 'text') d = Math.abs(y - a.y) < 12 && x >= a.x - 4 && x <= a.x + 140 ? 0 : 99;
        if (d < 7) return dr.id; }
      return null;
    }
    chart.subscribeClick(p => { if (tool !== 'cursor' || !p.point) return; const h = hit(p.point.x, p.point.y); if (h !== sel) { sel = h; ver++; renderBar(); } });
    const onKey = e => { if (dead) return; if ((e.key === 'Delete' || e.key === 'Backspace') && sel && !/INPUT|TEXTAREA|SELECT/.test((e.target || {}).tagName || '') && wrap.matches(':hover')) { st.draw = st.draw.filter(d => d.id !== sel); sel = null; persist(); renderBar(); } else if (e.key === 'Escape') { setTool('cursor'); } };
    document.addEventListener('keydown', onKey);
    const onSaved = e => { if (e.detail === t && !dead) { const o = load(t); if (o.draw.length !== st.draw.length) { st.draw = o.draw; ver++; renderBar(); } } };
    document.addEventListener('tv-saved', onSaved);
    // ---- toolbar
    function renderBar() {
      const b = q('b1'); if (!b) { return; }
      b.innerHTML = '<div class="tv-seg" id="tf">' + TFS.concat(hasTr ? ['Trade'] : []).map(k => '<button data-k="' + k + '"' + (L.tf === k ? ' class="on"' : '') + '>' + k + '</button>').join('') + '</div><span class="tv-sep"></span>' +
        '<div class="tv-seg" id="tl">' + TOOLS.map(x => '<button data-k="' + x[0] + '"' + (tool === x[0] ? ' class="on"' : '') + '>' + x[1] + '</button>').join('') + '</div>' +
        COLORS.map(c => '<i class="tv-sw' + (c === color ? ' on' : '') + '" data-c="' + c + '" style="background:' + c + '"></i>').join('') +
        '<button class="tv-b" id="un">Undo</button><button class="tv-b" id="dl"' + (sel ? '' : ' disabled style="opacity:.45"') + '>Delete</button><button class="tv-b" id="cl">Clear</button><span class="tv-sep"></span>' +
        [['ma', 'Averages']].concat(S.novol ? [] : [['vol', 'Volume']]).concat([ ['w52', '52w'], ['magnet', 'Magnet'], ['log', 'Log']]).concat(hasTr ? [['trades', 'Trades']] : []).concat(S.distChip ? [['dist', 'Dist/acc days']] : []).map(x => '<button class="tv-b' + (L[x[0]] ? ' on' : '') + '" data-o="' + x[0] + '">' + x[1] + '</button>').join('') +
        (S.entryIdx != null ? '<button class="tv-b" id="at">52w: ' + (L.at === 'entry' ? 'at entry' : 'today') + '</button>' : '') +
        '<span class="tv-sep"></span><button class="tv-b pri" id="sv">Save chart</button><span class="tv-st" id="stt">' + (st.draw.length ? st.draw.length + ' annotation' + (st.draw.length > 1 ? 's' : '') + ' saved' : 'No annotations yet') + '</span>';
      b.querySelector('#tf').onclick = e => { const k = e.target.closest('button'); if (k) { setTF(k.dataset.k); renderBar(); } };
      b.querySelector('#tl').onclick = e => { const k = e.target.closest('button'); if (k) setTool(k.dataset.k); };
      b.querySelectorAll('.tv-sw').forEach(s => s.onclick = () => { color = s.dataset.c; if (sel) { const d = st.draw.find(x => x.id === sel); if (d) { d.color = color; persist(); } } renderBar(); });
      b.querySelectorAll('[data-o]').forEach(x => x.onclick = () => { L[x.dataset.o] = !L[x.dataset.o]; if (x.dataset.o === 'dist') paintVol(); applyLines(); lastSig = ''; renderBar(); });
      b.querySelector('#un').onclick = () => { st.draw.pop(); sel = null; persist(); renderBar(); };
      b.querySelector('#dl').onclick = () => { if (sel) { st.draw = st.draw.filter(d => d.id !== sel); sel = null; persist(); renderBar(); } };
      b.querySelector('#cl').onclick = () => { st.draw = []; sel = null; persist(); renderBar(); };
      const at = b.querySelector('#at'); if (at) at.onclick = () => { L.at = L.at === 'entry' ? 'today' : 'entry'; applyLines(); renderBar(); };
      b.querySelector('#sv').onclick = () => { persist(); flash('Chart saved'); if (S.onSave) S.onSave(); };
    }
    renderBar();
    if (compact && !S.noExpand) { const o = document.createElement('button'); o.className = 'tv-b'; o.textContent = 'Expand'; o.style.cssText = 'position:absolute;right:66px;top:2px;z-index:4;padding:2px 8px;font-size:11px'; o.onclick = () => TV.open(t); wrap.appendChild(o);
      if (st.draw.length) { const bd = document.createElement('span'); bd.className = 'tv-st'; bd.style.cssText = 'position:absolute;left:8px;top:4px;z-index:4;background:var(--panel,var(--panel-bg,#fff));padding:1px 6px;border-radius:6px;border:1px solid var(--line,var(--border,#d0d4dc))'; bd.textContent = st.draw.length + ' note' + (st.draw.length > 1 ? 's' : ''); wrap.appendChild(bd); } }
    return { destroy() { dead = true; cancelAnimationFrame(raf); document.removeEventListener('keydown', onKey); document.removeEventListener('tv-saved', onSaved); try { chart.remove(); } catch (e) {} }, chart, series: cs };
  }

  const TV = { mount, listSaved, source: null, live: [],
    open(t, extra) {
      const s = TV.source && TV.source(t); if (!s) return;
      const m = document.createElement('div'); m.className = 'tv-modal'; m.innerHTML = '<div><div style="display:flex;align-items:center;gap:12px;margin-bottom:8px"><b style="font-size:16px">' + esc(t) + '</b><span class="tv-st">' + esc(s.name || '') + '</span><span style="flex:1"></span><button class="tv-b" id="x">Close</button></div><div id="m"></div></div>';
      document.body.appendChild(m); const h = TV.mount(m.querySelector('#m'), Object.assign({}, s, { t, ht: Math.max(380, Math.min(640, innerHeight - 250)) }, extra || {}));
      const close = () => { h.destroy(); m.remove(); }; m.querySelector('#x').onclick = close; m.onclick = e => { if (e.target === m) close(); };
    },
    card(el, t, opt) { const s = TV.source && TV.source(t); if (!s) { el.innerHTML = '<div class="tv-st" style="padding:12px">No price history in this demo.</div>'; return null; } const h = TV.mount(el, Object.assign({}, s, { t, compact: true }, opt || {})); TV.live.push(h); return h; },
    killAll() { TV.live.forEach(h => { try { h.destroy(); } catch (e) {} }); TV.live = []; }
  };
  window.StdChart = TV;
})();
