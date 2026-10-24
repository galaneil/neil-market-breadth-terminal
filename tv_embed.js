/* TVE: picks and draws the price chart on every page that has one.
 *
 * Three charts, one switch:
 *   Chart       our standard chart (std_chart.js): candles, 10 EMA blue / 20 EMA red / 50 EMA orange, volume with its
 *               30-day average, 52-week lines, drawing tools. Default when there is room.
 *   TradingView the embedded TradingView chart. Its free embed cannot colour each moving average separately and does
 *               not remember changes, so it is an option, not the default.
 *   Terminal    the compact chart each page already had. Default in tight spaces (cards, side sheets).
 *
 * TradingView's free embed also refuses NSE data and the raw US index symbols (SPX, IXIC, RUT); the matching ETFs
 * (SPY, QQQ, IWM) work and carry real volume, which an index lacks. Where it cannot show a symbol the button is off.
 * The choice is remembered separately for tight and wide spaces.
 */
(function () {
  var KEY = "chartEngine", uid = 0, loader = null, stdLoader = null;
  var BASE = (document.currentScript && document.currentScript.src || "").replace(/tv_embed\.js.*$/, "");
  var QS = (document.currentScript && document.currentScript.src || "").replace(/^[^?]*/, "");

  // index panel key -> [TradingView symbol, label shown to the user]
  var INDEX = {
    index_nasdaq100: ["NASDAQ:QQQ", "Nasdaq 100 via QQQ"],
    index_nasdaq: ["NASDAQ:ONEQ", "Nasdaq Composite via ONEQ"],
    index_sp500: ["AMEX:SPY", "S&P 500 via SPY"],
    index_russell2000: ["AMEX:IWM", "Russell 2000 via IWM"],
    index_sensex: ["BSE:SENSEX", "BSE Sensex"],
  };

  function sizeClass(w) { return w && w < 760 ? "tight" : "wide"; }
  function engine(w) {
    var c = sizeClass(w);
    try { var v = localStorage.getItem(KEY + "." + c); if (v === "std" || v === "tv" || v === "terminal") return v; } catch (e) {}
    return c === "tight" ? "terminal" : "std";
  }
  function setEngine(v, w) { try { localStorage.setItem(KEY + "." + sizeClass(w), v); } catch (e) {} }

  function isDark() {
    var bg = getComputedStyle(document.body).backgroundColor || "", m = bg.match(/\d+(\.\d+)?/g);
    if (!m || m.length < 3) return window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches;
    return (0.299 * m[0] + 0.587 * m[1] + 0.114 * m[2]) < 128;
  }

  function loadScript(src, ready, done) {
    if (ready()) return done();
    var s = document.createElement("script"); s.src = src;
    s.onload = function () { done(); }; s.onerror = function () { done(true); };
    document.head.appendChild(s);
  }
  function loadTV(cb) {
    if (window.TradingView && window.TradingView.widget) return cb();
    if (!loader) {
      loader = [];
      loadScript("https://s3.tradingview.com/tv.js", function () { return false; }, function (failed) {
        var q = loader; loader = null; q.forEach(function (f) { f(failed); });
      });
    }
    loader.push(cb);
  }
  function loadStd(cb) {
    if (window.StdChart) return cb();
    if (!stdLoader) {
      stdLoader = [];
      loadScript(BASE + "std_chart.js" + QS, function () { return false; }, function (failed) {
        var q = stdLoader; stdLoader = null; q.forEach(function (f) { f(failed); });
      });
    }
    stdLoader.push(cb);
  }

  var STYLE =
    ".tve-seg{display:inline-flex;border:1px solid var(--border,#d0d4dc);border-radius:8px;overflow:hidden;background:transparent;vertical-align:middle}" +
    ".tve-seg button{border:0;background:transparent;color:var(--text-dim,#667085);font:600 12px inherit;font-family:inherit;padding:4px 10px;cursor:pointer}" +
    ".tve-seg button.on{background:var(--accent,#2563eb);color:#fff}.tve-seg button:disabled{opacity:.4;cursor:not-allowed}" +
    ".tve-note{font-size:11.5px;color:var(--text-dim,#667085);margin-left:8px}.tve-host{width:100%}.tve-msg{padding:18px;color:var(--text-dim,#667085);font-size:12.5px}";
  if (!document.getElementById("tve-css")) { var st = document.createElement("style"); st.id = "tve-css"; st.textContent = STYLE; document.head.appendChild(st); }

  // US stocks: the bare ticker resolves to its primary listing. India: only a BSE symbol is embeddable.
  function stockSymbol(ticker, country, tvSym) {
    if (!ticker) return null;
    if (tvSym && /^[A-Z]+:/.test(tvSym)) return /^(NSE):/.test(tvSym) ? null : tvSym;
    if (country === "IN" || country === "India") return null;
    return String(ticker).toUpperCase();
  }
  function indexSymbol(key) { var x = INDEX[key]; return x ? x[0] : null; }
  function indexLabel(key) { var x = INDEX[key]; return x ? x[1] : ""; }

  function mount(host, symbol, height) {
    host.innerHTML = "";
    host.style.height = (height || 440) + "px";
    var id = "tve" + (++uid), d = document.createElement("div");
    d.id = id; d.style.height = "100%"; host.appendChild(d);
    loadTV(function (failed) {
      if (failed || !document.getElementById(id)) { if (failed) host.innerHTML = '<div class="tve-msg">Could not reach TradingView.</div>'; return; }
      new window.TradingView.widget({
        container_id: id, autosize: true, symbol: symbol, interval: "D", timezone: "exchange",
        theme: isDark() ? "dark" : "light", style: "1", locale: "en",
        enable_publishing: false, allow_symbol_change: true, withdateranges: true, hide_side_toolbar: false, save_image: false,
        studies: [
          { id: "MAExp@tv-basicstudies", inputs: { length: 10 } },
          { id: "MAExp@tv-basicstudies", inputs: { length: 20 } },
          { id: "MAExp@tv-basicstudies", inputs: { length: 50 } },
        ],
        // Legend: name, price and the bar's % change only. No OHLC, no moving-average readouts (the lines say it).
        overrides: {
          "paneProperties.legendProperties.showSeriesOHLC": false,
          "paneProperties.legendProperties.showStudyValues": false,
          "paneProperties.legendProperties.showStudyArguments": false,
          "paneProperties.legendProperties.showBarChange": true,
        },
      });
    });
  }

  // Our standard chart. `data` is {d,o,h,l,c,v,name,logoUrl,t,...} or a promise of it; null/missing volume is fine.
  var stdHandles = new WeakMap();
  function mountStd(host, data, opt) {
    var prev = stdHandles.get(host); if (prev) { try { prev.destroy(); } catch (e) {} stdHandles.delete(host); }
    host.innerHTML = '<div class="tve-msg">Loading chart…</div>'; host.style.height = "";
    Promise.resolve(data).then(function (D) {
      if (!D || !D.d || !D.d.length) { host.innerHTML = '<div class="tve-msg">No price history for this symbol.</div>'; return; }
      loadStd(function (failed) {
        if (failed || !window.StdChart) { host.innerHTML = '<div class="tve-msg">Could not load the chart.</div>'; return; }
        host.innerHTML = "";
        var clean = Object.assign({}, D);
        // drop any bar with a missing price so the arrays stay aligned
        var ok = []; for (var i = 0; i < clean.d.length; i++) if (clean.o[i] != null && clean.h[i] != null && clean.l[i] != null && clean.c[i] != null) ok.push(i);
        if (ok.length !== clean.d.length) {
          ["d", "o", "h", "l", "c", "v"].forEach(function (k) { if (clean[k]) clean[k] = ok.map(function (j) { return clean[k][j]; }); });
        }
        if (clean.v && clean.v.every(function (x) { return x == null; })) clean.v = null;
        else if (clean.v) clean.v = clean.v.map(function (x) { return x || 0; });
        var h = window.StdChart.mount(host, Object.assign(clean, opt || {}));
        stdHandles.set(host, h);
      });
    });
  }

  /* attach({ bar, host, symbol, data, height, hide, note, onApply })
   *   bar    element that receives the Chart | TradingView | Terminal switch
   *   host   empty element where the Chart / TradingView is drawn
   *   hide   elements belonging to the Terminal chart (hidden while Chart or TradingView shows)
   *   symbol TradingView symbol, or null when it cannot be embedded
   *   data   function returning the standard chart's data (or a promise of it), or omitted when not available
   */
  function attach(o) {
    var seg = document.createElement("div"); seg.className = "tve-seg";
    seg.innerHTML = '<button data-e="std">Chart</button><button data-e="tv">TradingView</button><button data-e="terminal">Terminal</button>';
    var note = document.createElement("span"); note.className = "tve-note";
    o.bar.appendChild(seg); o.bar.appendChild(note);
    var width = o.bar.clientWidth || (o.host.parentNode && o.host.parentNode.clientWidth) || 0;
    var stdBtn = seg.querySelector('[data-e="std"]'), tvBtn = seg.querySelector('[data-e="tv"]');
    function apply() {
      var canStd = typeof o.data === "function", canTV = !!o.symbol, want = engine(width), eng = want;
      if (eng === "std" && !canStd) eng = canTV ? "tv" : "terminal";
      if (eng === "tv" && !canTV) eng = canStd ? "std" : "terminal";
      stdBtn.disabled = !canStd; tvBtn.disabled = !canTV;
      tvBtn.title = canTV ? "" : "TradingView does not allow this market in embedded charts";
      ["std", "tv", "terminal"].forEach(function (e) { seg.querySelector('[data-e="' + e + '"]').classList.toggle("on", e === eng); });
      note.textContent = eng === "tv" ? (o.note || "") : (eng === "terminal" && !canTV && want === "tv" ? "TradingView cannot show this symbol, so the Terminal chart is used" : "");
      var showHost = eng !== "terminal";
      o.host.style.display = showHost ? "" : "none";
      (o.hide || []).forEach(function (el) { if (el) el.style.display = showHost ? "none" : ""; });
      if (eng === "tv") mount(o.host, o.symbol, o.height);
      else if (eng === "std") mountStd(o.host, o.data(), { ht: o.height, tf: o.tf || "1Y" });
      else o.host.innerHTML = "";
      if (o.onApply) o.onApply(eng);
    }
    seg.onclick = function (ev) { var b = ev.target.closest("button"); if (!b || b.disabled) return; setEngine(b.dataset.e, width); apply(); };
    apply();
    return { refresh: apply };
  }

  window.TVE = { engine: engine, setEngine: setEngine, stockSymbol: stockSymbol, indexSymbol: indexSymbol, indexLabel: indexLabel, mount: mount, mountStd: mountStd, attach: attach, loadStd: loadStd };
})();
