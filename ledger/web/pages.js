// The portal's working pages: transactions, review, people and connections. Each renders into its
// own container, kept while you visit other pages; data comes from App.data (in memory, revalidated).
const Pages = (() => {
let GROUPS = {}, el = {};
const api = (...a) => App.api(...a);
const pct0 = p => Math.round(p * 100) + "%";
const GROUP_LABEL = {essential: "Essential", discretionary: "Discretionary", other: "Not spending", income: "Income"};
let CAT_OPTS = "";
function catOptions(sel, first) {
  CAT_OPTS ||= Object.keys(GROUP_LABEL).map(g => `<optgroup label="${GROUP_LABEL[g]}">` + Object.keys(GROUPS).filter(c => GROUPS[c] === g).sort()
    .map(c => `<option>${esc(c)}</option>`).join("") + "</optgroup>").join("");
  let s = `<option value="">${first ? esc(first) : "—"}</option>` + CAT_OPTS;
  if (sel) s = s.replace(`<option>${esc(sel)}</option>`, `<option selected>${esc(sel)}</option>`);
  return s;
}

// ---------------------------------------------------------------- transactions
let TX = [], BYID = new Map(), KNOWN = [], TAGS = [], shown = [], limit = 100, txReady = false;
const STEP = 100;   // rows drawn at a time; more are added as you scroll to the end
const F = {q: "", acct: "", flow: "external", cat: ""};
const eff = t => t.effective || t.category || t.rule || "";
// coins show in their own unit, with what they were worth that day beside; totals add the dollars
const money = t => t.cur && t.cur !== "USD"
  ? `${t.amount < 0 ? "−" : ""}${Math.abs(t.amount).toFixed(4)} ${esc(t.cur)}${t.usd != null ? `<small style="display:block;font-size:11px;color:var(--mut)">≈ ${usd2(t.usd)}</small>` : ""}`
  : usd2(t.amount);
const dollars = t => t.cur && t.cur !== "USD" ? (t.usd ?? 0) : t.amount;
const hay = t => (t.desc + " " + (t.note || "") + " " + t.merchant).toLowerCase();
let VER = 0, drawn = "";   // VER counts data changes; drawn = what the table shows, to skip redrawing it on a revisit
function useTxns(d) {
  VER++;
  TX = d.txns; BYID = new Map(TX.map(t => [t.id, t]));
  for (const t of TX) t._hay = hay(t);
  KNOWN = [...new Set(TX.flatMap(x => x.account.startsWith("venmo") ? [x.desc.split(":")[0].trim()] : []))].filter(Boolean).sort();
  TAGS = [...new Set(TX.flatMap(x => x.tags))].sort();
}
// after an edit: fetch just the rows it could have changed, and fold them in
async function refresh(ids) {
  const d = await api("/api/txns?ids=" + encodeURIComponent([...new Set(ids)].join(",")));
  for (const f of d.txns) { const t = BYID.get(f.id); if (t) { Object.assign(t, f); t._hay = hay(t); } }
  VER++;
  App.touched();
}
async function txPage(c) {
  if (!txReady) {
    c.innerHTML = `<div class="head"><h1>Transactions</h1><span class="note" id="count"></span></div>
    <p class="sub">Every transaction has one kind, which is what the dashboard and budget count: a kind you pick overrides the rules. What it was shows as a tag; search <code>#groceries</code>, <code>#dining</code> and so on. "For" says who a purchase served and how long it lasts; click it to change that or to settle a repayment. Only your share counts as your spending. Edits save as you go.</p>
    <div class="bar">
      <input id="q" type="search" placeholder="Search" style="flex:1;min-width:200px">
      <select id="cat"><option value="">All kinds</option><option value="!review">Needs review</option><option value="!mine">Labeled by you</option></select>
      <select id="acct"><option value="">All accounts</option></select>
      <select id="flow"><option value="external">In and out</option><option value="transfer">Between your accounts</option><option value="">Everything</option></select>
      <details id="bulkBox" style="position:relative"><summary class="note" style="cursor:pointer;list-style:none;padding:6px 4px">Label or tag all shown…</summary>
        <div style="position:absolute;right:0;top:34px;z-index:3;display:flex;gap:6px;background:var(--popover);border:1px solid var(--grid);border-radius:9px;padding:8px;box-shadow:0 4px 14px rgba(0,0,0,.12)">
        <select id="bulk"></select><button id="bulkGo" class="pri">Apply</button>
        <input id="bulkTag" list="tagList" placeholder="tag, e.g. trip:beach" style="width:150px"><button id="bulkTagGo">Tag all shown</button></div></details>
      <datalist id="tagList"></datalist>
    </div>
    <div class="panel"><table class="txt"><colgroup><col style="width:64px"><col><col style="width:108px"><col style="width:200px"><col style="width:170px"><col style="width:160px"></colgroup>
      <thead><tr><th>Date</th><th>Description</th><th class="n">Amount</th><th>Kind</th><th>For</th><th>Note</th></tr></thead><tbody id="rows"></tbody></table></div>
    <button id="more" style="margin-top:12px" hidden>Show more</button>`;
    $("#cat", c).innerHTML += Object.keys(GROUPS).sort().map(x => `<option>${esc(x)}</option>`).join("");
    $("#bulk", c).innerHTML = catOptions("", "Back to the rule");
    let qt;
    for (const id of ["q", "acct", "flow", "cat"]) $("#" + id, c).addEventListener("input", e => {
      F[id] = e.target.value; limit = STEP;
      clearTimeout(qt); qt = setTimeout(txRender, id === "q" ? 120 : 0);
    });
    const more = () => {
      if (limit >= shown.length) return;
      $("#rows", c).insertAdjacentHTML("beforeend", shown.slice(limit, limit + STEP).map((t, k) => txRow(t, limit + k)).join(""));
      limit += STEP; $("#more", c).hidden = shown.length <= limit;
    };
    $("#more", c).onclick = more;
    new IntersectionObserver(es => { if (es.some(e => e.isIntersecting) && App.page === "txns") more(); }, {rootMargin: "600px"}).observe($("#more", c));
    $("#bulkTagGo", c).onclick = async () => {
      const tag = $("#bulkTag", c).value.trim().toLowerCase().replace(/\s+/g, "-"), xs = shown.filter(t => t.flow === "external");
      if (!tag || !xs.length || !confirm(`Tag ${xs.length} transaction(s) #${tag}?`)) return;
      await api("/api/annotate", {changes: xs.map(t => ({id: t.id, tags: [...t.myTags.filter(x => x !== tag && x !== "-" + tag), tag]}))});
      await refresh(xs.map(t => t.id));
      $("#bulkTag", c).value = ""; txRender();
    };
    $("#bulkGo", c).onclick = async () => {
      const v = $("#bulk", c).value, xs = shown.filter(t => t.flow === "external");
      if (!xs.length || !confirm(`Set ${xs.length} transaction(s) to ${v || "the kind their rules give"}?`)) return;
      await api("/api/annotate", {changes: xs.map(t => ({id: t.id, category: v || null}))});
      await refresh(xs.map(t => t.id));
      txRender();
    };
    wireRows(c);
    txReady = true;
  }
  const params = new URLSearchParams(location.hash.slice(1));
  for (const k of Object.keys(F)) if (params.has(k)) F[k] = params.get(k);
  const d = await App.data("txns", d2 => { useTxns(d2); fillAccts(c); if (App.page === "txns") txRender(); });
  if (d.txns !== TX) { useTxns(d); fillAccts(c); }
  $("#q", c).value = F.q; $("#acct", c).value = F.acct; $("#flow", c).value = F.flow; $("#cat", c).value = F.cat;
  if (drawn !== VER + JSON.stringify(F)) txRender();
}
function fillAccts(c) {
  const accts = [...new Map(TX.map(t => [t.account, t.accountName])).entries()].sort();
  $("#acct", c).innerHTML = `<option value="">All accounts</option>` + accts.map(([a, n]) => `<option value="${esc(a)}">${esc(n)}</option>`).join("");
  $("#acct", c).value = F.acct;
}
function txRow(t, i) {
  const cat = t.category || t.effective || t.rule || "—";
  return `<tr data-i="${i}">
    <td class="date">${t.date.slice(5)}<small>${t.date.slice(0, 4)}</small></td>
    <td class="desc" title="${esc(t.desc)}">${esc(t.desc)}<small>${esc(t.accountName)}${t.orders.length ? " · " + esc(t.orders.flatMap(o => o.items).join("; ")) : ""}${t.tags.map(g => ` <span class="tag">#${esc(g)}</span>`).join("")}</small></td>
    <td class="n ${t.amount > 0 ? "in" : ""}">${money(t)}</td>
    <td>${t.flow === "external" ? `<button class="catbtn ${t.category ? "set" : ""}" data-cat="${i}" title="${esc(t.source === "model" ? `Claude${t.model.why ? ": " + t.model.why : ""}` : t.source || "")}">${esc(cat)} ▾</button>` : `<span class="mut" style="padding:0 7px">${t.flow === "transfer" ? "Transfer" : esc(t.flow)}</span>`}</td>
    <td>${t.flow === "external" ? `<button class="forbtn ${t.overrides.for || t.overrides.life_months != null || t.overrides.repays ? "set" : ""}" data-for="${i}">${esc(forLabel(t))}</button>` : ""}</td>
    <td><input class="note" data-f="note" value="${esc(t.note)}" placeholder="—"></td></tr>`;
}
function txRender() {
  if (App.page !== "txns") return;
  history.replaceState(history.state, "", location.pathname + "#" + new URLSearchParams(Object.entries(F).filter(([, v]) => v)).toString());
  // "#tag" words in the search match tags (all of them); the rest is plain text
  const words = F.q.toLowerCase().split(/\s+/).filter(Boolean), want = words.filter(w => w.startsWith("#") && w.length > 1).map(w => w.slice(1));
  const q = words.filter(w => !w.startsWith("#")).join(" ");
  $("#tagList").innerHTML = TAGS.map(g => `<option value="${esc(g)}">`).join("");
  shown = TX.filter(t => (!F.flow || t.flow === F.flow) && (!F.acct || t.account === F.acct) && (!q || t._hay.includes(q))
    && want.every(g => t.tags.includes(g))
    && (!F.cat || (F.cat === "!review" ? t.review : F.cat === "!mine" ? t.category || Object.keys(t.overrides).length || t.note : eff(t) === F.cat)));
  let out = 0, inn = 0; for (const t of shown) { const v = dollars(t); if (v < 0) out -= v; else inn += v; }
  $("#count").textContent = `${shown.length.toLocaleString()} transactions · ${usd2(out)} out · ${usd2(inn)} in`;
  $("#rows").innerHTML = shown.slice(0, limit).map(txRow).join("");
  drawn = VER + JSON.stringify(F);
  $("#more").hidden = shown.length <= limit;
}
function redrawRow(tr) {
  const i = +tr.dataset.i, open = tr.nextElementSibling?.classList.contains("ed");
  tr.outerHTML = txRow(shown[i], i);
  return open;
}
function flash(tr) { tr.classList.add("saved"); setTimeout(() => tr.classList.remove("saved"), 700); }
function wireRows(c) {
  const rows = $("#rows", c);
  // the kind menu is built when you open it: 300 rows of 45 options each made the page slow
  rows.addEventListener("click", e => {
    const b = e.target.closest("[data-cat]");
    if (!b) return;
    const t = shown[+b.dataset.cat];
    const sel = document.createElement("select");
    sel.className = t.category ? "set" : "";
    sel.innerHTML = catOptions(t.category, t.effective || t.rule);
    b.replaceWith(sel);
    sel.focus();
    try { sel.showPicker(); } catch {}
    const i = b.dataset.cat;
    sel.addEventListener("blur", () => setTimeout(() => { const tr = sel.closest("tr"); if (tr) redrawRow(tr); }, 0), {once: true});
    sel.addEventListener("change", async () => {
      const v = sel.value || null;
      try {
        await api("/api/annotate", {changes: [{id: t.id, category: v}]});
        t.category = v; await refresh([t.id]);
        const row = $(`tr[data-i="${i}"]`, rows);
        if (row) { redrawRow(row); flash($(`tr[data-i="${i}"]`, rows)); }
      } catch (err) { alert("Not saved: " + err.message); }
    }, {once: true});
  });
  rows.addEventListener("change", async e => {
    const inp = e.target.closest("input[data-f]");
    if (!inp) return;
    const tr = inp.closest("tr"), t = shown[+tr.dataset.i], v = inp.value || null;
    try {
      await api("/api/annotate", {changes: [{id: t.id, note: v}]});
      t.note = v; t._hay = hay(t); VER++; drawn = VER + JSON.stringify(F); flash(tr); App.touched();
    } catch (err) { alert("Not saved: " + err.message); }
  });
  rows.addEventListener("click", e => {
    const b = e.target.closest("[data-for]");
    if (!b) return;
    const i = +b.dataset.for, tr = b.closest("tr"), open = tr.nextElementSibling?.classList.contains("ed");
    rows.querySelectorAll("tr.ed").forEach(x => x.remove());
    if (!open) openEditor(tr, i);
  });
}

// ---------- "For": who a purchase served, how long it lasts, what repaid it
const LIFE = [["", "auto"], ["0", "used up"], ["12", "≈1 year"], ["30", "2–3 years"], ["72", "5+ years"]];
function forLabel(t) {
  const life = t.life > 1 ? ` · ${t.life >= 60 ? "5+ yr" : t.life >= 24 ? "2–3 yr" : "1 yr"}` : "";
  if (t.amount > 0) return t.repays.length ? `repays ${t.repays.length}` + (t.overrides.repays ? " ✓" : "") : t.overrides.repays_none ? "not a repayment" : "—";
  if (!t.split) return "me" + life;
  const others = Object.entries(t.split).filter(([k, v]) => k !== "me" && v > 0.001);
  return (t.split.me > 0.001 ? `me ${pct0(t.split.me)} · ` : "") + others.map(([k, v]) => `${k.split(" ")[0]} ${pct0(v)}`).join(" · ") + life;
}
function forEditor(t) {
  const split = t.overrides.for || t.split || {me: 1};
  const linkRow = (l, dir) => { const o = BYID.get(dir === "in" ? l.outflow : l.inflow) || {};
    return `<div class="lk">${dir === "in" ? "Repays" : "Repaid by"} <b>${esc(dir === "in" ? o.desc || "?" : l.person)}</b> ${esc(o.date || "")} · ${usd2(l.amount)}
      <span class="note">${esc(l.method)}${l.conf < 1 ? `, ${pct0(l.conf)} sure` : ""}</span>
      ${dir === "in" ? `<button class="x" data-rej="${esc(l.outflow)}" title="Not this purchase">✕</button>` : ""}</div>`; };
  return `<tr class="ed"><td colspan="6"><div class="edbox">
    ${t.amount < 0 ? `<div><b>Who it served</b> <span class="note">${t.overrides.for ? "set by you" : t.split ? "from repayments" : "you, by default"}</span>
      <div class="shares">${Object.entries(split).map(([k, v]) => `<div class="sh"><input list="pplList" value="${esc(k)}" class="who"><input type="number" min="0" max="100" step="5" value="${Math.round(v * 100)}" class="pc">%</div>`).join("")}</div>
      <datalist id="pplList"><option value="me"><option value="household">${KNOWN.map(k => `<option value="${esc(k)}">`).join("")}</datalist>
      <div class="row"><button data-q="me">Just me</button><button data-q="household">Household</button><button data-q="add">+ person</button>
        <button class="pri" data-save>Save</button>${t.overrides.for ? `<button data-clear>Back to inferred</button>` : ""}</div></div>
      <div><b>Lasts</b> <select class="life">${LIFE.map(([v, l]) => `<option value="${v}" ${String(t.overrides.life_months ?? "") === v ? "selected" : ""}>${l}${v === "" && t.life ? ` (${t.life} months)` : ""}</option>`).join("")}</select></div>
      ${t.repaidBy.map(l => linkRow(l, "out")).join("")}`
    : `<div><b>Pays back</b> ${t.repays.length ? "" : `<span class="note">nothing matched</span>`}${t.repays.map(l => linkRow(l, "in")).join("")}
      <div class="row">${t.repays.length && !t.overrides.repays ? `<button class="pri" data-confirm>Confirm</button>` : ""}
        <button data-none>${t.overrides.repays_none ? "Undo: not a repayment" : "Not a repayment"}</button>${t.overrides.repays ? `<button data-unconfirm>Unconfirm</button>` : ""}</div></div>`}
    <div><b>Tags</b> <span class="note">${(auto => auto.length ? "from rules: " + auto.map(g => "#" + esc(g)).join(" ") + "; -name removes one" : "comma separated")(t.tags.filter(g => !t.myTags.includes(g)))}</span>
      <div class="row"><input class="tagsin" list="tagList" value="${esc(t.myTags.join(", "))}" placeholder="gifts, trip:beach" style="flex:1"><button data-tags>Save tags</button></div></div>
  </div></td></tr>`;
}
function openEditor(tr, i) {
  const t = shown[i];
  tr.insertAdjacentHTML("afterend", forEditor(t));
  const ed = tr.nextElementSibling;
  const read = () => { const o = {}; ed.querySelectorAll(".sh").forEach(r => { const w = $(".who", r).value.trim(), v = +$(".pc", r).value; if (w && v > 0) o[w] = (o[w] || 0) + v / 100; }); return o; };
  const saveTags = v => save(null, {tags: v.split(",").map(x => x.trim()).filter(Boolean)});
  const save = async (ov, top = null) => {
    const linked = [t.id, ...t.repays.map(l => l.outflow), ...t.repaidBy.map(l => l.inflow)];
    ed.style.opacity = .5;
    try {
      await api("/api/annotate", {changes: [{id: t.id, ...(ov ? {overrides: ov} : {}), ...(top || {})}]});
      await refresh(linked);
    } catch (err) { alert("Not saved: " + err.message); }
    const row = $(`#rows tr[data-i="${i}"]`);
    if (!row) return;
    row.nextElementSibling?.classList.contains("ed") && row.nextElementSibling.remove();
    row.outerHTML = txRow(shown[i], i);
    const fresh = $(`#rows tr[data-i="${i}"]`);
    flash(fresh); openEditor(fresh, i);
  };
  ed.querySelectorAll("[data-q]").forEach(q => q.onclick = () => {
    const box = $(".shares", ed);
    if (q.dataset.q === "add") box.insertAdjacentHTML("beforeend", `<div class="sh"><input list="pplList" class="who" placeholder="name"><input type="number" class="pc" value="50">%</div>`);
    else box.innerHTML = `<div class="sh"><input list="pplList" value="${q.dataset.q}" class="who"><input type="number" value="100" class="pc">%</div>`;
  });
  ed.querySelector("[data-save]")?.addEventListener("click", () => save({for: read()}));
  ed.querySelector("[data-tags]").addEventListener("click", () => saveTags($(".tagsin", ed).value));
  ed.querySelector("[data-clear]")?.addEventListener("click", () => save({for: null}));
  $(".life", ed)?.addEventListener("change", ev => save({life_months: ev.target.value === "" ? null : +ev.target.value}));
  ed.querySelector("[data-confirm]")?.addEventListener("click", () => save({repays: t.repays.map(l => ({txn: l.outflow, amount: l.amount}))}));
  ed.querySelector("[data-unconfirm]")?.addEventListener("click", () => save({repays: null}));
  ed.querySelector("[data-none]")?.addEventListener("click", () => save({repays_none: t.overrides.repays_none ? null : true, repays: null}));
  ed.querySelectorAll("[data-rej]").forEach(x => x.onclick = () => save({not_repays: [...(t.overrides.not_repays || []), x.dataset.rej],
    repays: t.overrides.repays ? t.overrides.repays.filter(l => l.txn !== x.dataset.rej) : null}));
}

// ---------------------------------------------------------------- review
const SRC = {you: "set by you", rule: "rule", model: "Claude", fallback: "unsure"};
let RV = [], rvReady = false;
async function reviewPage(c) {
  if (!rvReady) {
    c.innerHTML = `<div class="head"><h1>Review</h1><span class="note" id="rvCount"></span></div>
    <p class="sub">Transactions the rules and the model can't place confidently. Pick a guess, choose a kind, or say in a few words what it was: the model reads your note and tries again.</p>
    <div class="bar"><select id="rvDir"><option value="">In and out</option><option value="out">Money out</option><option value="in">Money in</option></select>
      <select id="rvSort"><option value="amount">Largest first</option><option value="date">Newest first</option></select></div>
    <div id="rvList"></div>`;
    $("#rvDir", c).oninput = rvRender; $("#rvSort", c).oninput = rvRender;
    wireReview(c);
    rvReady = true;
  }
  const d = await App.data("txns", d2 => { useTxns(d2); if (App.page === "review") { RV = TX.filter(t => t.review); rvRender(); } });
  if (d.txns !== TX) useTxns(d);
  RV = TX.filter(t => t.review);
  rvRender();
}
function rvRender() {
  if (App.page !== "review") return;
  const dir = $("#rvDir").value, sort = $("#rvSort").value;
  const xs = RV.filter(t => !dir || (dir === "out" ? t.amount < 0 : t.amount > 0))
    .sort((a, b) => sort === "date" ? b.date.localeCompare(a.date) : Math.abs(dollars(b)) - Math.abs(dollars(a)));
  $("#rvCount").textContent = `${xs.length} to look at`;
  $("#rvList").innerHTML = xs.slice(0, 60).map(t => `<div class="card rv" data-id="${esc(t.id)}" style="margin-bottom:12px">
    <div class="top"><div><b>${esc(t.desc)}</b><div class="note">${t.date} · ${esc(t.accountName)}</div></div>
      <div style="text-align:right"><div class="${t.amount > 0 ? "in" : ""}" style="font-size:17px;font-weight:650;font-variant-numeric:tabular-nums">${money(t)}</div>
      <div class="note">now: ${esc(t.effective || t.rule)}${t.what ? ` · #${esc(t.what)}` : ""} <span class="pill">${SRC[t.source] || ""}</span></div></div></div>
    ${t.orders.map(o => `<p class="note">Amazon: ${o.items.map(esc).join("; ")}${o.forOther ? ` — <b>shipped to ${esc(o.ship_to)}</b>` : ""}</p>`).join("")}
    ${t.model?.why ? `<p class="note">Claude: ${esc(t.model.why)}</p>` : ""}
    <div class="row" style="margin-top:0">${(t.model?.top || []).filter(([k]) => GROUPS[k]).map(([k, p]) => `<button class="guess" data-guess="${esc(k)}">${esc(k)} <span class="note">${pct0(p)}</span></button>`).join("")}
      <select class="pick"><option value="">Other kind…</option></select></div>
    <div class="row"><input class="what" placeholder="What was this? e.g. 'rent to my landlord', 'split dinner, my half was $20'" value="${esc(t.note)}" style="flex:1;min-width:220px">
      <button class="say">Save note</button><span class="note msg"></span></div></div>`).join("") || `<p class="note">Nothing to review.</p>`;
}
function wireReview(c) {
  const list = $("#rvList", c);
  const done = async (card, change, msg) => {
    const t = BYID.get(card.dataset.id);
    $(".msg", card).textContent = "Saving…";
    try {
      await api("/api/annotate", {changes: [{id: t.id, ...change}], reclassify: "note" in change});
      if ("category" in change) { t.category = change.category; RV = RV.filter(x => x.id !== t.id); card.style.opacity = .4; setTimeout(rvRender, 300); refresh([t.id]); return; }
      await refresh([t.id]);
      $(".msg", card).textContent = msg(t);
      if (!t.review) { RV = RV.filter(x => x.id !== t.id); setTimeout(rvRender, 1200); } else rvRender();
    } catch (err) { $(".msg", card).textContent = "Not saved: " + err.message; }
  };
  list.addEventListener("click", e => {
    const card = e.target.closest(".rv"); if (!card) return;
    const g = e.target.closest("[data-guess]");
    if (g) return done(card, {category: g.dataset.guess});
    if (e.target.closest(".say")) return done(card, {note: $(".what", card).value}, f => f.review ? `Still unsure; best guess ${f.model?.top?.[0]?.[0] || "none"}` : `Now: ${f.effective}`);
  });
  // the full kind list is filled in when the menu is first used
  list.addEventListener("focusin", e => { const s = e.target.closest(".pick"); if (s && s.options.length < 2) s.innerHTML = catOptions(null, "Other kind…"); });
  list.addEventListener("pointerdown", e => { const s = e.target.closest(".pick"); if (s && s.options.length < 2) s.innerHTML = catOptions(null, "Other kind…"); });
  list.addEventListener("change", e => { const s = e.target.closest(".pick"); if (s?.value) done(s.closest(".rv"), {category: s.value}); });
}

// ---------------------------------------------------------------- people
async function peoplePage(c) {
  const render = d => {
    if (App.page !== "people") return;
    const hh = new Set(d.household);
    c.innerHTML = `<div class="head"><h1>People</h1></div>
    <p class="sub">Money you fronted for others and what they paid back, from matched repayments and the shares you set. A purchase for the household is split equally among you and the people checked here.</p>
    <div class="card" style="margin-bottom:16px"><div class="top"><h2>Household</h2><button id="hhSave" class="pri">Save</button></div>
      <div class="row" style="margin-top:0">${d.known.map(k => `<label class="note" style="display:flex;gap:5px;align-items:center"><input type="checkbox" value="${esc(k)}" ${hh.has(k) ? "checked" : ""}>${esc(k)}</label>`).join("")}</div></div>
    <div class="grid">${d.people.map(b => `<div class="card"><div class="top"><h2>${esc(b.person)}</h2>
        <span class="pill ${Math.abs(b.net) < 0.5 ? "ok" : ""}">${Math.abs(b.net) < 0.5 ? "settled" : b.net > 0 ? `owes you ${usd2(b.net)}` : `you owe ${usd2(-b.net)}`}</span></div>
      <p>Fronted ${usd2(b.fronted)} · paid back ${usd2(b.repaid)}</p>
      <table>${b.items.slice(0, 15).map(i => `<tr><td class="date">${i.date}</td><td class="desc">${esc(i.desc)}</td><td class="n">${Math.round(i.share * 100)}%</td><td class="n">${usd2(i.amount)}</td></tr>`).join("")}</table>
    </div>`).join("") || `<p class="note">Nothing yet.</p>`}</div>`;
    $("#hhSave", c).onclick = async () => {
      await api("/api/household", {household: [...c.querySelectorAll("input[type=checkbox]:checked")].map(x => x.value)});
      App.touched(); render((await App.load("people")).data);
    };
  };
  render(await App.data("people", render));
}

// ---------------------------------------------------------------- connections
// Every source is an account in dither-accounts: it signs in, pulls on a schedule and keeps the files until `ledger pull`
// fetches them. Sign-ins, live views, passwords and the Monero key images are given on its page; this page only shows
// each account's state (read only), links there, and asks for a fetch.
const fmtT = s => s ? s.slice(0, 10) : "never";
const when = s => s ? new Date(s).toLocaleString([], {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"}) : "";
const pill = (ok, yes, no) => `<span class="pill ${ok ? "ok" : ""}">${ok ? yes : no}</span>`;
const SOURCES = [
  ["simplefin", "Bank and brokerage", "Bank and brokerage accounts, through SimpleFIN.", "simplefin_json"],
  ["privacy", "Privacy.com", "Card purchases with merchant names.", "privacy_json"],
  ["monero", "Monero", "A view-only wallet: it sees payments but can never spend.", "monero_json"],
  ["amazon", "Amazon", "Orders item by item, with who each one shipped to.", "amazon_orders"],
  ["venmo", "Venmo", "Payments with their notes. Venmo signs you out after 30 idle minutes, so the account pulls right after you sign in.", "venmo_csv"],
  ["schwab", "Schwab history", "Positions, reinvestments and realized gains, from Schwab's own History and Realized Gain/Loss exports. Schwab signs you out when idle, so the account pulls right after you sign in.", "schwab_csv"],
];
let poll = 0, openJobs = new Set();
async function connectPage(c) {
  if (!c.dataset.ready) {
    c.innerHTML = `<div class="head"><h1>Connections</h1></div>
    <p class="sub">Where the data comes from. Each source is an account in <a id="daLink" target="_blank" rel="noopener">dither-accounts</a>, which signs in, pulls on its own schedule and keeps the files until the ledger fetches them. Signing in, passwords, watching a pull and Monero key images all happen there; this page only shows how each account is doing.</p>
    <div class="grid" id="cards"><p class="loading">Loading…</p></div>
    <h2 class="sec">Recent jobs</h2><div class="jobs" id="jobs"></div>`;
    c.addEventListener("click", async e => {
      const b = e.target.closest("[data-pull]"); if (!b) return;
      await api("/api/pull", {sources: [b.dataset.pull]}); b.textContent = "Queued"; b.disabled = true; jobsRender(await api("/api/sources"));
    });
    c.addEventListener("toggle", e => { const d = e.target.closest("details[data-job]"); if (d) d.open ? openJobs.add(d.dataset.job) : openJobs.delete(d.dataset.job); }, true);
    c.dataset.ready = "1";
  }
  await connectRender();
  clearInterval(poll);
  poll = setInterval(async () => { if (App.page !== "connect" || document.hidden) return; jobsRender(await api("/api/sources")); }, 4000);
}
function card(title, status, body) { return `<div class="card"><div class="top"><h2>${title}</h2>${status}</div>${body}</div>`; }
function acctPill(x) {
  if (!x) return pill(false, "", "No account");
  if (x.attention) return `<span class="pill">Needs you</span>`;
  if (!x.online) return `<span class="pill">${esc(x.status || "Offline")}</span>`;
  if (x.pulling) return pill(true, "Pulling", "");
  return x.lastPullOk === false ? `<span class="pill">Last pull failed</span>` : pill(true, "Connected", "");
}
async function connectRender() {
  const [s, o] = await Promise.all([api("/api/sources"), api("/api/accounts")]);
  const f = s.files;
  $("#daLink").href = o.page;
  const cardFor = ([src, title, about, parser]) => {
    const x = (o.accounts || {})[src]?.[0];
    const fetched = s.sources[src] === "accounts";
    const notes = [];
    if (o.error) notes.push(esc(o.error));
    if (x?.attention) notes.push(`<b>${esc(x.attention)}</b>`);
    for (const w of x?.waiting || []) notes.push(esc(w));
    if (x && x.lastPullOk === false && x.lastPullError) notes.push(esc(x.lastPullError));
    const pulls = x ? (x.lastPullAt ? `Pulled ${when(x.lastPullAt)}` : "Not pulled yet")
      + (x.nextPullAt && /^\d/.test(x.nextPullAt) ? `, next ${when(x.nextPullAt)}.` : ".") : "";
    const ledger = f[parser] ? ` The ledger has it through ${fmtT(f[parser].through)}.` : "";
    return card(title, acctPill(x), `<p>${about}</p><p class="note">${pulls}${ledger}</p>`
      + (notes.length ? `<p class="note">${notes.join("<br>")}</p>` : "")
      + (fetched ? "" : `<p class="note">Not fetched into the ledger: this source is off in ledger/cli.py SOURCE.</p>`)
      + (src === "monero" ? `<p class="note">Key images (to see what you've spent) are given on the Monero account's page in dither-accounts.</p>
        <form class="cake" style="display:grid;gap:6px;margin-top:4px"><p class="note"><b>Cake Wallet history.</b> Cake's export (Transactions → Export CSV) says which payments you sent, their fees and your notes, which the view-only wallet can't see. Each export restates the whole history; give a fresh one now and then.${f.cake_csv ? ` The ledger has it through ${fmtT(f.cake_csv.through)}.` : ""}</p>
        <div class="row" style="margin-top:0"><input type="file" accept=".csv,text/csv" aria-label="Cake Wallet CSV export"><button class="pri" disabled>Give history</button><span class="note msg"></span></div></form>` : "")
      + `<div class="row">${x ? `<a class="btn ${x.attention ? "pri" : ""}" href="${esc(x.link)}" target="_blank" rel="noopener">${x.liveView ? "Sign in or watch" : "Open"} in dither-accounts</a>`
                              : `<a class="btn pri" href="${esc(o.page)}" target="_blank" rel="noopener">Add it in dither-accounts</a>`}
        <button data-pull="${src}" ${fetched && x ? "" : "disabled"}>Pull now</button></div>`);
  };
  $("#cards").innerHTML = SOURCES.map(cardFor).join("");
  for (const form of document.querySelectorAll("#cards form.cake")) {
    const inp = $("input", form), btn = $("button", form), msg = $(".msg", form);
    inp.onchange = () => { btn.disabled = !inp.files.length; msg.textContent = ""; };
    form.onsubmit = async e => {
      e.preventDefault();
      btn.disabled = true; msg.textContent = "Sending…";
      try {
        const r = await fetch("/api/upload/cake", {method: "POST", headers: {"Content-Type": "text/csv"}, body: inp.files[0]});
        if (r.status === 401) { location.href = "/login"; return; }
        const j = await r.json();
        if (!r.ok) throw new Error(j.error || r.statusText);
        msg.textContent = j.saved ? `Saved (${j.transactions} transactions); reading it in now.` : "Already had this one.";
        form.reset(); jobsRender(await api("/api/sources"));
      } catch (err) { msg.textContent = "Not saved: " + err.message; btn.disabled = false; }
    };
  }
  jobsRender(s);
}
function jobsRender(s) {
  const rows = [...(s.pulls || []).map(j => ({...j, when: j.started})), ...s.jobs.slice().reverse().map(j => ({...j, when: new Date(j.queued * 1000).toLocaleString()}))];
  const html = rows.length ? rows.map(j => { const k = j.name + "|" + j.when;
    return `<details data-job="${esc(k)}" ${openJobs.has(k) ? "open" : ""}><summary class="note" style="cursor:pointer"><b style="color:var(--fg)">${esc(j.name)}</b> · ${j.state} · ${esc(j.when)}</summary><pre>${esc(j.out || "")}</pre></details>`; }).join("") : `<p class="note">Nothing yet.</p>`;
  const jobs = $("#jobs");
  if (jobs && jobs.innerHTML !== html) jobs.innerHTML = html;
}

// ---------------------------------------------------------------- entry
const PAGES = {txns: txPage, review: reviewPage, people: peoplePage, connect: connectPage};
return {
  init(groups) { GROUPS = groups; },
  show: (name, c) => PAGES[name](c),
  has: name => name in PAGES,
};
})();
