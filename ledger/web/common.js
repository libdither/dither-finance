// Helpers shared by the dashboard and the portal pages.
const $ = (s, el = document) => el.querySelector(s);
const sum = xs => { let s = 0; for (const x of xs) s += x; return s; };
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
function compact(v) {
  const a = Math.abs(v);
  const s = a >= 1e6 ? (a / 1e6).toFixed(1) + "M" : a >= 1e4 ? Math.round(a / 1e3) + "K" : a >= 1e3 ? (a / 1e3).toFixed(1) + "K" : Math.round(a).toString();
  return (v < 0 ? "−$" : "$") + s;
}
const _f0 = new Intl.NumberFormat("en-US", {maximumFractionDigits: 0}), _f2 = new Intl.NumberFormat("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});
const usd = v => (v < 0 ? "−$" : "$") + _f0.format(Math.abs(v));
const usd2 = v => (v < 0 ? "−$" : "$") + _f2.format(Math.abs(v));
const day = s => new Date(s + "T12:00:00Z");
const iso = d => d.toISOString().slice(0, 10);
const addDays = (s, n) => iso(new Date(day(s).getTime() + n * 864e5));
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const mlabel = m => MON[+m.slice(5) - 1] + " " + m.slice(0, 4);
function months(a, b) { const out = []; let [y, m] = a.split("-").map(Number); const [yb, mb] = b.split("-").map(Number);
  while (y < yb || (y === yb && m <= mb)) { out.push(`${y}-${String(m).padStart(2, "0")}`); if (++m > 12) { m = 1; y++; } } return out; }
function niceMax(v) { if (v <= 0) return 1; const p = Math.pow(10, Math.floor(Math.log10(v))); for (const k of [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (k * p >= v) return k * p; return 10 * p; }
const width = el => Math.max(300, el.clientWidth);
// a column with rounded data-end (top) and square baseline
function colPath(x, y, w, h, r = 4) { r = Math.min(r, h, w / 2); if (h <= 0) return "";
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`; }
function barPath(x, y, w, h, r = 4) { r = Math.min(r, w, h / 2); if (w <= 0) return "";   // horizontal: rounded right end
  return `M${x},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h - r}Q${x + w},${y + h} ${x + w - r},${y + h}H${x}Z`; }
// January is labeled with its year; the first month too, unless a January follows within 3 months
const xlabel = (ms, i) => ms[i].endsWith("-01") ? ms[i].slice(0, 4)
  : i === 0 && !ms.slice(1, 4).some(m => m.endsWith("-01")) ? `${MON[+ms[i].slice(5) - 1]} ${ms[i].slice(0, 4)}` : "";
const tipRows = rows => rows.map(([k, v]) => `<div class="r"><span>${k}</span><span>${v}</span></div>`).join("");
const tile = (label, value, detail = "", cls = "") =>
  `<div class="tile ${cls}"><div class="l">${label}</div><div class="v">${value}</div>${detail ? `<div class="d">${detail}</div>` : ""}</div>`;
const legend = items => `<div class="legend">${items.map(([n, c]) => `<span style="--c:${c}">${esc(n)}</span>`).join("")}</div>`;
const idle = f => (window.requestIdleCallback || (g => setTimeout(g, 30)))(f, {timeout: 300});

// One tooltip for every chart: anything with data-tip shows it on hover.
(() => {
  let tip;
  document.addEventListener("mousemove", e => {
    tip ||= document.getElementById("tip");
    if (!tip) return;
    const t = e.target.closest?.("[data-tip]");
    if (!t) { if (tip.style.display !== "none") tip.style.display = "none"; return; }
    const html = t.getAttribute("data-tip");
    if (tip._for !== t) { tip.innerHTML = html; tip._for = t; }
    tip.style.display = "block";
    const r = tip.getBoundingClientRect();
    tip.style.left = Math.min(e.clientX + 14, innerWidth - r.width - 6) + "px";
    tip.style.top = (e.clientY + 14 + r.height > innerHeight ? e.clientY - r.height - 10 : e.clientY + 14) + "px";
  }, {passive: true});
})();
