/* TVE: the standard TradingView chart, used on every page that shows a price chart.
 *
 * Candles, volume, and the same three EMAs as the rest of the app (10, 20, 50).
 * One shared "chart engine" choice (TradingView / Terminal) is remembered in the
 * browser, so switching on one page applies everywhere.
 *
 * Two things TradingView's free embed cannot do, so they fall back to the
 * Terminal chart automatically:
 *   - NSE data (India) is not allowed in embeds; only BSE:SENSEX works.
 *   - The raw US index symbols (SPX, IXIC, RUT) are blocked; the matching ETFs
 *     (SPY, QQQ, IWM) are allowed and also carry real volume, which an index lacks.
 * Annotations drawn inside the embed are not saved by us (TradingView keeps them).
 */
(function () {
  var KEY = "chartEngine", uid = 0, loader = null;

  // index panel key -> [TradingView symbol, label shown to the user]
  var INDEX = {
    index_nasdaq: ["NASDAQ:QQQ", "Nasdaq via QQQ"],
    index_sp500: ["AMEX:SPY", "S&P 500 via SPY"],
    index_russell2000: ["AMEX:IWM", "Russell 2000 via IWM"],
    index_sensex: ["BSE:SENSEX", "BSE Sensex"],
  };

  function engine() { try { return localStorage.getItem(KEY) || "tv"; } catch (e) { return "tv"; } }
  function setEngine(v) { try { localStorage.setItem(KEY, v); } catch (e) {} }

  function isDark() {
    var bg = getComputedStyle(document.body).backgroundColor || "", m = bg.match(/\d+(\.\d+)?/g);
    if (!m || m.length < 3) return window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches;
    return (0.299 * m[0] + 0.587 * m[1] + 0.114 * m[2]) < 128;
  }

  function loadTV(cb) {
    if (window.TradingView && window.TradingView.widget) return cb();
    if (!loader) {
      loader = [];
      var s = document.createElement("script");
      s.src = "https://s3.tradingview.com/tv.js";
      s.onload = function () { var q = loader; loader = null; q.forEach(function (f) { f(); }); };
      s.onerror = function () { var q = loader; loader = null; q.forEach(function (f) { f(true); }); };
      document.head.appendChild(s);
    }
    loader.push(cb);
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
      });
    });
  }

  /* attach({ bar, host, symbol, height, hide, note, onApply })
   *   bar    element that receives the TradingView | Terminal switch
   *   host   empty element where the TradingView chart is drawn
   *   hide   elements belonging to the Terminal chart (hidden while TradingView shows)
   *   symbol TradingView symbol, or null when it cannot be embedded (Terminal only)
   */
  function attach(o) {
    var seg = document.createElement("div"); seg.className = "tve-seg";
    seg.innerHTML = '<button data-e="tv">TradingView</button><button data-e="terminal">Terminal</button>';
    var note = document.createElement("span"); note.className = "tve-note";
    o.bar.appendChild(seg); o.bar.appendChild(note);
    var tvBtn = seg.querySelector('[data-e="tv"]');
    function apply() {
      var can = !!o.symbol, tv = engine() === "tv" && can;
      tvBtn.disabled = !can; tvBtn.title = can ? "" : "TradingView does not allow this market in embedded charts";
      seg.querySelector('[data-e="tv"]').classList.toggle("on", tv);
      seg.querySelector('[data-e="terminal"]').classList.toggle("on", !tv);
      note.textContent = tv ? (o.note || "") : (can ? "" : "TradingView cannot show this symbol, so the Terminal chart is used");
      o.host.style.display = tv ? "" : "none";
      (o.hide || []).forEach(function (el) { if (el) el.style.display = tv ? "none" : ""; });
      if (tv) mount(o.host, o.symbol, o.height); else o.host.innerHTML = "";
      if (o.onApply) o.onApply(tv);
    }
    seg.onclick = function (ev) { var b = ev.target.closest("button"); if (!b || b.disabled) return; setEngine(b.dataset.e); apply(); };
    apply();
    return { refresh: apply, setSymbol: function (s) { o.symbol = s; apply(); } };
  }

  window.TVE = { engine: engine, setEngine: setEngine, stockSymbol: stockSymbol, indexSymbol: indexSymbol, indexLabel: indexLabel, mount: mount, attach: attach };
})();
