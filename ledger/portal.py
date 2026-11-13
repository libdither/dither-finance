"""`ledger serve`: the finance portal, a small web app over the ledger for your own network.

Pages: the dashboard (built live from the db), a transaction editor that writes annotations
(category, the share that was yours, notes) through to state.jsonl, and Connections: a read-only
list of each source's account as dither-accounts reports it, linked there (sign-ins, live views,
passwords and the Monero account's key images are all given on its page; nothing here manages an
account), with pull buttons (`ledger pull SOURCE` only fetches from it). The boundary: anything about a live account
(signing in, pulling, key images) is dither-accounts'; data you hand over yourself is this app's, so a wallet app's
history export (Cake Wallet's CSV) is uploaded here, on the Monero card.

Auth: a password whose scrypt hash is in secrets/portal_auth.json (`ledger portal-password`),
then an HttpOnly, SameSite=Strict session cookie. POSTs must come from the portal's own origin.
Stdlib only, so it runs anywhere the ledger does.
"""
import fcntl
import gzip
import hashlib
import hmac
import io
import json
import os
import queue
import re
import secrets as pysecrets
import shutil
import subprocess
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import classify, dashboard, flows, jev, merchants, prices
from . import accounts
from . import ingest as I
from .categories import CERTAINTY, GROUPS, categorize, clean_tags, kind_of, tags_for, what_for
from .cli import SOURCE
from .db import DATA, DB_PATH, ROOT, SECRETS, STATE_PATH, connect

AUTH = SECRETS / "portal_auth.json"
SESSIONS = SECRETS / "portal_sessions.json"
LOCK = DATA / ".ledger.lock"
BUDGET = DATA / "budget.json"
HERE = Path(__file__).parent
SESSION_DAYS = 90

@contextmanager
def ledger_lock():
    """Serializes writers: ingest, annotation edits and state export."""
    LOCK.parent.mkdir(exist_ok=True)
    with open(LOCK, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _write_private(path: Path, data: bytes | str):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data.encode() if isinstance(data, str) else data)
    tmp.replace(path)


# ---------------------------------------------------------------- auth

def set_password(pw: str):
    salt = pysecrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    _write_private(AUTH, json.dumps({"salt": salt.hex(), "scrypt": h.hex(), "n": 2 ** 14}))
    _write_private(SESSIONS, "{}")   # a new password signs every device out


def _check_password(pw: str) -> bool:
    if not AUTH.exists():
        return False
    a = json.loads(AUTH.read_text())
    h = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(a["salt"]), n=a["n"], r=8, p=1)
    return hmac.compare_digest(h.hex(), a["scrypt"])


_sess_lock = threading.Lock()


def _sessions() -> dict:
    try:
        return json.loads(SESSIONS.read_text())
    except (OSError, ValueError):
        return {}


def _new_session() -> str:
    tok = pysecrets.token_urlsafe(32)
    with _sess_lock:
        s = {k: v for k, v in _sessions().items() if v > time.time()}
        s[hashlib.sha256(tok.encode()).hexdigest()] = time.time() + SESSION_DAYS * 86400
        _write_private(SESSIONS, json.dumps(s))
    return tok


def _valid(tok: str | None) -> bool:
    if not tok:
        return False
    return _sessions().get(hashlib.sha256(tok.encode()).hexdigest(), 0) > time.time()


# ---------------------------------------------------------------- data

WEB = HERE / "web"


def _key(*paths: Path) -> tuple:
    return tuple(p.stat().st_mtime_ns if p.exists() else 0 for p in paths)


def _data_key() -> tuple:
    """Everything the dashboard and the transaction list are computed from."""
    return _key(DB_PATH, DB_PATH.with_suffix(".db-wal"), classify.DB, classify.DB.with_suffix(".db-wal"), flows.PEOPLE, prices.PATH)


class Payload:
    """A JSON answer kept with its ETag and its gzipped bytes, rebuilt only when its inputs change."""

    def __init__(self, build, key=_data_key):
        self.build, self.keyf, self.key, self.obj, self.lock = build, key, None, None, threading.Lock()

    def get(self):
        with self.lock:
            k = self.keyf()
            if k != self.key:
                self.obj = self.build()
                raw = json.dumps(self.obj, separators=(",", ":")).encode()
                self.raw, self.gz = raw, gzip.compress(raw, 6)
                self.etag = '"' + hashlib.sha256(raw).hexdigest()[:20] + '"'
                self.key = k
            return self


def _dashboard() -> dict:
    con = connect(DB_PATH)
    try:
        return dashboard.build(con)
    finally:
        con.close()


DASH = Payload(_dashboard, key=lambda: _data_key() + _key(BUDGET))


def dashboard_data() -> dict:
    return DASH.get().obj


def transactions() -> list:
    con = connect(DB_PATH)
    try:
        rows = con.execute("""
            SELECT l.txn_id, l.date, l.account_id, l.description, l.kind, l.amount_f, l.flow, l.category, l.note,
                   l.tags, l.counterparty, a.overrides,
                   coalesce(n.name, l.account_id) AS account_name, ac.base_asset, ac.kind AS acct_kind
            FROM ledger l JOIN account ac ON ac.id = l.account_id LEFT JOIN txn_annotation a ON a.txn_id = l.txn_id
            LEFT JOIN account_annotation n ON n.account_id = l.account_id
            WHERE l.account_id NOT LIKE 'ext:%' ORDER BY l.date DESC, l.txn_id""").fetchall()
        model = classify.results()
        m = merchants.match(con)
        waiting = {r[0] for r in con.execute("SELECT txn_id FROM posting WHERE account_id = 'ext:unreconciled'")}
        fl = flows.analyze(con)
    finally:
        con.close()
    by_in, by_out = {}, {}
    for l in fl["links"]:
        by_in.setdefault(l["inflow"], []).append(l)
        by_out.setdefault(l["outflow"], []).append(l)
    subs = {(x["merchant"], x["category"]) for x in dashboard_data()["recurring"]}
    out = []
    for r in rows:
        ext = r["flow"] == "external"
        mine = json.loads(r["tags"]) if r["tags"] else []
        rule, merchant = categorize(r["description"], r["account_id"], r["kind"], r["amount_f"], r["acct_kind"])
        x = model.get(r["txn_id"]) if ext else None
        eff, src = classify.effective(rule, merchant, r["category"], x, r["amount_f"] or 0) if ext else (None, None)
        orders = [{"items": [i.get("title") for i in o.get("items", [])][:6], "ship_to": o.get("ship_to"), "forOther": o.get("for_other")}
                  for o in m["by_txn"].get(r["txn_id"], [])]
        coin = (r["base_asset"] or "").startswith("crypto:")
        sym = r["base_asset"].split(":")[1] if coin else "USD"
        what = what_for(rule, mine) if ext else None
        px = prices.price(sym, r["date"][:10]) if coin else None
        out.append({"id": r["txn_id"], "date": r["date"][:10], "account": r["account_id"], "accountName": r["account_name"],
                    "desc": r["description"] or "", "amount": round(r["amount_f"] or 0, 8 if coin else 2), "flow": r["flow"],
                    "cur": sym, "usd": round((r["amount_f"] or 0) * px, 2) if px else None if coin else round(r["amount_f"] or 0, 2),
                    # rule: the kind the rules give it; ruleCat: their category; category: the kind you set; what: what it was
                    "rule": kind_of(rule) if ext else None, "ruleCat": rule if ext else None, "category": r["category"],
                    "what": what, "merchant": merchant,
                    "note": r["note"], "overrides": json.loads(r["overrides"]) if r["overrides"] else {},
                    "myTags": mine, "tags": tags_for(r["description"], r["account_id"], mine,
                                                     (*((what,) if what else ()), *(("subscription",) if ext and (merchant, eff) in subs else ()))),
                    "split": fl["split"].get(r["txn_id"]), "life": fl["life"].get(r["txn_id"]),
                    "repays": by_in.get(r["txn_id"], []), "repaidBy": by_out.get(r["txn_id"], []),
                    "effective": eff, "source": src,
                    "model": {"top": sorted(x["probs"].items(), key=lambda kv: -kv[1])[:3], "p": x["p"], "why": x.get("why")} if x else None,
                    # coins coming in don't count until labeled (dashboard.build), so they aren't asked about
                    "review": bool(ext and r["acct_kind"] != "brokerage" and r["txn_id"] not in waiting
                                   and not (coin and (r["amount_f"] or 0) > 0)
                                   and r["txn_id"] not in fl["repaid_in"] and classify.needs_review(rule, src, x)),
                    "orders": orders})
    return out


TXNS = Payload(lambda: {"txns": transactions()})


def _people() -> dict:
    con = connect(DB_PATH)
    try:
        fl = flows.analyze(con)
        known = sorted({flows._party(r)[0] for r in flows._rows(con) if flows._party(r)[0]})
    finally:
        con.close()
    return {"people": fl["people"], "household": fl["household"], "known": known}


PEOPLE = Payload(_people)
PAYLOADS = {"dashboard": DASH, "txns": TXNS, "people": PEOPLE}


def reclassify(ids: list[str]) -> dict:
    con = connect(DB_PATH)
    try:
        return classify.run(con, ids=set(ids), force=True, log=lambda *a: None)
    finally:
        con.close()


def _shares(v: dict) -> dict:
    out = {str(k)[:80]: round(float(x), 4) for k, x in v.items() if float(x) > 0}
    if not out:
        raise ValueError("for needs at least one share")
    return out


def _links(v: list) -> list:
    return [{"txn": str(x["txn"]), "amount": str(round(float(x["amount"]), 2))} for x in v]


# what the portal may set in txn_annotation.overrides (flows.py reads them), with a validator each
OVERRIDES = {"for": _shares, "life_months": lambda v: max(0, int(v)), "repays": _links,
             "repays_none": lambda v: bool(v) or None, "not_repays": lambda v: [str(x) for x in v]}


def annotate(changes: list[dict]) -> int:
    """Each change: {id, category?, note?, overrides?: {key: value or null}}; null clears a field."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with ledger_lock():
        con = connect(DB_PATH)
        try:
            with con:
                for c in changes:
                    tid = c["id"]
                    if "category" in c and c["category"] is not None and c["category"] not in GROUPS:
                        raise ValueError(f"unknown kind {c['category']!r}")
                    a = con.execute("SELECT * FROM txn_annotation WHERE txn_id=?", (tid,)).fetchone()
                    cat, note, tags = (a["category"], a["note"], a["tags"]) if a else (None, None, None)
                    ov = json.loads(a["overrides"]) if a and a["overrides"] else {}
                    if "category" in c:
                        cat = c["category"] or None
                    if "note" in c:
                        note = (c["note"] or "").strip() or None
                    if "tags" in c:
                        tags = json.dumps(clean_tags(c["tags"])) if clean_tags(c["tags"]) else None
                    for k, v in (c.get("overrides") or {}).items():
                        if k not in OVERRIDES:
                            raise ValueError(f"unknown override {k!r}")
                        if v is None:
                            ov.pop(k, None)
                        else:
                            ov[k] = OVERRIDES[k](v)
                    if not (cat or note or tags or ov):
                        con.execute("DELETE FROM txn_annotation WHERE txn_id=?", (tid,))
                        continue
                    con.execute("""INSERT OR REPLACE INTO txn_annotation(txn_id, category, tags, note, overrides, updated_at)
                                   VALUES (?,?,?,?,?,?)""", (tid, cat, tags, note, json.dumps(ov, sort_keys=True) if ov else None, now))
            I.export_state(con, STATE_PATH)
        finally:
            con.close()
    return len(changes)


CAKE_DIR = DATA / "raw" / "cake"


def monero_wallets() -> list[str]:
    """The view-only wallets the ledger knows, by their address's first 8 (how parsers/monero.py names them)."""
    con = connect(DB_PATH)
    try:
        return sorted(r[0].removeprefix("wallet-") for r in con.execute(
            "SELECT DISTINCT ext_key FROM account_alias WHERE feed = 'monero' AND ext_key LIKE 'wallet-%'"))
    finally:
        con.close()


def upload_cake(data: bytes, wallet: str | None) -> dict:
    """Cake Wallet's history export, given on the portal: saved byte for byte as data/raw/cake/cake-<wallet>-<stamp>-
    <sha8>.csv (the file doesn't say which wallet it is; its name does, for parsers/cake_csv.py), then ingested."""
    from .parsers import cake_csv
    if not data.decode("utf-8", "replace").lstrip("\ufeff").startswith(cake_csv.HEADER):
        raise ValueError("that isn't Cake Wallet's CSV export (its first line starts record_type,date_time,type,amount)")
    wallets = monero_wallets()
    if wallet is None and len(wallets) == 1:
        wallet = wallets[0]
    if wallet not in wallets:
        raise ValueError("which wallet is it? " + (", ".join(wallets) or "the ledger has no Monero wallet yet"))
    sha = hashlib.sha256(data).hexdigest()
    for f in CAKE_DIR.glob("cake-*.csv"):
        if hashlib.sha256(f.read_bytes()).hexdigest() == sha:
            return {"saved": False, "name": f.name, "reason": "the ledger already has this export"}
    name = f"cake-{wallet}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{sha[:8]}.csv"
    _write_private(CAKE_DIR / name, data)
    cmds = [["ingest"]]
    if classify.claude.available():   # the new rows get the model's opinion, as after a pull
        cmds.append([sys.executable, "-m", "ledger.cli", "classify"])
    JOBS.submit("Cake Wallet history", cmds)
    return {"saved": True, "name": name, "transactions": sum(1 for l in data.decode("utf-8", "replace").splitlines()
                                                             if l.startswith("transaction,"))}


def sources() -> dict:
    con = connect(DB_PATH)
    try:
        files = {r["parser"]: {"files": r["n"], "last": r["last"], "through": r["hi"]} for r in con.execute(
            """SELECT parser, count(*) n, max(first_seen) last, max(coverage_hi) hi FROM source_file
               WHERE present GROUP BY parser""")}
        asof = con.execute("SELECT max(date) FROM ledger").fetchone()[0]
    finally:
        con.close()
    return {"files": files, "asof": asof, "sources": SOURCE, "jobs": JOBS.status(), "pulls": pull_status()}


_overview: dict = {}


def account_overview() -> dict:
    """accounts.overview(), kept 10 s so the page's polling doesn't open a socket each time."""
    if time.time() - _overview.get("at", 0) > 10:
        _overview.update(at=time.time(), v=accounts.overview())
    return _overview["v"]


# ---------------------------------------------------------------- jobs

class Jobs:
    """One job at a time, in order; keeps the tail of each job's output (pullers print counts only)."""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.log: list[dict] = []
        self.running: dict | None = None
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, name: str, cmds: list[list[str]]):
        job = {"name": name, "state": "queued", "queued": time.time(), "out": ""}
        self.log = (self.log + [job])[-20:]
        self.q.put((job, cmds))
        return job

    def status(self):
        return [{k: j[k] for k in ("name", "state", "queued", "out") if k in j} | {"ended": j.get("ended")} for j in self.log]

    def _run(self):
        while True:
            job, cmds = self.q.get()
            job["state"] = "running"
            ok = True
            for cmd in cmds:
                try:
                    if cmd[0] == "ingest":   # takes the ledger lock itself
                        p = subprocess.run([sys.executable, "-m", "ledger.cli", "ingest"], cwd=ROOT,
                                           capture_output=True, text=True, timeout=1800)
                    else:
                        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=1800)
                    job["out"] = (job["out"] + p.stdout[-3000:] + p.stderr[-2000:])[-6000:]
                    if p.returncode:
                        ok = False
                        break
                except Exception as e:  # noqa: BLE001 - reported to the page
                    job["out"] += f"\n{type(e).__name__}: {e}"
                    ok = False
                    break
            job["state"], job["ended"] = ("done" if ok else "failed"), time.time()


JOBS = Jobs()


def pull(names: list[str]):
    """Pulls run as their own systemd units (finance-pull@SOURCE: `ledger pull SOURCE`, which asks the account in
    dither-accounts to pull now and fetches what it has), so restarting the portal doesn't cut one short."""
    started = []
    for n in names:
        if SOURCE.get(n) == "accounts":
            subprocess.run(["systemctl", "--user", "start", "--no-block", f"finance-pull@{n}.service"], check=True)
            started.append(n)
    return {"started": started}


_logs: dict = {}


def pull_status() -> list[dict]:
    units = [f"finance-pull@{n}.service" for n in SOURCE]
    shown = subprocess.run(["systemctl", "--user", "show", *units, "-p",
                            "Id,ActiveState,Result,ExecMainStartTimestamp,ExecMainExitTimestamp"],
                           capture_output=True, text=True).stdout
    out = []
    for block in shown.strip().split("\n\n"):
        st = dict(l.split("=", 1) for l in block.splitlines() if "=" in l)
        unit = st.get("Id")
        if not unit or not st.get("ExecMainStartTimestamp"):
            continue   # never ran since boot
        state = "running" if st.get("ActiveState") == "activating" else "done" if st.get("Result") == "success" else "failed"
        k = (st["ExecMainStartTimestamp"], st.get("ExecMainExitTimestamp"), state)
        if state == "running" or _logs.get(unit, (None,))[0] != k:   # the journal is read again only when the run changed
            log = subprocess.run(["journalctl", "--user", "-u", unit, "-n", "40", "-o", "cat", "--no-pager"],
                                 capture_output=True, text=True).stdout
            _logs[unit] = (k, log[-6000:])
        out.append({"name": f"pull {unit.split('@')[1].split('.')[0]}", "state": state, "started": st["ExecMainStartTimestamp"],
                    "out": _logs[unit][1]})
    return out


# ---------------------------------------------------------------- http

_static: dict = {}


def static_file(name: str):
    """(bytes, gzipped, version) of a file in ledger/web, re-read when it changes."""
    p = WEB / name
    if p.parent != WEB or not p.is_file():
        return None
    k = p.stat().st_mtime_ns
    if _static.get(name, (None,))[0] != k:
        b = p.read_bytes()
        _static[name] = (k, b, gzip.compress(b, 9), hashlib.sha256(b).hexdigest()[:12])
    return _static[name][1:]


PRELOAD = {"txns": "txns", "review": "txns", "people": "people", "connect": "sources"}


def shell(path: str) -> str:
    """The one page every route serves: the app picks its view from the URL."""
    html = static_file("index.html")[0].decode()
    html = re.sub(r"\{\{v:([\w.]+)\}\}", lambda m: static_file(m.group(1))[2], html)
    return html.replace("{{preload}}", PRELOAD.get(path.strip("/"), "dashboard")).replace("{{groups}}", json.dumps(GROUPS))


ROUTES = {"/", "/overview", "/spending", "/income", "/budget", "/txns", "/review", "/people", "/connect"}
TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".html": "text/html; charset=utf-8"}


LOGIN = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Finance portal</title><style>body{font:15px system-ui;display:grid;place-items:center;height:90vh;margin:0}
form{display:flex;flex-direction:column;gap:10px;width:260px}input,button{font:inherit;padding:8px;border-radius:6px;border:1px solid #bbb}
p{color:#b33;margin:0;min-height:1.2em}@media(prefers-color-scheme:dark){body{background:#161616;color:#eee}input{background:#222;color:#eee;border-color:#444}}</style>
<form method=post action=login><b>Finance portal</b><input type=password name=password id=password autofocus placeholder=Password
autocomplete=current-password><button>Sign in</button><p>%s</p></form>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "ledger-portal"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):   # paths only: no query strings or bodies
        path = getattr(self, "path", None)   # unset when the request line itself was bad
        sys.stderr.write("%s %s\n" % (getattr(self, "command", "-"), urlparse(path).path if path else "(bad request)"))

    # ------------------------------------------------------------ plumbing
    def _gzip_ok(self) -> bool:
        return "gzip" in (self.headers.get("Accept-Encoding") or "")

    def _send(self, code, body=b"", ctype="text/html; charset=utf-8", headers=(), gz=None, cache="no-store"):
        """gz: the body already compressed (cached payloads); otherwise text over 1 KB is compressed here."""
        if isinstance(body, str):
            body = body.encode()
        if self._gzip_ok() and (gz is not None or (len(body) > 1024 and ctype.split(";")[0] in
                                                   ("text/html", "application/json", "text/javascript", "text/css", "text/plain"))):
            body = gz if gz is not None else gzip.compress(body, 6)
            headers = [*headers, ("Content-Encoding", "gzip"), ("Vary", "Accept-Encoding")]
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")   # "no-referrer" makes form POSTs send Origin: null
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json")

    def _body(self, limit=50 << 20) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("too large")
        return self.rfile.read(n)

    def _authed(self) -> bool:
        if self.server.dev:
            return True
        c = SimpleCookie(self.headers.get("Cookie") or "")
        return _valid(c["ft"].value if "ft" in c else None)

    def _same_origin(self) -> bool:
        o = self.headers.get("Origin")
        return o is None or urlparse(o).netloc == self.headers.get("Host")

    # ------------------------------------------------------------ routes
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        if path == "/login":
            return self._send(200, LOGIN % "")
        if not self._authed():
            if path.startswith("/api/"):
                return self._json({"error": "signed out"}, 401)
            return self._send(303, headers=[("Location", "/login")])
        try:
            if path in ROUTES:
                return self._send(200, shell(path))
            if path.startswith("/static/"):
                f = static_file(path[len("/static/"):])
                if not f:
                    return self._send(404, "not found", "text/plain")
                # the shell names each file with its hash (?v=), so a version never changes: cache it for good
                return self._send(200, f[0], TYPES.get(Path(path).suffix, "application/octet-stream"), gz=f[1] if self._gzip_ok() else None,
                                  cache="private, max-age=31536000, immutable")
            name = path[len("/api/"):] if path.startswith("/api/") else None
            if name in PAYLOADS:
                q = parse_qs(u.query)
                if name == "txns" and "ids" in q:   # just these rows, after an edit
                    ids = set(q["ids"][0].split(","))
                    return self._json({"txns": [t for t in TXNS.get().obj["txns"] if t["id"] in ids]})
                pl = PAYLOADS[name].get()
                if self.headers.get("If-None-Match") == pl.etag:
                    return self._send(304, headers=[("ETag", pl.etag)])
                return self._send(200, pl.raw, "application/json", headers=[("ETag", pl.etag)], gz=pl.gz if self._gzip_ok() else None)
            if path == "/api/sources":
                return self._json(sources())
            if path == "/api/accounts":
                return self._json(account_overview())
            if path == "/dashboard.html":   # a standalone copy to keep
                return self._send(200, dashboard.standalone(dashboard_data()),
                                  headers=[("Content-Disposition", "attachment; filename=dashboard.html")])
            return self._send(404, "not found", "text/plain")
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return self._send(500, "server error (see the service log)", "text/plain")

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/")
        if not self._same_origin():
            return self._send(403, "cross-origin", "text/plain")
        if path == "/login":
            form = parse_qs(self._body(4096).decode())
            if _check_password((form.get("password") or [""])[0]):
                tok = _new_session()
                secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""   # behind a TLS reverse proxy
                return self._send(303, headers=[("Location", "/"), ("Set-Cookie",
                    f"ft={tok}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_DAYS * 86400}{secure}")])
            time.sleep(1.5)
            return self._send(401, LOGIN % ("Wrong password." if AUTH.exists() else "No password set: run `ledger portal-password`."))
        if not self._authed():
            return self._json({"error": "signed out"}, 401)
        try:
            if path == "/logout":
                return self._send(303, headers=[("Location", "/login"), ("Set-Cookie", "ft=; Path=/; Max-Age=0")])
            if path == "/api/upload/cake":   # the file itself is the body
                return self._json(upload_cake(self._body(16 << 20), parse_qs(u.query).get("wallet", [None])[0]))
            body = json.loads(self._body(1 << 20) or b"{}")
            if path == "/api/annotate":
                n = annotate(body["changes"])
                notes = [c["id"] for c in body["changes"] if "note" in c]
                if notes and body.get("reclassify"):   # a note is context: ask the model again
                    reclassify(notes)
                return self._json({"saved": n})
            if path == "/api/budget":
                _write_private(BUDGET, json.dumps(body, indent=1))
                return self._json({"saved": True})
            if path == "/api/household":
                _write_private(flows.PEOPLE, json.dumps({**flows.people(), "household": [str(x) for x in body["household"]]}))
                return self._json({"saved": True})
            if path == "/api/pull":
                return self._json(pull(body["sources"]))
            return self._send(404, "not found", "text/plain")
        except (ValueError, KeyError) as e:
            return self._json({"error": str(e)}, 400)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return self._json({"error": "server error"}, 500)


def serve(host: str, port: int, dev: bool = False):
    """dev skips sign-in, for looking at pages locally; it only binds to loopback."""
    os.umask(0o077)
    if dev and host not in ("127.0.0.1", "::1", "localhost"):
        sys.exit("--dev only binds to loopback")
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.dev = dev
    print(f"portal on http://{host}:{port}/", flush=True)
    httpd.serve_forever()
