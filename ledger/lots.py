"""Lots (FIFO) and evidence prices, derived from postings, opening balances and snapshots.

Per (owned account, security): the opening balance becomes an 'opening' lot of unknown cost;
then each event's net change in that security opens a lot (cost = the cash the event took
from the account, fees included) or closes the oldest lots (proceeds = the cash it brought in,
shared out by quantity). An event that nets to zero (a share journal) touches nothing.

Method: Schwab closes mutual funds at average cost and everything else FIFO, and its realized
page labels both 'FIFO'; the costs it reports are what settle it. At average cost a sale still
takes shares from the oldest lots (holding periods), but its cost is its share of the pool.

Realized rows give what snapshots can't: the cost of the opening lot (solved from them), each
loss disallowed by a wash sale (added to the next purchase's cost), and an assertion per sale
day. Cross-check: wherever a holdings snapshot reports a cost basis, the open lots' cost on
that day must equal it; a mismatch is recorded, not fixed.
"""
from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from decimal import Decimal

from .model import h, local_day, qstr

CENT = Decimal("0.01")
# Realized rows whose quantities are rounded per row: Schwab's Realized Gain/Loss details export (parsers/schwab_csv.py)
# gives each lot's quantity to 2 decimals, so a day of fractional lots is only known to ±0.005 share per row.
ROUNDED_REALIZED = {"schwab_csv": Decimal("0.005")}
WASH_DAYS = 30
PROBE = Decimal(1000)   # any nonzero cost: _solve_opening reads a straight line through it


def build(con) -> dict:
    owned = {r[0] for r in con.execute("SELECT id FROM account WHERE owned")}
    base = dict(con.execute("SELECT id, base_asset FROM account").fetchall())
    stats = defaultdict(int)

    # per (txn, account): net quantity of each security, and the account's cash change
    moves: dict[tuple, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    cash: dict[tuple, Decimal] = defaultdict(Decimal)
    day: dict[tuple, str] = {}
    for tid, acct, asset, q, role, d in con.execute(
            "SELECT txn_id, account_id, asset_id, quantity, role, date FROM posting ORDER BY txn_id, n"):
        if acct not in owned:
            continue
        k = (tid, acct)
        if asset.startswith("sec:"):
            moves[k][asset] += Decimal(q)
            day[k] = min(day.get(k, d), d)
        elif asset == base.get(acct) and role in ("cash", "main"):
            cash[k] += Decimal(q)
    events = defaultdict(list)   # (account, asset) -> [(day, txn, qty, cash share)]
    for (tid, acct), assets in moves.items():
        live = {a: q for a, q in assets.items() if q}
        for asset, q in live.items():
            c = cash[(tid, acct)] if len(live) == 1 else None   # cash is attributable to one security only
            events[(acct, asset)].append((day[(tid, acct)], tid, q, c))

    openings = {(a, s): (Decimal(q), d) for a, s, q, d in con.execute(
        "SELECT account_id, asset_id, quantity, as_of FROM opening_balance WHERE asset_id LIKE 'sec:%'")}

    # (account, asset) -> day -> the rows of one source file. Where several files give a day, the most exact one:
    # a printed page (full quantities) before Schwab's CSV export (its details round each lot's quantity to 2
    # decimals), then the smallest sha, so the choice never depends on what was pulled last.
    realized = defaultdict(lambda: defaultdict(list))
    chosen = {}
    for a, s, d, sha, parser in con.execute("""SELECT r.account_id, r.asset_id, r.closed_date, r.source_sha, f.parser
                                              FROM realized_close r JOIN source_file f ON f.sha256 = r.source_sha
                                              GROUP BY 1, 2, 3, 4"""):
        rank = (parser in ROUNDED_REALIZED, sha)
        if (a, s, d) not in chosen or rank < chosen[(a, s, d)]:
            chosen[(a, s, d)] = rank
    for r in con.execute("""SELECT r.*, f.parser FROM realized_close r JOIN source_file f ON f.sha256 = r.source_sha
                            ORDER BY closed_date, locator"""):
        if chosen[(r["account_id"], r["asset_id"], r["closed_date"])][1] == r["source_sha"]:
            realized[(r["account_id"], r["asset_id"])][r["closed_date"]].append(r)

    with con:
        for t in ("lot_close", "lot", "price"):
            con.execute(f"DELETE FROM {t}")
        for key in sorted(set(events) | set(openings)):
            acct, asset = key
            q0, d0 = openings.get(key, (Decimal(0), None))
            if q0 < 0:
                stats["negative_opening"] += 1
            evs = sorted(events.get(key, []), key=lambda e: (e[0], -e[2], e[1]))
            rows = realized.get(key, {})
            wash = {d: sum((Decimal(r["disallowed"]) for r in rs), Decimal(0)) for d, rs in rows.items()}
            q0 = max(q0, Decimal(0))
            opening_cost = _solve_opening(q0, d0, evs, asset, wash, rows) if q0 else None
            lots, closes, unplaced = _replay(q0, d0, opening_cost, evs, asset, wash)
            stats["short"] += sum(1 for c in closes if c["lot"] is None)
            stats["wash_unplaced"] += unplaced
            for lot in lots:
                lid = f"lot_{h(acct, asset, lot['txn'] or 'opening', n=16)}"
                lot["id"] = lid
                con.execute("""INSERT INTO lot(id, account_id, asset_id, opened_txn, opened_date, quantity, cost,
                                               cost_asset, remaining, remaining_cost, cost_source)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                            (lid, acct, asset, lot["txn"], lot["date"], qstr(lot["qty"]),
                             qstr(lot["cost"]) if lot["cost"] is not None else None,
                             base.get(acct) if lot["cost"] is not None else None, qstr(lot["rem"]),
                             qstr(lot["rem_cost"].quantize(CENT)) if lot["rem_cost"] is not None else None,
                             lot["source"]))
                stats["opened"] += bool(lot["txn"])
            for c in closes:
                if c["lot"] is None:
                    continue
                con.execute("INSERT INTO lot_close VALUES (?,?,?,?,?,?)",
                            (c["lot"]["id"], c["txn"], c["date"], qstr(c["qty"]),
                             qstr(c["proceeds"]) if c["proceeds"] is not None else None,
                             qstr(c["cost"]) if c["cost"] is not None else None))
                stats["closes"] += 1
            _check_realized(con, acct, asset, rows, closes)

        con.execute("""INSERT OR IGNORE INTO price
                       SELECT asset_id, date, price_asset, unit_price, 'trade', txn_id FROM posting
                       WHERE unit_price IS NOT NULL AND price_asset IS NOT NULL AND CAST(unit_price AS REAL) > 0""")
        checked = set()
        for s in con.execute("""SELECT asset_id, as_of, as_of_prec, as_of_lo, locator, quantity, value, value_asset, extra,
                                       account_id, source_sha FROM snapshot
                                WHERE kind = 'position' AND value IS NOT NULL AND CAST(quantity AS REAL) != 0
                                ORDER BY as_of, source_sha, locator""").fetchall():
            d = local_day(s["as_of"], s["as_of_prec"], "UTC", s["as_of_lo"])
            con.execute("INSERT OR IGNORE INTO price VALUES (?,?,?,?,?,?)",
                        (s["asset_id"], d, s["value_asset"], qstr(Decimal(s["value"]) / Decimal(s["quantity"])),
                         "holding", s["locator"]))
            basis = json.loads(s["extra"] or "{}").get("cost_basis")
            if basis is None or (s["account_id"], s["asset_id"], d, basis) in checked:
                continue   # overlapping pulls repeat the same holdings
            checked.add((s["account_id"], s["asset_id"], d, basis))
            got = _open_cost(con, s["account_id"], s["asset_id"], d)
            con.execute("INSERT OR IGNORE INTO balance_check VALUES (?,?,?,?,?,?,?,?,?,0)",
                        (s["account_id"], s["asset_id"], d, "cost_basis", s["source_sha"], s["locator"] + ";cost",
                         qstr(Decimal(str(basis))), qstr(got) if got is not None else "unknown",
                         int(got is not None and abs(got - Decimal(str(basis))) <= CENT)))
    return dict(stats)


def _average_cost(asset: str) -> bool:
    """Mutual funds: tickers of five letters ending in X. The only method mismatch the checks ever show."""
    sym = asset.removeprefix("sec:")
    return len(sym) == 5 and sym.endswith("X")


def _replay(q0, d0, opening_cost, evs, asset, wash) -> tuple[list[dict], list[dict], int]:
    """One position's history, oldest first. A disallowed loss joins the next purchase's cost."""
    average = _average_cost(asset)
    lots: list[dict] = []
    closes: list[dict] = []
    pool = Decimal(0)     # average cost only: total cost held; None once any of it is unknown
    held = Decimal(0)
    owed, unplaced = Decimal(0), 0
    owed_since = None

    def open_lot(txn, date, qty, cost, source):
        nonlocal pool, held
        lots.append(dict(txn=txn, date=date, qty=qty, cost=cost, rem=qty, source=source, rem_cost=None))
        held += qty
        pool = None if pool is None or cost is None else pool + cost

    if q0:
        open_lot(None, d0, q0, opening_cost, "realized" if opening_cost is not None else None)
    for d, tid, q, c in evs:
        if q > 0:
            cost = -c if c is not None and c < 0 else None
            source = None
            if owed and cost is not None:
                if (dt.date.fromisoformat(d) - dt.date.fromisoformat(owed_since)).days <= WASH_DAYS:
                    cost, source = cost + owed, "wash"
                else:
                    unplaced += 1
                owed, owed_since = Decimal(0), None
            open_lot(tid, d, q, cost, source)
            continue
        need = total = -q
        # the event's cash and, at average cost, its cost: shared by quantity, the last lot takes the rest
        left_cash = c
        whole = None
        if average and pool is not None and held:
            whole = (pool * total / held).quantize(CENT)
            pool -= whole
        left_cost = whole
        for lot in lots:
            if need <= 0:
                break
            if lot["rem"] <= 0:
                continue
            take = min(lot["rem"], need)
            lot["rem"] -= take
            need -= take
            last = need <= 0
            proceeds = None
            if c is not None:
                proceeds = left_cash if last else (c * take / total).quantize(CENT)
                left_cash -= proceeds
            if average:
                if whole is None:
                    cost = None
                else:
                    cost = left_cost if last else (whole * take / total).quantize(CENT)
                    left_cost -= cost
            else:
                cost = (lot["cost"] * take / lot["qty"]).quantize(CENT) if lot["cost"] is not None else None
            closes.append(dict(lot=lot, txn=tid, date=d, qty=take, proceeds=proceeds, cost=cost))
        if need > 0:
            closes.append(dict(lot=None, txn=tid, date=d, qty=need, proceeds=None, cost=None))   # history gap
        held -= total - need
        if wash.get(d):
            owed += wash[d]
            owed_since = owed_since or d
    if owed:
        unplaced += 1
    # what remains of each lot's cost
    for lot in lots:
        if average:
            lot["rem_cost"] = None if pool is None else pool * lot["rem"] / held if held else Decimal(0)
        elif lot["cost"] is not None:
            lot["rem_cost"] = lot["cost"] * lot["rem"] / lot["qty"]
    return lots, closes, unplaced


def _solve_opening(q0, d0, evs, asset, wash, rows) -> Decimal | None:
    """The opening lot's cost, from the first realized day it affects. Cost is linear in it."""
    def day_costs(guess):
        out = defaultdict(Decimal)
        for c in _replay(q0, d0, guess, evs, asset, wash)[1]:
            if c["cost"] is not None:
                out[c["date"]] += c["cost"]
        return out
    lo, hi = day_costs(Decimal(0)), day_costs(PROBE)
    for d in sorted(rows):
        slope = hi[d] - lo[d]
        if slope:
            want = sum((Decimal(r["cost"]) for r in rows[d]), Decimal(0))
            return ((want - lo[d]) * PROBE / slope).quantize(CENT)
    return None


def _check_realized(con, acct, asset, rows, closes):
    """Every realized row is an assertion about that day's closes: quantity, proceeds and cost."""
    for d, rs in rows.items():
        mine = [c for c in closes if c["date"] == d and c["lot"] is not None]
        # A rounded source's quantities hold to its rounding, row by row; money always to the cent.
        qtol = sum((ROUNDED_REALIZED.get(r["parser"], Decimal(0)) for r in rs), Decimal(0))
        for kind, field, tol in (("realized_quantity", "quantity", qtol), ("realized_proceeds", "proceeds", CENT),
                                 ("realized_cost", "cost", CENT)):
            want = sum((Decimal(r[field]) for r in rs), Decimal(0))
            key = "qty" if field == "quantity" else field
            unknown = any(c[key] is None for c in mine)
            got = sum((c[key] for c in mine if c[key] is not None), Decimal(0))
            ok = not unknown and abs(got - want) <= tol
            con.execute("INSERT OR IGNORE INTO balance_check VALUES (?,?,?,?,?,?,?,?,?,0)",
                        (acct, asset, d, kind, rs[0]["source_sha"], rs[0]["locator"], qstr(want),
                         "unknown" if unknown else qstr(got), int(ok)))


def _open_cost(con, acct: str, asset: str, day: str) -> Decimal | None:
    """Cost of the lots still open at the end of `day`; None if any of them has unknown cost."""
    if _average_cost(asset):
        costs = [r[0] for r in con.execute("SELECT cost FROM lot WHERE account_id=? AND asset_id=? AND opened_date<=?",
                                           (acct, asset, day))]
        gone = [r[0] for r in con.execute("""SELECT c.cost FROM lot_close c JOIN lot l ON l.id = c.lot_id
                                             WHERE l.account_id=? AND l.asset_id=? AND c.date<=?""", (acct, asset, day))]
        if None in costs or None in gone:
            return None
        return (sum(map(Decimal, costs), Decimal(0)) - sum(map(Decimal, gone), Decimal(0))).quantize(CENT)
    total = Decimal(0)
    for lid, qty, cost, opened in con.execute(
            "SELECT id, quantity, cost, opened_date FROM lot WHERE account_id=? AND asset_id=? AND opened_date<=?",
            (acct, asset, day)).fetchall():
        closed = sum((Decimal(q) for (q,) in con.execute(
            "SELECT quantity FROM lot_close WHERE lot_id=? AND date<=?", (lid, day))), Decimal(0))
        rem = Decimal(qty) - closed
        if rem == 0:
            continue
        if cost is None:
            return None
        total += Decimal(cost) * rem / Decimal(qty)
    return total.quantize(CENT)
