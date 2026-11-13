"""dither-accounts (an accounts manager: it signs in to each account and pulls its files) as a source of raw files.

FinanceTracker is an app there with the `statements` interface at read on its finance accounts. A pull asks each
granted account for the files it pulled since our cursor, saves each byte for byte as data/raw/<dir>/<name> (under
the names the parsers expect), acks them, and leaves ingest to `ledger pull` as before.

  uv run python -m ledger.accounts register      # once: ask for the grant (approve it on the accounts page)
  uv run python -m ledger.accounts status        # the app, its grant, the accounts it sees (no amounts)
  uv run python -m ledger.accounts fetch [SOURCE…] [--refresh]   # what `ledger pull` does for switched sources

FinanceTracker manages no account: it only reads `statements`. The portal's Connections page shows each account's
state from here (overview, read only) and links to the accounts page, where sign-ins, live views, passwords and the
Monero account's key images are given.

The app token is in secrets/accounts-app.json (600); cursors in data/accounts_cursor.json ({account: seq}). Cursors
are delivery bookkeeping, not evidence: losing them only re-fetches, and a re-fetched file is skipped (same sha).
stdlib only: a small WebSocket client for the manager's /consumer socket (loopback, no TLS).
"""
import argparse
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import config
from .db import DATA, SECRETS

URL = config.get("accounts", "url")
# The accounts page, for links (sign-ins and live views happen there).
PAGE = (config.get("accounts", "page") or URL).rstrip("/")
APP_FILE = SECRETS / "accounts-app.json"
CURSORS = DATA / "accounts_cursor.json"
RAW = DATA / "raw"
# FinanceTracker's source names -> dither-accounts kinds.
KINDS = {"simplefin": "simplefin", "monero": "monero", "privacy": "privacy", "venmo": "venmo", "amazon": "amazon",
         "schwab": "schwab"}
# How long `ledger pull` waits for an account's own pull before fetching what is there.
WAIT = {"simplefin": 600, "monero": 3 * 3600, "privacy": 900, "venmo": 900, "amazon": 3600, "schwab": 1800}
# The daily run asks an account to pull only when its last pull is older than this (it pulls daily by itself).
STALE_S = 18 * 3600


def _write_private(path: Path, data: bytes):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def app() -> dict | None:
    return json.loads(APP_FILE.read_text()) if APP_FILE.exists() else None


def register(kinds: list[str]):
    """Register FinanceTracker (once) asking for `statements` read on these kinds; the request waits on the page."""
    if app():
        sys.exit(f"already registered ({APP_FILE.name}); ask for more with: request {' '.join(kinds)}")
    body = {"name": "FinanceTracker", "describe": "Ledger and dashboard: saves your financial accounts' pulled files as evidence (read only).",
            "requests": [_request(kinds)]}
    r = _post("/api/apps/register", body)
    _write_private(APP_FILE, json.dumps({"url": URL, "app": r["app"], "token": r["token"]}).encode())
    print(f"registered as {r['app']}; {len(r['requests'])} request(s) wait on the accounts page")


def request(kinds: list[str]):
    a = app() or sys.exit("not registered")
    r = _post(f"/api/apps/{a['app']}/requests", {"requests": [_request(kinds)]}, token=a["token"])
    print(f"{len(r['requests'])} request(s) wait on the accounts page")


def _request(kinds):
    return {"interface": "statements", "level": "read", "kinds": [KINDS[k] for k in kinds],
            "reason": "save each pull's files as the ledger's raw evidence"}


def _post(path, body, token=None):
    req = urllib.request.Request(URL + path, json.dumps(body).encode(), {"content-type": "application/json",
                                 **({"authorization": f"Bearer {token}"} if token else {})})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


# ---------------------------------------------------------------- the /consumer socket (RFC 6455, client side)

class Consumer:
    """One connection to the manager as this app: calls, and the events and statuses that arrive meanwhile."""

    def __init__(self, a: dict, timeout=30):
        host, port = a["url"].split("://", 1)[1].split("/")[0].rsplit(":", 1)
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        q = urllib.parse.urlencode({"app": a["app"], "token": a["token"]})
        self.sock.sendall((f"GET /consumer?{q} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("the manager closed the socket")
            head += chunk
        head, self.buf = head.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise ConnectionError(head.split(b"\r\n")[0].decode(errors="replace"))
        want = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        if want.encode() not in head:
            raise ConnectionError("bad websocket handshake")
        self.n = 0
        self.accounts: list[dict] = []
        self.events: list[tuple[str, dict]] = []   # (account, event)
        self.status: dict[str, dict] = {}

    def _frame(self):
        """One whole frame from the buffer, or None; nothing is taken until all of it is there (so a timeout
        between reads never loses half a frame)."""
        b = self.buf
        if len(b) < 2:
            return None
        n, i = b[1] & 0x7F, 2
        if n == 126:
            if len(b) < 4:
                return None
            n, i = struct.unpack("!H", b[2:4])[0], 4
        elif n == 127:
            if len(b) < 10:
                return None
            n, i = struct.unpack("!Q", b[2:10])[0], 10
        if len(b) < i + n:
            return None
        self.buf = b[i + n:]
        return b[0], b[i:i + n]   # the server does not mask

    def _send(self, op: int, data: bytes):
        mask = os.urandom(4)
        n = len(data)
        hdr = bytes([0x80 | op]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack("!H", n)
                                     if n < 65536 else bytes([0x80 | 127]) + struct.pack("!Q", n))
        self.sock.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _message(self) -> dict:
        parts = []
        while True:
            fr = self._frame()
            if fr is None:
                chunk = self.sock.recv(1 << 20)
                if not chunk:
                    raise ConnectionError("the manager closed the socket")
                self.buf += chunk
                continue
            b0, data = fr
            op = b0 & 0x0F
            if op == 0x9:
                self._send(0xA, data)
            elif op == 0x8:
                raise ConnectionError("the manager closed the socket")
            elif op in (0x0, 0x1, 0x2):
                parts.append(data)
                if b0 & 0x80:
                    return json.loads(b"".join(parts))

    def _take(self, m: dict):
        if m.get("t") == "accounts":
            self.accounts = m["accounts"]
        elif m.get("t") == "events":
            for e in m["events"]:
                self.events.append((m["account"], e))
                if e.get("t") == "status":
                    self.status[m["account"]] = e

    def pump(self, seconds: float):
        """Read what arrives for this long (events, statuses)."""
        end = time.time() + seconds
        while (left := end - time.time()) > 0:
            self.sock.settimeout(left)
            try:
                m = self._message()
            except (socket.timeout, TimeoutError):
                return
            self._take(m)
        self.sock.settimeout(None)

    def settle(self, most: float = 3.0):
        """Read until the accounts list and every account's status are in (or `most` seconds)."""
        end = time.time() + most
        while time.time() < end and not (self.accounts and all(x["id"] in self.status for x in self.accounts if x.get("online"))):
            self.pump(min(0.25, end - time.time()))

    def call(self, account: str, op: str, inp: dict | None = None, timeout=120):
        self.n += 1
        rid = f"ft{self.n}"
        self._send(0x1, json.dumps({"t": "call", "id": rid, "account": account, "op": op, "input": inp or {}}).encode())
        self.sock.settimeout(timeout)
        try:
            while True:
                m = self._message()
                if m.get("t") == "result" and m.get("id") == rid:
                    if not m["ok"]:
                        e = m["error"]
                        raise RuntimeError(f"{op}: {e.get('code')}: {e.get('message')}")
                    return m["result"]
                self._take(m)
        finally:
            self.sock.settimeout(None)

    def close(self):
        try:
            self._send(0x8, b"\x03\xe8")
        except OSError:
            pass
        self.sock.close()


# ---------------------------------------------------------------- fetch

def _cursors() -> dict:
    return json.loads(CURSORS.read_text()) if CURSORS.exists() else {}


def _target(f: dict) -> Path:
    d, name = f["dir"], f["name"]
    if "/" in name or name.startswith(".") or any(p in ("..", ".", "") for p in d.split("/") if d) or d.startswith("/"):
        raise ValueError("a file name or dir that leaves data/raw")
    return RAW / d / name if d else RAW / name


def _iso_age(s: str | None) -> float | None:
    if not s:
        return None
    from datetime import datetime
    return time.time() - datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def granted(c: Consumer, sources: list[str]) -> list[tuple[str, dict]]:
    want = {KINDS[s]: s for s in sources}
    return [(want[a["kind"]], a) for a in c.accounts if a["kind"] in want and a.get("grants", {}).get("statements")]


def fetch(sources: list[str], refresh: str = "stale", log=print) -> dict:
    """For each granted account of these sources: maybe ask it to pull now (refresh: "always", "stale" or "never"),
    then save every file after our cursor into data/raw and ack. Returns {source: {"saved", "skipped", "error"?}}."""
    a = app()
    if not a:
        return {s: {"error": "FinanceTracker is not registered with dither-accounts"} for s in sources}
    os.umask(0o077)
    c = Consumer(a)
    out = {s: {"saved": 0, "skipped": 0} for s in sources}
    try:
        c.pump(1.5)   # the accounts list and each account's status
        accts = granted(c, sources)
        for s in sources:
            if not any(src == s for src, _ in accts):
                out[s]["error"] = "no account with a statements grant"
        for src, acct in accts:
            if not acct["online"]:
                log(f"{src}: account {acct['id']} is not online; fetching what it already pulled")
            elif refresh != "never":
                d = (c.status.get(acct["id"]) or {}).get("details") or {}
                age = _iso_age(d.get("lastPullAt"))
                if refresh == "always" or age is None or age > STALE_S:
                    seen = len(c.events)
                    r = c.call(acct["id"], "statements.refresh", {})
                    log(f"{src}: refresh {'started' if r.get('started') else 'not started: ' + str(r.get('reason'))}")
                    if r.get("started"):
                        end = time.time() + WAIT[src]
                        while time.time() < end and not any(acc == acct["id"] and e.get("t") == "statements.pulled" for acc, e in c.events[seen:]):
                            c.pump(min(5, end - time.time()))
                        p = next((e for acc, e in c.events[seen:] if acc == acct["id"] and e.get("t") == "statements.pulled"), None)
                        log(f"{src}: pull {'ok' if p and p.get('ok') else 'failed' if p else 'still running'}"
                            + (f", {p.get('saved')} file(s)" if p else ""))
                        if p and not p.get("ok"):
                            out[src]["error"] = str(p.get("needs") or "pull failed")
            _fetch_account(c, src, acct, out[src], log)
    finally:
        c.close()
    return out


def _fetch_account(c: Consumer, src: str, acct: dict, res: dict, log):
    cur = _cursors()
    after = int(cur.get(acct["id"], 0))
    while True:
        page = c.call(acct["id"], "statements.list", {"after": after, "limit": 200})
        if not page["files"]:
            break
        for f in page["files"]:
            path = _target(f)
            if path.exists():
                if hashlib.sha256(path.read_bytes()).hexdigest() == f["sha256"]:
                    res["skipped"] += 1
                else:   # never overwrite evidence; stop here so the cursor does not pass this file
                    res["error"] = f"{path.relative_to(DATA)} exists with other bytes; not overwritten"
                    log(f"{src}: {res['error']}")
                    return
            else:
                r = c.call(acct["id"], "statements.fetch", {"id": f["id"]}, timeout=300)
                data = base64.b64decode(r["base64"])
                if hashlib.sha256(data).hexdigest() != f["sha256"] or len(data) != f["size"]:
                    res["error"] = f"{f['name']}: bytes do not match their hash"
                    return
                _write_private(path, data)
                res["saved"] += 1
            after = f["seq"]
            cur[acct["id"]] = after
            _write_private(CURSORS, json.dumps(cur).encode())
        c.call(acct["id"], "statements.ack", {"through": after})
    log(f"{src}: saved {res['saved']}, already had {res['skipped']} (cursor {after})")


def overview() -> dict:
    """The finance accounts as dither-accounts sees them, per source, for the portal: state, attention, last and next
    pull, and a link to each on the accounts page. No amounts (statuses carry none)."""
    a = app()
    if not a:
        return {"error": "FinanceTracker is not registered with dither-accounts", "page": PAGE}
    try:
        c = Consumer(a, timeout=5)
    except OSError as e:
        return {"error": f"dither-accounts is not answering: {e}", "page": PAGE}
    try:
        c.settle()
        src_of = {k: s for s, k in KINDS.items()}
        out: dict[str, list] = {}
        for acct in c.accounts:
            if acct["kind"] not in src_of:
                continue
            st = c.status.get(acct["id"]) or {}
            d = st.get("details") or {}
            att = st.get("attention")
            waiting = [f"{l.get('name')}: {l.get('note') or l.get('state')}" for l in st.get("layers") or []
                       if l.get("state") == "failed"]
            out.setdefault(src_of[acct["kind"]], []).append({
                "id": acct["id"], "label": acct.get("label"), "online": bool(acct.get("online")),
                "status": st.get("status"), "attention": (att.get("message") or att.get("kind")) if isinstance(att, dict) else att,
                "waiting": waiting[:3], "pulling": d.get("pulling"), "lastPullAt": d.get("lastPullAt"),
                "lastPullOk": d.get("lastPullOk"), "lastPullError": d.get("lastPullError"), "nextPullAt": d.get("nextPullAt"),
                "grants": acct.get("grants") or {}, "liveView": bool(acct.get("liveView")),
                "link": f"{PAGE}/#/accounts/{urllib.parse.quote(acct['id'])}"})
        return {"accounts": out, "page": PAGE}
    finally:
        c.close()


def status():
    a = app()
    if not a:
        print("not registered")
        return
    c = Consumer(a)
    try:
        c.pump(1.5)
        print(f"app {a['app']} at {a['url']}")
        for acct in c.accounts:
            d = (c.status.get(acct["id"]) or {}).get("details") or {}
            print(f"  {acct['kind']:<10} {acct['id']} online={acct['online']} grants={acct.get('grants')} "
                  f"lastPull={d.get('lastPullAt')} ok={d.get('lastPullOk')} waiting={d.get('spoolWaiting')}")
    finally:
        c.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("register")
    p.add_argument("sources", nargs="*", default=["simplefin", "monero"])
    p = sub.add_parser("request")
    p.add_argument("sources", nargs="+")
    sub.add_parser("status")
    p = sub.add_parser("fetch")
    p.add_argument("sources", nargs="*", default=["simplefin", "monero"])
    p.add_argument("--refresh", choices=["always", "stale", "never"], default="never")
    a = ap.parse_args()
    if a.cmd == "register":
        register(a.sources)
    elif a.cmd == "request":
        request(a.sources)
    elif a.cmd == "status":
        status()
    else:
        print(json.dumps(fetch(a.sources, a.refresh)))


if __name__ == "__main__":
    main()
