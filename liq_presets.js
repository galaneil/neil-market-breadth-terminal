/* Saved liquidity presets (minimum price and minimum average dollar volume), shared by every page that filters
 * a list of stocks by liquidity: Breadth Internals, Movers, and anything added later.
 *
 * LiqPresets.mount(host, { scope, get, set })
 *   host   element the picker is drawn into
 *   scope  short name of the page; the last preset picked is remembered per page
 *   get()  returns the page's current { minPrice, minDV }
 *   set(p) applies { minPrice, minDV } to the page's inputs and re-runs it
 * Presets are stored in this browser (localStorage); the three starting ones can be edited or deleted.
 */
(function (w) {
  var KEY = "liqPresets.v1";
  var DEFAULTS = [
    { name: "Liquid (default)", minPrice: 5, minDV: 5 },
    { name: "Institutional", minPrice: 10, minDV: 25 },
    { name: "Strict", minPrice: 15, minDV: 50 },
  ];
  function read(k) { try { return JSON.parse(localStorage.getItem(k)); } catch (e) { return null; } }
  function write(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }
  var DEFAULTS_IN = [
    { name: "Liquid (default)", minPrice: 20, minDV: 10 },
    { name: "Institutional", minPrice: 100, minDV: 50 },
    { name: "Strict", minPrice: 200, minDV: 100 },
  ];
  // Each market keeps its own presets (a rupee limit is not a dollar limit).
  function list(market) {
    var a = read(KEY + "." + (market || "US"));
    var d = market === "IN" ? DEFAULTS_IN : DEFAULTS;
    return Array.isArray(a) && a.length ? a : d.map(function (p) { return Object.assign({}, p); });
  }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }

  function mount(host, opt) {
    var scope = opt.scope || "page", market = opt.market || "US", store = KEY + "." + market;
    var activeKey = "liqPresets.active." + scope;
    host.classList.add("liqp");
    function draw() {
      var all = list(market), cur = opt.get(), active = read(activeKey);
      var match = all.findIndex(function (p) { return p.minPrice === cur.minPrice && p.minDV === cur.minDV; });
      host.innerHTML =
        '<label class="liqp-l">Liquidity preset</label>' +
        '<select class="liqp-sel" aria-label="Liquidity preset">' +
        (match < 0 ? '<option value="-1" selected>Custom</option>' : "") +
        all.map(function (p, i) {
          return '<option value="' + i + '"' + (i === match ? " selected" : "") + ">" + esc(p.name) + " · " + (market === "IN" ? "₹" : "$") + p.minPrice + " / " + p.minDV + "M</option>";
        }).join("") + "</select>" +
        '<button type="button" class="liqp-b" data-a="save" title="Save the current price and volume limits as a preset">Save as…</button>' +
        '<button type="button" class="liqp-b" data-a="del" title="Delete the selected preset"' + (match < 0 ? " disabled" : "") + ">Delete</button>";
      if (active != null && match < 0) { /* the page was edited by hand: leave it as Custom */ }
    }
    host.addEventListener("change", function (e) {
      if (!e.target.classList.contains("liqp-sel")) return;
      var i = +e.target.value, p = list(market)[i];
      if (p) { write(activeKey, p.name); opt.set({ minPrice: p.minPrice, minDV: p.minDV }); draw(); }
    });
    host.addEventListener("click", function (e) {
      var b = e.target.closest("button[data-a]");
      if (!b) return;
      var all = list(market), cur = opt.get();
      if (b.dataset.a === "save") {
        var name = (w.prompt("Name this preset (price ≥ $" + cur.minPrice + ", $ volume ≥ " + cur.minDV + "M):", "") || "").trim();
        if (!name) return;
        var at = all.findIndex(function (p) { return p.name.toLowerCase() === name.toLowerCase(); });
        var p = { name: name, minPrice: cur.minPrice, minDV: cur.minDV };
        if (at >= 0) all[at] = p; else all.push(p);
        write(store, all); write(activeKey, name); draw();
      } else if (b.dataset.a === "del") {
        var sel = host.querySelector(".liqp-sel"), i = +sel.value;
        if (i >= 0 && all.length > 1 && w.confirm("Delete the preset “" + all[i].name + "”?")) { all.splice(i, 1); write(store, all); draw(); }
      }
    });
    // Re-run the picker when the page's own inputs change, so "Custom" appears as soon as a limit is edited.
    host.refresh = draw;
    draw();
    return { refresh: draw, restore: function () {
      var name = read(activeKey), p = list(market).find(function (x) { return x.name === name; });
      if (p) { opt.set({ minPrice: p.minPrice, minDV: p.minDV }); draw(); }
    } };
  }

  w.LiqPresets = { mount: mount, list: list };
  if (!document.getElementById("liqp-css")) {
    var st = document.createElement("style");
    st.id = "liqp-css";
    st.textContent = ".liqp{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.liqp-l{font-size:11px;text-transform:uppercase;letter-spacing:.06em;opacity:.7}" +
      ".liqp-sel{padding:6px 8px;border-radius:7px;border:1px solid var(--border,#2a3441);background:var(--panel,transparent);color:inherit;font:inherit;font-size:13px}" +
      ".liqp-b{padding:6px 10px;border-radius:7px;border:1px solid var(--border,#2a3441);background:none;color:inherit;font:inherit;font-size:12.5px;cursor:pointer}.liqp-b:hover:not(:disabled){border-color:var(--accent,#2563eb)}.liqp-b:disabled{opacity:.4;cursor:default}";
    document.head.appendChild(st);
  }
})(window);
