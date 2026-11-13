"""Market prices from outside the evidence: XMR/USD daily closes from Kraken's public API.

Kept in data/prices.json (not the ledger, which stays a function of data/raw and state.jsonl): the
dashboard reads it to put a dollar value on coins. `ledger prices` (and `ledger pull`) fetches the
last 720 days and merges them in, so the history only grows. Days before that (Kraken's daily candles stop at
720) are filled once from Kraken's public trade history: the median of the first trades after midnight UTC, for each
day with a coin transaction and each Monday since the first (`sampled` lists them). Requests carry nothing but the
pair name and a time.
"""
import datetime as dt
import json
import os
import ssl
import statistics
import time
from bisect import bisect_right
import urllib.request
from pathlib import Path

from .db import DATA

PATH = DATA / "prices.json"
KRAKEN = "https://api.kraken.com/0/public/OHLC?pair={pair}&interval=1440"
TRADES = "https://api.kraken.com/0/public/Trades?pair={pair}&since={ns}&count=50"
PAIRS = {"XMR": "XMRUSD"}
# some Python builds don't find the system's trust store: use the system bundle when there is one
_CA = next((p for p in (os.environ.get("SSL_CERT_FILE"), "/etc/ssl/certs/ca-certificates.crt", "/etc/ssl/cert.pem")
            if p and Path(p).exists()), None)
_TLS = ssl.create_default_context(cafile=_CA)


def load() -> dict:
    """{"XMR": {"source": str, "fetched": iso, "days": {"YYYY-MM-DD": close}}}"""
    try:
        return json.loads(PATH.read_text())
    except (OSError, ValueError):
        return {}


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "ledger-prices"})
    r = json.loads(urllib.request.urlopen(req, timeout=30, context=_TLS).read())
    if r.get("error"):
        raise RuntimeError(f"kraken: {r['error']}")
    return next(v for k, v in r["result"].items() if k != "last")


def _save(have: dict):
    tmp = PATH.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(have, f, separators=(",", ":"))
    tmp.replace(PATH)


def _wanted(con, sym: str, before: str) -> list[str]:
    """Days before `before` worth a price: each day the ledger moved the coin, and each Monday since the first."""
    days = {r[0][:10] for r in con.execute(
        "SELECT e.date FROM entry e JOIN account a ON a.id = e.account_id WHERE a.base_asset = ?", (f"crypto:{sym}",))}
    if days:
        d = dt.date.fromisoformat(min(days))
        d -= dt.timedelta(days=d.weekday())
        while d.isoformat() < before:
            days.add(d.isoformat())
            d += dt.timedelta(days=7)
    return sorted(x for x in days if x < before)


def fetch(con=None, log=print) -> dict:
    have = load()
    for sym, pair in PAIRS.items():
        rows = _get(KRAKEN.format(pair=pair))
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        days = {dt.datetime.fromtimestamp(t[0], dt.timezone.utc).date().isoformat(): float(t[4])
                for t in rows}
        days.pop(today, None)   # today's candle is still open; its close isn't one yet
        cur = have.setdefault(sym, {"days": {}})
        cur["days"] = dict(sorted({**cur.get("days", {}), **days}.items()))
        cur["source"] = f"Kraken {pair[:3]}/{pair[3:]} daily close (UTC)"
        cur["fetched"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        log(f"prices {sym}: {len(days)} days fetched, {len(cur['days'])} kept")
        _save(have)
        if con is None or not days:
            continue
        # earlier days, once each: the first trades of the day from Kraken's trade history (about 1 request a second)
        sampled = set(cur.get("sampled", []))
        todo = [d for d in _wanted(con, sym, min(days)) if d not in cur["days"]]
        for i, d in enumerate(todo):
            ns = int(dt.datetime.fromisoformat(d).replace(tzinfo=dt.timezone.utc).timestamp()) * 10**9
            trades = [t for t in _get(TRADES.format(pair=pair, ns=ns))
                      if dt.datetime.fromtimestamp(t[2], dt.timezone.utc).date().isoformat() == d]
            if trades:
                cur["days"][d] = statistics.median(float(t[0]) for t in trades)
                sampled.add(d)
            if i % 20 == 19:
                cur["days"] = dict(sorted(cur["days"].items())); cur["sampled"] = sorted(sampled); _save(have)
                log(f"prices {sym}: {i + 1}/{len(todo)} earlier days")
            time.sleep(1.1)
        cur["days"] = dict(sorted(cur["days"].items()))
        cur["sampled"] = sorted(sampled)
        if todo:
            cur["source"] = f"Kraken {pair[:3]}/{pair[3:]}: daily close (UTC), and before {min(days)} the first trades of each day"
            log(f"prices {sym}: {len(todo)} earlier days sampled from trades")
        _save(have)
    return have


def price(sym: str, day: str, back: int = 10, _cache={}) -> float | None:
    """The price on `day`, or the latest within `back` days before it."""
    key = (sym, PATH.stat().st_mtime_ns if PATH.exists() else 0)
    if _cache.get("key") != key:
        _cache.update(key=key, s=series(sym))
    ds, vs = _cache["s"]
    i = bisect_right(ds, day)
    if not i or (dt.date.fromisoformat(day) - dt.date.fromisoformat(ds[i - 1])).days > back:
        return None
    return vs[i - 1]


def series(sym: str) -> tuple[list[str], list[float]]:
    """Sorted days and closes, for step lookups."""
    days = load().get(sym, {}).get("days", {})
    ks = sorted(days)
    return ks, [days[k] for k in ks]
