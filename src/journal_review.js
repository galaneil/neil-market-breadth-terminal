/* Trade Journal > Monthly review. Loaded by the journal page; uses its globals (ALL_TRADES, openDetail, esc, activeAccount, jlogo).
   Everything on the page is clickable: a trade opens its journal entry, a setup opens that setup in Setups, the Nasdaq 100 opens the
   index with the period shaded, and every count filters the trade list below it. */
(function () {
  const $ = id => document.getElementById(id);
  const MR = { months: new Set(), data: null, avail: null, flt: { k: "all", v: null }, sk: "cad", sd: 1, acct: null };
  const css = document.createElement("style");
  css.textContent =
    ".mr-top{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center;margin:4px 0 14px}.mr-chips{display:flex;gap:6px;flex-wrap:wrap}" +
    ".mr-chip{font:inherit;font-size:12.5px;font-weight:600;padding:5px 12px;border-radius:99px;border:1px solid var(--line);background:var(--panel);color:var(--text);cursor:pointer}" +
    ".mr-chip.on{background:var(--accent);border-color:var(--accent);color:#fff}.mr-chip small{opacity:.7;margin-left:5px;font-weight:500}" +
    ".mr-card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:12px}.mr-card h2{font-size:15px;margin:0 0 3px}" +
    ".mr-note{font-size:12px;color:var(--dim)}.mr-say{font-size:15px;line-height:1.65}.mr-say p{margin:7px 0}" +
    ".mr-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}.mr-kpi{border:1px solid var(--line);border-radius:10px;padding:8px 12px;background:var(--panel);text-align:left;font:inherit;color:var(--text)}" +
    "button.mr-kpi{cursor:pointer}button.mr-kpi:hover{border-color:var(--accent)}.mr-kpi .k{font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim)}.mr-kpi .v{font-size:21px;font-weight:600;font-family:'IBM Plex Mono',monospace}" +
    ".mr-link{color:var(--accent);cursor:pointer;font-weight:600;text-decoration:none;border:0;background:none;font:inherit;font-weight:600;padding:0}.mr-link:hover{text-decoration:underline}" +
    ".mr-two{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media(max-width:760px){.mr-two{grid-template-columns:1fr}}" +
    ".mr-hold{display:grid;grid-template-columns:130px 1fr 170px;gap:10px;align-items:center;margin:8px 0;width:100%;border:0;background:none;color:var(--text);font:inherit;text-align:left;cursor:pointer;padding:3px;border-radius:8px}.mr-hold:hover,.mr-rt:hover{background:rgba(120,140,160,.12)}" +
    ".mr-bar{background:rgba(120,140,160,.18);border-radius:4px;height:22px}.mr-bar i{display:block;height:100%;border-radius:4px}" +
    ".mr-rt{display:grid;grid-template-columns:90px 1fr 120px;gap:10px;align-items:center;margin:4px 0;width:100%;border:0;background:none;color:var(--text);font:inherit;text-align:left;cursor:pointer;padding:3px;border-radius:8px}" +
    ".mr-tw{overflow:auto;max-height:430px}.mr-tw table{width:100%;border-collapse:collapse;font-size:12.5px}.mr-tw th{position:sticky;top:0;background:var(--panel);text-align:left;font-size:10.5px;text-transform:uppercase;letter-spacing:.04em;color:var(--dim);padding:7px 8px;border-bottom:1px solid var(--line);cursor:pointer;white-space:nowrap}" +
    ".mr-tw td{padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}.mr-tw tr.mr-row{cursor:pointer}.mr-tw tr.mr-row:hover td{background:rgba(120,140,160,.12)}.mr-tw .r{text-align:right}" +
    ".mr-pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:11px;font-weight:700;background:rgba(202,138,4,.15);color:var(--warn)}" +
    ".mr-item{display:grid;grid-template-columns:26px 1fr;gap:10px;padding:10px 0;border-top:1px solid var(--line)}.mr-item:first-child{border-top:0}.mr-num{width:24px;height:24px;border-radius:99px;background:var(--accent);color:#fff;font-weight:700;font-size:12px;display:flex;align-items:center;justify-content:center}" +
    ".mr-wi{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:10px}.mr-wi>div{border:1px solid var(--line);border-radius:10px;padding:10px 12px}" +
    ".mr-flt{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}.mr-mono{font-family:'IBM Plex Mono',monospace;font-variant-numeric:tabular-nums}" +
    ".mr-band{cursor:pointer}.mr-band:hover{opacity:1!important}";
  document.head.appendChild(css);

  const fmtN = (v, d) => Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: d || 0, minimumFractionDigits: d || 0 });
  let SYM = "C$";
  const fm = (v, d) => (v < 0 ? "−" : "") + SYM + fmtN(v, d);
  const fs = (v, d) => (v >= 0 ? "+" : "−") + SYM + fmtN(v, d);
  const pc = (v, d) => (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(d == null ? 1 : d) + "%";
  const cl = v => (v >= 0 ? "up" : "down");
  const plural = (n, a, b) => n + " " + (n === 1 ? a : b);
  const mon = m => new Date(m + "-15T00:00:00").toLocaleDateString(undefined, { month: "short" });
  const hub = msg => { try { window.parent.postMessage(msg, location.origin); } catch (e) {} };
  const openTrade = id => { const t = ALL_TRADES.find(x => x.pageId === id); if (t) openDetail(t); };
  const openSetup = name => { if (name) hub({ hubGo: "Setups", open: { setup: name } }); };
  const link = (id, label) => '<button type="button" class="mr-link" data-open="' + id + '">' + esc(label) + "</button>";
  const flink = (k, v, label) => '<button type="button" class="mr-link" data-flt="' + k + '"' + (v != null ? ' data-v="' + esc(v) + '"' : "") + ">" + label + "</button>";
  const slink = name => name ? '<button type="button" class="mr-link" data-setup="' + esc(name) + '">' + esc(name) + "</button>" : '<span class="mr-pill" data-flt="nt">No tag</span>';

  function agg(rs) {
    const w = rs.filter(r => r.cad > 0), l = rs.filter(r => r.cad <= 0), gw = w.reduce((a, r) => a + r.cad, 0), gl = -l.reduce((a, r) => a + r.cad, 0);
    return { n: rs.length, w: w.length, wr: rs.length ? Math.round(w.length / rs.length * 100) : 0, net: rs.reduce((a, r) => a + r.cad, 0), avgW: w.length ? gw / w.length : 0, avgL: l.length ? -gl / l.length : 0, pf: gl ? gw / gl : null };
  }
  const med = a => { const s = a.slice().sort((x, y) => x - y); return s.length ? s[Math.floor(s.length / 2)] : 0; };

  // ---------- load ----------
  function account() { return (typeof activeAccount !== "undefined" && activeAccount && activeAccount !== "all") ? activeAccount : "NG-IBKR"; }
  async function getAvail() {
    const a = account();
    if (MR.avail && MR.acct === a) return;
    MR.acct = a; MR.avail = (await fetch("/api/journal/review?account=" + encodeURIComponent(a)).then(r => r.json())).available || [];
    const good = MR.avail.filter(x => x.n >= 5);
    MR.months = new Set(good.length ? [good[good.length - 1].m] : MR.avail.slice(-1).map(x => x.m));
  }
  async function load() {
    if (!MR.months.size) { MR.data = null; return; }
    const url = "/api/journal/review?account=" + encodeURIComponent(account()) + "&months=" + [...MR.months].sort().join(",");
    const d = await fetch(url).then(r => r.json());
    if (d.error) throw new Error(d.error);
    SYM = d.display || "C$";
    d.trades.forEach(r => { r.cad = r.pnl * d.fx; r.cost2 = r.cost * d.fx; r.gave = Math.max(0, r.mfe - r.pct) / 100 * r.cost * d.fx; r.peakC = Math.max(0, r.mfe) / 100 * r.cost * d.fx; });
    MR.data = d;
  }
  window.drawReview = async function () {
    const host = $("tab-review"); if (!host) return;
    if (!host.dataset.built) { host.dataset.built = "1"; host.innerHTML = '<div class="mr-top"><div class="mr-chips" id="mr-chips"></div></div><div id="mr-body"><div class="mr-note">Loading the review&hellip;</div></div>'; host.addEventListener("click", onClick); }
    try { await getAvail(); drawChips(); await load(); drawBody(); } catch (e) { $("mr-body").innerHTML = '<div class="mr-card">Could not build the review: ' + esc(e.message) + "</div>"; }
  };
  function drawChips() {
    const yrs = {}; MR.avail.forEach(a => { (yrs[a.m.slice(0, 4)] = yrs[a.m.slice(0, 4)] || []).push(a); });
    $("mr-chips").innerHTML = Object.keys(yrs).sort().map(y => '<span class="mr-note" style="margin:0 2px 0 6px">' + y + "</span>" + yrs[y].map(a => '<button type="button" class="mr-chip' + (MR.months.has(a.m) ? " on" : "") + '" data-m="' + a.m + '">' + mon(a.m) + "<small>" + a.n + "</small></button>").join("")).join("") +
      '<span class="mr-note" style="margin-left:8px">Pick one month, or several to review them together.</span>';
  }

  // ---------- page ----------
  function drawBody() {
    const D = MR.data; if (!D) { $("mr-body").innerHTML = '<div class="mr-card">Pick a month above.</div>'; return; }
    const T = D.trades, A = agg(T), nav = D.nav, hasNav = nav.length > 1;
    const first = nav[0], last = nav[nav.length - 1];
    const peak = hasNav ? nav.reduce((a, b) => b[1] > a[1] ? b : a) : null, pkI = hasNav ? nav.indexOf(peak) : -1;
    const ndxOK = hasNav && first[2] && last[2], ndxPk = ndxOK ? nav.filter(r => r[2]).reduce((a, b) => b[2] > a[2] ? b : a) : null;
    const label = D.months.map(m => mon(m)).join(" + ") + " " + D.months[D.months.length - 1].slice(0, 4);
    const sh = T.filter(r => r.hold <= 5), lg = T.filter(r => r.hold > 5), aS = agg(sh), aL = agg(lg);
    const rt = T.filter(r => r.mfe >= 5 && r.pct < r.mfe / 3), big = T.filter(r => r.pct <= -5), bigSum = big.reduce((a, r) => a + r.cad, 0);
    const up3 = T.filter(r => r.mfe >= 3), up5 = T.filter(r => r.mfe >= 5), noTag = T.filter(r => !r.setup), noStop = T.filter(r => !r.stop);
    const wiA = big.reduce((a, r) => a + (-r.pct - 5) / 100 * r.cost2, 0), brk = T.filter(r => r.mfe >= 5 && r.pnl < 0), wiB = brk.reduce((a, r) => a - r.cad, 0);
    // stretches around the account's high: before it, the 8 sessions after, the rest
    let PH = [];
    if (hasNav && pkI < nav.length - 2) {
      const endA = nav[Math.min(nav.length - 1, pkI + 8)][0];
      PH = [["ph1", "Before the high", T.filter(r => r.opened <= peak[0])], ["ph2", "Right after the high", T.filter(r => r.opened > peak[0] && r.opened <= endA)], ["ph3", "The rest", T.filter(r => r.opened > endA)]]
        .map(p => Object.assign({ k: p[0], name: p[1], rs: p[2], med: med(p[2].map(r => r.hold)) }, agg(p[2])));
    }
    const best = T.slice().sort((a, b) => b.cad - a.cad), worst = T.slice().sort((a, b) => a.cad - b.cad);
    const N = D.navFrom;

    // KPI tiles (each one filters the trade list)
    const kp = [];
    if (hasNav) kp.push(['<button type="button" class="mr-kpi" data-index="1"><div class="k">Account, ' + label + '</div><div class="v ' + cl(last[1] - first[1]) + '">' + pc((last[1] / first[1] - 1) * 100) + '</div><div class="mr-note">' + fs((last[1] - first[1]) * 1, 0) + (ndxOK ? " &middot; Nasdaq 100 " + pc((last[2] / first[2] - 1) * 100) : "") + "</div></button>"]);
    else kp.push('<div class="mr-kpi"><div class="k">Account curve</div><div class="v">&ndash;</div><div class="mr-note">No saved account history for this period' + (N ? " (starts " + N + ")" : "") + "</div></div>");
    if (hasNav) kp.push('<div class="mr-kpi"><div class="k">From its high (' + peak[0].slice(5) + ')</div><div class="v ' + cl(last[1] - peak[1]) + '">' + pc((last[1] / peak[1] - 1) * 100) + '</div><div class="mr-note">' + (ndxOK ? "Nasdaq 100 from its high " + pc((last[2] / ndxPk[2] - 1) * 100) : "") + "</div></div>");
    kp.push('<button type="button" class="mr-kpi" data-flt="win"><div class="k">Win rate</div><div class="v ' + (A.wr >= 40 ? "up" : "down") + '">' + A.wr + '%</div><div class="mr-note">' + A.w + " of " + A.n + " won &middot; click for the winners</div></button>");
    kp.push('<button type="button" class="mr-kpi" data-flt="loss"><div class="k">Average win / loss</div><div class="v">' + fm(A.avgW) + " / " + fm(A.avgL) + '</div><div class="mr-note">profit factor ' + (A.pf == null ? "&ndash;" : A.pf.toFixed(2)) + " &middot; click for the losers</div></button>");
    kp.push('<button type="button" class="mr-kpi" data-flt="all"><div class="k">Realised, ' + T.length + ' trades</div><div class="v ' + cl(A.net) + '">' + fs(A.net) + '</div><div class="mr-note">from the journal &middot; click for all trades</div></button>');

    // the spoken verdict, every clickable name is a real link
    const nm = r => link(r.id, r.t);
    let say = "";
    if (T.length < 3) say = "<p>Only " + plural(T.length, "trade", "trades") + " closed in this period, too few to say anything about.</p>";
    else {
      say += "<p><b>" + (A.net >= 0 ? "A profitable stretch." : "A losing stretch.") + "</b> You closed " + plural(T.length, "position", "positions") + ", " + A.w + " winners (" + A.wr + "%), for " + fs(A.net) + ". Winners averaged " + fm(A.avgW) + " and losers " + fm(-A.avgL) + (A.pf != null && A.pf < 1 ? ", so you needed a win rate near " + Math.round(100 * (-A.avgL) / (-A.avgL + A.avgW)) + "% to break even." : ".") + "</p>";
      if (hasNav) say += "<p>The account went " + pc((last[1] / first[1] - 1) * 100) + " (" + fs(last[1] - first[1]) + ")" + (ndxOK ? ", the " + '<button type="button" class="mr-link" data-index="1">Nasdaq 100</button>' + " " + pc((last[2] / first[2] - 1) * 100) + " over the same days" : "") + ". From its high on " + peak[0].slice(5) + " the account is " + pc((last[1] / peak[1] - 1) * 100) + (ndxOK ? ", about " + ((last[1] / peak[1] - 1) / (last[2] / ndxPk[2] - 1)).toFixed(1) + " times the index's move from its own high" : "") + ".</p>";
      if (aS.n >= 3 && aL.n >= 3) say += "<p>Holding time is the clearest line. Positions held more than 5 days: " + flink("h5", null, plural(aL.n, "trade", "trades")) + ", " + fs(aL.net) + " (" + aL.w + " won). Held 5 days or less: " + flink("hs", null, plural(aS.n, "trade", "trades")) + ", " + fs(aS.net) + " (" + aS.w + " won). " + (aL.net > aS.net ? "Your best results came from the patient trades: " + best.slice(0, 3).map(nm).join(", ") + "." : "") + "</p>";
      if (PH.length && PH[1].n >= 5) say += "<p>Right after the account's high you opened " + flink("ph2", null, PH[1].n + " new trades") + ", held a median of " + PH[1].med + " day" + (PH[1].med === 1 ? "" : "s") + ", and " + PH[1].w + " worked (" + fs(PH[1].net) + "). " + (PH[0].n ? "Before the high you held a median of " + PH[0].med + " days (" + fs(PH[0].net) + " over " + PH[0].n + " trades)." : "") + "</p>";
      say += "<p>" + (up3.length / T.length >= 0.5 ? "<b>Your entries were mostly right:</b> " : "<b>Your entries often did not work right away:</b> ") + up3.length + " of " + T.length + " trades were up 3% or more at some point. The leak is managing them: " + flink("rt", null, plural(rt.length, "trade", "trades")) + " were up 5%+ and gave nearly all of it back" + (rt.length ? " (" + rt.slice().sort((a, b) => b.gave - a.gave).slice(0, 3).map(nm).join(", ") + ")" : "") + ", and " + flink("big", null, plural(big.length, "loss", "losses")) + " ran past 5% (" + fs(bigSum) + ")" + (big.length ? ", worst " + worst.slice(0, 3).map(r => link(r.id, r.t + " " + pc(r.pct))).join(", ") : "") + ". " + (noStop.length ? flink("ns", null, noStop.length + " of " + T.length) + " trades have no stop recorded." : "") + "</p>";
    }

    // chart
    let chart = "";
    if (hasNav) chart = navChart(nav, peak, pkI, PH, ndxOK);

    // hold buckets
    const hb = [["h1", "1 day or less", T.filter(r => r.hold <= 1)], ["h25", "2 to 5 days", T.filter(r => r.hold >= 2 && r.hold <= 5)], ["h5", "More than 5 days", lg]].map(b => [b[0], b[1], agg(b[2])]);
    const mx = Math.max.apply(null, hb.map(b => Math.abs(b[2].net)).concat([1]));
    const holdHtml = hb.map(b => '<button type="button" class="mr-hold" data-flt="' + b[0] + '"><div>' + b[1] + '<div class="mr-note">' + b[2].n + ' trades</div></div><div class="mr-bar"><i style="width:' + Math.abs(b[2].net) / mx * 100 + "%;background:var(--" + (b[2].net >= 0 ? "up" : "down") + ')"></i></div><div class="mr-mono"><b class="' + cl(b[2].net) + '">' + fs(b[2].net) + '</b><div class="mr-note">' + plural(b[2].w, "win", "wins") + " (" + b[2].wr + "%)</div></div></button>").join("");

    // round trips
    const rtRows = rt.slice().sort((a, b) => b.gave - a.gave).slice(0, 10).map(r => {
      const m = 25, pk = (Math.min(m, r.mfe) + m) / (2 * m) * 100, ex = (Math.max(-m, Math.min(m, r.pct)) + m) / (2 * m) * 100;
      return '<button type="button" class="mr-rt" data-open="' + r.id + '"><div><b>' + esc(r.t) + '</b><div class="mr-note">' + r.opened.slice(5) + '</div></div><div style="position:relative;height:18px;background:rgba(120,140,160,.18);border-radius:4px"><div style="position:absolute;left:50%;top:0;bottom:0;width:1px;background:var(--dim)"></div><div style="position:absolute;top:8px;height:2px;left:' + Math.min(ex, pk) + "%;width:" + Math.abs(pk - ex) + '%;background:var(--warn)"></div><div style="position:absolute;left:calc(' + pk + '% - 5px);top:4px;width:10px;height:10px;border-radius:99px;background:var(--up)"></div><div style="position:absolute;left:calc(' + ex + '% - 5px);top:4px;width:10px;height:10px;border-radius:99px;background:var(--down)"></div></div><div class="mr-mono mr-note">peak ' + pc(r.mfe, 0) + " &rarr; " + pc(r.pct) + "</div></button>";
    }).join("");

    // by setup
    const g = {}; T.forEach(r => (g[r.setup || ""] = g[r.setup || ""] || []).push(r));
    const setupRows = Object.keys(g).map(k => [k, agg(g[k])]).sort((a, b) => a[1].net - b[1].net).map(r => '<tr><td>' + slink(r[0]) + '</td><td class="r"><button type="button" class="mr-link mr-mono" data-flt="setup" data-v="' + esc(r[0]) + '">' + r[1].n + '</button></td><td class="r mr-mono">' + r[1].w + '</td><td class="r mr-mono ' + cl(r[1].net) + '">' + fs(r[1].net) + "</td></tr>").join("");

    const coach = [
      ["Fewer, patient trades after a turn", PH.length && PH[1].n >= 5 ? "From the account's high, " + flink("ph2", null, PH[1].n + " trades") + " were opened over the next 8 sessions with a median hold of " + PH[1].med + " day" + (PH[1].med === 1 ? "" : "s") + " and " + PH[1].w + " worked." : "Held more than 5 days: " + fs(aL.net) + ". Held 5 days or less: " + fs(aS.net) + "."],
      ["A stop on every trade before you buy", flink("ns", null, noStop.length + " of " + T.length) + " trades have no stop recorded. " + flink("big", null, plural(big.length, "loss", "losses")) + " went past 5% for " + fs(bigSum) + "; capping them at 5% is worth about " + fm(wiA) + "."],
      ["Protect the winners that run", flink("rt", null, plural(brk.length, "trade", "trades")) + " were up 5%+ and still closed at a loss. A stop at breakeven after +5% would have saved about " + fm(wiB) + "."],
      ["Tag every trade", noTag.length ? flink("nt", null, plural(noTag.length, "trade", "trades")) + " have no setup tag" + (agg(noTag).net < 0 ? " and lost " + fm(-agg(noTag).net) : "") + ". Untagged usually means it was not a planned setup." : "Every trade has a setup tag. Keep it up."]
    ];

    $("mr-body").innerHTML =
      '<div class="mr-card"><div class="mr-kpis">' + kp.join("") + "</div></div>" +
      '<div class="mr-card mr-say">' + say + "</div>" +
      (chart ? '<div class="mr-card"><h2>The account against the Nasdaq 100</h2><div class="mr-note">Same starting point. Click a shaded stretch to list the trades you opened in it, or the Nasdaq 100 to open the index for these days.</div>' + chart + "</div>" : "") +
      '<div class="mr-card"><h2>Where the losses came from: how long you held</h2><div class="mr-note">Click a row to list those trades.</div>' + holdHtml + "</div>" +
      '<div class="mr-card"><h2>Stock picking vs trade management</h2><div class="mr-two"><div><div class="mr-note">PICKING</div><p>' + flink("q3", null, up3.length + " of " + T.length) + " trades were up 3%+ at some point and " + flink("q5", null, up5.length) + " reached 5%. The median trade peaked at <b>" + (T.length ? med(T.map(r => r.mfe)).toFixed(1) : "0") + "%</b>.</p></div><div><div class=\"mr-note\">MANAGING</div><p>Of the " + up5.length + " that reached +5%, " + flink("rt", null, rt.length) + " ended with less than a third of it. " + flink("big", null, big.length) + " losses ran past &minus;5%.</p></div></div></div>" +
      (rt.length ? '<div class="mr-card"><h2>Gave it back: peak profit against where you sold</h2><div class="mr-note">Up 5% or more at some point, finished with under a third of it. Green: best price after you bought. Red: where you sold. Click one to open the trade.</div><p><b>' + rt.length + " trades</b> peaked at <b>" + fm(rt.reduce((a, r) => a + r.peakC, 0)) + "</b> of open profit between them and finished at <b class=\"" + cl(rt.reduce((a, r) => a + r.cad, 0)) + '">' + fs(rt.reduce((a, r) => a + r.cad, 0)) + "</b>.</p>" + rtRows + "</div>" : "") +
      '<div class="mr-card"><h2>What if &hellip;</h2><div class="mr-note">Two simple rules on the same trades, with hindsight. Gaps and slippage would eat some of it, and the two overlap, so do not add them.</div><div class="mr-wi"><div><div class="mr-note">Never lose more than 5%</div><div class="mr-mono up" style="font-size:22px;font-weight:600">' + fs(wiA) + '</div><div class="mr-note">' + flink("big", null, plural(big.length, "loss", "losses")) + ' went past 5%</div></div><div><div class="mr-note">Stop to breakeven once up 5%</div><div class="mr-mono up" style="font-size:22px;font-weight:600">' + fs(wiB) + '</div><div class="mr-note">' + flink("rt", null, plural(brk.length, "trade", "trades")) + ' were up 5%+ and closed at a loss</div></div></div></div>' +
      '<div class="mr-card"><h2>By setup</h2><div class="mr-note">The name opens that setup in Setups; the count lists those trades.</div><div class="mr-tw"><table><thead><tr><th>Setup</th><th class="r">Trades</th><th class="r">Wins</th><th class="r">Net</th></tr></thead><tbody>' + setupRows + "</tbody></table></div></div>" +
      '<div class="mr-card" id="mr-trades"><h2>Every trade</h2><div class="mr-flt" id="mr-flt"></div><div class="mr-tw"><table id="mr-all"></table></div></div>' +
      '<div class="mr-card"><h2>Where to improve, in order</h2>' + coach.map((c, i) => '<div class="mr-item"><div class="mr-num">' + (i + 1) + '</div><div><b>' + c[0] + '</b><div class="mr-note" style="font-size:13px;margin-top:2px">' + c[1] + "</div></div></div>").join("") + "</div>";
    drawTable();
  }

  function navChart(nav, peak, pkI, PH, ndxOK) {
    const W = 840, H = 250, L = 48, R = 16, Tp = 14, B = 26, xs = nav.map((_, i) => L + i * (W - L - R) / Math.max(1, nav.length - 1));
    const base = nav[0][2], nd = ndxOK ? nav.map(r => nav[0][1] * (r[2] || base) / base) : [];
    const all = nav.map(r => r[1]).concat(nd); const lo = Math.min.apply(null, all) * .995, hi = Math.max.apply(null, all) * 1.005;
    const y = v => Tp + (1 - (v - lo) / (hi - lo)) * (H - Tp - B);
    let s = '<svg viewBox="0 0 ' + W + " " + H + '" width="100%" role="img"><title>Account against Nasdaq 100</title>';
    if (PH.length) {
      const ix = d => { const i = nav.findIndex(r => r[0] >= d); return i < 0 ? nav.length - 1 : i; };
      const endA = Math.min(nav.length - 1, pkI + 8), cuts = [[0, pkI], [pkI, endA], [endA, nav.length - 1]], tone = ["rgba(34,197,94,.10)", "rgba(220,38,38,.12)", "rgba(202,138,4,.10)"];
      cuts.forEach((c, k) => { if (c[1] <= c[0]) return; s += '<g class="mr-band" data-flt="' + PH[k].k + '" opacity=".9"><rect x="' + xs[c[0]] + '" y="' + Tp + '" width="' + (xs[c[1]] - xs[c[0]]) + '" height="' + (H - Tp - B) + '" fill="' + tone[k] + '"><title>Click: trades opened in this stretch</title></rect><text x="' + (xs[c[0]] + 5) + '" y="' + (H - B - 8) + '" style="fill:var(--dim);font-size:11px">' + ((xs[c[1]] - xs[c[0]]) > 170 ? PH[k].name + " &middot; " + PH[k].n + " trades, " + PH[k].w + " won" : PH[k].n + " trades, " + PH[k].w + " won") + "</text></g>"; });
    }
    for (let k = 0; k <= 4; k++) { const v = lo + (hi - lo) * k / 4; s += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + y(v) + '" y2="' + y(v) + '" stroke="var(--line)"/><text x="4" y="' + (y(v) + 4) + '" style="fill:var(--dim);font-size:11px">' + (Math.round(v / 100) / 10) + "k</text>"; }
    if (ndxOK) s += '<polyline fill="none" stroke="var(--dim)" stroke-width="1.6" stroke-dasharray="5 4" points="' + nd.map((v, i) => xs[i] + "," + y(v)).join(" ") + '"/><polyline data-index="1" style="cursor:pointer" fill="none" stroke="transparent" stroke-width="12" points="' + nd.map((v, i) => xs[i] + "," + y(v)).join(" ") + '"><title>Open the Nasdaq 100 for these days</title></polyline>';
    s += '<polyline fill="none" stroke="var(--accent)" stroke-width="2.4" points="' + nav.map((r, i) => xs[i] + "," + y(r[1])).join(" ") + '"/>';
    s += '<circle cx="' + xs[pkI] + '" cy="' + y(peak[1]) + '" r="4.5" fill="var(--accent)"/><text x="' + (xs[pkI] + 8) + '" y="' + (y(peak[1]) - 6) + '" style="fill:var(--text);font-size:11px">' + peak[0].slice(5) + " " + SYM + fmtN(peak[1]) + "</text>";
    const li = nav.length - 1; s += '<circle cx="' + xs[li] + '" cy="' + y(nav[li][1]) + '" r="4.5" fill="var(--down)"/><text x="' + (xs[li] - 8) + '" y="' + (y(nav[li][1]) + 16) + '" text-anchor="end" style="fill:var(--text);font-size:11px">' + nav[li][0].slice(5) + " " + SYM + fmtN(nav[li][1]) + "</text>";
    [0, Math.floor(nav.length / 4), Math.floor(nav.length / 2), Math.floor(nav.length * 3 / 4), nav.length - 1].forEach(i => { s += '<text x="' + xs[i] + '" y="' + (H - 8) + '" text-anchor="middle" style="fill:var(--dim);font-size:11px">' + nav[i][0].slice(5) + "</text>"; });
    if (ndxOK) s += '<text data-index="1" style="cursor:pointer;fill:var(--accent);font-size:11px;font-weight:600" x="' + (W - R) + '" y="' + (y(nd[nd.length - 1]) - 6) + '" text-anchor="end">Nasdaq 100, rebased ↗</text>';
    return s + "</svg>";
  }

  // ---------- the trade list ----------
  const TESTS = {
    all: () => true, win: r => r.cad > 0, loss: r => r.cad <= 0, big: r => r.pct <= -5, rt: r => r.mfe >= 5 && r.pct < r.mfe / 3, nt: r => !r.setup, ns: r => !r.stop,
    h1: r => r.hold <= 1, h25: r => r.hold >= 2 && r.hold <= 5, hs: r => r.hold <= 5, h5: r => r.hold > 5, q3: r => r.mfe >= 3, q5: r => r.mfe >= 5
  };
  const NAMES = { all: "All trades", win: "Winners", loss: "Losers", big: "Lost more than 5%", rt: "Gave it back", nt: "No setup tag", ns: "No stop recorded", h1: "1 day or less", h25: "2 to 5 days", hs: "5 days or less", h5: "More than 5 days", q3: "Up 3%+ at some point", q5: "Up 5%+ at some point", ph1: "Opened before the high", ph2: "Opened right after the high", ph3: "Opened later" };
  function phaseTest(k) {
    const D = MR.data, nav = D.nav; if (nav.length < 2) return () => true;
    const peak = nav.reduce((a, b) => b[1] > a[1] ? b : a), pkI = nav.indexOf(peak), endA = nav[Math.min(nav.length - 1, pkI + 8)][0];
    return { ph1: r => r.opened <= peak[0], ph2: r => r.opened > peak[0] && r.opened <= endA, ph3: r => r.opened > endA }[k];
  }
  function currentTest() {
    const f = MR.flt;
    if (f.k === "setup") return r => (r.setup || "") === f.v;
    if (f.k.startsWith("ph")) return phaseTest(f.k);
    return TESTS[f.k] || TESTS.all;
  }
  function drawTable() {
    const D = MR.data; if (!D || !$("mr-all")) return;
    const f = MR.flt, keys = ["all", "win", "loss", "big", "rt", "nt", "h1"];
    const chips = keys.concat(keys.indexOf(f.k) < 0 && f.k !== "setup" ? [f.k] : []).map(k => '<button type="button" class="mr-chip' + (f.k === k ? " on" : "") + '" data-flt="' + k + '">' + (NAMES[k] || k) + "</button>").join("") +
      (f.k === "setup" ? '<button type="button" class="mr-chip on" data-flt="all">' + esc(f.v || "No tag") + " &times;</button>" : "");
    $("mr-flt").innerHTML = chips;
    const rows = D.trades.filter(currentTest()).sort((a, b) => (a[MR.sk] > b[MR.sk] ? 1 : a[MR.sk] < b[MR.sk] ? -1 : 0) * MR.sd);
    const C = [["t", "Ticker"], ["setup", "Setup"], ["opened", "Opened"], ["hold", "Held", 1], ["mfe", "Peak %", 1], ["pct", "Result %", 1], ["cad", "P&L", 1], ["gave", "Gave back", 1]];
    $("mr-all").innerHTML = "<thead><tr>" + C.map(c => '<th data-k="' + c[0] + '" class="' + (c[2] ? "r" : "") + '">' + c[1] + (c[0] === MR.sk ? (MR.sd > 0 ? " ▴" : " ▾") : "") + "</th>").join("") + "</tr></thead><tbody>" +
      rows.map(r => '<tr class="mr-row" data-open="' + r.id + '"><td>' + jlogo(r.t, r.logoid || ((ALL_TRADES.find(x => x.pageId === r.id) || {}).logoid), 18) + "<b>" + esc(r.t) + "</b></td><td>" + slink(r.setup) + '</td><td class="mr-mono">' + r.opened.slice(5) + '</td><td class="r mr-mono">' + r.hold + 'd</td><td class="r mr-mono">' + pc(r.mfe) + '</td><td class="r mr-mono ' + cl(r.pct) + '">' + pc(r.pct) + '</td><td class="r mr-mono ' + cl(r.cad) + '">' + fs(r.cad) + '</td><td class="r mr-mono">' + (r.gave > 50 ? fm(r.gave) : "&ndash;") + "</td></tr>").join("") + "</tbody>";
  }

  // ---------- clicks ----------
  function onClick(e) {
    const t = e.target;
    const m = t.closest("[data-m]");
    if (m) { const k = m.dataset.m; MR.months.has(k) ? (MR.months.size > 1 && MR.months.delete(k)) : MR.months.add(k); MR.flt = { k: "all", v: null }; drawReview(); return; }
    const s = t.closest("[data-setup]");
    if (s) { e.stopPropagation(); openSetup(s.dataset.setup); return; }
    const ix = t.closest("[data-index]");
    if (ix && MR.data && MR.data.nav.length > 1) {
      const nav = MR.data.nav; hub({ hubGo: "Indices", child: "Nasdaq 100", focus: [{ key: "nasdaq100", start: nav[0][0], end: nav[nav.length - 1][0], type: (nav[nav.length - 1][2] || 0) < (nav[0][2] || 0) ? "down" : "up" }] }); return;
    }
    const f = t.closest("[data-flt]");
    if (f) { MR.flt = { k: f.dataset.flt, v: f.dataset.v == null ? null : f.dataset.v }; if (f.dataset.flt === "nt") MR.flt = { k: "setup", v: "" }; drawTable(); const el = $("mr-trades"); if (el) el.scrollIntoView({ block: "start", behavior: "smooth" }); return; }
    const o = t.closest("[data-open]"); if (o) { openTrade(o.dataset.open); return; }
    const th = t.closest("#mr-all th"); if (th) { const k = th.dataset.k; if (MR.sk === k) MR.sd *= -1; else { MR.sk = k; MR.sd = ["t", "setup", "opened"].indexOf(k) >= 0 ? 1 : -1; } drawTable(); }
  }
})();
