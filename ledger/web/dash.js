// The dashboard: overview (with net worth), spending, income and the budget model.
// Mounted by the portal (app.js) or by the standalone page `ledger dashboard` writes.
// Data is summarized once per load (derive); each view is drawn when first shown and kept until
// something it shows changes (data, theme, width, the durables setting or the budget).
const Dash = (() => {
// ---------------------------------------------------------------- palette (validated; see dataviz palette.md)
const LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"];
const DARK  = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"];
const SEQ_L = ["#cde2fb", "#86b6ef", "#3987e5", "#1c5cab"], SEQ_D = ["#184f95", "#2a78d6", "#5598e7", "#9ec5f4"];
let SER = LIGHT, SEQ = SEQ_L, POS = "#2a78d6", NEGC = "#e34948";
function palette() {
  const m = getComputedStyle(document.body).color.match(/\d+/g);
  const dark = m ? (0.299 * m[0] + 0.587 * m[1] + 0.114 * m[2]) > 140 : false;
  [SER, SEQ, POS, NEGC] = dark ? [DARK, SEQ_D, "#3987e5", "#e66767"] : [LIGHT, SEQ_L, "#2a78d6", "#e34948"];
}

const VIEWS = [["overview", "Overview"], ["spending", "Spending"], ["income", "Income"], ["budget", "Budget"]];
let D, X, P, BASE, OPTS = {}, ROOT, VIEW = null, store;
const dirty = new Set();
let SMOOTH = (() => { try { return localStorage.getItem("ft-smooth") === "1"; } catch { return false; } })();

// ---------------------------------------------------------------- derived data: one pass each
function derive() {
  const GROUP = D.groups, ASOF = D.asof;
  const CONS = new Set(Object.keys(GROUP).filter(c => GROUP[c] === "essential" || GROUP[c] === "discretionary"));
  const L12 = addDays(ASOF, -365), P12 = addDays(ASOF, -730);
  const spend = (SMOOTH && D.spendSmooth ? D.spendSmooth : D.spend).map(([d, cat, merchant, amt, acct, id, life, tags, what]) =>   // cat is the kind; w what it was (a tag), or the kind
    ({d, m: d.slice(0, 7), cat, merchant, amt, acct, id, life, tags: tags || [], what, w: what || cat.toLowerCase(), g: GROUP[cat]}));
  const income = D.income.map(([d, cat, merchant, amt, cls, id, tags, what]) => ({d, m: d.slice(0, 7), cat, merchant, amt, cls, id, tags: tags || [], what, w: what || cat.toLowerCase()}));
  const cons = spend.filter(t => CONS.has(t.cat));
  const monthCat = new Map(), monthWhat = new Map(), dayCons = new Map(), monthInc = {}, monthTax = {}, monthCons = {};
  const catL12 = {}, catP12 = {}, whatL12 = {}, whatP12 = {}, whatGroup = {}, clsL12 = {}, otherL12 = {}, incCatL12 = {};
  let first = null;
  for (const t of cons) {
    let mc = monthCat.get(t.m); if (!mc) monthCat.set(t.m, mc = {});
    mc[t.cat] = (mc[t.cat] || 0) + t.amt;
    let mw = monthWhat.get(t.m); if (!mw) monthWhat.set(t.m, mw = {});
    mw[t.w] = (mw[t.w] || 0) + t.amt;
    whatGroup[t.w] ||= t.g;
    monthCons[t.m] = (monthCons[t.m] || 0) + t.amt;
    let dd = dayCons.get(t.d); if (!dd) dayCons.set(t.d, dd = []);
    dd.push(t);
    if (t.d > L12) { catL12[t.cat] = (catL12[t.cat] || 0) + t.amt; whatL12[t.w] = (whatL12[t.w] || 0) + t.amt; }
    else if (t.d > P12) { catP12[t.cat] = (catP12[t.cat] || 0) + t.amt; whatP12[t.w] = (whatP12[t.w] || 0) + t.amt; }
    if (!first || t.d < first) first = t.d;
  }
  for (const t of spend) {
    if (t.cat === "Taxes") monthTax[t.m] = (monthTax[t.m] || 0) + t.amt;
    if (t.d > L12 && !CONS.has(t.cat)) otherL12[t.cat] = (otherL12[t.cat] || 0) + t.amt;
  }
  for (const t of income) {
    monthInc[t.m] = (monthInc[t.m] || 0) + t.amt;
    if (t.d > L12) { clsL12[t.cls] = (clsL12[t.cls] || 0) + t.amt; (incCatL12[t.cls] ||= {})[t.w] = ((incCatL12[t.cls] ||= {})[t.w] || 0) + t.amt; }
  }
  const consL12 = sum(Object.values(catL12)), consP12 = sum(Object.values(catP12));
  const essL12 = sum(Object.entries(catL12).filter(([c]) => GROUP[c] === "essential").map(([, v]) => v));
  const discL12 = consL12 - essL12;
  const incL12 = sum(Object.values(clsL12)), taxL12 = otherL12.Taxes || 0;
  // ---- net worth: counted rows are what you own less what you owe; "owed to you" may be gifts, so it's shown, not counted
  const W = D.worth, acc = W.accounts || [];
  const counted = r => r.value != null && r.counted !== false;
  const gtot = g => sum(acc.filter(r => r.group === g && counted(r)).map(r => r.value));
  const worth = sum(acc.filter(counted).map(r => r.value));
  const durables = D.spend.filter(([d, cat, , amt, , , life]) => life > 1 && amt > 0 && CONS.has(cat)).map(([d, cat, merchant, amt, , , life]) => {
    const used = (day(ASOF) - day(d)) / 864e5 / 30.44;
    return {d, cat, merchant, amt, life, left: amt * Math.max(0, 1 - used / life)};
  }).filter(x => x.left > 0.5);
  const holdV = sum((W.holdings || []).map(h => h.value));
  return {GROUP, CONS, ASOF, AMON: ASOF.slice(0, 7), L12, P12, spend, income, cons, monthCat, monthWhat, dayCons, monthInc, monthTax, monthCons,
          catL12, catP12, whatL12, whatP12, whatGroup, clsL12, otherL12, incCatL12, consL12, consP12, essL12, discL12, incL12, taxL12, first: first || ASOF,
          earnedL12: clsL12.earned || 0, W, acc, counted, gtot, worth, durables, holdV, cashV: gtot("Cash"), invV: gtot("Investments")};
}

// ---------------------------------------------------------------- budget parameters
const AUTO = ["wealth", "stockShare"];
const KIND_P = {earned: 100, steady: 90, windfall: 40};
function base() {
  return {
    wealth: Math.round(X.worth / 100) * 100, age: 30, horizonAge: 90, retireAge: 65,
    gamma: 3, rho: 2, resolveYears: 2, reserveMonths: 6,
    essentials: Math.round(X.essL12 / 12 / 10) * 10, covers: "discretionary", capWeeks: 6,
    start: X.ASOF.slice(0, 4) + "-01-01",
    inflation: 2.5, cashRate: 3.5, stockReturn: 8, stockVol: 16,
    stockShare: X.worth > 0 ? Math.round(X.invV / X.worth * 100) : 0,
    scenarios: [
      {name: "Example: steady job", p: 50, y1: Math.round(X.earnedL12 / 1000) * 1000, y2: 60000, growth: 2},
      {name: "Example: high upside", p: 25, y1: Math.round(X.earnedL12 / 1000) * 1000, y2: 120000, growth: 2},
      {name: "Example: lean years", p: 25, y1: Math.round(X.earnedL12 / 1000) * 1000, y2: 30000, growth: 0},
    ],
    streams: [
      {name: "Steady income besides work", kind: "steady", p: 90, amount: Math.round((X.clsL12.steady || 0) / 100) * 100, years: 5},
      {name: "Gifts and windfalls", kind: "windfall", p: 40, amount: Math.round((X.clsL12.windfall || 0) / 100) * 100, years: 5},
    ],
  };
}
// Saved parameters come from the portal (data/budget.json) or, in a standalone file, the browser.
// Fields added since they were saved take their defaults; fields in P.auto follow the ledger.
function makeStore() {
  return OPTS.portal ? {
      get: () => D.budget, set(v) { clearTimeout(this.t); this.t = setTimeout(() => fetch("/api/budget", {method: "POST",
        headers: {"Content-Type": "application/json"}, body: JSON.stringify(v)}), 600); }}
    : {get() { try { return JSON.parse(localStorage.getItem("ft-budget")) || D.budget; } catch { return D.budget; } },
       set(v) { try { v ? localStorage.setItem("ft-budget", JSON.stringify(v)) : localStorage.removeItem("ft-budget"); } catch {} }};
}

// The allowance: accrues daily at the plan's rate (less essentials, if they're paid separately),
// in today's dollars, so earlier days accrue a little less by inflation. It is drawn down by what
// is actually spent and never rises above the cap; the overflow stays saved.
const _byDay = {};
function bucket(P, c) {
  const rateYear = P.covers === "all" ? c : c - 12 * P.essentials;
  const rate = rateYear / 365, cap = Math.max(0, rate * 7 * P.capWeeks), inf = P.inflation / 100;
  const k = P.covers + "|" + P.start;
  if (!_byDay[k]) {
    const counts = t => P.covers === "all" ? X.CONS.has(t.cat) : t.g === "discretionary", m = {};
    for (const t of X.spend) if (counts(t) && t.d >= P.start) m[t.d] = (m[t.d] || 0) + t.amt;
    _byDay[k] = m;
  }
  const byDay = _byDay[k];
  let b = 0, swept = 0; const path = [];
  const end = day(X.ASOF).getTime();
  for (let ms = day(P.start).getTime(); ms <= end; ms += 864e5) {
    const d = iso(new Date(ms)), kk = Math.pow(1 + inf, (ms - end) / 864e5 / 365);
    b += rate * kk - (byDay[d] || 0);
    if (b > cap * kk) { swept += b - cap * kk; b = cap * kk; }
    path.push([d, b, byDay[d] || 0]);
  }
  return {rate, cap, b, swept, path, spent: sum(Object.values(byDay))};
}

// ---------------------------------------------------------------- shell
function renderShell() {
  const chg = X.consP12 ? (X.consL12 / X.consP12 - 1) * 100 : 0;
  const saved = X.incL12 - X.consL12 - X.taxL12;
  ROOT.innerHTML = `
  ${OPTS.portal ? "" : `<div class="tabs" id="tabs">${VIEWS.map(([k, l]) => `<button data-v="${k}">${l}</button>`).join("")}</div>`}
  <div class="view" data-view="overview" hidden>
    <div class="head"><h1>Overview</h1><div class="ctl"><div class="seg" id="smooth" title="Furniture, tools and electronics serve you for years">
      <button data-v="0" class="${SMOOTH ? "" : "on"}">Durables when bought</button><button data-v="1" class="${SMOOTH ? "on" : ""}">Spread over their life</button></div>
      <span class="note">Through ${X.ASOF}</span></div></div>
    <div class="kpis">
      ${tile("Available to spend", `<span id="ovAllow">—</span>`, `<a class="more" href="${OPTS.portal ? "/budget" : "#budget"}" data-nav>from your budget →</a>`)}
      ${tile("Spending per month", compact(X.consL12 / 12), `last 12 months, ${chg >= 0 ? "+" : "−"}${Math.abs(chg).toFixed(0)}% on the year before`)}
      ${tile("Income, last 12 months", compact(X.incL12), `${compact(saved)} left after spending and taxes`)}
      ${tile("Net worth", compact(X.worth), `<a class="more" href="#networth" data-scroll="networth">breakdown ↓</a>`)}
    </div>
    <div class="card"><div class="ctl"><h2>Monthly spending</h2>
      <div class="seg" id="mfilter"><button data-v="all" class="${mview === "all" ? "on" : ""}">All</button><button data-v="essential" class="${mview === "essential" ? "on" : ""}">Essential</button><button data-v="discretionary" class="${mview === "discretionary" ? "on" : ""}">Discretionary</button></div></div>
      <div id="mlegend"></div><div class="chart" id="monthly"></div>
      <p class="note">Shaded months are incomplete: not every account's history reaches back before ${mlabel(D.fullFrom)}.</p></div>
    <div class="card"><h2>What it was</h2><p class="sub">Last 12 months against the 12 before. The dot says whether it counted as essential (blue) or discretionary (orange).</p><div id="cats"></div></div>
    <div class="card nw" id="networth"><div class="nw-head"><div><h2>Net worth</h2><div class="v" id="nwTotal"></div></div>
      <span class="note" id="nwAsof"></span></div>
      <p class="sub" id="nwSub"></p><div id="nwLegend"></div><div class="chart" id="nwChart"></div>
      <div id="nwTable" style="margin-top:14px"></div></div>
  </div>
  <div class="view" data-view="spending" hidden>
    <div class="head"><h1>Spending</h1><span class="note">Last 12 months</span></div>
    <div class="card"><h2>When you spend</h2><p class="sub">Each square is a day. Hover for what you bought.</p><div class="chart" id="cal"></div></div>
    <div class="cols">
      <div class="card"><h2>Where it goes</h2><p class="sub">Top merchants.</p><div class="chart" id="merch"></div></div>
      <div class="card"><h2>Day of the week</h2><p class="sub">Average per weekday.</p><div class="chart" id="dow"></div></div>
    </div>
    <div class="cols">
      <div class="card"><h2>Big purchases</h2><p class="sub" id="bigsub"></p><div id="big"></div></div>
      <div class="card"><h2>Subscriptions</h2><p class="sub" id="subsub"></p><div id="subs"></div></div>
    </div>
    <div class="card"><h2>Tags</h2><p class="sub">Money out by tag, whatever its category: a payment can carry several, so these overlap and don't add up to your spending.${OPTS.portal ? " Click one to see its transactions." : ""}</p><div id="tagt"></div></div>
    <div class="card"><h2>Fronted for others</h2><p class="sub">What you paid for other people, and what they've paid back. Only your own share of a purchase counts as your spending.</p><div id="ppl"></div></div>
    <div class="card"><h2>Not counted as spending</h2><p class="sub">Money that left your accounts without being consumption.</p><div id="othr"></div></div>
  </div>
  <div class="view" data-view="income" hidden>
    <div class="head"><h1>Income</h1><span class="note">Last 12 months</span></div>
    <div class="cols">
      <div class="card"><h2>By how dependable it is</h2><p class="sub">Only earned and steady income is planned on. Gifts count once they arrive.</p><div id="inc"></div></div>
      <div class="card"><h2>Money left over each month</h2><p class="sub" id="netsub"></p><div class="chart" id="net"></div></div>
    </div>
  </div>
  <div class="view" data-view="budget" id="budget" hidden></div>`;
  $("#mfilter").addEventListener("click", e => { const b = e.target.closest("button"); if (!b) return; mview = b.dataset.v;
    $("#mfilter").querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b)); renderMonthly(); });
  $("#tabs")?.addEventListener("click", e => { const b = e.target.closest("button"); if (b) location.hash = b.dataset.v; });
  $("#smooth").addEventListener("click", e => { const b = e.target.closest("button"); if (!b || (b.dataset.v === "1") === SMOOTH) return;
    SMOOTH = b.dataset.v === "1";
    try { localStorage.setItem("ft-smooth", SMOOTH ? "1" : "0"); } catch {}
    X = derive(); for (const k in _byDay) delete _byDay[k];
    rebuild(); });
  ROOT.addEventListener("click", e => { const a = e.target.closest("[data-scroll]"); if (!a) return;
    e.preventDefault(); document.getElementById(a.dataset.scroll)?.scrollIntoView({behavior: "smooth", block: "start"}); });
  renderBudget();
}
function rebuild() {   // data or the durables setting changed: everything is redrawn as it's shown
  renderShell();
  VIEWS.forEach(([k]) => dirty.add(k));
  const v = VIEW; VIEW = null; show(v || "overview", true);
}

function show(v, keepScroll) {
  if (!VIEWS.some(([k]) => k === v)) v = "overview";
  const changed = v !== VIEW;
  VIEW = v;
  ROOT.querySelectorAll(":scope > .view").forEach(el => el.hidden = el.dataset.view !== v);
  ROOT.querySelectorAll("#tabs button").forEach(b => b.classList.toggle("on", b.dataset.v === v));
  if (dirty.has(v)) { dirty.delete(v); draw(v); }
  if (changed && !keepScroll) scrollTo(0, 0);
}
// charts measure their container, so a view is drawn when it's shown
function draw(v) {
  updateTiles();
  if (v === "overview") { renderMonthly(); renderCats(); renderWorth(); }
  if (v === "spending") { renderCal(); renderMerch(); renderSubs(); renderTags(); renderBig(); renderPeople(); renderOther(); }
  if (v === "income") { renderIncome(); renderNet(); }
  if (v === "budget") budgetCharts();
}
function redrawAll() {   // width or theme changed
  VIEWS.forEach(([k]) => dirty.add(k));
  if (VIEW && ROOT.offsetParent !== null) { dirty.delete(VIEW); draw(VIEW); }
}

// ---------------------------------------------------------------- overview
let mview = "all";
function renderMonthly() {
  const el = $("#monthly"), w = width(el), h = 230, m = {l: 44, r: 8, t: 14, b: 22};
  const ms = months(X.first.slice(0, 7), X.AMON);
  let series;
  const catsOf = g => [...X.CONS].filter(c => X.GROUP[c] === g);
  if (mview === "all") series = [["Essential", SER[0], catsOf("essential")], ["Discretionary", SER[1], catsOf("discretionary")]];
  else {
    const cats = Object.keys(X.whatGroup).filter(w => X.whatGroup[w] === mview).map(w => [w, X.whatL12[w] || 0]).sort((a, b) => b[1] - a[1]).map(x => x[0]);
    const top = cats.slice(0, cats.length > 8 ? 7 : 8), rest = cats.slice(top.length);
    series = top.map((c, i) => [c, SER[i], [c]]);
    if (rest.length) series.push(["Other", "var(--other)", rest]);
  }
  const grid = ms.map(mo => { const mc = (mview === "all" ? X.monthCat : X.monthWhat).get(mo) || {}; return series.map(([, , cs]) => Math.max(0, sum(cs.map(c => mc[c] || 0)))); });
  const ymax = niceMax(Math.max(...grid.map(sum)));
  const iw = w - m.l - m.r, ih = h - m.t - m.b, band = iw / ms.length, bw = Math.min(24, band * 0.72);
  const y = v => m.t + ih - v / ymax * ih;
  const s = [`<svg width="${w}" height="${h}" role="img" aria-label="Monthly spending by category">`];
  const cov = ms.findIndex(mo => mo >= D.fullFrom);
  if (cov > 0) s.push(`<rect x="${m.l}" y="${m.t}" width="${cov * band}" height="${ih}" fill="var(--mut)" opacity=".07"/><text x="${m.l + 4}" y="${m.t + 11}">incomplete</text>`);
  for (let k = 0; k <= 4; k++) { const v = ymax * k / 4; s.push(`<line x1="${m.l}" x2="${w - m.r}" y1="${y(v)}" y2="${y(v)}" stroke="var(--grid)"/><text x="${m.l - 6}" y="${y(v) + 4}" text-anchor="end">${compact(v)}</text>`); }
  ms.forEach((mo, i) => {
    const x = m.l + i * band + (band - bw) / 2; let acc = 0; const vals = grid[i];
    let topIdx = -1; vals.forEach((v, j) => { if (v > 0) topIdx = j; });
    vals.forEach((v, j) => {
      if (v <= 0) return;
      const y0 = y(acc), y1 = y(acc + v), hh = y0 - y1 - (acc > 0 ? 2 : 0);
      if (hh > 0) s.push(j === topIdx ? `<path d="${colPath(x, y1, bw, hh)}" fill="${series[j][1]}"/>` : `<rect x="${x}" y="${y1}" width="${bw}" height="${hh}" fill="${series[j][1]}"/>`);
      acc += v;
    });
    if (xlabel(ms, i)) s.push(`<text x="${x + bw / 2}" y="${h - 6}" text-anchor="middle">${xlabel(ms, i)}</text>`);
    const tip = `<b>${mlabel(mo)}${mo === X.AMON ? " (partial)" : ""}${mo < D.fullFrom ? ", incomplete" : ""}</b>` + tipRows([...series.map(([n], j) => [n, usd(vals[j])]).filter((_, j) => vals[j] > 0).reverse(), ["Total", usd(sum(vals))]]);
    s.push(`<rect x="${m.l + i * band}" y="${m.t}" width="${band}" height="${ih}" fill="transparent" data-tip="${esc(tip)}"/>`);
  });
  s.push(`<line x1="${m.l}" x2="${w - m.r}" y1="${y(0)}" y2="${y(0)}" stroke="var(--base)"/></svg>`);
  el.innerHTML = s.join("");
  $("#mlegend").innerHTML = legend(series.map(([n, c]) => [n, c]));
}

function spark(vals, color) {
  const w = 90, h = 22, mx = Math.max(...vals, 1);
  const pts = vals.map((v, i) => `${(i / (vals.length - 1) * (w - 6) + 3).toFixed(1)},${(h - 3 - v / mx * (h - 6)).toFixed(1)}`);
  const last = pts[pts.length - 1].split(",");
  return `<svg width="${w}" height="${h}"><polyline points="${pts.join(" ")}" fill="none" stroke="var(--mut)" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" opacity=".7"/><circle cx="${last[0]}" cy="${last[1]}" r="3" fill="${color}"/></svg>`;
}
function renderCats() {
  const ms = months(addDays(X.ASOF, -330).slice(0, 7), X.AMON).slice(-12);
  const rows = Object.keys(X.whatGroup).map(c => ({c, g: X.whatGroup[c], a: X.whatL12[c] || 0, b: X.whatP12[c] || 0,
    ms: ms.map(mo => Math.max(0, (X.monthWhat.get(mo) || {})[c] || 0))})).filter(r => Math.abs(r.a) >= 1).sort((x, y) => y.a - x.a);
  $("#cats").innerHTML = `<table><thead><tr><th>What it was</th><th class="n">Last 12 mo</th><th class="n">Per month</th><th class="n">Share</th><th class="n">vs year before</th><th>Trend</th></tr></thead><tbody>
  ${rows.map(r => { const d = r.a - r.b, col = r.g === "essential" ? SER[0] : SER[1]; return `<tr><td><span class="sw" style="--c:${col}" title="${r.g}"></span>${esc(r.c)}</td><td class="n">${usd(r.a)}</td><td class="n">${usd(r.a / 12)}</td><td class="n">${X.consL12 ? Math.round(r.a / X.consL12 * 100) : 0}%</td>
    <td class="n">${r.b ? (d >= 0 ? "+" : "−") + usd(Math.abs(d)) : "new"}</td><td>${spark(r.ms, col)}</td></tr>`; }).join("")}
  </tbody></table>`;
}

// ---------- net worth: history by kind, then every account
const NW_GROUPS = ["Cash", "Investments", "Crypto", "You owe", "Owed to you"];
function renderWorth() {
  const H = X.W.history || {}, days = H.days || [];
  $("#nwTotal").textContent = usd(X.worth);
  $("#nwAsof").textContent = `Balances as of ${X.W.asof}`;
  const xmr = X.acc.find(r => r.group === "Crypto");
  $("#nwSub").textContent = "Each week's balances from the ledger, at the last price seen by then."
    + (H.full && days.length && H.full > days[0] ? ` Before ${H.full} some accounts' history isn't in yet (shaded).` : "")
    + (H.unpriced ? ` Coins before ${addDays(H.unpriced, 7)} have no price yet and are left out.` : "");
  const keys = ["Cash", "Investments", "Crypto"].filter(g => (H.series?.[g] || []).some(v => Math.abs(v) > 0.5));
  const cols = {Cash: SER[0], Investments: SER[2], Crypto: SER[3]};
  $("#nwLegend").innerHTML = legend([...keys.map(k => [k, cols[k]]), ["Total", "var(--fg)"]]);
  const el = $("#nwChart");
  if (days.length < 2) { el.innerHTML = `<p class="note">No history yet.</p>`; }
  else {
    const w = width(el), h = 220, m = {l: 52, r: 10, t: 12, b: 22}, iw = w - m.l - m.r, ih = h - m.t - m.b;
    const tot = days.map((_, i) => sum(keys.map(k => H.series[k][i])));
    const stack = days.map((_, i) => { let a = 0; return keys.map(k => [a, a += Math.max(0, H.series[k][i])]); });
    const hi = niceMax(Math.max(...stack.map(s => s.length ? s[s.length - 1][1] : 0), ...tot)), lo = Math.min(0, ...tot);
    const x = i => m.l + i / (days.length - 1) * iw, y = v => m.t + ih - (v - lo) / (hi - lo) * ih;
    const s = [`<svg width="${w}" height="${h}" role="img" aria-label="Net worth over time">`];
    const fi = days.findIndex(d => d >= H.full);
    if (fi > 0) s.push(`<rect x="${m.l}" y="${m.t}" width="${x(fi) - m.l}" height="${ih}" fill="var(--mut)" opacity=".07"/>`);
    for (let k = 0; k <= 4; k++) { const v = lo + (hi - lo) * k / 4; s.push(`<line x1="${m.l}" x2="${w - m.r}" y1="${y(v)}" y2="${y(v)}" stroke="var(--grid)"/><text x="${m.l - 6}" y="${y(v) + 4}" text-anchor="end">${compact(v)}</text>`); }
    keys.forEach((k, j) => {
      const up = days.map((_, i) => `${x(i).toFixed(1)},${y(stack[i][j][1]).toFixed(1)}`), dn = days.map((_, i) => `${x(i).toFixed(1)},${y(stack[i][j][0]).toFixed(1)}`).reverse();
      s.push(`<polygon points="${up.join(" ")} ${dn.join(" ")}" fill="${cols[k]}" opacity=".75"/>`);
    });
    s.push(`<polyline points="${days.map((_, i) => `${x(i).toFixed(1)},${y(tot[i]).toFixed(1)}`).join(" ")}" fill="none" stroke="var(--fg)" stroke-width="1.5" stroke-linejoin="round"/>`);
    let lastY = "";
    days.forEach((d, i) => { if (d.slice(0, 4) !== lastY) { if (lastY) s.push(`<text x="${x(i)}" y="${h - 6}" text-anchor="middle">${d.slice(0, 4)}</text>`); lastY = d.slice(0, 4); } });
    const bw = iw / days.length;
    days.forEach((d, i) => s.push(`<rect x="${x(i) - bw / 2}" y="${m.t}" width="${bw}" height="${ih}" fill="transparent" data-tip="${esc(`<b>Week of ${d}</b>` + tipRows([...keys.map(k => [k, usd(H.series[k][i])]), ["Total", usd(tot[i])]]))}"/>`));
    s.push(`<line x1="${m.l}" x2="${w - m.r}" y1="${y(0)}" y2="${y(0)}" stroke="var(--base)"/></svg>`);
    el.innerHTML = s.join("");
  }
  // the table
  const out = [];
  const detail = r => {
    const bits = [];
    if (r.group === "Investments" && r.quantity != null) {
      bits.push(`${+r.quantity.toFixed(3)} sh`);
      if (r.cost) { const g = r.value - r.cost; bits.push(`cost ${usd(r.cost)}, <span class="${g >= 0 ? "gain" : "neg"}">${g >= 0 ? "+" : "−"}${usd(Math.abs(g))} (${(g / r.cost * 100).toFixed(1)}%)</span>`); }
    } else if (r.group === "Crypto") {
      bits.push(`${+r.quantity.toFixed(4)} ${esc(r.name.split(" ")[0])}` + (r.price ? ` at ${usd2(r.price)}` : ", no price yet"));
    }
    if (r.asof && r.asof < X.W.asof) bits.push(`as of ${r.asof}`);
    if (r.note) bits.push(esc(r.note));
    return bits.join(" · ");
  };
  for (const g of NW_GROUPS) {
    const rs = X.acc.filter(r => r.group === g);
    if (!rs.length) continue;
    const nc = rs.every(r => !X.counted(r));
    out.push(`<tr class="g"><td>${g}${g === "Owed to you" && OPTS.portal ? ` <a class="more" href="/people" data-nav>people →</a>` : ""}</td><td class="d">${nc ? "not counted in the total" : ""}</td><td class="n">${nc ? "" : usd2(X.gtot(g))}</td></tr>`);
    for (const r of rs.sort((a, b) => Math.abs(b.value || 0) - Math.abs(a.value || 0)))
      out.push(`<tr class="i ${X.counted(r) ? "" : "nc"}"><td>${esc(r.name)}</td><td class="d">${detail(r)}</td><td class="n">${r.value == null ? "—" : usd2(r.value)}</td></tr>`);
  }
  if (X.durables.length) {
    const left = sum(X.durables.map(x => x.left));
    out.push(`<tr class="g"><td>Durable goods</td><td class="d">not counted in the total</td><td class="n"></td></tr>
      <tr class="i nc" data-tip="${esc("<b>Value left, straight-line over each one's life</b>" + tipRows(X.durables.sort((a, b) => b.left - a.left).slice(0, 10).map(x => [`${x.d} ${esc(x.merchant)}`, usd(x.left)])))}">
      <td>${X.durables.length} purchases still in use</td><td class="d">what's left of their cost, spread over their life</td><td class="n">${usd2(left)}</td></tr>`);
  }
  out.push(`<tr class="t"><td>Net worth</td><td class="d">${X.acc.some(r => r.group === "Crypto" && r.note) ? "coins as received; see the note" : ""}</td><td class="n">${usd2(X.worth)}</td></tr>`);
  $("#nwTable").innerHTML = `<table class="nw"><tbody>${out.join("")}</tbody></table>`;
}

// ---------------------------------------------------------------- spending
function renderTags() {
  const ms = months(addDays(X.ASOF, -330).slice(0, 7), X.AMON).slice(-12), by = {};
  for (const t of X.spend) for (const g of t.tags) {
    const o = (by[g] ||= {g, l12: 0, all: 0, n: 0, ms: {}, merch: {}});
    o.all += t.amt; o.n++;
    if (t.d > X.L12) { o.l12 += t.amt; o.merch[t.merchant] = (o.merch[t.merchant] || 0) + t.amt; }
    o.ms[t.m] = (o.ms[t.m] || 0) + t.amt;
  }
  const rows = Object.values(by).sort((a, b) => b.l12 - a.l12 || b.all - a.all);
  const link = g => OPTS.portal ? `<a class="tag" href="/txns#q=${encodeURIComponent("#" + g)}" data-nav>#${esc(g)}</a>` : `<span class="tag">#${esc(g)}</span>`;
  $("#tagt").innerHTML = rows.length ? `<table><thead><tr><th>Tag</th><th class="n">Last 12 mo</th><th class="n">Per month</th><th class="n">All time</th><th class="n">Payments</th><th>Trend</th></tr></thead><tbody>
  ${rows.map(r => `<tr data-tip="${esc(`<b>#${esc(r.g)}, last 12 months</b>` + tipRows(Object.entries(r.merch).sort((a, b) => b[1] - a[1]).slice(0, 8).map(([m, v]) => [esc(m), usd(v)])))}">
    <td>${link(r.g)}</td><td class="n">${usd(r.l12)}</td><td class="n">${usd(r.l12 / 12)}</td><td class="n">${usd(r.all)}</td><td class="n">${r.n}</td><td>${spark(ms.map(mo => Math.max(0, r.ms[mo] || 0)), SER[2])}</td></tr>`).join("")}
  </tbody></table>` : `<p class="note">No tags yet.</p>`;
}
function renderSubs() {
  const R = D.recurring, act = R.filter(r => r.active);
  $("#subsub").textContent = `${act.length} active, ${usd(sum(act.map(r => r.monthly)))} a month. Found by a steady amount on a steady schedule.`;
  const every = d => d <= 40 ? "monthly" : d <= 100 ? "quarterly" : "yearly";
  $("#subs").innerHTML = `<table><thead><tr><th>Merchant</th><th>Billed</th><th class="n">Per month</th><th class="n">Last charge</th></tr></thead><tbody>
  ${R.map(r => `<tr style="${r.active ? "" : "color:var(--mut)"}" data-tip="${esc(`<b>${esc(r.merchant)}</b>` + tipRows([["Category", esc(r.category)], ["Typical charge", usd2(r.typical)], ["Last 12 months", usd(r.last12)], ["Since", r.first]]))}">
    <td class="${r.active ? "ok" : "off"}">${esc(r.merchant)}</td><td>${r.active ? every(r.every) : "stopped"}</td><td class="n">${usd2(r.monthly)}</td><td class="n">${r.last}</td></tr>`).join("")}
  </tbody></table>`;
}
function renderCal() {
  const el = $("#cal"), w = width(el);
  const byDay = {}; for (const [d, xs] of X.dayCons) if (d > X.L12) byDay[d] = sum(xs.map(t => t.amt));
  const end = day(X.ASOF), start = new Date(end.getTime() - (52 * 7 + end.getUTCDay()) * 864e5);
  const weeks = 53, cell = Math.min(14, Math.floor((w - 30) / weeks)) - 2, step = cell + 2;
  const pos = Object.values(byDay).filter(v => v > 0).sort((a, b) => a - b);
  const q = k => pos[Math.floor(k * (pos.length - 1))] || 0, cuts = [q(.25), q(.5), q(.75)];
  const fill = v => v <= 0 ? "var(--grid)" : SEQ[v <= cuts[0] ? 0 : v <= cuts[1] ? 1 : v <= cuts[2] ? 2 : 3];
  const h = 7 * step + 34;
  const s = [`<svg width="${w}" height="${h}" role="img" aria-label="Daily spending calendar">`]; let lastLabel = -99;
  ["Mon", "Wed", "Fri"].forEach((n, i) => s.push(`<text x="0" y="${16 + (2 * i + 1) * step + cell - 2}">${n}</text>`));
  for (let i = 0; i <= weeks * 7; i++) {
    const d = new Date(start.getTime() + i * 864e5); if (d > end) break;
    const k = iso(d), wk = Math.floor(i / 7), wd = d.getUTCDay(), v = byDay[k] || 0, x = 28 + wk * step, yy = 16 + wd * step;
    if (d.getUTCDate() === 1 && x - lastLabel >= 26) { s.push(`<text x="${x}" y="10">${MON[d.getUTCMonth()]}</text>`); lastLabel = x; }
    const items = (X.dayCons.get(k) || []).slice().sort((a, b) => b.amt - a.amt).slice(0, 5);
    const tip = `<b>${d.toUTCString().slice(0, 16)}</b>` + tipRows(items.map(t => [esc(t.merchant), usd2(t.amt)])) + (v ? tipRows([["Total", usd2(v)]]) : "<div>Nothing spent</div>");
    s.push(`<rect x="${x}" y="${yy}" width="${cell}" height="${cell}" rx="2" fill="${fill(v)}" data-tip="${esc(tip)}"/>`);
  }
  const ly = 16 + 7 * step + 6;
  s.push(`<text x="${w - 150}" y="${ly + 9}" text-anchor="end">Less</text>` + [0, 1, 2, 3].map(i => `<rect x="${w - 144 + i * 16}" y="${ly}" width="12" height="12" rx="2" fill="${SEQ[i]}"/>`).join("") + `<text x="${w - 78}" y="${ly + 9}">More</text></svg>`);
  el.innerHTML = s.join("");
  // average spend by weekday
  const dEl = $("#dow"), dw = width(dEl), dh = 150, names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"], tot = [0, 0, 0, 0, 0, 0, 0], cnt = [0, 0, 0, 0, 0, 0, 0];
  for (let i = 0; i < 364; i++) { const ms = end.getTime() - i * 864e5, wd = new Date(ms).getUTCDay(); tot[wd] += byDay[iso(new Date(ms))] || 0; cnt[wd]++; }
  const avg = tot.map((t, i) => t / cnt[i]), ymax = niceMax(Math.max(...avg)), band = (dw - 40) / 7, bw = Math.min(24, band * .6);
  let t = `<svg width="${dw}" height="${dh}" role="img" aria-label="Average spending by weekday">`;
  avg.forEach((v, i) => { const hh = v / ymax * (dh - 46), x = 40 + i * band + (band - bw) / 2;
    t += `<path d="${colPath(x, dh - 20 - hh, bw, hh)}" fill="${SER[0]}"/><text x="${x + bw / 2}" y="${dh - 24 - hh}" text-anchor="middle" class="ink">${compact(v)}</text><text x="${x + bw / 2}" y="${dh - 6}" text-anchor="middle">${names[i]}</text>`;
    t += `<rect x="${40 + i * band}" y="16" width="${band}" height="${dh - 36}" fill="transparent" data-tip="${esc(`<b>${names[i]}</b>` + tipRows([["Average", usd2(v)], ["Total, 12 mo", usd(tot[i])]]))}"/>`; });
  dEl.innerHTML = t + `<line x1="40" x2="${dw}" y1="${dh - 20}" y2="${dh - 20}" stroke="var(--base)"/></svg>`;
}
function hbars(el, rows, color) {
  const w = width(el), rh = 22, lw = Math.min(150, w * .38), h = rows.length * rh + 4, mx = Math.max(...rows.map(r => r[1]), 1);
  let s = `<svg width="${w}" height="${h}">`;
  rows.forEach(([n, v, tip], i) => { const y = i * rh + 4, bw = Math.max(2, v / mx * (w - lw - 60));
    const fit = Math.floor(lw / 6.6);
    s += `<text x="${lw - 8}" y="${y + 12}" text-anchor="end" class="ink">${esc(n.length > fit ? n.slice(0, fit - 1) + "…" : n)}</text><path d="${barPath(lw, y + 2, bw, 14)}" fill="${color}"/><text x="${lw + bw + 5}" y="${y + 13}">${compact(v)}</text>`;
    s += `<rect x="0" y="${y}" width="${w}" height="${rh}" fill="transparent" data-tip="${esc(tip)}"/>`; });
  el.innerHTML = s + `<line x1="${lw}" x2="${lw}" y1="0" y2="${h}" stroke="var(--base)"/></svg>`;
}
function renderMerch() {
  const by = {}; for (const t of X.cons) if (t.d > X.L12) { const k = t.merchant; (by[k] ||= {v: 0, n: 0, cat: t.cat, w: t.w}); by[k].v += t.amt; by[k].n++; }
  const rows = Object.entries(by).sort((a, b) => b[1].v - a[1].v).slice(0, 14)
    .map(([k, o]) => [k, o.v, `<b>${esc(k)}</b>` + tipRows([["What", esc(o.w)], ["Kind", esc(o.cat)], ["Charges", o.n], ["Total", usd2(o.v)], ["Average", usd2(o.v / o.n)]])]);
  hbars($("#merch"), rows, SER[0]);
}
function renderBig() {
  const xs = X.cons.filter(t => t.d > X.L12 && t.amt > 0).sort((a, b) => b.amt - a.amt), top = xs.slice(0, 10);
  $("#bigsub").textContent = `The 10 largest purchases in the last 12 months were ${X.consL12 ? Math.round(sum(top.map(t => t.amt)) / X.consL12 * 100) : 0}% of all spending. A typical purchase was ${usd2(xs[Math.floor(xs.length / 2)]?.amt || 0)}.`;
  $("#big").innerHTML = `<table><thead><tr><th>Date</th><th>Merchant</th><th>What</th><th class="n">Amount</th></tr></thead><tbody>${top.map(t => `<tr><td class="n" style="text-align:left">${t.d}</td><td>${esc(t.merchant)}</td><td style="color:var(--mut)" title="${esc(t.cat)}">${esc(t.w)}</td><td class="n">${usd2(t.amt)}</td></tr>`).join("")}</tbody></table>`;
}
function renderOther() {
  const oth = Object.entries(X.otherL12).filter(([, v]) => Math.abs(v) >= 1);
  $("#othr").innerHTML = oth.length ? `<table><tbody>${oth.sort((a, b) => b[1] - a[1]).map(([c, v]) => `<tr><td>${esc(c)}</td><td class="n">${usd(v)}</td></tr>`).join("")}</tbody></table>` : `<p class="note">Nothing in the last 12 months.</p>`;
}
function renderPeople() {
  const PP = D.people || [];
  $("#ppl").innerHTML = PP.length ? `<table><thead><tr><th>Person</th><th class="n">Fronted</th><th class="n">Paid back</th><th class="n">Outstanding</th></tr></thead><tbody>
    ${PP.map(b => `<tr data-tip="${esc(`<b>${esc(b.person)}</b>` + b.items.slice(0, 12).map(i => `<div class="r"><span>${i.date} ${esc(i.desc.slice(0, 30))}</span><span>${usd2(i.amount)}</span></div>`).join(""))}">
      <td>${esc(b.person)}</td><td class="n">${usd2(b.fronted)}</td><td class="n">${usd2(b.repaid)}</td><td class="n" style="${Math.abs(b.net) < 0.5 ? "color:var(--mut)" : ""}">${Math.abs(b.net) < 0.5 ? "settled" : usd2(b.net)}</td></tr>`).join("")}
  </tbody></table>` : `<p class="note">Nothing yet. Repayments are matched by <code>ledger classify</code>.</p>`;
}

// ---------------------------------------------------------------- income
const CLS = [["earned", "Earned"], ["steady", "Steady, not earned"], ["investment", "Investment"], ["windfall", "Gifts and one-offs"], ["unknown", "Unexplained"]];
function renderIncome() {
  let rows = "";
  for (const [k, label] of CLS) {
    const inc = X.incCatL12[k];
    if (!inc) continue;
    rows += `<tr><td><b>${label}</b></td><td class="n"><b>${usd(sum(Object.values(inc)))}</b></td></tr>`
      + Object.entries(inc).sort((a, b) => b[1] - a[1]).map(([c, v]) => `<tr><td style="padding-left:18px;color:var(--mut)">${esc(c)}</td><td class="n" style="color:var(--mut)">${usd(v)}</td></tr>`).join("");
  }
  $("#inc").innerHTML = `<table><tbody>${rows}<tr><td><b>Total</b></td><td class="n"><b>${usd(X.incL12)}</b></td></tr></tbody></table>`;
}
function renderNet() {
  const el = $("#net"), w = width(el), h = 200, m = {l: 44, r: 8, t: 10, b: 22};
  const ms = months(D.fullFrom, X.AMON);
  const inc = mo => X.monthInc[mo] || 0, con = mo => X.monthCons[mo] || 0, tax = mo => X.monthTax[mo] || 0;
  const vals = ms.map(mo => inc(mo) - con(mo) - tax(mo));
  const mx = niceMax(Math.max(...vals.map(Math.abs))), iw = w - m.l - m.r, ih = h - m.t - m.b, band = iw / ms.length, bw = Math.min(24, band * .7);
  const y = v => m.t + ih / 2 - v / mx * ih / 2;
  $("#netsub").textContent = `Income minus spending and taxes, from ${mlabel(D.fullFrom)}, when every account is visible. Total: ${usd(sum(vals))}. Income arrives in lumps, so single months swing.`;
  let s = `<svg width="${w}" height="${h}" role="img" aria-label="Monthly income minus spending">`;
  for (const v of [-mx, -mx / 2, mx / 2, mx]) s += `<line x1="${m.l}" x2="${w - m.r}" y1="${y(v)}" y2="${y(v)}" stroke="var(--grid)"/><text x="${m.l - 6}" y="${y(v) + 4}" text-anchor="end">${compact(v)}</text>`;
  ms.forEach((mo, i) => { const v = vals[i], x = m.l + i * band + (band - bw) / 2, hh = Math.abs(y(v) - y(0));
    if (v >= 0) s += `<path d="${colPath(x, y(v), bw, hh)}" fill="${POS}"/>`;
    else s += `<path d="${colPath(x, y(0), bw, hh)}" transform="translate(0 ${2 * y(0) + hh}) scale(1 -1)" fill="${NEGC}"/>`;
    if (xlabel(ms, i)) s += `<text x="${x + bw / 2}" y="${h - 6}" text-anchor="middle">${xlabel(ms, i)}</text>`;
    s += `<rect x="${m.l + i * band}" y="${m.t}" width="${band}" height="${ih}" fill="transparent" data-tip="${esc(`<b>${mlabel(mo)}</b>` + tipRows([["Income", usd(inc(mo))], ["Spending", usd(con(mo))], ["Taxes", usd(tax(mo))], ["Left over", usd(v)]]))}"/>`; });
  el.innerHTML = s + `<line x1="${m.l}" x2="${w - m.r}" y1="${y(0)}" y2="${y(0)}" stroke="var(--base)"/></svg>`;
}

// ---------------------------------------------------------------- budget
const FIELDS = [
  ["wealth", "Net worth now ($)", 100], ["essentials", "Essentials ($/month)", 10],
  ["age", "Age", 1], ["horizonAge", "Plan to age", 1], ["retireAge", "Retire at", 1],
  ["resolveYears", "Years until career is clear", 1], ["reserveMonths", "Emergency reserve (months of essentials)", 1],
  ["rho", "Impatience (%/yr)", 0.5], ["capWeeks", "Allowance cap (weeks)", 1],
];
const RFIELDS = [
  ["inflation", "Inflation (%/yr)", 0.1], ["cashRate", "Interest on cash (%/yr)", 0.1],
  ["stockReturn", "Expected stock return (%/yr, with dividends)", 0.5], ["stockVol", "Stock volatility (%/yr)", 1],
  ["stockShare", "Share of savings in stocks (%)", 1],
];
const autoTag = k => P.auto.includes(k) ? ` <span class="auto">(from the ledger)</span>` : "";
const KIND_LABEL = {earned: "Earned", steady: "Steady", windfall: "Windfall or gift"};
const pct = (x, d = 1) => (x * 100).toFixed(d) + "%";
function renderBudget() {
  const el = $("#budget");
  el.innerHTML = `<div class="head"><h1>Budget</h1><span class="note">What you can spend, given what you believe about the future</span></div>
  <div class="hero"><div class="tile main" id="bh"></div><div class="side" id="bs"></div></div>
  <div class="card"><h2>Allowance</h2><p class="sub">Builds up every day, goes down with what you spend, and stops growing at the cap. Past the cap, it stays saved.</p><div class="chart" id="bchart"></div></div>
  <div class="card"><div class="ctl"><h2>Your spending over a lifetime</h2>
    <div class="seg" id="fanv"><button data-v="spend" class="${fanView === "spend" ? "on" : ""}">Spending per month</button><button data-v="wealth" class="${fanView === "wealth" ? "on" : ""}">Savings</button></div></div>
    <p class="sub" id="fansub"></p><div class="chart" id="bfan"></div></div>
  <div class="cols">
    <div class="card"><h2>Once your career is clear</h2><p class="sub">Spending per month in the first year after, by scenario.</p><div class="chart" id="bscen"></div></div>
    <div class="card"><h2>Risk aversion</h2><p class="sub">How the sustainable rate moves with it.</p><div class="chart" id="bgam"></div></div>
  </div>
  <div class="kpis" id="bret" style="margin-top:16px"></div>
  <details class="card"><summary>Assumptions</summary>
  <h3 style="margin-top:0">You</h3>
  <div class="params">
    <label>Risk aversion <span class="row"><input type="range" min="0.5" max="10" step="0.5" data-k="gamma"><input type="number" step="0.5" data-k="gamma" style="width:70px"></span></label>
    ${FIELDS.map(([k, l, st]) => `<label>${l}${autoTag(k)}<input type="number" step="${st}" data-k="${k}"></label>`).join("")}
    <label>Allowance covers<select data-k="covers"><option value="discretionary">Discretionary only (essentials paid separately)</option><option value="all">All spending</option></select></label>
    <label>Allowance starts<input type="date" data-k="start"></label>
  </div>
  <h3>Returns and inflation</h3>
  <div class="params">${RFIELDS.map(([k, l, st]) => `<label>${l}${autoTag(k)}<input type="number" step="${st}" data-k="${k}"></label>`).join("")}</div>
  <h3>Career scenarios <span class="note">after tax, today's dollars</span></h3>
  <table class="scen"><thead><tr><th>Scenario</th><th class="n">Chance %</th><th class="n">Income/yr until clear</th><th class="n">Income/yr after</th><th class="n">Raise above inflation %/yr</th><th></th></tr></thead><tbody id="scen"></tbody></table>
  <button class="btn" id="addS">Add scenario</button>
  <h3>Other income <span class="note">side income, family help, gifts; the chance is how sure you are it keeps coming</span></h3>
  <table class="scen"><thead><tr><th>Stream</th><th>Kind</th><th class="n">Chance %</th><th class="n">$/yr</th><th class="n">For years</th><th></th></tr></thead><tbody id="strm"></tbody></table>
  <button class="btn" id="addX">Add income</button><button class="btn" id="reset">Reset to defaults</button><button class="btn" id="copy">Copy as JSON</button>
  <span class="note" id="copied"></span>
  </details>
  <details class="card"><summary>How it's computed</summary>
    <p>Until your career is clear, you spend a steady amount each year. You can't borrow, and the emergency reserve is never planned away. After it clears, each scenario funds the rest of your life from what's left plus its income, along the best spending path for that scenario. The model picks the steady amount with the highest expected satisfaction, weighting each scenario by its chance.</p>
    <p>Satisfaction from spending has diminishing returns. Risk aversion sets how much it hurts when a bad scenario forces cuts. At 1 you care about the average outcome. At 3 to 5, a lean scenario pulls your spending down well before you know you're in it. That's the reason to save before you know.</p>
    <p>Each other income stream is either real or not, with the chance you give it, and you find out when the career clears. Gifts are uncertain by nature, so give them a low chance, or leave them out and let them raise your wealth when they arrive. Either way the plan never spends a gift all at once.</p>
    <p>Everything is in today's dollars. Rates you enter are nominal and get reduced by inflation. Savings earn a mix of cash interest and stock returns. Stocks earn more on average but swing, so the return the plan relies on is the average minus a charge that grows with your risk aversion and with the share in stocks. Dividends are part of the stock return. Spending them is not extra money on top.</p>
    <p>The lifetime chart simulates 500 lives with random scenarios and stock returns. You follow the plan, and after the career clears you re-plan every year from what you actually have. The bands show the middle 50% and 80% of outcomes.</p>
    <p>The cap stops the allowance from piling up into one big spree, and stops you from deciding later that you're owed it all at once. Re-run the model as your savings and beliefs change. The rate then adjusts, which is what keeps the plan consistent over time.</p>
    <p>${OPTS.portal ? "Changes save automatically to data/budget.json." : "Save these parameters as <code>data/budget.json</code> to make them the page's defaults. Until then the browser keeps them."}</p>
  </details>`;
  el.querySelectorAll("[data-k]").forEach(inp => {
    inp.value = P[inp.dataset.k];
    inp.addEventListener("input", () => { const k = inp.dataset.k; P[k] = inp.type === "number" || inp.type === "range" ? +inp.value : inp.value;
      P.auto = P.auto.filter(x => x !== k); inp.closest("label").querySelector(".auto")?.remove();
      el.querySelectorAll(`[data-k="${k}"]`).forEach(o => { if (o !== inp) o.value = inp.value; }); changed(); });
  });
  $("#addS").onclick = () => { P.scenarios.push({name: "New scenario", p: 10, y1: 0, y2: 50000, growth: 2}); scenRows(); changed(); };
  $("#addX").onclick = () => { P.streams.push({name: "New income", kind: "steady", p: 90, amount: 1000, years: 3}); scenRows(); changed(); };
  $("#reset").onclick = () => { P = {auto: AUTO, ...structuredClone(BASE)}; store.set(null); renderBudget(); changed(); };
  $("#copy").onclick = () => { navigator.clipboard?.writeText(JSON.stringify(P, null, 2)); $("#copied").textContent = "Copied."; };
  $("#fanv").addEventListener("click", e => { const b = e.target.closest("button"); if (!b) return; fanView = b.dataset.v;
    $("#fanv").querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b)); fanChart(); });
  scenRows();
}
function scenRows() {
  const num = (arr, i, f, v, st) => `<td><input type="number" data-a="${arr}" data-i="${i}" data-f="${f}" value="${v}" step="${st}" style="text-align:right"></td>`;
  const del = (arr, i) => `<td><button class="btn x" data-a="${arr}" data-del="${i}" aria-label="Remove">✕</button></td>`;
  $("#scen").innerHTML = P.scenarios.map((s, i) => `<tr><td><input data-a="scenarios" data-i="${i}" data-f="name" value="${esc(s.name)}"></td>
    ${num("scenarios", i, "p", s.p, 1)}${num("scenarios", i, "y1", s.y1, 1000)}${num("scenarios", i, "y2", s.y2, 1000)}${num("scenarios", i, "growth", s.growth, 0.5)}${del("scenarios", i)}</tr>`).join("");
  $("#strm").innerHTML = P.streams.map((x, i) => `<tr><td><input data-a="streams" data-i="${i}" data-f="name" value="${esc(x.name)}"></td>
    <td><select data-a="streams" data-i="${i}" data-f="kind">${Object.entries(KIND_LABEL).map(([k, l]) => `<option value="${k}" ${x.kind === k ? "selected" : ""}>${l}</option>`).join("")}</select></td>
    ${num("streams", i, "p", x.p, 5)}${num("streams", i, "amount", x.amount, 100)}${num("streams", i, "years", x.years, 1)}${del("streams", i)}</tr>`).join("");
  document.querySelectorAll("#scen input, #strm input, #strm select").forEach(inp => inp.addEventListener("input", () => {
    const o = P[inp.dataset.a][+inp.dataset.i], f = inp.dataset.f;
    o[f] = f === "name" || f === "kind" ? inp.value : +inp.value;
    if (f === "kind") { o.p = KIND_P[inp.value]; scenRows(); }
    changed(); }));
  document.querySelectorAll("[data-del]").forEach(b => b.onclick = () => { P[b.dataset.a].splice(+b.dataset.del, 1); scenRows(); changed(); });
}
let fanView = "spend", raf = 0;
const solveP = () => P.scenarios.length ? Budget.solve(P) : null;
function changed() {
  store.set(P);
  updateTiles();
  dirty.add("budget");
  if (VIEW === "budget") { cancelAnimationFrame(raf); raf = requestAnimationFrame(() => { dirty.delete("budget"); budgetCharts(); }); }
}
// the allowance (overview tile and the budget's headline): cheap, so always current
function updateTiles() {
  const res = solveP();
  if (!res) {
    if ($("#ovAllow")) $("#ovAllow").textContent = "—";
    $("#bh").innerHTML = `<div class="l">Available to spend now</div><div class="v">—</div><div class="d">No spending rate keeps the emergency reserve intact in every scenario. Lower the reserve, or check the scenarios' income.</div>`;
    $("#bs").innerHTML = ""; return;
  }
  const c = res.c, B = bucket(P, c);
  if ($("#ovAllow")) $("#ovAllow").innerHTML = `<span class="${B.b < 0 ? "neg" : ""}">${usd(B.b)}</span>`;
  $("#bh").innerHTML = `<div class="l">Available to spend now</div><div class="v ${B.b < 0 ? "neg" : ""}">${usd(B.b)}</div>
    <div class="d">Grows ${usd2(B.rate)} a day, up to ${usd(B.cap)}.</div>`;
  const actual = P.covers === "all" ? X.consL12 : X.discL12;
  $("#bs").innerHTML = tile("Sustainable spending", `${compact(c / 12)}/mo`, `${compact(c)} a year, everything included`)
    + tile(P.covers === "all" ? "Allowance" : "Allowance after essentials", `${compact(B.rate * 365 / 12)}/mo`, P.covers === "all" ? "covers all spending" : `essentials budgeted at ${compact(P.essentials)}/mo`)
    + tile("You actually spent", `${compact(actual / 12)}/mo`, `last 12 months, ${P.covers === "all" ? "all spending" : "discretionary"}`)
    + tile("Could spend at most", `${compact(res.cmax / 12)}/mo`, "before some scenario breaks the reserve");
}
function budgetCharts() {
  const res = solveP();
  if (!res) { ["#bchart", "#bscen", "#bgam", "#bfan", "#bret"].forEach(s => $(s).innerHTML = ""); return; }
  bucketChart(bucket(P, res.c));
  const rows = P.scenarios.map((s, i) => [s.name, res.after[i] / 12, `<b>${esc(s.name)}</b>` + tipRows([["Chance", `${s.p}%`], ["Spending after, first year", usd(res.after[i])], ["Per month", usd(res.after[i] / 12)]])]);
  hbars($("#bscen"), rows, SER[0]);
  returnsTiles();
  fanChart();
  gammaChart(res.c);
}
function returnsTiles() {
  const R = Budget.rates(P), human = sum(Budget.states(P).map(st => {   // expected value of future income, discounted like cash
    let pv = 0; for (let t = 0; t < P.horizonAge - P.age; t++) {
      const y = t < P.resolveYears ? +st.sc.y1 || 0 : P.age + t < P.retireAge ? (+st.sc.y2 || 0) * Math.pow(1 + (+st.sc.growth || 0) / 100, t - P.resolveYears) : 0;
      pv += (y + Budget.streamAt(st.on, t)) / Math.pow(1 + Math.max(R.rc, 0.005), t); } return st.p * pv; }));
  const fin = Math.max(1, P.wealth), sug = R.star * (fin + human) / fin, divL12 = X.clsL12.investment || 0, brok = X.holdV + X.W.brokerageCash;
  $("#bret").innerHTML =
    tile("Expected real return", pct(R.exp), `${pct(R.s, 0)} stocks at ${pct(R.mu)} real, cash at ${pct(R.rc)} real`)
    + tile("Return the plan counts on", pct(R.r), `after a ${pct(R.exp - R.r)} charge for volatility at risk aversion ${P.gamma}`)
    + tile("Dividends and interest", `${compact(divL12)}/yr`, X.holdV ? `${pct(divL12 / brok)} of the brokerage; part of the return, not extra` : "last 12 months")
    + tile("Inflation on cash", `${compact(X.cashV * Math.max(0, P.inflation - P.cashRate) / 100)}/yr`, `lost on ${compact(X.cashV)} cash if it earns ${P.cashRate}%`)
    + tile("Suggested stock share", sug >= 1 ? "100%" : pct(Math.max(0, sug), 0), `of savings. Future income of about ${compact(human)} works like a bond, so savings can hold more stocks`);
}
function fanChart() {
  const res = solveP(); if (!res) return;
  const S = Budget.simulate(P, res.c), B = fanView === "spend" ? S.spend : S.wealth;
  const el = $("#bfan"), w = width(el), h = 220, m = {l: 52, r: 12, t: 12, b: 24};
  const st = niceMax(Math.max(...B.map(b => b[4]), ...B.map(b => -b[0])) / 4);   // a round step, so the ticks are round
  const hi = st * Math.ceil(Math.max(...B.map(b => b[4])) / st), lo = fanView === "wealth" ? -st * Math.ceil(Math.max(0, ...B.map(b => -b[0])) / st) : 0;
  const iw = w - m.l - m.r, ih = h - m.t - m.b, x = i => m.l + i / Math.max(1, B.length - 1) * iw, y = v => m.t + ih - (v - lo) / (hi - lo) * ih;
  const area = (a, b) => `${B.map((r, i) => `${x(i).toFixed(1)},${y(r[a]).toFixed(1)}`).join(" ")} ${B.map((r, i) => `${x(i).toFixed(1)},${y(r[b]).toFixed(1)}`).reverse().join(" ")}`;
  let s = `<svg width="${w}" height="${h}" role="img" aria-label="Simulated ${fanView === "spend" ? "monthly spending" : "savings"} by age">`;
  for (let v = lo; v <= hi + 1e-9; v += st) s += `<line x1="${m.l}" x2="${w - m.r}" y1="${y(v)}" y2="${y(v)}" stroke="var(--grid)"/><text x="${m.l - 6}" y="${y(v) + 4}" text-anchor="end">${compact(v)}</text>`;
  s += `<polygon points="${area(0, 4)}" fill="${SER[0]}" opacity=".14"/><polygon points="${area(1, 3)}" fill="${SER[0]}" opacity=".22"/>`;
  s += `<polyline points="${B.map((r, i) => `${x(i).toFixed(1)},${y(r[2]).toFixed(1)}`).join(" ")}" fill="none" stroke="${SER[0]}" stroke-width="2" stroke-linejoin="round"/>`;
  const T1 = Math.max(1, Math.round(P.resolveYears));
  s += `<line x1="${x(T1)}" x2="${x(T1)}" y1="${m.t}" y2="${m.t + ih}" stroke="var(--mut)" stroke-dasharray="3 3"/><text x="${x(T1) + 4}" y="${m.t + 10}">career clear</text>`;
  if (P.retireAge > P.age && P.retireAge < P.horizonAge) s += `<line x1="${x(P.retireAge - P.age)}" x2="${x(P.retireAge - P.age)}" y1="${m.t}" y2="${m.t + ih}" stroke="var(--mut)" stroke-dasharray="3 3"/><text x="${x(P.retireAge - P.age) + 4}" y="${m.t + 10}">retire</text>`;
  B.forEach((r, i) => { const a = P.age + i; if (a % 10 === 0) s += `<text x="${x(i)}" y="${h - 6}" text-anchor="middle">${a}</text>`; });
  const step = Math.max(1, Math.floor(B.length / 70));
  for (let i = 0; i < B.length; i += step) s += `<rect x="${x(i) - iw / B.length / 2}" y="${m.t}" width="${iw / B.length}" height="${ih}" fill="transparent" data-tip="${esc(`<b>Age ${P.age + i}</b>` + tipRows([["Typical", usd(B[i][2])], ["Middle half", `${usd(B[i][1])} – ${usd(B[i][3])}`], ["8 in 10 lives", `${usd(B[i][0])} – ${usd(B[i][4])}`]]))}"/>`;
  el.innerHTML = s + `<line x1="${m.l}" x2="${w - m.r}" y1="${y(0)}" y2="${y(0)}" stroke="var(--base)"/></svg>` + legend([["Typical life", SER[0]], ["Middle half and 8 in 10 of simulated lives", SER[0] + "55"]]);
  const sp = S.spend, at = a => sp[Math.min(sp.length - 1, Math.max(0, a - P.age))];
  $("#fansub").textContent = `Today's dollars. At ${P.age + 10}, typical spending is ${usd(at(P.age + 10)[2])}/mo; 8 in 10 simulated lives land between ${usd(at(P.age + 10)[0])} and ${usd(at(P.age + 10)[4])}. In retirement it's typically ${usd(at(Math.min(P.horizonAge - 1, P.retireAge + 5))[2])}/mo.`;
}
function bucketChart(B) {
  const el = $("#bchart"), w = width(el), h = 190, m = {l: 48, r: 10, t: 12, b: 22}, pts = B.path;
  let lo = 0, top = B.cap; for (const p of pts) { if (p[1] < lo) lo = p[1]; if (p[1] > top) top = p[1]; }
  const hi = niceMax(top);
  const iw = w - m.l - m.r, ih = h - m.t - m.b, x = i => m.l + i / Math.max(1, pts.length - 1) * iw, y = v => m.t + ih - (v - lo) / (hi - lo) * ih;
  let s = `<svg width="${w}" height="${h}" role="img" aria-label="Allowance balance over time">`;
  for (let k = 0; k <= 4; k++) { const v = lo + (hi - lo) * k / 4; s += `<line x1="${m.l}" x2="${w - m.r}" y1="${y(v)}" y2="${y(v)}" stroke="var(--grid)"/><text x="${m.l - 6}" y="${y(v) + 4}" text-anchor="end">${compact(v)}</text>`; }
  s += `<line x1="${m.l}" x2="${w - m.r}" y1="${y(B.cap)}" y2="${y(B.cap)}" stroke="var(--mut)" opacity=".7"/><text x="${w - m.r}" y="${y(B.cap) - 4}" text-anchor="end">cap ${compact(B.cap)}</text>`;
  if (lo < 0) s += `<line x1="${m.l}" x2="${w - m.r}" y1="${y(0)}" y2="${y(0)}" stroke="var(--base)"/>`;
  const line = pts.map((p, i) => `${x(i).toFixed(1)},${y(p[1]).toFixed(1)}`).join(" ");
  s += `<polygon points="${m.l},${y(Math.max(lo, 0))} ${line} ${x(pts.length - 1)},${y(Math.max(lo, 0))}" fill="${SER[0]}" opacity=".1"/>`;
  s += `<polyline points="${line}" fill="none" stroke="${SER[0]}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
  const last = pts[pts.length - 1];
  s += `<circle cx="${x(pts.length - 1)}" cy="${y(last[1])}" r="4.5" fill="${SER[0]}" stroke="var(--background, #fff)" stroke-width="2"/>`;
  pts.forEach((p, i) => { if (p[0].endsWith("-01")) s += `<text x="${x(i)}" y="${h - 6}" text-anchor="middle">${MON[+p[0].slice(5, 7) - 1]}</text>`; });
  const step = Math.max(1, Math.floor(pts.length / 120));
  for (let i = 0; i < pts.length; i += step) s += `<rect x="${x(i) - iw / pts.length * step / 2}" y="${m.t}" width="${iw / pts.length * step}" height="${ih}" fill="transparent" data-tip="${esc(`<b>${pts[i][0]}</b>` + tipRows([["Allowance", usd(pts[i][1])], ["Spent that day", usd2(pts[i][2])]]))}"/>`;
  el.innerHTML = s + "</svg>";
}
function gammaChart(c) {
  const el = $("#bgam"), w = width(el), h = 170, m = {l: 48, r: 12, t: 12, b: 30};
  const gs = []; for (let g = 0.5; g <= 10.001; g += 0.5) gs.push(g);
  const cs = gs.map(g => { const r = Budget.solve(P, g); return r ? r.c / 12 : null; });
  const ok = cs.filter(v => v != null); if (!ok.length) { el.innerHTML = ""; return; }
  const hi = niceMax(Math.max(...ok)), iw = w - m.l - m.r, ih = h - m.t - m.b, x = g => m.l + (g - 0.5) / 9.5 * iw, y = v => m.t + ih - v / hi * ih;
  let s = `<svg width="${w}" height="${h}" role="img" aria-label="Sustainable monthly spending by risk aversion">`;
  for (let k = 0; k <= 3; k++) { const v = hi * k / 3; s += `<line x1="${m.l}" x2="${w - m.r}" y1="${y(v)}" y2="${y(v)}" stroke="var(--grid)"/><text x="${m.l - 6}" y="${y(v) + 4}" text-anchor="end">${compact(v)}</text>`; }
  [1, 2, 4, 6, 8, 10].forEach(g => s += `<text x="${x(g)}" y="${h - 14}" text-anchor="middle">${g}</text>`);
  s += `<text x="${m.l + iw / 2}" y="${h - 1}" text-anchor="middle">risk aversion</text>`;
  s += `<polyline points="${gs.map((g, i) => cs[i] == null ? "" : `${x(g).toFixed(1)},${y(cs[i]).toFixed(1)}`).join(" ")}" fill="none" stroke="${SER[0]}" stroke-width="2" stroke-linejoin="round"/>`;
  s += `<circle cx="${x(P.gamma)}" cy="${y(c / 12)}" r="4.5" fill="${SER[1]}" stroke="var(--background, #fff)" stroke-width="2"/><text x="${x(P.gamma) + 8}" y="${y(c / 12) - 8}" class="ink">you: ${compact(c / 12)}/mo</text>`;
  gs.forEach((g, i) => { if (cs[i] != null) s += `<rect x="${x(g) - iw / 40}" y="${m.t}" width="${iw / 20}" height="${ih}" fill="transparent" data-tip="${esc(tipRows([["Risk aversion", g], ["Per month", usd(cs[i])]]))}"/>`; });
  el.innerHTML = s + `<line x1="${m.l}" x2="${w - m.r}" y1="${y(0)}" y2="${y(0)}" stroke="var(--base)"/></svg>`;
}

// ---------------------------------------------------------------- lifecycle
function mount(root, data, opts = {}) {
  ROOT = root; OPTS = opts; D = data;
  palette();
  X = derive();
  store = makeStore();
  BASE = base();
  P = {auto: AUTO, ...structuredClone(BASE), ...(store.get() || {})};
  for (const k of P.auto) P[k] = BASE[k];
  renderShell();
  VIEWS.forEach(([k]) => dirty.add(k));
  addEventListener("themechange", () => { palette(); redrawAll(); });
  let rt; addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(redrawAll, 150); });
}
// New data from the server (an edit, a pull): re-summarize, keep the budget parameters.
function setData(data) {
  D = data; X = derive(); for (const k in _byDay) delete _byDay[k];
  BASE = base();
  for (const k of P.auto) P[k] = BASE[k];
  rebuild();
}
return {mount, setData, show, VIEWS, get view() { return VIEW; }};
})();
