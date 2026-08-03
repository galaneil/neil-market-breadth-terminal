(function () {
  "use strict";
  const DATA = JSON.parse(document.getElementById("dashboard-data").textContent);
  // The replay page carries a differently-shaped payload with no `series`
  // key, and shares this script, so this must not be assumed present.
  const S = DATA.series || {};
  const root = document.documentElement;

  // ---------- Theme ----------
  const themeBtn = document.getElementById("theme-toggle");
  function applyTheme(t) {
    if (t) root.setAttribute("data-theme", t); else root.removeAttribute("data-theme");
    themeBtn.innerHTML = (currentTheme() === "dark") ? "&#9728;" : "&#9789;";
  }
  function currentTheme() {
    const explicit = root.getAttribute("data-theme");
    if (explicit) return explicit;
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  applyTheme(localStorage.getItem("mbt-theme") || "");
  themeBtn.addEventListener("click", function () {
    const next = currentTheme() === "dark" ? "light" : "dark";
    localStorage.setItem("mbt-theme", next);
    applyTheme(next);
    redrawAllCharts();
  });

  // ---------- Shared context across panels ----------
  // The replay page and the stock page are separate documents — in Notion,
  // two separate embeds — but both are served from the same origin, so they
  // can share state. Writing to localStorage fires a `storage` event in every
  // OTHER same-origin document (never in the one that wrote it), including a
  // sibling iframe on the same page. So a ticker typed into the replay search
  // drives the stock chart, and a date stepped on either side moves both,
  // without anything being retyped.
  //
  // Echo loops are avoided structurally rather than with a guard flag: only
  // user-event handlers ever publish, and incoming state is applied by
  // setting .value programmatically (which does not fire input/change) and
  // calling the render functions directly.
  //
  // Every access is wrapped, because an iframe sandboxed without
  // allow-same-origin throws on any storage access. If that happens the
  // pages simply stop talking to each other and still work standalone.
  // Namespaced per country. The two markets have different tickers AND
  // different trading calendars, so a US date or symbol arriving on an Indian
  // panel is meaningless — it silently snapped the Indian charts back to
  // whatever session the US panels were last left on. Panels sync with their
  // own market's panels only.
  const SYNC_KEY = "mbt-context-" + (DATA.country || "US");
  const Sync = {
    read: function () {
      try { return JSON.parse(localStorage.getItem(SYNC_KEY) || "null") || {}; }
      catch (e) { return {}; }
    },
    publish: function (patch) {
      try {
        const merged = Object.assign(this.read(), patch, { ts: Date.now() });
        localStorage.setItem(SYNC_KEY, JSON.stringify(merged));
      } catch (e) { /* storage unavailable — sync is best-effort */ }
    },
    subscribe: function (fn) {
      window.addEventListener("storage", function (e) {
        if (e.key !== SYNC_KEY || !e.newValue) return;
        let payload;
        try { payload = JSON.parse(e.newValue); } catch (err) { return; }
        fn(payload);
      });
    },
  };

  // Newest date on or before `wanted`, clamped to the ends. Shared context can
  // carry a date a given ticker has no bar for (a holiday, or a listing that
  // starts later than the market history), so both panels snap rather than
  // showing nothing.
  function snapToDate(dates, wanted) {
    if (!dates.length) return null;
    if (!wanted || wanted >= dates[dates.length - 1]) return dates[dates.length - 1];
    if (wanted <= dates[0]) return dates[0];
    let chosen = dates[0];
    for (let i = 0; i < dates.length; i++) {
      if (dates[i] <= wanted) chosen = dates[i]; else break;
    }
    return chosen;
  }

  // ---------- Formatting helpers ----------
  function fmtNum(v, d) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return Number(v).toLocaleString(undefined, { minimumFractionDigits: d || 0, maximumFractionDigits: d || 2 });
  }
  function fmtSignedPct(v) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return (v >= 0 ? "+" : "") + v.toFixed(2) + "%";
  }
  function fmtPlainPct(v) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return v.toFixed(2) + "%";
  }
  function fmtSignedInt(v) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return (v > 0 ? "+" : "") + v;
  }
  function pctClass(v) {
    if (v === null || v === undefined || isNaN(v)) return "";
    return v > 0 ? "up" : (v < 0 ? "down" : "");
  }
  function average(items, field) {
    const vals = items.map(function (r) { return r[field]; })
      .filter(function (v) { return v !== null && v !== undefined && !isNaN(v); });
    if (!vals.length) return null;
    return vals.reduce(function (a, b) { return a + b; }, 0) / vals.length;
  }
  // How many consecutive days (ending today) has the same side been ahead -
  // a plain run-length count, not a synthesized signal. Always computed off
  // the FULL history regardless of the selected timeframe, since "how many
  // days in a row" is a property of the raw day-by-day series, not something
  // that means anything averaged over a window.
  function currentStreak(rows, field) {
    if (!rows.length) return null;
    const sign = function (v) { return v > 0 ? 1 : (v < 0 ? -1 : 0); };
    const lastSign = sign(rows[rows.length - 1][field]);
    if (lastSign === 0) return { sign: 0, days: 0 };
    let days = 0;
    for (let i = rows.length - 1; i >= 0; i--) {
      if (sign(rows[i][field]) === lastSign) days++;
      else break;
    }
    return { sign: lastSign, days: days };
  }
  function cssVar(name) {
    return getComputedStyle(root).getPropertyValue(name).trim();
  }
  function themeColors() {
    return {
      grid: cssVar("--grid-line"),
      text: cssVar("--text-dim"),
      accent: cssVar("--accent"),
      up: cssVar("--up"),
      down: cssVar("--down"),
    };
  }
  // With few data points (e.g. day 1 of the system, or a fresh drill-down
  // selection), a line has nothing to connect and a 0-radius point draws
  // nothing at all — so show a visible dot until there's enough history for
  // a clean connected line to read better on its own.
  function dotRadius(n) {
    return n <= 3 ? 3 : 0;
  }

  // The net figures are signed, so the caption has to follow the sign — a
  // negative advance/decline average under the words "more stocks rising than
  // falling" says the opposite of the number above it.
  function netAdvDeclCaption(v) {
    if (v === null || v === undefined || isNaN(v)) return "advancers versus decliners on a typical day";
    return v >= 0 ? "more stocks rising than falling on a typical day"
                  : "more stocks falling than rising on a typical day";
  }
  function netHiLoPhrase(v) {
    return (v >= 0 ? "more new highs than new lows" : "more new lows than new highs");
  }

  // ---------- Timeframe filtering ----------
  const TIMEFRAMES = ["1W", "1M", "3M", "6M", "YTD", "ALL"];
  const TIMEFRAME_DAYS = { "1W": 7, "1M": 30, "3M": 91, "6M": 182 };
  function filterByTimeframe(rows, tf) {
    if (!rows.length || tf === "ALL") return rows;
    const lastDate = new Date(rows[rows.length - 1].date);
    let cutoff = null;
    if (tf === "YTD") {
      cutoff = new Date(lastDate.getFullYear(), 0, 1);
    } else if (TIMEFRAME_DAYS[tf]) {
      cutoff = new Date(lastDate);
      cutoff.setDate(cutoff.getDate() - TIMEFRAME_DAYS[tf]);
    }
    if (!cutoff) return rows;
    return rows.filter(function (r) { return new Date(r.date) >= cutoff; });
  }

  const registeredCards = []; // functions to call to redraw at the currently-active timeframe
  function setupTimeframeToggle(container, renderFn) {
    container.innerHTML = "";
    TIMEFRAMES.forEach(function (tf) {
      const btn = document.createElement("button");
      btn.className = "tf-btn" + (tf === "ALL" ? " active" : "");
      btn.textContent = tf === "ALL" ? "Since Inception" : tf;
      btn.dataset.tf = tf;
      btn.addEventListener("click", function () {
        container.querySelectorAll(".tf-btn").forEach(function (b) { b.classList.remove("active"); });
        btn.classList.add("active");
        renderFn(tf);
      });
      container.appendChild(btn);
    });
    function currentTf() {
      const active = container.querySelector(".tf-btn.active");
      return active ? active.dataset.tf : "ALL";
    }
    registeredCards.push(function () { renderFn(currentTf()); });
    renderFn("ALL");
  }
  function redrawAllCharts() {
    registeredCards.forEach(function (fn) { fn(); });
    lwUpdaters.forEach(function (fn) { fn(); });
  }

  // ---------- Chart.js wrapper ----------
  const charts = {};
  function lineChart(canvasId, labels, datasets, opts) {
    opts = opts || {};
    const ctx = document.getElementById(canvasId);
    if (!ctx) return;
    if (charts[canvasId]) charts[canvasId].destroy();
    const colors = themeColors();
    charts[canvasId] = new Chart(ctx, {
      type: "line",
      data: { labels: labels, datasets: datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: datasets.length > 1, labels: { color: colors.text, boxWidth: 10, font: { size: 10 } } },
        },
        scales: {
          x: { ticks: { color: colors.text, maxTicksLimit: 6, font: { size: 10 } }, grid: { color: colors.grid } },
          y: {
            reverse: !!opts.yReverse,
            ticks: { color: colors.text, font: { size: 10 } },
            grid: { color: colors.grid },
          },
        },
      },
    });
  }

  // ---------- Market environment panel ----------
  // Reads the stored daily environment record rather than deriving anything
  // here, so the browser and any later consumer (replay, trade log) always
  // agree on what the environment was on a given date.
  // Keyed by the bare index key (no "index_" prefix), which is how the
  // environment record's per_index map is keyed. Derived from the payload so
  // India's four indices render their factor rows too — a hardcoded US map
  // silently produced an empty factor list for any other market.
  const INDEX_SHORT = (function () {
    const labels = DATA.indexLabels;
    if (!labels) return { nasdaq: "NASDAQ", sp500: "S&P 500", russell2000: "Russell 2000" };
    const out = {};
    Object.keys(labels).forEach(function (k) { out[k.replace(/^index_/, "")] = labels[k]; });
    return out;
  })();
  const MOVER_WINDOW_LABELS = { "1w": "1 week", "1m": "1 month" };

  // Which groups are gaining and losing traction. Shown over a week and a
  // month rather than a day, so a name that has genuinely rotated from dead
  // to leading (or has quietly fallen apart) actually surfaces.
  function moverList(entries, direction) {
    if (!entries || !entries.length) return "";
    let html = '<div class="mover-group"><div class="mover-group-label ' + direction + '">' +
      (direction === "up" ? "Leaders" : "Laggards") + "</div>";
    entries.forEach(function (e) {
      html += '<div class="mover-row">' +
        '<span class="mover-name">' + e.name + "</span>" +
        '<span class="mover-chg ' + pctClass(e.chg) + '">' + fmtSignedPct(e.chg) + "</span>" +
        "</div>";
    });
    return html + "</div>";
  }

  function moversColumn(title, block) {
    return '<div><div class="movers-col-title">' + title + "</div>" +
      moverList(block.top, "up") + moverList(block.bottom, "down") + "</div>";
  }

  function moversHtml(leaders) {
    const windows = Object.keys(MOVER_WINDOW_LABELS).filter(function (w) { return leaders[w]; });
    if (!windows.length) return "";
    let buttons = "";
    windows.forEach(function (w, idx) {
      buttons += '<button class="tf-btn mover-btn' + (idx === 0 ? " active" : "") +
        '" data-window="' + w + '">' + MOVER_WINDOW_LABELS[w] + "</button>";
    });
    return '<div class="movers">' +
      '<div class="movers-head"><span class="movers-title">Leaders and laggards</span>' + buttons + "</div>" +
      '<div class="movers-grid"></div></div>';
  }

  function wireMoversToggle(host, leaders) {
    if (!leaders) return;
    const grid = host.querySelector(".movers-grid");
    const buttons = host.querySelectorAll(".mover-btn");
    if (!grid || !buttons.length) return;

    function draw(w) {
      const data = leaders[w];
      grid.innerHTML = moversColumn("Sectors", data.sectors) + moversColumn("Industries", data.industries);
    }
    buttons.forEach(function (btn) {
      btn.addEventListener("click", function () {
        buttons.forEach(function (b) { b.classList.remove("active"); });
        btn.classList.add("active");
        draw(btn.dataset.window);
      });
    });
    draw(buttons[0].dataset.window);
  }

  function renderEnvironmentPanel() {
    const host = document.getElementById("environment-panel");
    if (!host) return;
    const rows = S.environment || [];
    if (!rows.length) {
      host.innerHTML = '<div class="empty-note">No data yet.</div>';
      return;
    }
    const env = rows[rows.length - 1];
    // 9 for a 3-index market, 12 for India's 4 — read off the record rather
    // than assumed, so the caption never contradicts the chart.
    const factorTotal = (env.trend && env.trend.factors_total) || 9;

    function tag(label) {
      return '<span class="env-tag ' + label + '">' + label + "</span>";
    }
    function factorPill(on, text) {
      return '<span class="factor-pill ' + (on ? "up" : "down") + '">' + text + "</span>";
    }

    let factorRows = "";
    const per = (env.trend && env.trend.per_index) || {};
    Object.keys(INDEX_SHORT).forEach(function (key) {
      const f = per[key];
      if (!f) return;
      factorRows +=
        '<div class="factor-row">' +
          '<span class="factor-name">' + INDEX_SHORT[key] + "</span>" +
          factorPill(f.above_ema10, "10") +
          factorPill(f.above_ema20, "20") +
          factorPill(f.above_ema50, "50") +
        "</div>";
    });

    const t = env.trend, p = env.participation, i = env.internals;

    host.innerHTML =
      '<div class="card env-card">' +
        '<div class="env-headline">' +
          '<span class="env-verdict ' + env.overall + '">' + env.overall + "</span>" +
          '<span class="env-date">as of ' + env.date + "</span>" +
        "</div>" +
        '<div class="env-grid">' +

          '<div class="env-block">' +
            '<div class="env-block-title">Trend ' + (t ? tag(t.label) : "") + "</div>" +
            '<div class="env-block-value">' + (t ? t.factors_favourable + " / " + t.factors_total : "—") + "</div>" +
            '<div class="env-block-sub">index vs EMA 10 / 20 / 50' +
              (t ? " &middot; large caps only " + t.large_cap_favourable + " / " + t.large_cap_total : "") + "</div>" +
            '<div class="factor-grid">' + factorRows + "</div>" +
          "</div>" +

          '<div class="env-block">' +
            '<div class="env-block-title">Participation ' + (p ? tag(p.label) : "") + "</div>" +
            '<div class="env-stat">' +
              '<span class="env-stat-num">' + (p && p.sectors_positive !== null ? p.sectors_positive + " / " + p.sectors_total : "—") + "</span>" +
              '<span class="env-stat-label">sectors' + (p && p.sectors_positive_pct !== null ? " &middot; " + p.sectors_positive_pct + "%" : "") + "</span>" +
            "</div>" +
            '<div class="env-stat">' +
              '<span class="env-stat-num">' + (p && p.industries_positive !== null ? p.industries_positive + " / " + p.industries_total : "—") + "</span>" +
              '<span class="env-stat-label">industries' + (p && p.industries_positive_pct !== null ? " &middot; " + p.industries_positive_pct + "%" : "") + "</span>" +
            "</div>" +
            '<div class="env-block-sub">positive over the last 20 days</div>' +
          "</div>" +

          '<div class="env-block">' +
            '<div class="env-block-title">Internals ' + (i ? tag(i.label) : "") + "</div>" +
            '<div class="env-block-value ' + (i ? pctClass(i.adv_decl_avg) : "") + '">' +
              (i && i.adv_decl_avg !== null ? fmtSignedInt(Math.round(i.adv_decl_avg)) : "—") + "</div>" +
            '<div class="env-block-sub">' + netAdvDeclCaption(i ? i.adv_decl_avg : null) + "</div>" +
            '<div class="env-block-extra">' +
              (i && i.new_hilo_avg !== null
                ? fmtSignedInt(Math.round(i.new_hilo_avg)) + " " + netHiLoPhrase(i.new_hilo_avg) + " &middot; both averaged over " + i.lookback_days + " sessions"
                : "&nbsp;") +
            "</div>" +
          "</div>" +

        "</div>" +
        (env.leaders ? moversHtml(env.leaders) : "") +
        '<div class="env-chart-block">' +
          '<div class="env-chart-title">Trend strength over time</div>' +
          '<div class="env-block-sub">how many of the ' + factorTotal + " index-vs-EMA factors were favourable each day &middot; " +
            factorTotal + " is fully bullish, 0 fully bearish</div>" +
          '<div class="tf-toggle"></div>' +
          '<div class="chart-wrap"><canvas id="environment-canvas"></canvas></div>' +
        "</div>" +
      "</div>";

    wireMoversToggle(host, env.leaders);

    setupTimeframeToggle(host.querySelector(".tf-toggle"), function (tf) {
      const filtered = filterByTimeframe(rows, tf);
      const colors = themeColors();
      const dotR = dotRadius(filtered.length);
      lineChart("environment-canvas", filtered.map(function (r) { return r.date; }), [
        {
          label: "Favourable index factors (of 9)",
          data: filtered.map(function (r) { return r.trend ? r.trend.factors_favourable : null; }),
          borderColor: colors.accent, borderWidth: 1.5, pointRadius: dotR,
          pointBackgroundColor: colors.accent, tension: 0.15,
        },
      ]);
    });
  }

  // ---------- Index cards (TradingView lightweight-charts) ----------
  // Drawn as HLC bars rather than a line: with four overlapping lines the
  // close was hard to pick out. Bars also make the daily range visible, and
  // the chart is scrollable/zoomable like a real charting package.
  // EMA colours match Neil's own TradingView layout so the two read the same:
  // 10 blue, 20 red, 50 orange.
  const EMA_COLORS = { ema10: "#2962FF", ema20: "#F23645", ema50: "#FF9800" };
  const lwUpdaters = [];

  // Shift-drag anywhere on a chart to measure the move between two points,
  // reported as a percentage. lightweight-charts has no measure tool, but it
  // exposes coordinate/price conversion, which is all this needs. Shift is
  // required so a plain drag still pans the chart.
  function enableMeasure(chart, series, container) {
    container.style.position = "relative";
    const box = document.createElement("div");
    box.className = "measure-box";
    const shade = document.createElement("div");
    shade.className = "measure-shade";
    container.appendChild(shade);
    container.appendChild(box);

    let startY = null, startX = null, startPrice = null;

    function hide() {
      box.style.display = "none";
      shade.style.display = "none";
      startY = null;
    }
    hide();

    container.addEventListener("mousedown", function (e) {
      if (!e.shiftKey) return;
      const rect = container.getBoundingClientRect();
      startY = e.clientY - rect.top;
      startX = e.clientX - rect.left;
      startPrice = series.coordinateToPrice(startY);
      if (startPrice === null) { startY = null; return; }
      e.preventDefault();
    });

    container.addEventListener("mousemove", function (e) {
      if (startY === null) return;
      const rect = container.getBoundingClientRect();
      const y = e.clientY - rect.top, x = e.clientX - rect.left;
      const price = series.coordinateToPrice(y);
      if (price === null || !startPrice) return;

      const pct = (price / startPrice - 1) * 100;
      const up = pct >= 0;
      shade.style.display = "block";
      shade.style.left = Math.min(startX, x) + "px";
      shade.style.top = Math.min(startY, y) + "px";
      shade.style.width = Math.abs(x - startX) + "px";
      shade.style.height = Math.abs(y - startY) + "px";
      shade.style.background = up ? "rgba(22,163,74,0.14)" : "rgba(220,38,38,0.14)";

      box.style.display = "block";
      box.style.left = Math.min(x + 8, container.clientWidth - 120) + "px";
      box.style.top = Math.max(2, y - 30) + "px";
      box.className = "measure-box " + (up ? "up" : "down");
      box.textContent = (up ? "+" : "") + pct.toFixed(2) + "%  ·  " +
        fmtNum(Math.abs(price - startPrice), 2);
    });

    ["mouseup", "mouseleave"].forEach(function (ev) {
      container.addEventListener(ev, hide);
    });
  }

  function lwTheme() {
    return {
      bg: cssVar("--panel-bg"),
      text: cssVar("--text-dim"),
      grid: cssVar("--grid-line"),
      border: cssVar("--border"),
      up: cssVar("--up"),
      down: cssVar("--down"),
    };
  }

  function renderIndexCard(seriesKey, label, grid) {
    const rows = S[seriesKey] || [];
    const card = document.createElement("div");
    card.className = "card";
    grid.appendChild(card);
    const chartId = seriesKey + "-lw";

    if (!rows.length) {
      card.innerHTML = '<div class="card-title">' + label + '</div><div class="empty-note">No data yet.</div>';
      return;
    }
    const last = rows[rows.length - 1];
    const prev = rows.length > 1 ? rows[rows.length - 2] : null;
    const chg = prev && prev.close ? ((last.close - prev.close) / prev.close * 100) : null;

    function badge(txt, dir) {
      return '<span class="badge on ' + dir + '">' + txt + "</span>";
    }

    card.innerHTML =
      '<div class="card-title">' + label + "</div>" +
      '<div class="card-value ' + pctClass(chg) + '">' + fmtNum(last.close, 2) + "</div>" +
      '<div class="card-sub">' + (chg === null ? "&nbsp;" : fmtSignedPct(chg) + " vs prior close") + "</div>" +
      '<div class="badge-row">' +
        badge("EMA10 " + (last.above_ema10 ? "above" : "below"), last.above_ema10 ? "up" : "down") +
        badge("EMA20 " + (last.above_ema20 ? "above" : "below"), last.above_ema20 ? "up" : "down") +
        badge("EMA50 " + (last.above_ema50 ? "above" : "below"), last.above_ema50 ? "up" : "down") +
      "</div>" +
      '<div class="tf-toggle"></div>' +
      '<div class="lw-chart" id="' + chartId + '"></div>';

    const container = document.getElementById(chartId);
    const th = lwTheme();

    const chart = LightweightCharts.createChart(container, {
      height: container.clientHeight || 380,
      layout: { background: { type: "solid", color: th.bg }, textColor: th.text, fontSize: 11 },
      grid: { vertLines: { color: th.grid }, horzLines: { color: th.grid } },
      rightPriceScale: { borderColor: th.border },
      timeScale: { borderColor: th.border, rightOffset: 2 },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    });

    const barSeries = chart.addBarSeries({
      upColor: th.up,
      downColor: th.down,
      openVisible: false,   // HLC bars, not OHLC
      thinBars: false,
    });
    enableMeasure(chart, barSeries, container);
    const emaSeries = {};
    ["ema10", "ema20", "ema50"].forEach(function (key) {
      emaSeries[key] = chart.addLineSeries({
        color: EMA_COLORS[key],
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      });
    });

    function draw(tf) {
      const filtered = filterByTimeframe(rows, tf);
      barSeries.setData(filtered.map(function (r) {
        return { time: r.date, open: r.open, high: r.high, low: r.low, close: r.close };
      }));
      ["ema10", "ema20", "ema50"].forEach(function (key) {
        emaSeries[key].setData(filtered.map(function (r) { return { time: r.date, value: r[key] }; }));
      });
      chart.timeScale().fitContent();
    }

    setupTimeframeToggle(card.querySelector(".tf-toggle"), draw);

    lwUpdaters.push(function () {
      const t = lwTheme();
      chart.applyOptions({
        layout: { background: { type: "solid", color: t.bg }, textColor: t.text },
        grid: { vertLines: { color: t.grid }, horzLines: { color: t.grid } },
        rightPriceScale: { borderColor: t.border },
        timeScale: { borderColor: t.border },
      });
      barSeries.applyOptions({ upColor: t.up, downColor: t.down });
    });

    if (window.ResizeObserver) {
      new ResizeObserver(function () {
        chart.applyOptions({ width: container.clientWidth, height: container.clientHeight });
        // Resizing keeps the existing bar spacing, so a chart that was fitted
        // at one width ends up showing only part of its range at another -
        // which is exactly what happens on first paint inside an iframe.
        // Re-fit so the selected timeframe always fills the visible area.
        chart.timeScale().fitContent();
      }).observe(container);
    }
  }

  // ---------- Simple current-value + chart cards (breadth internals) ----------
  // The headline number reflects the AVERAGE over whichever timeframe is
  // currently selected, not always the latest day — a single day's breadth
  // reading is noisy, so "today's value" doesn't answer "what has breadth
  // looked like over the last month/YTD/etc.", which is what the timeframe
  // buttons are for.
  function renderSimpleMetricCard(seriesKey, label, valueField, grid, opts) {
    opts = opts || {};
    const rows = S[seriesKey] || [];
    const card = document.createElement("div");
    card.className = "card";
    grid.appendChild(card);
    const canvasId = seriesKey + "-canvas";

    if (!rows.length) {
      card.innerHTML = '<div class="card-title">' + label + '</div><div class="empty-note">No data yet.</div>';
      return;
    }

    card.innerHTML =
      '<div class="card-title">' + label + "</div>" +
      '<div class="card-value">&nbsp;</div>' +
      '<div class="card-sub">&nbsp;</div>' +
      (opts.streakLabels ? '<div class="streak-badge"></div>' : "") +
      '<div class="tf-toggle"></div>' +
      '<div class="chart-wrap"><canvas id="' + canvasId + '"></canvas></div>';

    const valueEl = card.querySelector(".card-value");
    const subEl = card.querySelector(".card-sub");

    if (opts.streakLabels) {
      const streak = currentStreak(rows, opts.streakField || valueField);
      const streakEl = card.querySelector(".streak-badge");
      if (streak && streak.days > 0) {
        const streakLabel = streak.sign > 0 ? opts.streakLabels.pos : opts.streakLabels.neg;
        streakEl.className = "streak-badge " + (streak.sign > 0 ? "up" : "down");
        streakEl.textContent = streakLabel + " · " + streak.days + " straight day" + (streak.days === 1 ? "" : "s");
      }
    }

    setupTimeframeToggle(card.querySelector(".tf-toggle"), function (tf) {
      const filtered = filterByTimeframe(rows, tf);
      const colors = themeColors();
      const dotR = dotRadius(filtered.length);
      const avgVal = average(filtered, valueField);

      valueEl.className = "card-value " + (opts.colorByValue ? pctClass(avgVal) : "");
      valueEl.textContent = avgVal === null ? "—" : (opts.isPct ? fmtPlainPct(avgVal) : fmtSignedInt(Math.round(avgVal)));

      const n = filtered.length;
      const extra = opts.subText ? opts.subText(filtered) + " — " : "";
      subEl.textContent = extra + "avg over " + n + " session" + (n === 1 ? "" : "s");

      lineChart(canvasId, filtered.map(function (r) { return r.date; }), [
        { label: label, data: filtered.map(function (r) { return r[valueField]; }), borderColor: colors.accent, borderWidth: 1.5, pointRadius: dotR, pointBackgroundColor: colors.accent, tension: 0.15 },
      ]);
    });
  }

  // ---------- 52-week highs and lows, as two separate counts ----------
  // The net card next to this one collapses both sides into one number, which
  // hides the thing that matters: 210 highs against 200 lows and 12 against 2
  // both net to +10, and they are not the same market. Here each side keeps its
  // own number and its own line.
  //
  // The headline is the LATEST session (the number to write down), while the
  // sub-line carries the average over the selected timeframe — the opposite
  // emphasis to the other breadth cards, because a raw count is what gets
  // logged and an average is the context for it.
  function renderHiLoCountsCard(seriesKey, label, grid) {
    const rows = S[seriesKey] || [];
    const card = document.createElement("div");
    card.className = "card";
    grid.appendChild(card);
    const canvasId = seriesKey + "-canvas";

    if (!rows.length) {
      card.innerHTML = '<div class="card-title">' + label + '</div><div class="empty-note">No data yet.</div>';
      return;
    }

    const latest = rows[rows.length - 1];

    // Every sector that has ever contributed a high or a low, so the selector
    // is built from the data rather than a hardcoded taxonomy — the US and
    // India use different sector names and neither is enumerated here.
    const sectorSet = {};
    rows.forEach(function (r) {
      Object.keys(r.hi_by_sector || {}).forEach(function (s) { sectorSet[s] = true; });
      Object.keys(r.lo_by_sector || {}).forEach(function (s) { sectorSet[s] = true; });
    });
    const sectors = Object.keys(sectorSet).sort();

    // A row's count for the current selection: the whole-market total, or one
    // sector's share of it. Rows written before sector composition was stored
    // have no map at all, which reads as zero rather than breaking the line.
    function hiOf(r, sector) {
      return sector ? ((r.hi_by_sector || {})[sector] || 0) : r.new_highs;
    }
    function loOf(r, sector) {
      return sector ? ((r.lo_by_sector || {})[sector] || 0) : r.new_lows;
    }

    // Which sector is contributing more highs (or lows) than its size alone
    // would produce.
    //
    // A RAW count answers the wrong question. Finance is 348 of the 1,500 US
    // names — 23% of the universe — so it tops a raw count on an ordinary day
    // without leading anything at all. Over the full history it supplied 23% of
    // all new highs: exactly its weight, i.e. no signal. So the leader is
    // chosen by share ÷ weight, and the multiple is shown, which is the
    // "is this sector genuinely strong or just big" test.
    //
    // The minimum-count guard stops a three-member sector with two highs from
    // claiming an 8x lead off a sample too small to mean anything.
    const MEMBERS = DATA.sectorMembers || {};
    const UNIVERSE = Object.keys(MEMBERS).reduce(function (a, s) { return a + MEMBERS[s]; }, 0);

    function concentration(items, field) {
      const totals = {};
      let all = 0;
      items.forEach(function (r) {
        const m = r[field];
        // Sessions stored before sector composition existed have no map. They
        // are skipped on both sides, so the share is a percentage of the days
        // it can actually account for rather than a diluted one.
        if (!m) return;
        Object.keys(m).forEach(function (s) { totals[s] = (totals[s] || 0) + m[s]; });
        all += (field === "hi_by_sector" ? r.new_highs : r.new_lows);
      });
      const names = Object.keys(totals);
      if (!names.length || !all) return null;

      // A small sector clears a low bar on noise: Communications is ~15 names,
      // and 4 hits out of 78 read as an 8.5x lead. Requiring a tenth of the
      // total keeps the claim proportional to the sample it rests on.
      const floor = Math.max(5, all * 0.10);
      const eligible = names.filter(function (s) { return totals[s] >= floor; });
      const pool = eligible.length ? eligible : names;

      function weightOf(s) {
        return UNIVERSE && MEMBERS[s] ? MEMBERS[s] / UNIVERSE : null;
      }
      function ratioOf(s) {
        const w = weightOf(s);
        return w ? (totals[s] / all) / w : 0;
      }
      pool.sort(function (a, b) {
        const d = ratioOf(b) - ratioOf(a);
        return d !== 0 ? d : totals[b] - totals[a];
      });
      const top = pool[0];
      const w = weightOf(top);
      return {
        sector: top, count: totals[top], total: all,
        share: Math.round(100 * totals[top] / all),
        weight: w === null ? null : Math.round(100 * w),
        ratio: w ? (totals[top] / all) / w : null,
      };
    }

    function leadPhrase(c, kind, cls) {
      if (!c) return "";
      const ratio = c.ratio === null ? "" :
        ' <span class="hilo-ratio">' + c.ratio.toFixed(1) + "&times; its " + c.weight + "% of the market</span>";
      return kind + ' led by <b class="' + cls + '">' + c.sector + "</b> — " +
        c.count + " of " + c.total + " (" + c.share + "%)" + ratio;
    }

    card.innerHTML =
      '<div class="card-title">' + label + "</div>" +
      '<div class="hilo-pair">' +
        '<div class="hilo-side"><div class="card-value up" data-hi>' + latest.new_highs + "</div>" +
          '<div class="hilo-label" data-hi-label>new highs</div></div>' +
        '<div class="hilo-side"><div class="card-value down" data-lo>' + latest.new_lows + "</div>" +
          '<div class="hilo-label" data-lo-label>new lows</div></div>' +
      "</div>" +
      '<div class="card-sub">&nbsp;</div>' +
      (sectors.length
        ? '<div class="hilo-lead"></div>' +
          '<select class="sector-filter"><option value="">All sectors</option>' +
          sectors.map(function (s) { return '<option value="' + s + '">' + s + "</option>"; }).join("") +
          "</select>"
        : "") +
      '<div class="tf-toggle"></div>' +
      '<div class="chart-wrap"><canvas id="' + canvasId + '"></canvas></div>';

    const subEl = card.querySelector(".card-sub");
    const hiEl = card.querySelector("[data-hi]");
    const loEl = card.querySelector("[data-lo]");
    const hiLabelEl = card.querySelector("[data-hi-label]");
    const loLabelEl = card.querySelector("[data-lo-label]");
    const leadEl = card.querySelector(".hilo-lead");
    const selectEl = card.querySelector(".sector-filter");
    let sector = "";
    let currentTf = null;

    function draw(tf) {
      currentTf = tf || currentTf;
      const filtered = filterByTimeframe(rows, currentTf);
      const colors = themeColors();
      const dotR = dotRadius(filtered.length);
      const n = filtered.length;
      const hiData = filtered.map(function (r) { return hiOf(r, sector); });
      const loData = filtered.map(function (r) { return loOf(r, sector); });

      // The headline follows the timeframe. Pinned to the latest session it
      // made the buttons look broken: filtering to Electronic Technology and
      // stepping 1W -> 1M -> YTD left "1 high / 2 lows" frozen on screen while
      // everything below it moved. The daily average is the figure that varies
      // with the window; the latest session moves to the sub-line, where it is
      // still there to be logged.
      const mean = function (a) {
        return a.length ? a.reduce(function (x, y) { return x + y; }, 0) / a.length : 0;
      };
      // A sector averaging 0.4 highs a day is not averaging "0" — under ten,
      // rounding to a whole number throws away the signal.
      const show = function (v) { return v >= 10 ? String(Math.round(v)) : v.toFixed(1); };
      const tfName = currentTf === "ALL" ? "since inception" : currentTf;
      const hiLatest = hiOf(latest, sector), loLatest = loOf(latest, sector);

      hiEl.textContent = show(mean(hiData));
      loEl.textContent = show(mean(loData));
      hiLabelEl.textContent = "new highs/day · " + tfName;
      loLabelEl.textContent = "new lows/day · " + tfName;

      subEl.textContent =
        (sector ? sector + " · " : "") +
        "latest " + latest.date + ": " + hiLatest + " high" + (hiLatest === 1 ? "" : "s") +
        ", " + loLatest + " low" + (loLatest === 1 ? "" : "s") +
        " · peaks " + Math.max.apply(null, hiData) + " / " + Math.max.apply(null, loData) +
        " over " + n + " session" + (n === 1 ? "" : "s");

      if (leadEl) {
        const h = leadPhrase(concentration(filtered, "hi_by_sector"), "Highs", "up");
        const l = leadPhrase(concentration(filtered, "lo_by_sector"), "Lows", "down");
        leadEl.innerHTML = h + (h && l ? "<br>" : "") + l;
      }

      lineChart(canvasId, filtered.map(function (r) { return r.date; }), [
        { label: "New highs" + (sector ? " — " + sector : ""), data: hiData,
          borderColor: colors.up, borderWidth: 1.5, pointRadius: dotR,
          pointBackgroundColor: colors.up, tension: 0.15 },
        { label: "New lows" + (sector ? " — " + sector : ""), data: loData,
          borderColor: colors.down, borderWidth: 1.5, pointRadius: dotR,
          pointBackgroundColor: colors.down, tension: 0.15 },
      ]);
    }

    if (selectEl) {
      selectEl.addEventListener("change", function () {
        sector = selectEl.value;
        draw();
      });
    }
    setupTimeframeToggle(card.querySelector(".tf-toggle"), draw);
  }

  // ---------- New highs / lows screener ----------
  // The panel above this one counts hits per session. This one counts
  // COMPANIES over a period, which is a different number and the one worth
  // acting on: 215 distinct names made a 52-week high in the week to
  // 2026-07-29, while the daily totals for that week sum past 400 because a
  // stock printing highs all week is counted every day.
  //
  // Three lookbacks, because a 52-week high is a late signal for anything
  // coming off a deep base — a stock 70% off its high can double and still be
  // nowhere near a one-year high, but it will print 13-week highs immediately.
  const HILO_WINDOWS = [["w13", "13-week"], ["w26", "26-week"], ["w52", "52-week"]];

  function renderHiloScreener() {
    // Two pages share this: the screener (table) and the group chart, split
    // apart so each can be embedded on its own. Every section below is
    // guarded, so a page carrying only one of them works untouched.
    const table = document.getElementById("hilo-screener-table");
    const compositionCanvas = document.getElementById("hilo-composition-canvas");
    if (!table && !compositionCanvas) return;

    const counts = DATA.hiloCounts || [];
    const names = DATA.hiloNames || [];
    const quotes = DATA.quotes || {};
    const cls = DATA.classification || {};
    const body = table ? table.querySelector("tbody") : null;
    const hiEl = document.querySelector("[data-hi]");
    const loEl = document.querySelector("[data-lo]");
    const hiLabelEl = document.querySelector("[data-hi-label]");
    const loLabelEl = document.querySelector("[data-lo-label]");
    const leadEl = document.querySelector(".hilo-lead");
    const sectorSel = document.getElementById("hilo-sector");
    const industrySel = document.getElementById("hilo-industry");
    const searchEl = document.getElementById("hilo-search");
    const resultEl = document.getElementById("hilo-result-count");

    let win = "w52";
    let side = "hi";
    let tf = "1M";
    let indexFilter = "";
    let adrBand = "";
    let rows = [];

    // ADR bands, from the actual distribution of the US universe: median
    // 3.6%, a tenth below 1.9%, a tenth above 7.1%. A 2% ADR name at a new
    // high is a different instrument from a 9% one — same signal, but one
    // cannot be traded on a stop that sits outside the daily noise.
    const ADR_BANDS = {
      low: [0, 3], mid: [3, 7], high: [7, Infinity],
    };

    function buildToggle(id, items, get, set) {
      const host = document.getElementById(id);
      if (!host) return;
      host.innerHTML = "";
      items.forEach(function (item) {
        const btn = document.createElement("button");
        btn.className = "tf-btn" + (item[0] === get() ? " active" : "");
        btn.textContent = item[1];
        btn.addEventListener("click", function () {
          host.querySelectorAll(".tf-btn").forEach(function (b) { b.classList.remove("active"); });
          btn.classList.add("active");
          set(item[0]);
          refresh();
        });
        host.appendChild(btn);
      });
    }

    // Sessions in the chosen period that we hold NAMES for. Ticker lists are
    // capped at six months while counts run a full year, so a longer timeframe
    // silently falls back to the names we have rather than under-reporting.
    function namesInPeriod() {
      const filtered = filterByTimeframe(names, tf);
      return filtered.length ? filtered : names;
    }

    // Index membership. Screening the whole 3,300-name tape buries the real
    // constituents under shells and microcaps, so the default is to look at
    // one index at a time.
    const MEMBERSHIP = DATA.indexMembership || {};
    function inIndex(ticker) {
      if (!indexFilter) return true;
      const codes = MEMBERSHIP[ticker];
      return !!codes && codes.indexOf(indexFilter) !== -1;
    }

    // One row per distinct company, with how many sessions it printed on and
    // when it last did. The session count separates a name making highs every
    // day from one that touched a high once and rolled over.
    function buildRows() {
      const period = namesInPeriod();
      const hits = {}, lastSeen = {};
      period.forEach(function (r) {
        const list = ((r[win] || {})[side]) || [];
        list.forEach(function (t) {
          if (!inIndex(t)) return;
          hits[t] = (hits[t] || 0) + 1;
          if (!lastSeen[t] || r.date > lastSeen[t]) lastSeen[t] = r.date;
        });
      });
      return Object.keys(hits).map(function (t) {
        const tags = cls[t] || ["", ""];
        const q = quotes[t] || [null, null];
        return { ticker: t, sector: tags[0] || "—", industry: tags[1] || "—",
                 close: q[0], chg: q[1], adr: q[2] === undefined ? null : q[2],
                 hits: hits[t], last: lastSeen[t] };
      });
    }

    function fillSelect(sel, values, keepValue) {
      const first = sel.options[0].cloneNode(true);
      sel.innerHTML = "";
      sel.appendChild(first);
      values.forEach(function (v) {
        const o = document.createElement("option");
        o.value = v; o.textContent = v;
        sel.appendChild(o);
      });
      if (keepValue && values.indexOf(keepValue) !== -1) sel.value = keepValue;
    }

    function visibleRows() {
      const s = sectorSel.value, ind = industrySel.value;
      const q = (searchEl.value || "").trim().toUpperCase();
      const band = ADR_BANDS[adrBand];
      return rows.filter(function (r) {
        if (s && r.sector !== s) return false;
        if (ind && r.industry !== ind) return false;
        if (q && r.ticker.indexOf(q) === -1) return false;
        // A name with no ADR is dropped by an ADR filter rather than passed
        // through it — "unknown" is not "matches".
        if (band && (r.adr === null || r.adr < band[0] || r.adr >= band[1])) return false;
        return true;
      });
    }

    // A broad screen returns 1,500+ names, and building that many rows makes
    // the panel stutter inside an iframe. The list is always sorted, so the cap
    // only ever hides the tail — narrow with the filters to see further in.
    const MAX_ROWS = 400;

    function draw(sorted) {
      if (!body) return;
      const shown = sorted.slice(0, MAX_ROWS);
      if (resultEl) resultEl.textContent = sorted.length === rows.length
        ? rows.length + " companies" + (sorted.length > MAX_ROWS ? " — showing the first " + MAX_ROWS : "")
        : sorted.length + " of " + rows.length + " companies" +
          (sorted.length > MAX_ROWS ? " — showing the first " + MAX_ROWS : "");
      if (!sorted.length) {
        body.innerHTML = '<tr><td colspan="7" class="empty-note">Nothing matches.</td></tr>';
        return;
      }
      body.innerHTML = shown.map(function (r) {
        return '<tr data-ticker="' + r.ticker + '">' +
          '<td class="name-cell">' + logoImg(r.ticker) + "<span><b>" + r.ticker + "</b></span></td>" +
          "<td>" + r.sector + "</td><td>" + r.industry + "</td>" +
          "<td>" + (r.close === null ? "—" : r.close.toFixed(2)) + "</td>" +
          '<td class="pct ' + pctClass(r.chg) + '">' + (r.chg === null ? "—" : fmtSignedPct(r.chg)) + "</td>" +
          "<td>" + (r.adr === null ? "—" : r.adr.toFixed(1) + "%") + "</td>" +
          "<td>" + r.hits + "</td><td>" + r.last + "</td>" +
        "</tr>";
      }).join("");
    }

    function redrawTable() {
      if (!table) return;
      draw(sortRows(visibleRows(), "hits", -1));
    }

    function drawGroups() {
      const views = {
        digest: document.getElementById("hilo-digest"),
        heat: document.getElementById("hilo-heatmap"),
        bars: document.querySelector(".composition"),
      };
      // The caption and legend belong to the heat map alone.
      const heatOnly = [document.getElementById("hilo-heat-caption"),
                        document.querySelector(".heat-legend")];
      Object.keys(views).forEach(function (k) {
        if (views[k]) views[k].style.display = groupView === k ? "" : "none";
      });
      heatOnly.forEach(function (el) {
        if (el) el.style.display = groupView === "heat" ? "" : "none";
      });
      const modeToggle = document.getElementById("hilo-groupmode");
      if (modeToggle) modeToggle.style.display = groupView === "bars" ? "" : "none";

      if (groupView === "bars") drawComposition();
      else if (groupView === "heat") drawHeatmap();
      else drawDigest();
    }

    function redrawAll() { redrawTable(); drawGroups(); }

    // ---- Composition: which groups these names are coming from ----
    //
    // Ranked horizontal bars rather than a pie. A pie can carry five slices
    // legibly; there are 20 sectors and 127 industries, and past about six
    // wedges you are comparing angles you cannot actually compare. Bars sorted
    // by size answer "which group has the most" at a glance, which is the
    // question, and they leave room for the names to be readable.
    //
    // Two modes, because they answer different questions:
    //   Count         — how many companies, i.e. where the volume is
    //   Participation — what share of the group's own members qualified, i.e.
    //                   where the intensity is. 30 semiconductors at new highs
    //                   means one thing if the group holds 40 and another if
    //                   it holds 200.
    //
    // Participation is a percentage of members, not a multiple of market
    // share. The multiple was tried first and degenerates: it is capped at
    // universe/hits, so every group with full participation ties at exactly
    // that ceiling — six industries came back at 2.0956 and the ranking said
    // nothing. A share of members is bounded at 100%, and 100% means something.
    let groupBy = "sector";
    let groupMode = "count";
    let groupView = "digest";
    // Group sizes must be counted WITHIN the selected index, not across the
    // whole universe. The precomputed map covers all 3,311 priced names, so
    // scoped to the S&P 500 it reported Finance as 846 members and diluted
    // every participation figure roughly tenfold. Counted here instead, from
    // the same universe the rows come from, and memoised because the answer
    // only changes when the scope or the grouping does.
    const GROUP_MEMBERS = DATA.groupMembers || {};
    const memberCache = {};
    function memberCounts() {
      const key = groupBy + "|" + indexFilter;
      if (memberCache[key]) return memberCache[key];
      // With no index filter the precomputed map is already correct.
      if (!indexFilter) {
        memberCache[key] = GROUP_MEMBERS[groupBy] || {};
        return memberCache[key];
      }
      const counts = {};
      Object.keys(quotes).forEach(function (t) {
        if (!inIndex(t)) return;
        const tags = cls[t];
        if (!tags) return;
        const name = groupBy === "sector" ? tags[0] : tags[1];
        if (name) counts[name] = (counts[name] || 0) + 1;
      });
      memberCache[key] = counts;
      return counts;
    }

    // Both sides at once, per group, so "Finance is number one" stops being
    // ambiguous. Reading the highs list and the lows list separately, Finance
    // and Health Technology topped BOTH — which says nothing except that they
    // are the two largest groups. Side by side on one row, a group with 40
    // highs and 38 lows visibly cancels while one with 40 highs and 2 lows
    // does not.
    function composition() {
      const period = namesInPeriod();
      const hiTotals = {}, loTotals = {};      // distinct companies over the period
      const hiDaily = {}, loDaily = {};        // summed daily counts, for the rate
      const seen = { hi: {}, lo: {} };
      period.forEach(function (r) {
        ["hi", "lo"].forEach(function (which) {
          (((r[win] || {})[which]) || []).forEach(function (t) {
            if (!inIndex(t)) return;
            const tags = cls[t] || ["", ""];
            const key = groupBy === "sector" ? tags[0] : tags[1];
            if (!key) return;
            // Two tallies from one pass. The daily one counts every session a
            // name prints on; the distinct one counts the name once.
            const daily = which === "hi" ? hiDaily : loDaily;
            daily[key] = (daily[key] || 0) + 1;
            if (seen[which][t]) return;
            seen[which][t] = true;
            const bucket = which === "hi" ? hiTotals : loTotals;
            bucket[key] = (bucket[key] || 0) + 1;
          });
        });
      });
      const sessions = period.length || 1;

      const members = memberCounts();
      const groups = {};
      Object.keys(hiTotals).forEach(function (k) { groups[k] = true; });
      Object.keys(loTotals).forEach(function (k) { groups[k] = true; });

      return Object.keys(groups).map(function (k) {
        const hi = hiTotals[k] || 0, lo = loTotals[k] || 0;
        const size = members[k] || 0;
        return {
          group: k, hi: hi, lo: lo, size: size, sessions: sessions,
          // AVERAGE DAILY PARTICIPATION — the share of the group at a new
          // high on a typical session.
          //
          // The obvious measure, "what fraction of the group made a high at
          // some point in this period", is the one this replaces, because it
          // saturates: over six months 100% of S&P semiconductors made a
          // 13-week high AND 89% made a 13-week low, netting to +11% and
          // telling you nothing except that six months is a long time. A rate
          // does not accumulate — semis read +10% net at 52-week over both
          // three and six months, which is the stable fact about them.
          hiRate: size ? 100 * (hiDaily[k] || 0) / sessions / size : 0,
          loRate: size ? 100 * (loDaily[k] || 0) / sessions / size : 0,
          // Kept for the "total companies" view, where cumulative is the
          // point rather than the bug.
          hiShare: size ? 100 * hi / size : 0,
          loShare: size ? 100 * lo / size : 0,
          eligible: size >= 5,
        };
      }).filter(function (g) {
        return g.eligible && (g.hi || g.lo);
      }).sort(function (a, b) {
        // Ranked by the GAP, not by either side. A group at the top is
        // genuinely one-sided; a group in the middle is split.
        const av = groupMode === "weighted" ? a.hiRate - a.loRate : a.hi - a.lo;
        const bv = groupMode === "weighted" ? b.hiRate - b.loRate : b.hi - b.lo;
        return bv - av;
      });
    }

    // Top and bottom of the ranking, not the top 14 — the groups being sold
    // are as much of the story as the ones being bought, and a straight
    // "first 14" shows only the buying.
    function compositionShown() {
      const all = composition();
      if (all.length <= 16) return all;
      return all.slice(0, 8).concat(all.slice(-8));
    }

    // ---- Heat map ----
    //
    // The bar chart ranks groups but makes you read fourteen labels to find
    // the shape of the market. A treemap gives it in one look, the way a
    // TradingView heat map does: area is how big the group IS, colour is how
    // one-sided it is. A big red block and a small green one is a different
    // market from the reverse, and no ranking conveys that as fast.
    //
    // Squarified layout — rows are packed until adding another tile would make
    // the aspect ratios worse, which keeps tiles near-square and readable
    // rather than the slivers a naive slice-and-dice produces.
    function squarify(items, x, y, w, h) {
      const out = [];
      const total = items.reduce(function (a, i) { return a + i.value; }, 0);
      if (!total) return out;
      let list = items.slice();
      let scale = (w * h) / total;

      function worst(row, side) {
        const sum = row.reduce(function (a, i) { return a + i.value * scale; }, 0);
        const mx = Math.max.apply(null, row.map(function (i) { return i.value * scale; }));
        const mn = Math.min.apply(null, row.map(function (i) { return i.value * scale; }));
        const s2 = sum * sum, s3 = side * side;
        return Math.max(s3 * mx / s2, s2 / (s3 * mn));
      }

      while (list.length) {
        const horizontal = w >= h;
        const side = horizontal ? h : w;
        let row = [list[0]];
        let i = 1;
        while (i < list.length && worst(row.concat([list[i]]), side) <= worst(row, side)) {
          row.push(list[i]); i++;
        }
        const rowSum = row.reduce(function (a, it) { return a + it.value * scale; }, 0);
        const thickness = rowSum / side;
        let offset = 0;
        row.forEach(function (it) {
          const length = (it.value * scale) / thickness;
          out.push(horizontal
            ? { item: it, x: x, y: y + offset, w: thickness, h: length }
            : { item: it, x: x + offset, y: y, w: length, h: thickness });
          offset += length;
        });
        if (horizontal) { x += thickness; w -= thickness; } else { y += thickness; h -= thickness; }
        list = list.slice(row.length);
      }
      return out;
    }

    // Colour is scaled to the strongest group currently on screen rather than
    // to a fixed ceiling. Daily-participation rates run small (a group at 10%
    // is very strong), and different windows and timeframes produce different
    // ranges — a fixed cap left whole maps washed to grey.
    function heatColor(net, cap) {
      const scale = Math.max(cap || 0, 1.5);   // floor, so a flat map is not all-saturated
      const capped = Math.max(-scale, Math.min(scale, net)) / scale;
      const strength = Math.abs(capped);
      const base = capped >= 0 ? [22, 163, 74] : [220, 38, 38];
      const alpha = 0.12 + 0.78 * strength;
      return "rgba(" + base[0] + "," + base[1] + "," + base[2] + "," + alpha.toFixed(3) + ")";
    }

    // ---- The daily read ----
    //
    // Default view, and the answer to "this is data overload". The heat map
    // and the bars both hand over a measurement and leave the conclusion to
    // the reader — 33 tiles, each needing two percentages interpreted. On a
    // normal morning nobody does that.
    //
    // So: rank by the daily-participation rate, which is the statistically
    // sound measure, but SHOW plain counts, which is what a person actually
    // reads. "17 of 18 semiconductors have hit a 52-week high" needs no
    // interpretation at all. Six groups, two lists, one sentence.
    const LEAD_COUNT = 5;

    function drawDigest() {
      const host = document.getElementById("hilo-digest");
      if (!host) return;
      const all = composition();
      const scopeName = (SCOPES.filter(function (s) { return s.code === indexFilter; })[0] || {}).label || "the market";
      const winName = HILO_WINDOWS.filter(function (w) { return w[0] === win; })[0][1];
      const tfName = tf === "ALL" ? "since inception" : "the last " + tf;

      if (!all.length) {
        host.innerHTML = '<div class="empty-note">Nothing to report for this selection.</div>';
        return;
      }

      // A group only counts as leading or lagging if it is genuinely lopsided.
      // Without this the lists fill with groups at +0.3 and read as signal.
      const leaders = all.filter(function (g) { return g.hiRate - g.loRate > 0.5 && g.hi > g.lo; })
                         .slice(0, LEAD_COUNT);
      const laggards = all.filter(function (g) { return g.loRate - g.hiRate > 0.5 && g.lo > g.hi; })
                          .reverse().slice(0, LEAD_COUNT);

      function line(g, side) {
        const n = side === "hi" ? g.hi : g.lo;
        const word = side === "hi" ? "at new highs" : "at new lows";
        return '<div class="digest-row" data-group="' + g.group + '">' +
          '<span class="digest-name">' + g.group + "</span>" +
          '<span class="digest-count ' + (side === "hi" ? "up" : "down") + '">' +
            n + " of " + g.size + "</span>" +
          '<span class="digest-word">' + word + "</span>" +
        "</div>";
      }

      const verdict = leaders.length && !laggards.length
        ? "Broadly one-sided to the upside."
        : laggards.length && !leaders.length
          ? "Broadly one-sided to the downside."
          : leaders.length && laggards.length
            ? "Split: money is going into the first list and coming out of the second."
            : "No group is meaningfully one-sided right now.";

      host.innerHTML =
        '<div class="digest-head">' + scopeName + " &middot; " + winName +
          " highs and lows &middot; " + tfName + "</div>" +
        '<div class="digest-verdict">' + verdict + "</div>" +
        '<div class="digest-cols">' +
          '<div class="digest-col"><div class="digest-col-title up">Leading</div>' +
            (leaders.length ? leaders.map(function (g) { return line(g, "hi"); }).join("")
                            : '<div class="digest-none">nothing</div>') +
          "</div>" +
          '<div class="digest-col"><div class="digest-col-title down">Lagging</div>' +
            (laggards.length ? laggards.map(function (g) { return line(g, "lo"); }).join("")
                             : '<div class="digest-none">nothing</div>') +
          "</div>" +
        "</div>" +
        '<div class="digest-note">Counts are companies that printed at least one ' +
          winName + " high or low in " + tfName +
          ". Order is by how much of each group was doing it on a typical day, not by the raw count.</div>";

      host.querySelectorAll(".digest-row").forEach(function (row) {
        row.addEventListener("click", function () {
          const sel = groupBy === "sector" ? sectorSel : industrySel;
          if (!sel) return;
          sel.value = (sel.value === row.dataset.group) ? "" : row.dataset.group;
          redrawAll();
        });
      });
    }

    function drawHeatmap() {
      const host = document.getElementById("hilo-heatmap");
      if (!host) return;

      // Say what is being measured, in the words of the current selection.
      // Nobody should have to infer what "45%" meant.
      const capEl = document.getElementById("hilo-heat-caption");
      if (capEl) {
        const scopeName = (SCOPES.filter(function (s) { return s.code === indexFilter; })[0] || {}).label || "all listed";
        const winName = HILO_WINDOWS.filter(function (w) { return w[0] === win; })[0][1];
        const tfName = tf === "ALL" ? "since inception" : "the last " + tf;
        capEl.textContent =
          "Each tile is one " + (groupBy === "sector" ? "sector" : "industry") +
          " of the " + scopeName + ". Its number is how many of its companies sat at a " +
          winName + " high on an average session over " + tfName +
          ", minus how many sat at a " + winName + " low — in percentage points of the group. " +
          "Green means more of it was making highs than lows. Tile size is how many companies the group holds.";
      }
      const items = composition()
        .map(function (g) {
          return {
            group: g.group, hi: g.hi, lo: g.lo, size: g.size,
            hiRate: g.hiRate, loRate: g.loRate, sessions: g.sessions,
            net: g.hiRate - g.loRate,
            // Area is the group's SIZE, not its hit count, so the map keeps
            // the same shape as you change window and timeframe and only the
            // colours move. A map whose boxes jump around cannot be compared
            // with the one you looked at yesterday.
            value: g.size,
          };
        })
        .sort(function (a, b) { return b.value - a.value; })
        .slice(0, 40);

      host.innerHTML = "";
      if (!items.length) {
        host.innerHTML = '<div class="empty-note">Nothing to show for this selection.</div>';
        return;
      }

      const W = host.clientWidth || 900, H = host.clientHeight || 420;
      const cap = Math.max.apply(null, items.map(function (i) { return Math.abs(i.net); }));
      squarify(items, 0, 0, W, H).forEach(function (t) {
        const el = document.createElement("div");
        el.className = "heat-tile";
        el.style.left = t.x + "px";
        el.style.top = t.y + "px";
        el.style.width = Math.max(0, t.w - 2) + "px";
        el.style.height = Math.max(0, t.h - 2) + "px";
        el.style.background = heatColor(t.item.net, cap);
        // Spelled out, because "45%" on its own was unreadable: it is a share
        // of the group, on an average day, not a share of anything cumulative.
        el.title = t.item.group + " — " + t.item.size + " companies. On an " +
          "average session: " + t.item.hiRate.toFixed(1) + "% of them at a new high, " +
          t.item.loRate.toFixed(1) + "% at a new low. Net " +
          (t.item.net >= 0 ? "+" : "") + t.item.net.toFixed(1) +
          " points. Over the period, " + t.item.hi + " of " + t.item.size +
          " printed a high at least once and " + t.item.lo + " printed a low.";
        // Below roughly this size a label is unreadable noise, so the tile
        // carries only its colour and its tooltip.
        if (t.w > 70 && t.h > 34) {
          el.innerHTML = '<div class="heat-name">' + t.item.group + "</div>" +
            '<div class="heat-net">' + (t.item.net >= 0 ? "+" : "") +
            t.item.net.toFixed(1) + "</div>" +
            (t.h > 58 ? '<div class="heat-sub">' + t.item.hiRate.toFixed(1) +
              "% hi / " + t.item.loRate.toFixed(1) + "% lo</div>" : "");
        }
        el.addEventListener("click", function () {
          const sel = groupBy === "sector" ? sectorSel : industrySel;
          if (!sel) return;
          sel.value = (sel.value === t.item.group) ? "" : t.item.group;
          redrawAll();
        });
        host.appendChild(el);
      });
    }

    function drawComposition() {
      const items = compositionShown();
      const colors = themeColors();
      const canvas = document.getElementById("hilo-composition-canvas");
      if (!canvas) return;
      if (charts["hilo-composition-canvas"]) charts["hilo-composition-canvas"].destroy();
      if (!items.length) return;

      const weighted = groupMode === "weighted";
      const hiOf_ = function (i) { return weighted ? i.hiRate : i.hi; };
      const loOf_ = function (i) { return weighted ? i.loRate : i.lo; };

      charts["hilo-composition-canvas"] = new Chart(canvas, {
        type: "bar",
        data: {
          labels: items.map(function (i) { return i.group; }),
          datasets: [
            // Lows are plotted NEGATIVE so they run left from a zero line.
            // The axis then reads as one scale, and the eye compares bar
            // lengths across the middle instead of across two charts.
            { label: "New lows", data: items.map(function (i) { return -loOf_(i); }),
              backgroundColor: colors.down, borderWidth: 0, stack: "s" },
            { label: "New highs", data: items.map(function (i) { return hiOf_(i); }),
              backgroundColor: colors.up, borderWidth: 0, stack: "s" },
          ],
        },
        options: {
          indexAxis: "y",
          responsive: true,
          maintainAspectRatio: false,
          animation: false,
          plugins: {
            legend: { display: true, labels: { color: colors.text, boxWidth: 10, font: { size: 10 } } },
            tooltip: {
              callbacks: {
                label: function (ctx) {
                  const i = items[ctx.dataIndex];
                  const isHi = ctx.datasetIndex === 1;
                  const n = isHi ? i.hi : i.lo;
                  const rate = isHi ? i.hiRate : i.loRate;
                  return (isHi ? "Highs: " : "Lows: ") + n + " of " + i.size +
                    " members (" + Math.round(rate) + "%)";
                },
              },
            },
          },
          scales: {
            x: {
              stacked: true,
              ticks: {
                color: colors.text, font: { size: 10 },
                // The left half is drawn negative but represents a count of
                // lows, not a negative number of anything.
                callback: function (v) { return Math.abs(v) + (weighted ? "%" : ""); },
              },
              grid: { color: colors.grid },
            },
            y: { stacked: true, ticks: { color: colors.text, font: { size: 10 }, autoSkip: false }, grid: { display: false } },
          },
          onClick: function (evt, els) {
            if (!els.length) return;
            const picked = items[els[0].index].group;
            const sel = groupBy === "sector" ? sectorSel : industrySel;
            // Clicking the group you are already filtered to clears it, so the
            // chart is a toggle rather than a one-way trip.
            sel.value = (sel.value === picked) ? "" : picked;
            redrawAll();
          },
        },
      });
    }

    function refresh() {
      rows = buildRows();

      // Filter options come from the rows actually on screen, so a sector with
      // nothing making new highs this week does not sit in the list as a dead
      // option.
      const secs = {}, inds = {};
      rows.forEach(function (r) { secs[r.sector] = true; inds[r.industry] = true; });
      if (sectorSel) fillSelect(sectorSel, Object.keys(secs).sort(), sectorSel.value);
      if (industrySel) fillSelect(industrySel, Object.keys(inds).sort(), industrySel.value);

      const period = filterByTimeframe(counts, tf);
      const n = namesInPeriod().length;
      const sideLabel = side === "hi" ? "highs" : "lows";
      const winLabel = HILO_WINDOWS.filter(function (w) { return w[0] === win; })[0][1];

      // Distinct companies on each side over the period — computed from the
      // names, which is the only way to get it right.
      const distinct = function (which) {
        const seen = {};
        namesInPeriod().forEach(function (r) {
          (((r[win] || {})[which]) || []).forEach(function (t) {
            if (inIndex(t)) seen[t] = true;
          });
        });
        return Object.keys(seen).length;
      };
      const match = SCOPES.filter(function (s) { return s.code === indexFilter; })[0];
      const scope = match ? match.label : "all listed";
      if (hiEl) hiEl.textContent = distinct("hi");
      if (loEl) loEl.textContent = distinct("lo");
      if (hiLabelEl) hiLabelEl.textContent = scope + " at " + winLabel + " highs · " + tf;
      if (loLabelEl) loLabelEl.textContent = scope + " at " + winLabel + " lows · " + tf;

      if (leadEl && table) {
        const last = period.length ? period[period.length - 1] : null;
        leadEl.textContent = last
          ? "Latest session " + last.date + ": " + (last[win] || {}).hi + " " + winLabel +
            " highs, " + (last[win] || {}).lo + " lows · showing " + sideLabel +
            " over " + n + " session" + (n === 1 ? "" : "s")
          : "";
      }

      const colors = themeColors();
      const dotR = dotRadius(period.length);
      if (document.getElementById("hilo-screener-canvas")) lineChart("hilo-screener-canvas", period.map(function (r) { return r.date; }), [
        { label: winLabel + " highs", data: period.map(function (r) { return (r[win] || {}).hi; }),
          borderColor: colors.up, borderWidth: 1.5, pointRadius: dotR, tension: 0.15 },
        { label: winLabel + " lows", data: period.map(function (r) { return (r[win] || {}).lo; }),
          borderColor: colors.down, borderWidth: 1.5, pointRadius: dotR, tension: 0.15 },
      ]);

      redrawTable();
      drawGroups();
    }

    buildToggle("hilo-window", HILO_WINDOWS, function () { return win; },
                function (v) { win = v; });
    buildToggle("hilo-side", [["hi", "New highs"], ["lo", "New lows"]],
                function () { return side; }, function (v) { side = v; });
    buildToggle("hilo-timeframe", TIMEFRAMES.map(function (t) {
      return [t, t === "ALL" ? "Since Inception" : t];
    }), function () { return tf; }, function (v) { tf = v; });

    buildToggle("hilo-adr", [["", "Any"], ["low", "Low <3%"],
                             ["mid", "Tradeable 3-7%"], ["high", "High >7%"]],
                function () { return adrBand; }, function (v) { adrBand = v; });

    [sectorSel, industrySel].forEach(function (el) {
      if (el) el.addEventListener("change", redrawAll);
    });
    if (searchEl) searchEl.addEventListener("input", redrawAll);
    if (table) {
      table.querySelectorAll("th[data-sort]").forEach(function (th) {
        th.addEventListener("click", function () {
          draw(sortRows(visibleRows(), th.dataset.sort,
                        th.dataset.sort === "ticker" ? 1 : -1));
        });
      });
    }
    // Index scopes are supplied per country (S&P 500 / Nasdaq 100 / Russell
    // 2000 for the US; Nifty 100 / Midcap 150 / Smallcap 250 / Nifty 500 for
    // India). There is deliberately no "everything" option: the untracked tape
    // is what the filter exists to exclude.
    const SCOPES = DATA.screenerIndexes || [];
    if (SCOPES.length) {
      indexFilter = SCOPES[0].code;
      buildToggle("hilo-index", SCOPES.map(function (s) { return [s.code, s.label]; }),
                  function () { return indexFilter; }, function (v) { indexFilter = v; });
    }
    buildToggle("hilo-view", [["digest", "Summary"], ["heat", "Heat map"], ["bars", "Bars"]],
                function () { return groupView; }, function (v) { groupView = v; });
    buildToggle("hilo-groupby", [["sector", "By sector"], ["industry", "By industry"]],
                function () { return groupBy; }, function (v) { groupBy = v; });
    buildToggle("hilo-groupmode", [["count", "Companies"], ["weighted", "% of group"]],
                function () { return groupMode; }, function (v) { groupMode = v; });

    if (table) makeRowsClickable(table);
    if (window.ResizeObserver && document.getElementById("hilo-heatmap")) {
      let last = 0;
      new ResizeObserver(function () {
        const w = document.getElementById("hilo-heatmap").clientWidth;
        if (Math.abs(w - last) < 8) return;   // ignore sub-pixel churn
        last = w;
        if (groupView === "heat") drawHeatmap();
      }).observe(document.getElementById("hilo-heatmap"));
    }
    refresh();
  }

  // ---------- Sector / industry rank tables + drill-down ----------
  function renderRankPanel(seriesKey, itemsField, nameField, tableId, drilldownId) {
    const rows = S[seriesKey] || [];
    const table = document.getElementById(tableId);
    const tbody = table.querySelector("tbody");
    const drilldown = document.getElementById(drilldownId);
    const canvasId = drilldownId + "-canvas";
    const titleEl = drilldown.querySelector(".drilldown-title");
    const toggleEl = drilldown.querySelector(".tf-toggle");

    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="5" class="empty-note">No data yet.</td></tr>';
      return;
    }

    const latest = rows[rows.length - 1];
    const items = (latest[itemsField] || []).slice();
    let sortKey = "rank";
    let sortAsc = true;
    let selectedName = null;

    function renderTable() {
      items.sort(function (a, b) {
        const av = a[sortKey], bv = b[sortKey];
        if (av === null || av === undefined) return 1;
        if (bv === null || bv === undefined) return -1;
        if (typeof av === "string") return sortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
        return sortAsc ? av - bv : bv - av;
      });
      tbody.innerHTML = "";
      items.forEach(function (item) {
        const tr = document.createElement("tr");
        if (item[nameField] === selectedName) tr.classList.add("selected");
        tr.innerHTML =
          "<td>" + item[nameField] + "</td>" +
          '<td class="pct ' + pctClass(item.chg_1d) + '">' + fmtSignedPct(item.chg_1d) + "</td>" +
          '<td class="pct ' + pctClass(item.chg_5d) + '">' + fmtSignedPct(item.chg_5d) + "</td>" +
          '<td class="pct ' + pctClass(item.chg_20d) + '">' + fmtSignedPct(item.chg_20d) + "</td>" +
          '<td class="rank-cell">' + item.rank + "</td>";
        tr.addEventListener("click", function () { selectItem(item[nameField]); });
        tbody.appendChild(tr);
      });
    }

    table.querySelectorAll("th[data-sort]").forEach(function (th) {
      th.addEventListener("click", function () {
        const key = th.dataset.sort;
        if (sortKey === key) sortAsc = !sortAsc; else { sortKey = key; sortAsc = true; }
        renderTable();
      });
    });

    function selectItem(name) {
      selectedName = name;
      drilldown.classList.add("visible");
      titleEl.textContent = name + " — rank over time (1 = best)";
      renderTable();

      const history = rows.map(function (r) {
        const match = (r[itemsField] || []).find(function (it) { return it[nameField] === name; });
        return { date: r.date, rank: match ? match.rank : null };
      }).filter(function (r) { return r.rank !== null; });

      setupTimeframeToggle(toggleEl, function (tf) {
        const filtered = filterByTimeframe(history, tf);
        const colors = themeColors();
        const dotR = dotRadius(filtered.length);
        lineChart(canvasId, filtered.map(function (r) { return r.date; }), [
          { label: name + " rank", data: filtered.map(function (r) { return r.rank; }), borderColor: colors.accent, borderWidth: 1.5, pointRadius: dotR || 2, pointBackgroundColor: colors.accent, tension: 0.15 },
        ], { yReverse: true });
      });
    }

    renderTable();
  }

  // ---------- Market replay ----------
  // Rewinds every stored reading to a chosen session, so a chart being studied
  // in TradingView can be paired with the market context of that exact day.
  function renderReplayPanel() {
    const host = document.getElementById("replay-panel");
    if (!host) return;
    const D = DATA;
    const envRows = D.environment || [];
    if (!envRows.length) {
      document.getElementById("replay-body").innerHTML = '<div class="empty-note">No data yet.</div>';
      return;
    }

    const dates = envRows.map(function (r) { return r.date; });
    const envByDate = {};
    envRows.forEach(function (r) { envByDate[r.date] = r; });

    const dateInput = document.getElementById("replay-date");
    const tickerInput = document.getElementById("replay-ticker");
    const tickerResult = document.getElementById("replay-ticker-result");
    const body = document.getElementById("replay-body");

    dateInput.min = dates[0];
    dateInput.max = dates[dates.length - 1];

    let highlight = { sector: null, industry: null };

    // Snap to the newest session on or before the requested date, so picking a
    // weekend or a holiday lands on the last day that actually traded instead
    // of showing nothing.
    function resolveDate(wanted) {
      let chosen = null;
      for (let i = 0; i < dates.length; i++) {
        if (dates[i] <= wanted) chosen = dates[i]; else break;
      }
      return chosen || dates[0];
    }

    function groupRows(pack, dateStr) {
      const raw = pack.byDate[dateStr] || [];
      return raw.map(function (v) {
        return { name: pack.names[v[0]], rank: v[1], chg_1d: v[2], chg_5d: v[3], chg_20d: v[4] };
      }).sort(function (a, b) { return (a.rank || 999) - (b.rank || 999); });
    }

    function groupTable(title, rows, highlightName) {
      if (!rows.length) return '<div class="empty-note">No ' + title.toLowerCase() + " for this date.</div>";
      let html = '<div class="replay-col-title">' + title + "</div>" +
        '<div class="replay-table-wrap"><table><thead><tr>' +
        "<th>" + title + "</th><th>Rank</th><th>1D %</th><th>5D %</th><th>20D %</th>" +
        "</tr></thead><tbody>";
      rows.forEach(function (r) {
        const hit = highlightName && r.name === highlightName;
        html += '<tr class="' + (hit ? "selected" : "") + '">' +
          "<td>" + r.name + "</td>" +
          '<td class="rank-cell">' + (r.rank === null ? "—" : r.rank) + "</td>" +
          '<td class="pct ' + pctClass(r.chg_1d) + '">' + fmtSignedPct(r.chg_1d) + "</td>" +
          '<td class="pct ' + pctClass(r.chg_5d) + '">' + fmtSignedPct(r.chg_5d) + "</td>" +
          '<td class="pct ' + pctClass(r.chg_20d) + '">' + fmtSignedPct(r.chg_20d) + "</td>" +
          "</tr>";
      });
      return html + "</tbody></table></div>";
    }

    function indexRow(key, label, dateStr) {
      const rows = (D.indices && D.indices[key]) || [];
      let match = null;
      for (let i = 0; i < rows.length; i++) {
        if (rows[i].date <= dateStr) match = rows[i]; else break;
      }
      if (!match) return "";
      function pill(on, text) {
        return '<span class="factor-pill ' + (on ? "up" : "down") + '">' + text + "</span>";
      }
      return '<div class="factor-row">' +
        '<span class="factor-name">' + label + "</span>" +
        '<span class="replay-close">' + fmtNum(match.close, 2) + "</span>" +
        pill(match.a10, "10") + pill(match.a20, "20") + pill(match.a50, "50") +
        "</div>";
    }

    function draw(dateStr) {
      const env = envByDate[dateStr];
      const t = env && env.trend, p = env && env.participation, i = env && env.internals;

      function tag(label) { return '<span class="env-tag ' + label + '">' + label + "</span>"; }

      body.innerHTML =
        '<div class="card">' +
          '<div class="env-headline">' +
            '<span class="env-verdict ' + (env ? env.overall : "") + '">' + (env ? env.overall : "—") + "</span>" +
            '<span class="env-date">' + dateStr + "</span>" +
          "</div>" +
          '<div class="env-grid">' +
            '<div class="env-block">' +
              '<div class="env-block-title">Trend ' + (t ? tag(t.label) : "") + "</div>" +
              '<div class="env-block-value">' + (t ? t.factors_favourable + " / " + t.factors_total : "—") + "</div>" +
              '<div class="env-block-sub">large caps only ' + (t ? t.large_cap_favourable + " / " + t.large_cap_total : "—") + "</div>" +
              '<div class="factor-grid">' +
                indexRow("index_nasdaq", "NASDAQ", dateStr) +
                indexRow("index_sp500", "S&P 500", dateStr) +
                indexRow("index_russell2000", "Russell 2000", dateStr) +
              "</div>" +
            "</div>" +
            '<div class="env-block">' +
              '<div class="env-block-title">Participation ' + (p ? tag(p.label) : "") + "</div>" +
              '<div class="env-stat"><span class="env-stat-num">' +
                (p && p.sectors_positive !== null ? p.sectors_positive + " / " + p.sectors_total : "—") +
                '</span><span class="env-stat-label">sectors</span></div>' +
              '<div class="env-stat"><span class="env-stat-num">' +
                (p && p.industries_positive !== null ? p.industries_positive + " / " + p.industries_total : "—") +
                '</span><span class="env-stat-label">industries</span></div>' +
              '<div class="env-block-sub">positive over the prior 20 days</div>' +
            "</div>" +
            '<div class="env-block">' +
              '<div class="env-block-title">Internals ' + (i ? tag(i.label) : "") + "</div>" +
              '<div class="env-block-value ' + (i ? pctClass(i.adv_decl_avg) : "") + '">' +
                (i && i.adv_decl_avg !== null ? fmtSignedInt(Math.round(i.adv_decl_avg)) : "—") + "</div>" +
              '<div class="env-block-sub">' + netAdvDeclCaption(i ? i.adv_decl_avg : null) + "</div>" +
              '<div class="env-block-extra">' +
                (i && i.new_hilo_avg !== null ? fmtSignedInt(Math.round(i.new_hilo_avg)) + " " + netHiLoPhrase(i.new_hilo_avg) : "&nbsp;") +
              "</div>" +
            "</div>" +
          "</div>" +
          '<div class="replay-groups">' +
            "<div>" + groupTable("Sectors", groupRows(D.sectors, dateStr), highlight.sector) + "</div>" +
            "<div>" + groupTable("Industries", groupRows(D.industries, dateStr), highlight.industry) + "</div>" +
          "</div>" +
        "</div>";
    }

    function go(wanted) {
      const resolved = resolveDate(wanted);
      dateInput.value = resolved;
      draw(resolved);
    }

    function step(delta) {
      const idx = dates.indexOf(dateInput.value);
      const next = Math.min(dates.length - 1, Math.max(0, (idx === -1 ? dates.length - 1 : idx) + delta));
      go(dates[next]);
    }

    // Applies a ticker without publishing it, so this is safe to call both
    // from the local input handler and from an inbound sync message.
    function applyTicker(sym) {
      sym = (sym || "").trim().toUpperCase();
      if (tickerInput.value.trim().toUpperCase() !== sym) tickerInput.value = sym;
      const hit = D.classification ? D.classification[sym] : null;
      if (sym && hit) {
        highlight = { sector: hit[0], industry: hit[1] };
        tickerResult.className = "replay-hit";
        tickerResult.textContent = sym + " → " + hit[1] + " (" + hit[0] + ")";
      } else {
        highlight = { sector: null, industry: null };
        tickerResult.className = "replay-miss";
        tickerResult.textContent = sym ? "not found" : "";
      }
      draw(dateInput.value);
    }

    function publishDate() { Sync.publish({ date: dateInput.value }); }

    dateInput.addEventListener("change", function () { go(dateInput.value); publishDate(); });
    document.getElementById("replay-prev").addEventListener("click", function () { step(-1); publishDate(); });
    document.getElementById("replay-next").addEventListener("click", function () { step(1); publishDate(); });
    document.getElementById("replay-latest").addEventListener("click", function () {
      go(dates[dates.length - 1]);
      publishDate();
    });

    tickerInput.addEventListener("input", function () {
      applyTicker(tickerInput.value);
      Sync.publish({ ticker: tickerInput.value.trim().toUpperCase() });
    });

    Sync.subscribe(function (ctx) {
      if (ctx.date && ctx.date !== dateInput.value) go(ctx.date);
      if (typeof ctx.ticker === "string" && ctx.ticker !== tickerInput.value.trim().toUpperCase()) {
        applyTicker(ctx.ticker);
      }
    });

    // Same ticker list the stock page offers, so a name typed here
    // autocompletes to something the other panel can actually load.
    const knownNames = Object.keys(D.classification || {}).sort();
    const replayList = document.getElementById("replay-tickers");
    if (replayList && knownNames.length) {
      replayList.innerHTML = knownNames.map(function (t) { return '<option value="' + t + '">'; }).join("");
      tickerInput.placeholder = "Search " + knownNames.length.toLocaleString() + " tickers";
    }

    const shared = Sync.read();
    go(shared.date ? resolveDate(shared.date) : dates[dates.length - 1]);
    if (shared.ticker) applyTicker(shared.ticker);
  }

  // ---------- Stock context ----------
  function renderStockPanel() {
    const host = document.getElementById("stock-panel");
    if (!host) return;

    const tickerInput = document.getElementById("stock-ticker");
    const dateInput = document.getElementById("stock-date");
    const statusEl = document.getElementById("stock-status");
    const body = document.getElementById("stock-body");
    const dir = DATA.tickerDir || "tickers";

    const cache = {};
    let bench = null;
    let current = null;

    // How many trailing sessions the price and RS charts draw. 0 means every
    // bar stored. This used to be hard-coded to 126, which silently threw away
    // most of the history actually on disk (a name with two years stored still
    // only ever drew its last six months).
    // 5Y earns its place now that the files reach 2020 — a 2021 setup is only
    // findable if the chart will draw back that far.
    const RANGES = [["1M", 21], ["3M", 63], ["6M", 126], ["1Y", 252],
                    ["2Y", 504], ["5Y", 1260], ["ALL", 0]];
    let rangeBars = 252;

    function ema(values, span) {
      const k = 2 / (span + 1);
      const out = [];
      values.forEach(function (v, i) { out.push(i === 0 ? v : v * k + out[i - 1] * (1 - k)); });
      return out;
    }

    function fetchJson(path) {
      if (cache[path]) return cache[path];
      cache[path] = fetch(path).then(function (r) {
        if (!r.ok) throw new Error("not found");
        return r.json();
      });
      return cache[path];
    }

    function pctBetween(values, endIdx, back) {
      const startIdx = endIdx - back;
      if (startIdx < 0 || !values[startIdx]) return null;
      return (values[endIdx] / values[startIdx] - 1) * 100;
    }

    // "sp500" -> "S&P 500", "sensex" -> "BSE Sensex". The renderer ships the
    // labels because the index set is per country; the raw key is a readable
    // enough fallback if one is ever missing.
    function benchLabel(key) {
      const labels = DATA.indexLabels || {};
      return labels["index_" + key] || key;
    }

    function benchAt(key, dates, upto) {
      // Align a benchmark onto the stock's own trading days, carrying the last
      // known close forward across any date the benchmark doesn't share.
      const out = [];
      let j = 0, last = null;
      for (let i = 0; i <= upto; i++) {
        while (j < bench.dates.length && bench.dates[j] <= dates[i]) {
          if (bench[key][j] !== null && bench[key][j] !== undefined) last = bench[key][j];
          j++;
        }
        out.push(last);
      }
      return out;
    }


    // The one number worth logging in a setups database. Everything else on
    // this page is a pair of returns that only means something next to its own
    // history; this is a percentile against the whole market that day, so 92
    // means the same thing on every ticker and on every date. It is read at
    // the SELECTED date, not today — the point is recording what the setup
    // looked like on the day it triggered.
    function rsRatingBlock(d, end) {
      const rs = (d.rs || [])[end];
      if (rs === null || rs === undefined) return "";
      const band = rs >= 90 ? "rs-elite" : (rs >= 70 ? "rs-strong"
                 : (rs >= 40 ? "rs-mid" : "rs-weak"));
      const words = rs >= 90 ? "stronger than " + rs + "% of the market"
                  : (rs >= 70 ? "outperforming most of the market"
                  : (rs >= 40 ? "middle of the pack" : "lagging the market"));
      return '<div class="rs-rating ' + band + '">' +
        '<div class="rs-rating-num">' + rs + "</div>" +
        "<div><div class='rs-rating-label'>RS Rating</div>" +
        '<div class="rs-rating-sub">' + words + " &middot; on " + d.dates[end] + "</div></div>" +
      "</div>";
    }

    function draw() {
      if (!current) return;
      const d = current.data, sym = current.symbol;
      const wanted = dateInput.value;
      let end = -1;
      for (let i = 0; i < d.dates.length; i++) { if (d.dates[i] <= wanted) end = i; else break; }
      if (end < 20) { body.innerHTML = '<div class="empty-note">Not enough history before this date.</div>'; return; }
      dateInput.value = d.dates[end];

      // EMAs are computed over the FULL history and sliced afterwards, so the
      // averages at the left edge of the window are already seeded and correct
      // rather than restarting from the first visible bar.
      const e10 = ema(d.close, 10), e20 = ema(d.close, 20), e50 = ema(d.close, 50);
      const startIdx = rangeBars ? Math.max(0, end - (rangeBars - 1)) : 0;
      const slice = function (a) { return a.slice(startIdx, end + 1); };
      const dates = slice(d.dates), close = slice(d.close);

      // Which two indices this stock is measured against comes from the
      // country config (S&P 500 / Nasdaq for the US, Sensex / Nifty 500 for
      // India) rather than being hard-coded here.
      const benchKeys = (DATA.benchmarkKeys || ["sp500", "nasdaq"])
        .filter(function (k) { return bench && bench[k]; });
      const benchSeries = benchKeys.map(function (k) {
        return { key: k, label: benchLabel(k), values: benchAt(k, d.dates, end).slice(startIdx) };
      });
      function rsLine(b) {
        const base = close[0] / b[0];
        return close.map(function (c, i) { return b[i] ? (c / b[i]) / base * 100 : null; });
      }

      const cls = DATA.classification ? DATA.classification[sym] : null;
      const windows = [["1 week", 5], ["1 month", 21], ["3 months", 63], ["6 months", 126]];
      let cards = "";
      windows.forEach(function (w) {
        const s = pctBetween(d.close, end, w[1]);
        // Plain "outperformed / underperformed by X%". Strictly the gap between
        // two returns is measured in percentage points, but both returns are
        // already on screen, so the precise unit costs more clarity than it buys.
        function gap(a, b) {
          if (a === null || b === null) return "—";
          const v = a - b;
          return '<span class="' + (v >= 0 ? "up" : "down") + '">' +
            (v >= 0 ? "outperformed" : "underperformed") + " by " + Math.abs(v).toFixed(1) + "%</span>";
        }
        cards +=
          '<div class="rs-card">' +
            '<div class="rs-window">' + w[0] + "</div>" +
            '<div class="rs-main ' + pctClass(s) + '">' + (s === null ? "—" : fmtSignedPct(s)) + "</div>" +
            '<div class="rs-sub">' + sym + " over " + w[0] + "</div>" +
            benchSeries.map(function (b) {
              const bv = pctBetween(benchAt(b.key, d.dates, end), end, w[1]);
              return '<div class="rs-line"><span>vs ' + b.label + " (" +
                (bv === null ? "—" : fmtSignedPct(bv)) + ")</span>" +
                '<span class="rs-gap">' + gap(s, bv) + "</span></div>";
            }).join("") +
          "</div>";
      });

      body.innerHTML =
        '<div class="card">' +
          '<div class="stock-head">' +
            '<img class="stock-logo" src="https://images.financialmodelingprep.com/symbol/' + sym + '.png" alt="" onerror="this.style.display=\'none\'">' +
            "<div>" +
              '<div class="stock-sym">' + sym + "</div>" +
              '<div class="card-sub">' + (cls ? cls[1] + " &middot; " + cls[0] : "&nbsp;") + "</div>" +
            "</div>" +
            '<div class="stock-price"><div class="card-value">' + fmtNum(d.close[end], 2) + "</div>" +
              '<div class="card-sub">close on ' + d.dates[end] + "</div></div>" +
          "</div>" +
          rsRatingBlock(d, end) +
          '<div class="tf-toggle stock-tf">' +
            RANGES.map(function (r) {
              // A range longer than the stored history would just render the
              // same chart as ALL, so it is disabled rather than offered.
              const tooLong = r[1] && r[1] > end + 1;
              return '<button data-bars="' + r[1] + '"' +
                (r[1] === rangeBars ? ' class="active"' : "") +
                (tooLong ? " disabled" : "") + ">" + r[0] + "</button>";
            }).join("") +
          "</div>" +
          '<div class="lw-chart" id="stock-chart"></div>' +
          '<div class="rs-block">' +
            '<div class="env-chart-title">Relative strength</div>' +
            '<div class="env-block-sub">the stock&rsquo;s strength against each index, ' +
              "10-day trend versus 30-day &middot; green while it is gaining on that index, " +
              "red while it is losing ground &middot; the longer a run of one colour, the more " +
              "persistent the move</div>" +
            '<div class="rs-charts">' +
              benchSeries.map(function (b) {
                return '<div><div class="rs-chart-label">vs ' + b.label + "</div>" +
                  '<div class="lw-chart rs-chart" id="rs-' + b.key + '"></div></div>';
              }).join("") +
            "</div>" +
            '<div class="rs-grid">' + cards + "</div>" +
          "</div>" +
        "</div>";

      body.querySelectorAll(".stock-tf button").forEach(function (btn) {
        btn.addEventListener("click", function () {
          rangeBars = Number(btn.dataset.bars);
          draw();
        });
      });

      const th = lwTheme();
      function makeChart(id, height) {
        const el = document.getElementById(id);
        const c = LightweightCharts.createChart(el, {
          height: el.clientHeight || height,
          layout: { background: { type: "solid", color: th.bg }, textColor: th.text, fontSize: 11 },
          grid: { vertLines: { color: th.grid }, horzLines: { color: th.grid } },
          rightPriceScale: { borderColor: th.border },
          timeScale: { borderColor: th.border, rightOffset: 2 },
          crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
        });
        if (window.ResizeObserver) {
          new ResizeObserver(function () {
            c.applyOptions({ width: el.clientWidth, height: el.clientHeight });
            c.timeScale().fitContent();
          }).observe(el);
        }
        // A chart created before its container has finished laying out sizes
        // itself to whatever width exists at that instant and keeps that bar
        // spacing, which leaves the series drawn across only part of the pane.
        // Re-fitting on the next frame corrects it once layout is settled.
        requestAnimationFrame(function () {
          c.applyOptions({ width: el.clientWidth, height: el.clientHeight || height });
          c.timeScale().fitContent();
        });
        return c;
      }

      const priceChart = makeChart("stock-chart", 340);
      const bars = priceChart.addBarSeries({
        upColor: th.up, downColor: th.down, openVisible: false, thinBars: false,
      });
      bars.setData(dates.map(function (dt, i) {
        return { time: dt, open: slice(d.open)[i], high: slice(d.high)[i], low: slice(d.low)[i], close: close[i] };
      }));
      enableMeasure(priceChart, bars, document.getElementById("stock-chart"));
      [["ema10", e10, "#2962FF"], ["ema20", e20, "#F23645"], ["ema50", e50, "#FF9800"]].forEach(function (cfg) {
        const s = priceChart.addLineSeries({ color: cfg[2], lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
        s.setData(dates.map(function (dt, i) { return { time: dt, value: slice(cfg[1])[i] }; }));
      });
      priceChart.timeScale().fitContent();

      // Relative strength, plotted as its distance from its own 20-day trend
      // rather than as the raw ratio. Two problems this solves:
      //
      //  - Filling a raw RS line down to the axis coloured the area by LEVEL,
      //    not direction, so the pane came out as one near-solid block: a
      //    stock far above where its RS history started read as green almost
      //    everywhere regardless of which way it was actually moving.
      //    Centring on the trend puts zero exactly where the colour flips, so
      //    green means "gaining on the index" and red means "losing ground".
      //
      //  - The raw level is not comparable between stocks — one reads 469 and
      //    another 750 purely because of where each series was indexed from.
      //    Distance from trend is a percentage, so +4% means the same thing on
      //    every ticker and on every date.
      //
      // The two sides are a fast and a slow average of the RS line, not the
      // raw line against one average. Raw-vs-average crosses back and forth
      // every few sessions, which chops the chart into confetti and hides
      // exactly the thing it is meant to show. Comparing a 10-day to a 30-day
      // average holds a colour for as long as the move actually persists, so
      // a run of green is a real stretch of outperformance rather than a
      // week of noise.
      benchSeries.forEach(function (b) {
        const chart = makeChart("rs-" + b.key, 150);
        const line = rsLine(b.values);
        const valid = line.map(function (v) { return v === null ? 100 : v; });
        const fast = ema(valid, 10);
        const trend = ema(valid, 30);

        const series = chart.addBaselineSeries({
          baseValue: { type: "price", price: 0 },
          topLineColor: th.up,
          topFillColor1: "rgba(22,163,74,0.45)",
          topFillColor2: "rgba(22,163,74,0.04)",
          bottomLineColor: th.down,
          bottomFillColor1: "rgba(220,38,38,0.04)",
          bottomFillColor2: "rgba(220,38,38,0.45)",
          lineWidth: 2, priceLineVisible: false, lastValueVisible: false,
          priceFormat: {
            type: "custom",
            formatter: function (v) { return (v >= 0 ? "+" : "") + v.toFixed(1) + "%"; },
          },
        });
        series.setData(dates.map(function (dt, i) {
          return (line[i] === null || !trend[i])
            ? { time: dt }
            : { time: dt, value: (fast[i] / trend[i] - 1) * 100 };
        }));
        chart.timeScale().fitContent();
      });
    }

    // `wantDate` lets an inbound sync message set the ticker and the date in
    // one go — without it, loading a new symbol would reset the date to its
    // latest bar and throw away the date the other panel just sent.
    function load(sym, wantDate) {
      sym = sym.trim().toUpperCase();
      if (!sym) return;
      statusEl.className = "replay-miss";
      statusEl.textContent = "loading…";
      Promise.all([
        fetchJson(dir + "/" + sym + ".json"),
        bench ? Promise.resolve(bench) : fetchJson(dir + "/_benchmarks.json"),
      ]).then(function (res) {
        bench = res[1];
        current = { symbol: sym, data: res[0] };
        dateInput.min = res[0].dates[0];
        dateInput.max = res[0].dates[res[0].dates.length - 1];
        dateInput.value = snapToDate(res[0].dates, wantDate || dateInput.value);
        statusEl.textContent = "";
        draw();
      }).catch(function () {
        statusEl.className = "replay-miss";
        statusEl.textContent = "No price history stored for " + sym + ".";
        body.innerHTML = "";
        current = null;
      });
    }

    // Applies a date without publishing it — safe for inbound sync messages.
    function applyDate(wanted) {
      if (!current) { dateInput.value = wanted; return; }
      const snapped = snapToDate(current.data.dates, wanted);
      if (!snapped || snapped === dateInput.value) return;
      dateInput.value = snapped;
      draw();
    }

    function step(delta) {
      if (!current) return;
      const dates = current.data.dates;
      let idx = dates.indexOf(dateInput.value);
      if (idx === -1) { for (let i = 0; i < dates.length; i++) { if (dates[i] <= dateInput.value) idx = i; else break; } }
      const next = Math.min(dates.length - 1, Math.max(0, idx + delta));
      dateInput.value = dates[next];
      draw();
    }

    function publishDate() { Sync.publish({ date: dateInput.value }); }

    tickerInput.addEventListener("change", function () {
      load(tickerInput.value);
      Sync.publish({ ticker: tickerInput.value.trim().toUpperCase() });
    });
    dateInput.addEventListener("change", function () { draw(); publishDate(); });
    document.getElementById("stock-prev").addEventListener("click", function () { step(-1); publishDate(); });
    document.getElementById("stock-next").addEventListener("click", function () { step(1); publishDate(); });
    document.getElementById("stock-latest").addEventListener("click", function () {
      if (current) { dateInput.value = current.data.dates[current.data.dates.length - 1]; draw(); publishDate(); }
    });

    Sync.subscribe(function (ctx) {
      const wantTicker = typeof ctx.ticker === "string" ? ctx.ticker.trim().toUpperCase() : null;
      // A partially-typed ticker in the replay box shouldn't blank this panel;
      // only switch once it names something actually stored.
      if (wantTicker && wantTicker !== (current && current.symbol) && (DATA.classification || {})[wantTicker]) {
        tickerInput.value = wantTicker;
        load(wantTicker, ctx.date);
      } else if (ctx.date) {
        applyDate(ctx.date);
      }
    });

    // Populate suggestions from the classification map — every classified
    // name is available, not a curated subset.
    const known = Object.keys(DATA.classification || {}).sort();
    const dl = document.getElementById("stock-tickers");
    if (dl && known.length) {
      dl.innerHTML = known.map(function (t) { return '<option value="' + t + '">'; }).join("");
      tickerInput.placeholder = "Search " + known.length.toLocaleString() + " tickers";
    }

    // Open on whatever the shared context already holds — so if the replay
    // panel was left on a ticker, this panel comes up showing that ticker
    // rather than resetting to a default every reload.
    const shared = Sync.read();
    const initial = (shared.ticker && known.indexOf(shared.ticker) !== -1) ? shared.ticker
      : (known.indexOf("NVDA") !== -1 ? "NVDA" : (known.length ? known[0] : null));
    if (initial) {
      tickerInput.value = initial;
      load(initial, shared.date);
    }
  }

  // ---------- TMLE: the leader engine ----------
  // Score and stage are shown together everywhere and never merged. The score
  // says how strong the advance has been; the stage says whether it is still
  // intact. A broken ex-leader keeps a high score — that history is real — and
  // is simply marked not actionable rather than quietly demoted, which would
  // hide both facts at once.
  const STAGE_CLASS = { 1: "stage-1", 2: "stage-2", 3: "stage-3", 4: "stage-4" };
  const STAGE_NAME = { 1: "Basing", 2: "Advancing", 3: "Topping", 4: "Declining" };

  function stageBadge(stage, actionable) {
    const cls = STAGE_CLASS[stage] || "";
    const name = STAGE_NAME[stage] || "—";
    const flag = actionable ? "" : ' <span class="nogo">no-go</span>';
    return '<span class="stage-badge ' + cls + '">' + name + "</span>" + flag;
  }

  // Company logo, same source the stock page uses. Hidden on failure rather
  // than leaving a broken-image box in the middle of a table row.
  function logoImg(ticker, cls) {
    return '<img class="' + (cls || "row-logo") + '" alt="" loading="lazy" ' +
      'src="https://images.financialmodelingprep.com/symbol/' + encodeURIComponent(ticker) + '.png" ' +
      "onerror=\"this.style.visibility='hidden'\">";
  }

  // A score of 66 means nothing on its own. Bands give it a shape, matching
  // the colour language the rest of the terminal already uses.
  function scoreClass(v) {
    if (v === null || v === undefined || isNaN(v)) return "";
    if (v >= 65) return "score-strong";
    if (v >= 55) return "score-good";
    if (v >= 45) return "score-fair";
    return "score-weak";
  }

  // The one-line read. This is the difference between a table of numbers and
  // something that tells you what it thinks — the same job the environment
  // panel's headline does for breadth.
  function leaderVerdict(r) {
    const dd = r.drawdown, days = r.episode_days, weeks = days ? Math.round(days / 5) : null;
    const bits = [];
    if (r.stage === 2 && r.actionable) {
      bits.push("Advancing");
    } else if (r.stage === 2) {
      bits.push("Was advancing, now damaged");
    } else if (r.stage === 4) {
      bits.push("In decline");
    } else if (r.stage === 3) {
      bits.push("Topping");
    } else {
      bits.push("Basing");
    }
    if (weeks) bits.push(weeks + (weeks === 1 ? " week" : " weeks") + " into the move");
    if (r.gain !== null && r.gain !== undefined) bits.push("up " + Math.round(r.gain) + "% from its low");
    if (dd !== null && dd !== undefined) {
      bits.push(dd <= -1 ? Math.abs(Math.round(dd)) + "% off its high" : "at its high");
    }
    if (r.pct_below_10w === 0) bits.push("never lost the 10-week");
    else if (r.pct_below_10w !== undefined && r.pct_below_10w !== null) {
      bits.push("below the 10-week " + Math.round(r.pct_below_10w) + "% of the move");
    }
    return bits.join(" &middot; ");
  }

  // Clicking a row anywhere sends that ticker to every other panel on the
  // page — leaderboard to score card, and on to the stock and replay pages.
  function makeRowsClickable(table) {
    table.addEventListener("click", function (e) {
      const tr = e.target.closest("tr");
      if (!tr || !tr.dataset.ticker) return;
      table.querySelectorAll("tr.selected").forEach(function (x) { x.classList.remove("selected"); });
      tr.classList.add("selected");
      Sync.publish({ ticker: tr.dataset.ticker });
    });
  }

  // "Move 82%" told you nothing about what was measured. Both cells that carry
  // an episode figure now say from when, on hover — the advance's start date is
  // the whole point of episode anchoring, so it should be visible.
  function gainCell(r) {
    if (r.gain === null || r.gain === undefined) return '<td class="pct">—</td>';
    const from = r.episode_start ? " (advance began " + r.episode_start + ")" : "";
    return '<td class="pct up" title="up ' + Math.round(r.gain) +
      "% from this advance's low" + from + '">+' + fmtNum(r.gain, 0) + "%</td>";
  }

  function sessionsCell(r) {
    const d = r.episode_days;
    if (!d) return "<td>—</td>";
    return '<td title="' + d + " session" + (d === 1 ? "" : "s") +
      ' into this advance">' + d + "</td>";
  }

  function sortRows(rows, key, dir) {
    return rows.slice().sort(function (a, b) {
      let x = a[key], y = b[key];
      if (x === null || x === undefined) return 1;
      if (y === null || y === undefined) return -1;
      if (typeof x === "string") return dir * x.localeCompare(y);
      return dir * (x - y);
    });
  }

  function attachSorting(table, rows, draw, initialKey, initialDir) {
    let key = initialKey, dir = initialDir;
    table.querySelectorAll("th[data-sort]").forEach(function (th) {
      th.addEventListener("click", function () {
        const next = th.dataset.sort;
        dir = (next === key) ? -dir : (next === "ticker" ? 1 : -1);
        key = next;
        draw(sortRows(rows, key, dir));
      });
    });
    draw(sortRows(rows, key, dir));
  }

  function renderTmleLeaders() {
    const table = document.getElementById("tmle-leaders-table");
    if (!table) return;
    const all = DATA.leaders || [];
    const broken = DATA.broken || [];
    const counts = DATA.counts || {};
    const body = table.querySelector("tbody");
    let filter = "actionable";

    function visible() {
      if (filter === "broken") return broken;
      if (filter === "all") return all;
      return all.filter(function (r) { return r.actionable; });
    }

    // The verdict, before the table. A 250-row leaderboard does not tell you
    // what kind of market it is; two sentences and three numbers do.
    function drawDigest() {
      const host = document.getElementById("tmle-digest");
      if (!host) return;
      const buyable = all.filter(function (r) { return r.actionable; });
      const nActionable = counts.actionable !== undefined ? counts.actionable : buyable.length;

      // Where the buyable names actually are — the one-line "what is working".
      const byIndustry = {};
      buyable.slice(0, 40).forEach(function (r) {
        if (r.industry) byIndustry[r.industry] = (byIndustry[r.industry] || 0) + 1;
      });
      const leadIndustries = Object.keys(byIndustry)
        .sort(function (a, b) { return byIndustry[b] - byIndustry[a]; })
        .slice(0, 3);

      const verdict = nActionable === 0
        ? "Nothing is buyable. Every name that scores is either broken or below its 30-week."
        : leadIndustries.length
          ? "Leadership is in " + leadIndustries.join(", ") + "."
          : "Leadership is thin.";

      host.innerHTML =
        '<div class="digest-head">' + (DATA.asOf || "") + "</div>" +
        '<div class="digest-verdict">' + verdict + "</div>" +
        '<div class="tmle-stats">' +
          '<div class="tmle-stat"><div class="tmle-stat-label">Buyable now</div>' +
            '<div class="tmle-stat-num up">' + nActionable + "</div></div>" +
          '<div class="tmle-stat"><div class="tmle-stat-label">Broken ex-leaders</div>' +
            '<div class="tmle-stat-num down">' + (counts.broken || broken.length) + "</div></div>" +
          '<div class="tmle-stat"><div class="tmle-stat-label">Scored</div>' +
            '<div class="tmle-stat-num">' + (counts.scored || all.length) + "</div></div>" +
        "</div>" +
        '<div class="digest-note">Score ranks, stage permits. A broken name keeps the score it earned and never appears in the buyable list.</div>';
    }
    function draw(rows) {
      if (!rows.length) {
        body.innerHTML = '<tr><td colspan="11" class="empty-note">' +
          (filter === "broken"
            ? "No name that scored well has broken down."
            : "Nothing qualifies right now — no name is advancing within " +
              Math.abs(DATA.maxDrawdown || 25) + "% of its high.") +
          "</td></tr>";
        return;
      }
      body.innerHTML = rows.map(function (r) {
        return '<tr data-ticker="' + r.ticker + '" title="' + leaderVerdict(r).replace(/&middot;/g, "-") + '">' +
          "<td>" + (r.rank === null || r.rank === undefined ? "—" : r.rank) + "</td>" +
          '<td class="name-cell">' + logoImg(r.ticker) +
            "<span><b>" + r.ticker + "</b>" +
            (r.industry ? '<span class="row-sub">' + r.industry + "</span>" : "") +
            "</span></td>" +
          '<td class="score-cell ' + scoreClass(r.composite) + '">' + fmtNum(r.composite, 1) + "</td>" +
          "<td>" + fmtNum(r.F1, 0) + "</td><td>" + fmtNum(r.F4, 0) + "</td>" +
          "<td>" + fmtNum(r.F4B, 0) + "</td><td>" + fmtNum(r.F5, 0) + "</td>" +
          gainCell(r) +
          '<td class="pct ' + pctClass(r.drawdown) + '">' + (r.drawdown === null ? "—" : fmtNum(r.drawdown, 0) + "%") + "</td>" +
          sessionsCell(r) +
          "<td>" + stageBadge(r.stage, r.actionable) + "</td>" +
        "</tr>";
      }).join("");
    }

    // Ascending on rank, so #1 is at the top; unranked (non-actionable) names
    // sort last because sortRows pushes nulls to the end either way.
    drawDigest();
    let rows = visible();
    attachSorting(table, rows, draw, "rank", 1);
    makeRowsClickable(table);

    const filterBox = document.getElementById("tmle-filter");
    if (filterBox) {
      filterBox.querySelectorAll("button").forEach(function (btn) {
        btn.addEventListener("click", function () {
          filterBox.querySelectorAll("button").forEach(function (b) { b.classList.remove("active"); });
          btn.classList.add("active");
          filter = btn.dataset.filter;
          rows = visible();
          const key = filter === "actionable" ? "rank" : "composite";
          draw(sortRows(rows, key, key === "rank" ? 1 : -1));
        });
      });
    }
  }

  function renderTmleEmerging() {
    const table = document.getElementById("tmle-emerging-table");
    if (!table) return;
    const rows = DATA.emerging || [];
    const body = table.querySelector("tbody");

    // A missing delta means the name was not in the universe at that
    // checkpoint — a newly listed stock, or one that only just cleared the
    // $2B gate. "new" read as a judgement about the stock; it is a gap in the
    // history, so it says so.
    function delta(v) {
      if (v === null || v === undefined) {
        return '<span class="rs-new" title="not scored back then — no history to compare">not scored yet</span>';
      }
      const cls = v > 0 ? "up" : (v < 0 ? "down" : "");
      return '<span class="' + cls + '" title="' + (v >= 0 ? "+" : "") + v.toFixed(1) +
        ' points of composite score">' + (v >= 0 ? "+" : "") + v.toFixed(1) + "</span>";
    }
    function draw(sorted) {
      if (!sorted.length) {
        body.innerHTML = '<tr><td colspan="7" class="empty-note">No actionable names yet.</td></tr>';
        return;
      }
      body.innerHTML = sorted.map(function (r) {
        return '<tr data-ticker="' + r.ticker + '" title="' + leaderVerdict(r).replace(/&middot;/g, "-") + '">' +
          '<td class="name-cell">' + logoImg(r.ticker) +
            "<span><b>" + r.ticker + "</b>" +
            (r.industry ? '<span class="row-sub">' + r.industry + "</span>" : "") +
            "</span></td>" +
          '<td class="score-cell ' + scoreClass(r.composite) + '">' + fmtNum(r.composite, 1) + "</td>" +
          "<td>" + delta(r.d4) + "</td><td>" + delta(r.d12) + "</td>" +
          gainCell(r) +
          '<td class="pct ' + pctClass(r.drawdown) + '">' + (r.drawdown === null ? "—" : fmtNum(r.drawdown, 0) + "%") + "</td>" +
          sessionsCell(r) +
        "</tr>";
      }).join("");
    }
    attachSorting(table, rows, draw, "d4", -1);
    makeRowsClickable(table);
  }

  function renderTmleStock() {
    const host = document.getElementById("tmle-stock-panel");
    if (!host) return;
    const input = document.getElementById("tmle-ticker");
    const statusEl = document.getElementById("tmle-status");
    const body = document.getElementById("tmle-stock-body");
    const dir = DATA.tmleDir || "tmle";
    let chart = null;

    const scored = (DATA.scored || []).slice().sort();
    const list = document.getElementById("tmle-tickers");
    if (list && scored.length) {
      list.innerHTML = scored.map(function (t) { return '<option value="' + t + '">'; }).join("");
      input.placeholder = "Search " + scored.length.toLocaleString() + " scored names";
    }

    function factorBar(key, value) {
      const meta = (DATA.factorMeta || {})[key] || {};
      const pct = value === null || value === undefined ? 0 : Math.max(0, Math.min(100, value));
      return '<div class="factor-line">' +
        '<div class="factor-head"><span class="factor-key">' + key + "</span>" +
          "<span>" + (meta.label || key) + "</span>" +
          '<span class="factor-val">' + (value === null ? "—" : fmtNum(value, 0)) + "</span></div>" +
        '<div class="factor-track"><div class="factor-fill" style="width:' + pct + '%"></div></div>' +
        '<div class="factor-blurb">' + (meta.blurb || "") + "</div>" +
      "</div>";
    }

    function draw(data) {
      const n = data.dates.length - 1;
      const stage = data.stage[n];
      const score = data.composite[n];
      const dd = data.drawdown[n];
      const actionable = stage === 2 && dd !== null && dd >= (DATA.maxDrawdown || -25);

      const cls = (DATA.classification || {})[data.ticker];
      const verdict = leaderVerdict({
        stage: stage, actionable: actionable, drawdown: dd,
        gain: (data.gain || [])[n],
        episode_days: (data.episode_days || [])[n],
        pct_below_10w: (data.pct_below_10w || [])[n],
      });

      body.innerHTML =
        '<div class="card">' +
          '<div class="tmle-head">' +
            logoImg(data.ticker, "stock-logo") +
            "<div>" +
              '<div class="stock-sym">' + data.ticker + "</div>" +
              '<div class="card-sub">' + (cls ? cls[1] + " &middot; " + cls[0] + "<br>" : "") +
                stageBadge(stage, actionable) + "</div>" +
            "</div>" +
            '<div class="tmle-score"><div class="card-value ' + scoreClass(score) + '">' + fmtNum(score, 1) + "</div>" +
              '<div class="card-sub">leader score &middot; ' + data.dates[n] + "</div></div>" +
          "</div>" +
          '<div class="tmle-verdict ' + (actionable ? "ok" : "nope") + '">' + verdict + "</div>" +
          '<div class="env-block-sub">' +
            (actionable
              ? "Advancing and within " + Math.abs(DATA.maxDrawdown || 25) + "% of its high — the engine will let you buy this."
              : "Not actionable: " + (stage !== 2 ? "not in a confirmed advance." :
                 "more than " + Math.abs(DATA.maxDrawdown || 25) + "% off its high (" + fmtNum(dd, 0) + "%).")) +
          "</div>" +
          '<div class="factor-list">' +
            Object.keys(DATA.factorMeta || {}).map(function (key) {
              const arr = (data.factors || {})[key] || [];
              return factorBar(key, arr[n]);
            }).join("") +
          "</div>" +
          '<div class="env-chart-title">Score over time</div>' +
          '<div class="env-block-sub">the level matters less than the slope &middot; a score climbing week after week is leadership forming</div>' +
          '<div class="chart-wrap"><canvas id="tmle-canvas"></canvas></div>' +
        "</div>";

      const th = themeColors();
      if (chart) chart.destroy();
      chart = new Chart(document.getElementById("tmle-canvas"), {
        type: "line",
        data: {
          labels: data.dates,
          datasets: [{
            data: data.composite,
            borderColor: th.accent,
            backgroundColor: "rgba(41,98,255,0.10)",
            fill: true, tension: 0.25, borderWidth: 2,
            pointRadius: dotRadius(data.dates.length),
            // Colour each point by the stage it was in, so the chart shows not
            // just how the score moved but when the move stopped being safe.
            pointBackgroundColor: data.stage.map(function (s) {
              return s === 2 ? th.up : (s === 4 ? th.down : th.text);
            }),
          }],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { grid: { color: th.grid }, ticks: { color: th.text, maxTicksLimit: 8 } },
            y: { grid: { color: th.grid }, ticks: { color: th.text }, suggestedMin: 0, suggestedMax: 100 },
          },
        },
      });
    }

    function load(sym) {
      sym = (sym || "").trim().toUpperCase();
      if (!sym) return;
      statusEl.className = "replay-miss";
      statusEl.textContent = "loading…";
      fetch(dir + "/" + encodeURIComponent(sym) + ".json")
        .then(function (r) { if (!r.ok) throw new Error("404"); return r.json(); })
        .then(function (data) {
          statusEl.textContent = "";
          draw(data);
        })
        .catch(function () {
          statusEl.className = "replay-miss";
          statusEl.textContent = sym + " has no leader score — it has not reached the scored set.";
          body.innerHTML = "";
        });
    }

    input.addEventListener("change", function () {
      load(input.value);
      Sync.publish({ ticker: input.value.trim().toUpperCase() });
    });
    Sync.subscribe(function (ctx) {
      const want = typeof ctx.ticker === "string" ? ctx.ticker.trim().toUpperCase() : null;
      if (want && scored.indexOf(want) !== -1 && want !== input.value.trim().toUpperCase()) {
        input.value = want;
        load(want);
      }
    });

    const shared = Sync.read();
    const initial = (shared.ticker && scored.indexOf(shared.ticker) !== -1)
      ? shared.ticker : (scored.length ? scored[0] : null);
    if (initial) { input.value = initial; load(initial); }
  }

  // ---------- Wire everything up ----------
  renderEnvironmentPanel();
  renderReplayPanel();
  renderStockPanel();
  renderTmleLeaders();
  renderTmleEmerging();
  renderTmleStock();
  renderHiloScreener();

  // Each grid container's data-keys attribute lists which series to render
  // there (comma-separated). This lets the same script serve both the full
  // dashboard (all keys) and an individual single-panel embed page (one key)
  // without needing separate JS per page — a page simply omits the
  // container element for any panel it doesn't include, and the guards
  // below skip anything not present in the DOM.
  // Supplied by the renderer, because the index set differs per country
  // (nasdaq/sp500/russell2000 for the US, sensex/nifty500/midcap/smallcap for
  // India). The US names are kept only as a fallback for any page rendered
  // before indexLabels was added to the payload.
  const INDEX_LABELS = DATA.indexLabels || {
    index_nasdaq: "NASDAQ Composite",
    index_sp500: "S&P 500",
    index_russell2000: "Russell 2000",
  };

  const indicesGrid = document.getElementById("indices-grid");
  if (indicesGrid) {
    (indicesGrid.dataset.keys || "").split(",").filter(Boolean).forEach(function (key) {
      renderIndexCard(key, INDEX_LABELS[key] || key, indicesGrid);
    });
  }

  if (document.getElementById("sector-table")) {
    renderRankPanel("sector_ranks", "sectors", "sector", "sector-table", "sector-drilldown");
  }
  if (document.getElementById("industry-table")) {
    renderRankPanel("industry_ranks", "industries", "industry", "industry-table", "industry-drilldown");
  }

  const BREADTH_DEFS = {
    breadth_adv_decl: {
      label: "Net Advancers − Decliners", valueField: "net",
      opts: {
        colorByValue: true,
        streakLabels: { pos: "Advancers ahead", neg: "Decliners ahead" },
        subText: function (items) {
          return "Avg Adv " + Math.round(average(items, "advancers")) + " / Avg Decl " + Math.round(average(items, "decliners"));
        },
      },
    },
    breadth_new_hilo: {
      label: "Net New Highs − New Lows", valueField: "net",
      opts: {
        colorByValue: true,
        streakLabels: { pos: "New highs ahead", neg: "New lows ahead" },
        subText: function (items) {
          return "Avg Highs " + Math.round(average(items, "new_highs")) + " / Avg Lows " + Math.round(average(items, "new_lows"));
        },
      },
    },
    breadth_pct_up20: { label: "% Up 20%+ (5D)", valueField: "value", opts: { isPct: true } },
    breadth_pct_up30: { label: "% Up 30%+ (5D)", valueField: "value", opts: { isPct: true } },
    breadth_pct_down20: { label: "% Down 20%+ (5D)", valueField: "value", opts: { isPct: true } },
    breadth_pct_down30: { label: "% Down 30%+ (5D)", valueField: "value", opts: { isPct: true } },
  };

  const breadthGrid = document.getElementById("breadth-grid");
  if (breadthGrid) {
    (breadthGrid.dataset.keys || "").split(",").filter(Boolean).forEach(function (key) {
      // The highs/lows counts card carries two series and two headline
      // numbers, so it has its own renderer rather than a BREADTH_DEFS entry.
      if (key === "breadth_hilo_counts") {
        renderHiLoCountsCard(key, "52-Week Highs & Lows", breadthGrid);
        return;
      }
      const def = BREADTH_DEFS[key];
      if (def) renderSimpleMetricCard(key, def.label, def.valueField, breadthGrid, def.opts);
    });
  }
})();
