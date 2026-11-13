// The portal as one page: the nav switches views without reloading, every view stays built while
// you're elsewhere, and data is held in memory. Coming back to a view shows what it had at once,
// then asks the server whether anything changed (ETag: an unchanged answer costs a few bytes).
const App = (() => {
const ROUTES = {
  "/": ["dash", "overview"], "/overview": ["dash", "overview"], "/spending": ["dash", "spending"], "/income": ["dash", "income"], "/budget": ["dash", "budget"],
  "/txns": ["page", "txns"], "/review": ["page", "review"], "/people": ["page", "people"], "/connect": ["page", "connect"],
};
let busyN = 0;
const busy = d => { busyN += d; $("#busy")?.classList.toggle("on", busyN > 0); };

async function api(path, body) {
  busy(1);
  try {
    const r = await fetch(path, body === undefined ? {cache: "no-store"} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    if (r.status === 401) { location.href = "/login"; throw new Error("signed out"); }
    const j = r.headers.get("content-type")?.includes("json") ? await r.json() : null;
    if (!r.ok) throw new Error(j?.error || r.statusText);
    return j;
  } finally { busy(-1); }
}

// ---------------------------------------------------------------- data: in memory, revalidated by ETag
const cache = {}, etag = {}, inflight = {};
function load(name) {
  if (inflight[name]) return inflight[name];
  busy(1);
  const pre = window.PRE?.name === name && !etag[name] ? window.PRE.p : null;   // started by the page's head
  if (pre) window.PRE = null;
  return inflight[name] = (pre || fetch("/api/" + name, {cache: "no-store", headers: etag[name] ? {"If-None-Match": etag[name]} : {}})).then(async r => {
    if (r.status === 401) { location.href = "/login"; throw new Error("signed out"); }
    if (r.status === 304) return {data: cache[name], changed: false};
    if (!r.ok) throw new Error(r.statusText);
    cache[name] = await r.json(); etag[name] = r.headers.get("ETag");
    return {data: cache[name], changed: true};
  }).finally(() => { delete inflight[name]; busy(-1); });
}
// What we have now (revalidating in the background; onChange gets newer data), or a first fetch.
async function data(name, onChange) {
  if (name in cache) {
    load(name).then(r => { if (r.changed) onChange?.(r.data); }).catch(() => {});
    return cache[name];
  }
  return (await load(name)).data;
}

// ---------------------------------------------------------------- routing
let page = null, dashMounted = false;
const views = {};
function container(name, cls) {
  if (!views[name]) {
    const el = document.createElement("main");
    el.className = cls; el.id = "v-" + name; el.hidden = true;
    $("#views").append(el);
    views[name] = el;
  }
  return views[name];
}
async function route() {
  let path = location.pathname.replace(/\/+$/, "") || "/";
  if (path === "/" && ROUTES["/" + location.hash.slice(1)]?.[0] === "dash") {   // old links: /#spending
    path = "/" + location.hash.slice(1);
    history.replaceState(null, "", path);
  }
  const [kind, name] = ROUTES[path] || ["dash", "overview"];
  const prev = page;
  page = kind === "dash" ? "dash" : name;
  if (prev && prev !== page) scrollTo(0, 0);
  document.querySelectorAll("nav.top a[data-p]").forEach(a => a.classList.toggle("on", a.dataset.p === name));
  for (const [k, el] of Object.entries(views)) el.hidden = k !== (kind === "dash" ? "dash" : name);
  const c = container(kind === "dash" ? "dash" : name, kind === "dash" ? "dash" : "pg");
  c.hidden = false;
  try {
    if (kind === "dash") {
      if (!dashMounted) {
        c.innerHTML = `<p class="loading">Loading…</p>`;
        const d = await data("dashboard", d2 => Dash.setData(d2));
        if (dashMounted) { Dash.show(name); return; }
        Dash.mount(c, d, {portal: true});
        dashMounted = true;
      } else {
        data("dashboard", d2 => Dash.setData(d2));   // show what we have; redraw if the server has newer
      }
      if (page === "dash") Dash.show(name);
    } else {
      if (!c.firstChild) c.innerHTML = `<p class="loading">Loading…</p>`;
      await Pages.show(name, c);
    }
  } catch (e) {
    if (e.message !== "signed out") c.innerHTML = `<p class="loading">Couldn't load this page: ${esc(e.message)}</p>`;
  }
}
function go(href) {
  const u = new URL(href, location.href);
  if (u.origin !== location.origin) return false;
  const path = u.pathname.replace(/\/+$/, "") || "/";
  if (!ROUTES[path]) return false;
  if (u.pathname + u.hash !== location.pathname + location.hash) history.pushState(null, "", u.pathname + u.hash);
  route();
  return true;
}
document.addEventListener("click", e => {
  const a = e.target.closest("a[href]");
  if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || a.target) return;
  if (!(a.closest("nav.top") || a.hasAttribute("data-nav"))) return;
  if (go(a.href)) e.preventDefault();
});
addEventListener("popstate", route);

// ---------------------------------------------------------------- theme
(() => {
  const btn = $("#themeBtn"), root = document.documentElement;
  const label = () => btn.textContent = root.dataset.theme === "dark" ? "☀ Light" : "☾ Dark";
  label();
  btn.onclick = () => {
    root.dataset.theme = root.dataset.theme === "dark" ? "light" : "dark";
    try { localStorage.setItem("ft-theme", root.dataset.theme); } catch {}
    label(); dispatchEvent(new Event("themechange"));
  };
})();

function start() {
  Pages.init(window.GROUPS);
  route().then(() => idle(() => {   // fetch the other big payloads while idle, so the first switch is instant
    for (const n of ["dashboard", "txns"]) if (!(n in cache)) load(n).catch(() => {});
  }));
}
return {api, data, load, start, touched() {}, get page() { return page; }};
})();
App.start();
