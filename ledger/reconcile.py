"""Balance assertions: check replayed postings against every balance/holding a source reported.

Assertions per (owned account, asset), all compared at end of a local day:
  running   checking PDFs: the balance printed after each row (latest row of each day)
  balance   SimpleFIN account balance, for cash accounts
  cash      SimpleFIN brokerage balance minus its holdings' market value at the same instant
  position  SimpleFIN holdings
  absent    a security this account traded but that is missing from a holdings list: 0
The earliest assertion fixes the opening balance; the rest must match exactly. Instant
assertions also accept the previous day's balance (a pull can precede that day's postings), and
the next day's when the instant already falls on that day in New York: US banks post on Eastern
days, so a balance read after midnight Eastern includes rows dated the next local day (lag -1).
"""
from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from decimal import Decimal

from .model import local_day, qstr

EASTERN = "America/New_York"


def _replay(con) -> dict[tuple, list[tuple[str, Decimal]]]:
    """(account, asset) -> [(day, end-of-day quantity from postings alone)], posted only."""
    acc: dict[tuple, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for a, asset, day, q in con.execute("""
            SELECT p.account_id, p.asset_id, p.date, p.quantity FROM posting p
            JOIN account a ON a.id = p.account_id AND a.owned
            JOIN entry e ON e.txn_id = p.txn_id AND e.account_id = p.account_id
            WHERE NOT e.pending"""):
        acc[(a, asset)][day] += Decimal(q)
    out = {}
    for k, days in acc.items():
        run, series = Decimal(0), []
        for d in sorted(days):
            run += days[d]
            series.append((d, run))
        out[k] = series
    return out


def _at(series: list[tuple[str, Decimal]], day: str) -> Decimal:
    v = Decimal(0)
    for d, q in series:
        if d > day:
            break
        v = q
    return v


def _assertions(con) -> list[dict]:
    kinds = dict(con.execute("SELECT id, kind FROM account").fetchall())
    base = dict(con.execute("SELECT id, base_asset FROM account").fetchall())
    out, seen = [], set()

    def add(account, asset, day, kind, sha, loc, qty, instant, lead=False):
        key = (account, asset, day, kind, qty)
        if key not in seen:   # overlapping pulls repeat the same snapshot
            seen.add(key)
            out.append(dict(account=account, asset=asset, day=day, kind=kind, sha=sha, loc=loc,
                            expected=qty, instant=instant, lead=lead))

    # running balances: the newest row of each day (rows print newest first: lowest page, then y)
    latest: dict[tuple, tuple] = {}
    for sha, loc, acct, asset, day, q in con.execute(
            "SELECT source_sha, locator, account_id, asset_id, as_of, quantity FROM snapshot WHERE kind='running'"):
        m = re.fullmatch(r"p(\d+):y(\d+)", loc)
        order = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        k = (sha, acct, asset, day)
        if k not in latest or order < latest[k][0]:
            latest[k] = (order, loc, Decimal(q))
    for (sha, acct, asset, day), (_, loc, q) in latest.items():
        add(acct, asset, day, "running", sha, loc, q, False)

    snaps = con.execute("""SELECT source_sha, locator, account_id, asset_id, kind, as_of, as_of_prec, as_of_lo,
                                  quantity, value FROM snapshot WHERE kind IN ('balance', 'position')""").fetchall()
    held = defaultdict(set)   # (sha, account, as_of) -> assets in that holdings list
    for s in snaps:
        if s["kind"] == "position":
            held[(s["source_sha"], s["account_id"], s["as_of"])].add(s["asset_id"])
    for s in snaps:
        day = local_day(s["as_of"], s["as_of_prec"], "UTC", s["as_of_lo"])
        lead = s["as_of_prec"] not in ("year", "month", "day") and local_day(s["as_of"], s["as_of_prec"], EASTERN, s["as_of_lo"]) > day
        if s["kind"] == "position" and s["quantity"] is not None:
            add(s["account_id"], s["asset_id"], day, "position", s["source_sha"], s["locator"],
                Decimal(s["quantity"]), True, lead)
        elif s["kind"] == "balance" and s["quantity"] is not None:
            if kinds.get(s["account_id"]) != "brokerage":
                add(s["account_id"], s["asset_id"], day, "balance", s["source_sha"], s["locator"],
                    Decimal(s["quantity"]), True, lead)
                continue
            vals = [Decimal(h["value"]) for h in snaps if h["kind"] == "position" and h["value"] is not None
                    and (h["source_sha"], h["account_id"], h["as_of"]) == (s["source_sha"], s["account_id"], s["as_of"])]
            add(s["account_id"], base[s["account_id"]], day, "cash", s["source_sha"], s["locator"] + ";cash",
                Decimal(s["quantity"]) - sum(vals, Decimal(0)), True, lead)
    traded = defaultdict(set)
    for a, asset in con.execute("SELECT DISTINCT account_id, asset_id FROM posting WHERE asset_id LIKE 'sec:%'"):
        traded[a].add(asset)
    for (sha, acct, as_of), assets in sorted(held.items()):
        s = next(s for s in snaps if (s["source_sha"], s["account_id"], s["as_of"]) == (sha, acct, as_of))
        day = local_day(as_of, s["as_of_prec"], "UTC", s["as_of_lo"])
        for asset in sorted(traded[acct] - assets):
            add(acct, asset, day, "absent", sha, f"{s['locator']};absent={asset}", Decimal(0), True)
    return out


def reconcile(con) -> dict:
    series = _replay(con)
    checks = _assertions(con)
    by_key = defaultdict(list)
    for c in checks:
        by_key[(c["account"], c["asset"])].append(c)
    with con:
        con.execute("DELETE FROM balance_check")
        con.execute("DELETE FROM opening_balance")
        for (acct, asset), cs in sorted(by_key.items()):
            s = series.get((acct, asset), [])
            cs.sort(key=lambda c: (c["day"], c["kind"], c["loc"]))
            first = cs[0]
            opening = first["expected"] - _at(s, first["day"])
            day0 = min([first["day"]] + [d for d, _ in s[:1]])
            con.execute("INSERT INTO opening_balance VALUES (?,?,?,?,?)",
                        (acct, asset, qstr(opening),
                         (dt.date.fromisoformat(day0) - dt.timedelta(days=1)).isoformat(), first["kind"]))
            for c in cs:
                got = opening + _at(s, c["day"])
                lag = 0
                if got != c["expected"] and c["instant"]:
                    d0 = dt.date.fromisoformat(c["day"])
                    if opening + _at(s, (d0 - dt.timedelta(days=1)).isoformat()) == c["expected"]:
                        lag = 1
                    elif c["lead"] and opening + _at(s, (d0 + dt.timedelta(days=1)).isoformat()) == c["expected"]:
                        lag = -1
                con.execute("INSERT INTO balance_check VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (acct, asset, c["day"], c["kind"], c["sha"], c["loc"], qstr(c["expected"]),
                             qstr(got), int(got == c["expected"] or lag != 0), lag))
    r = con.execute("SELECT count(*), coalesce(sum(ok), 0) FROM balance_check").fetchone()
    return {"checks": r[0], "ok": r[1]}
