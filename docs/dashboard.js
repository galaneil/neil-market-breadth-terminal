(function () {
  "use strict";
  const DATA = JSON.parse(document.getElementById("dashboard-data").textContent);
  const S = DATA.series;
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

  // ---------- Wire everything up ----------
  renderEnvironmentPanel();

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
