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
  const INDEX_SHORT = { nasdaq: "NASDAQ", sp500: "S&P 500", russell2000: "Russell 2000" };
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
            '<div class="env-block-sub">more stocks rising than falling on a typical day</div>' +
            '<div class="env-block-extra">' +
              (i && i.new_hilo_avg !== null
                ? fmtSignedInt(Math.round(i.new_hilo_avg)) + " more new highs than new lows &middot; both averaged over " + i.lookback_days + " sessions"
                : "&nbsp;") +
            "</div>" +
          "</div>" +

        "</div>" +
        (env.leaders ? moversHtml(env.leaders) : "") +
        '<div class="env-chart-block">' +
          '<div class="env-chart-title">Trend strength over time</div>' +
          '<div class="env-block-sub">how many of the 9 index-vs-EMA factors were favourable each day &middot; 9 is fully bullish, 0 fully bearish</div>' +
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
              '<div class="env-block-sub">more stocks rising than falling on a typical day</div>' +
              '<div class="env-block-extra">' +
                (i && i.new_hilo_avg !== null ? fmtSignedInt(Math.round(i.new_hilo_avg)) + " more new highs than new lows" : "&nbsp;") +
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

    dateInput.addEventListener("change", function () { go(dateInput.value); });
    document.getElementById("replay-prev").addEventListener("click", function () { step(-1); });
    document.getElementById("replay-next").addEventListener("click", function () { step(1); });
    document.getElementById("replay-latest").addEventListener("click", function () { go(dates[dates.length - 1]); });

    tickerInput.addEventListener("input", function () {
      const sym = tickerInput.value.trim().toUpperCase();
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
    });

    go(dates[dates.length - 1]);
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

    function draw() {
      if (!current) return;
      const d = current.data, sym = current.symbol;
      const wanted = dateInput.value;
      let end = -1;
      for (let i = 0; i < d.dates.length; i++) { if (d.dates[i] <= wanted) end = i; else break; }
      if (end < 20) { body.innerHTML = '<div class="empty-note">Not enough history before this date.</div>'; return; }
      dateInput.value = d.dates[end];

      const e10 = ema(d.close, 10), e20 = ema(d.close, 20), e50 = ema(d.close, 50);
      const startIdx = Math.max(0, end - 125);
      const slice = function (a) { return a.slice(startIdx, end + 1); };
      const dates = slice(d.dates), close = slice(d.close);

      const spx = benchAt("sp500", d.dates, end).slice(startIdx);
      const ndx = benchAt("nasdaq", d.dates, end).slice(startIdx);
      function rsLine(b) {
        const base = close[0] / b[0];
        return close.map(function (c, i) { return b[i] ? (c / b[i]) / base * 100 : null; });
      }

      const cls = DATA.classification ? DATA.classification[sym] : null;
      const windows = [["1 week", 5], ["1 month", 21], ["3 months", 63], ["6 months", 126]];
      let cards = "";
      windows.forEach(function (w) {
        const s = pctBetween(d.close, end, w[1]);
        const bs = pctBetween(benchAt("sp500", d.dates, end), end, w[1]);
        const bn = pctBetween(benchAt("nasdaq", d.dates, end), end, w[1]);
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
            '<div class="rs-line"><span>vs S&amp;P 500 (' + (bs === null ? "—" : fmtSignedPct(bs)) + ")</span>" +
              '<span class="rs-gap">' + gap(s, bs) + "</span></div>" +
            '<div class="rs-line"><span>vs Nasdaq (' + (bn === null ? "—" : fmtSignedPct(bn)) + ")</span>" +
              '<span class="rs-gap">' + gap(s, bn) + "</span></div>" +
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
          '<div class="lw-chart" id="stock-chart"></div>' +
          '<div class="rs-block">' +
            '<div class="env-chart-title">Relative strength</div>' +
            '<div class="env-block-sub">the stock divided by each index &middot; ' +
              "green while it is trending up against that index, red while trending down</div>" +
            '<div class="rs-charts">' +
              '<div><div class="rs-chart-label">vs S&amp;P 500</div><div class="lw-chart rs-chart" id="rs-sp500"></div></div>' +
              '<div><div class="rs-chart-label">vs Nasdaq</div><div class="lw-chart rs-chart" id="rs-nasdaq"></div></div>' +
            "</div>" +
            '<div class="rs-grid">' + cards + "</div>" +
          "</div>" +
        "</div>";

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

      // Shaded green while relative strength is trending up and red while it
      // is trending down — so a stock quietly losing ground to the index shows
      // as the shading flipping, even if it is still ahead overall.
      // Trend is measured against the RS line's own 20-day average rather than
      // day-to-day direction, which would flicker colour on every wiggle.
      // Two area series sharing one scale: each carries values only while its
      // side is active and whitespace otherwise, with the turning point given
      // to both so the segments join without a gap.
      [["rs-sp500", spx], ["rs-nasdaq", ndx]].forEach(function (cfg) {
        const chart = makeChart(cfg[0], 150);
        const line = rsLine(cfg[1]);
        const valid = line.map(function (v) { return v === null ? 100 : v; });
        const trend = ema(valid, 20);

        const upData = [], downData = [];
        let prevUp = null;
        dates.forEach(function (dt, i) {
          if (line[i] === null) { upData.push({ time: dt }); downData.push({ time: dt }); return; }
          const isUp = valid[i] >= trend[i];
          const turning = prevUp !== null && prevUp !== isUp;
          upData.push(isUp || turning ? { time: dt, value: line[i] } : { time: dt });
          downData.push(!isUp || turning ? { time: dt, value: line[i] } : { time: dt });
          prevUp = isUp;
        });

        chart.addAreaSeries({
          lineColor: th.up, topColor: "rgba(22,163,74,0.32)", bottomColor: "rgba(22,163,74,0.02)",
          lineWidth: 2, priceLineVisible: false, lastValueVisible: false,
        }).setData(upData);
        chart.addAreaSeries({
          lineColor: th.down, topColor: "rgba(220,38,38,0.32)", bottomColor: "rgba(220,38,38,0.02)",
          lineWidth: 2, priceLineVisible: false, lastValueVisible: false,
        }).setData(downData);
        chart.timeScale().fitContent();
      });
    }

    function load(sym) {
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
        if (!dateInput.value || dateInput.value > dateInput.max || dateInput.value < dateInput.min) {
          dateInput.value = dateInput.max;
        }
        statusEl.textContent = "";
        draw();
      }).catch(function () {
        statusEl.className = "replay-miss";
        statusEl.textContent = sym + " is not tracked — add it to the watchlist in config.py";
        body.innerHTML = "";
        current = null;
      });
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

    tickerInput.addEventListener("change", function () { load(tickerInput.value); });
    dateInput.addEventListener("change", draw);
    document.getElementById("stock-prev").addEventListener("click", function () { step(-1); });
    document.getElementById("stock-next").addEventListener("click", function () { step(1); });
    document.getElementById("stock-latest").addEventListener("click", function () {
      if (current) { dateInput.value = current.data.dates[current.data.dates.length - 1]; draw(); }
    });

    const first = (host.dataset.tickers || "").split(",")[0];
    if (first) { tickerInput.value = first; load(first); }
  }

  // ---------- Wire everything up ----------
  renderEnvironmentPanel();
  renderReplayPanel();
  renderStockPanel();

  // Each grid container's data-keys attribute lists which series to render
  // there (comma-separated). This lets the same script serve both the full
  // dashboard (all keys) and an individual single-panel embed page (one key)
  // without needing separate JS per page — a page simply omits the
  // container element for any panel it doesn't include, and the guards
  // below skip anything not present in the DOM.
  const INDEX_LABELS = {
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
      const def = BREADTH_DEFS[key];
      if (def) renderSimpleMetricCard(key, def.label, def.valueField, breadthGrid, def.opts);
    });
  }
})();
