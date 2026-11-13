"""`ledger dashboard`: one self-contained HTML page of spending, income and a budget model.

Everything is computed from the db at build time and embedded as JSON; the page itself does the
charts and the budget solve (so its parameters can be changed in place). The page holds amounts:
it is written under data/ (mode 600) and loads nothing from the network.
"""
import json
import re
import statistics
from bisect import bisect_right
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from . import classify, config, flows, prices
from .categories import CERTAINTY, GROUPS, categorize, tags_for, what_for

WEB = Path(__file__).with_name("web")


def build(con) -> dict:
    rows = con.execute("""SELECT l.txn_id, l.date, l.account_id, l.description, l.kind, l.amount_f, l.category, ac.base_asset, l.tags, ac.kind acct_kind
                          FROM ledger l JOIN account ac ON ac.id = l.account_id
                          WHERE l.flow = 'external' AND (ac.base_asset = 'fiat:USD' OR ac.base_asset LIKE 'crypto:%') AND NOT l.pending
                            AND NOT EXISTS (SELECT 1 FROM posting px WHERE px.txn_id = l.txn_id AND px.account_id = 'ext:unreconciled')   -- awaiting the bank's own row
                          ORDER BY l.date""").fetchall()
    spend, income = [], []
    model = classify.results()
    fl = flows.analyze(con)
    unpriced = 0
    for r in rows:
        rule, merchant = categorize(r["description"], r["account_id"], r["kind"], r["amount_f"], r["acct_kind"])
        cat, src = classify.effective(rule, merchant, r["category"], model.get(r["txn_id"]), r["amount_f"])   # the kind
        if r["base_asset"].startswith("crypto:"):
            # Coins count at that day's price. What you sent is spending like any other; what came in is most often
            # your own coins arriving (an exchange withdrawal), so it counts as income only once you label it so.
            if r["amount_f"] > 0 and src != "you":
                continue
            px = prices.price(r["base_asset"].split(":")[1], r["date"][:10])
            if px is None:
                unpriced += 1
                continue
            r = {**dict(r), "amount_f": r["amount_f"] * px}
        acct, d, tid = r["account_id"].split("-")[0], r["date"][:10], r["txn_id"]
        mtags = json.loads(r["tags"]) if r["tags"] else None
        what = what_for(rule, mtags)   # what it was: the descriptive tag the breakdowns use
        tags = tags_for(r["description"], r["account_id"], mtags, (what,) if what else ())
        life = fl["life"].get(tid, 0)
        if r["amount_f"] > 0 and (back := fl["repaid_in"].get(tid)):   # someone paying you back: cancels what you fronted
            back = min(back, r["amount_f"])
            spend.append([d, "Fronted for others", merchant, round(-back, 2), acct, tid, 0, tags, what])
            if r["amount_f"] - back < 0.005:
                continue
            r = {**dict(r), "amount_f": r["amount_f"] - back}
        if GROUPS[cat] == "income":
            income.append([d, cat, merchant, round(r["amount_f"], 2), CERTAINTY.get(cat, "unknown"), tid, tags, what])
            continue
        amt = -r["amount_f"]   # positive = money out; a refund is negative within its category
        mine = fl["split"].get(tid, {}).get(flows.ME, 1.0) if tid in fl["split"] else 1.0
        if mine < 1:   # the part that served someone else
            spend.append([d, "Fronted for others", merchant, round(amt * (1 - mine), 2), acct, tid, 0, tags, what])
        if mine > 0:
            spend.append([d, cat, merchant, round(amt * mine, 2), acct, tid, life, tags, what])

    asof = max(r["date"][:10] for r in rows)
    # months before every account's history is in (the page shades them): config [dashboard] full_from, else the
    # first month of a checking account
    full_from = config.get("dashboard", "full_from") or (con.execute(
        "SELECT min(l.date) FROM ledger l JOIN account a ON a.id = l.account_id WHERE a.kind = 'checking'").fetchone()[0]
        or rows[0]["date"])[:7]
    # the same spending with durable goods spread over their life (a slice a month, up to asof)
    smooth = []
    for row in spend:
        if row[6] > 1:
            smooth += [[dk, *row[1:3], round(x, 2), *row[4:6], 0, row[7], row[8]] for dk, x in flows.spread(row[0], row[3], row[6], asof)]
        else:
            smooth.append(row)
    recurring = _recurring(spend, asof)
    subs = {(r["merchant"], r["category"]) for r in recurring}
    for row in (*spend, *smooth):
        if (row[2], row[1]) in subs and "subscription" not in row[7]:
            row[7] = sorted([*row[7], "subscription"])
    return {
        "asof": asof,
        "fullFrom": full_from,
        "groups": GROUPS,
        "spend": spend,
        "spendSmooth": sorted(smooth, key=lambda r: r[0]),
        "people": fl["people"],
        "income": income,
        "worth": _worth(con, fl["people"]),
        "recurring": recurring,
        "budget": _budget_defaults(),
    }


def _step(days: list[str], vals: list[float], d: str):
    """The latest value on or before d (None before the first)."""
    i = bisect_right(days, d)
    return vals[i - 1] if i else None


MONEY_MARKET = re.compile(r"^sec:[A-Z]{3,4}XX$")   # SWVXX, SNOXX: cash in all but name


def _worth(con, people: list) -> dict:
    """What you own, account by account, and how the total moved.

    Now: each bank's latest reported balance (snapshots), the brokerage's holdings at their latest
    price, coins at the latest market close (data/prices.json), what Privacy is owed, and per-person
    balances from flows. Over time: the ledger's running balance of every account that has a known
    starting point, priced at the last price seen on or before each day (trades and holdings for
    securities, daily closes for coins)."""
    asof = con.execute("SELECT max(as_of) FROM snapshot WHERE kind = 'balance'").fetchone()[0][:10]
    names = {r["account_id"]: r["name"] for r in con.execute("SELECT account_id, name FROM account_annotation")}
    # a brokerage's reported balance is the whole account (cash and holdings): its cash is the balance less the
    # holdings in the same snapshot, as reconcile's cash check reads it
    snap = {r["account_id"]: (r["q"] - (r["held"] if r["kind"] == "brokerage" else 0), r["as_of"][:10], r["kind"]) for r in con.execute(
        """SELECT s.account_id, CAST(s.quantity AS REAL) q, s.as_of, a.kind,
                  (SELECT coalesce(sum(CAST(h.value AS REAL)), 0) FROM snapshot h WHERE h.kind = 'position'
                     AND h.source_sha = s.source_sha AND h.account_id = s.account_id AND h.as_of = s.as_of) held
           FROM snapshot s JOIN account a ON a.id = s.account_id
           WHERE s.kind = 'balance' AND s.asset_id = 'fiat:USD' AND s.as_of = (SELECT max(as_of) FROM snapshot x
             WHERE x.account_id = s.account_id AND x.kind = 'balance' AND x.asset_id = 'fiat:USD')""")}
    rows = []
    for acct, (q, d, kind) in sorted(snap.items()):
        name = names.get(acct) or acct
        rows.append({"group": "Cash", "name": name + " cash" if kind == "brokerage" else name, "account": acct,
                     "value": round(q, 2), "asof": d})
    holdings = []
    for r in con.execute("SELECT * FROM holding ORDER BY value_f DESC"):
        sym = r["asset_id"][4:]
        h = {"symbol": sym, "value": round(r["value_f"] or 0, 2), "cost": round(r["cost_f"] or 0, 2)}
        holdings.append(h)
        mm = bool(MONEY_MARKET.match(r["asset_id"]))
        rows.append({"group": "Cash" if mm else "Investments", "name": sym + (" (money market)" if mm else ""),
                     "account": r["account_id"], "quantity": round(r["quantity_f"], 4), "value": h["value"],
                     "cost": None if mm or r["lots_without_cost"] else h["cost"], "asof": r["price_day"]})
    # coins: quantities from the ledger; a view-only wallet sees what came in, and what went out only once
    # key images are given, so without a balance snapshot the quantity is everything received
    xd, xv = prices.series("XMR")
    coins = []
    for r in con.execute("""SELECT a.id, a.base_asset, sum(e.amount_f) q, sum(e.amount_f < 0) outs FROM account a
                            JOIN ledger e ON e.account_id = a.id
                            WHERE a.base_asset LIKE 'crypto:%' AND NOT e.pending GROUP BY a.id"""):
        sym = r["base_asset"].split(":")[1]
        coins.append({"symbol": sym, "account": r["id"], "quantity": round(r["q"] or 0, 6)})
        px = (xv[-1], xd[-1]) if sym == "XMR" and xv else (None, None)
        # with no spend ever seen, the quantity is only what came in: shown, not counted, until key images say what's left
        seen_out = bool(r["outs"]) or r["id"] in snap
        rows.append({"group": "Crypto", "name": f"{sym} ({names.get(r['id']) or 'wallet'})", "account": r["id"],
                     "quantity": round(r["q"] or 0, 6), "value": round((r["q"] or 0) * px[0], 2) if px[0] else None,
                     "price": px[0], "asof": px[1], "counted": seen_out,
                     "note": None if seen_out else "everything ever received: the wallet can't see spends until key images are given in dither-accounts"})
    # Privacy: a pass-through card account; a settled balance below zero is what Privacy is owed
    pc = con.execute("SELECT sum(amount_f) FROM ledger WHERE account_id = 'privacy-cards' AND NOT pending").fetchone()[0] or 0
    if abs(pc) >= 0.005:
        rows.append({"group": "You owe" if pc < 0 else "Cash", "name": "Privacy cards", "account": "privacy-cards",
                     "value": round(pc, 2), "note": "settled charges not yet collected" if pc < 0 else "credit at Privacy"})
    for b in people:
        if abs(b["net"]) >= 0.5:
            rows.append({"group": "Owed to you" if b["net"] > 0 else "You owe", "name": b["person"], "person": True,
                         "value": round(b["net"], 2), "counted": b["net"] < 0,
                         "note": "fronted, not paid back (or a gift)" if b["net"] > 0 else "they fronted for you"})
    return {"asof": asof, "accounts": rows, "history": _worth_history(con, xd, xv),
            "xmr": {"source": prices.load().get("XMR", {}).get("source"), "from": xd[0] if xd else None},
            # the budget model's inputs (web/dash.js)
            "checking": round(sum(q for q, _, k in snap.values() if k == "checking"), 2),
            "brokerageCash": round(sum(q for q, _, k in snap.values() if k == "brokerage"), 2),
            "holdings": holdings, "coins": coins}


def _worth_history(con, xd: list, xv: list) -> dict:
    """Weekly totals by group from the ledger's running balances (the balance view)."""
    # accounts whose running balance means something: a known start, or a pass-through that starts at zero; coins
    # only once a spend has been seen (a view-only wallet without key images sees only what came in)
    known = {r["account_id"] for r in con.execute("SELECT DISTINCT account_id FROM opening_balance")} | {"privacy-cards"} \
        | {r["id"] for r in con.execute("""SELECT a.id FROM account a JOIN entry e ON e.account_id = a.id
                                           WHERE a.base_asset LIKE 'crypto:%' GROUP BY a.id HAVING sum(e.amount_f < 0) > 0""")}
    bal: dict[tuple, tuple[list, list]] = {}
    for r in con.execute("SELECT account_id, asset_id, date, quantity_f FROM balance ORDER BY date"):
        if r["account_id"] in known:
            k = (r["account_id"], r["asset_id"])
            bal.setdefault(k, ([], []))
            bal[k][0].append(r["date"][:10])
            bal[k][1].append(r["quantity_f"])
    px: dict[str, tuple[list, list]] = {}
    for r in con.execute("SELECT asset_id, day, CAST(value AS REAL) v FROM price WHERE price_asset = 'fiat:USD' ORDER BY day, source"):
        px.setdefault(r["asset_id"], ([], []))
        if px[r["asset_id"]][0] and px[r["asset_id"]][0][-1] == r["day"]:
            px[r["asset_id"]][1][-1] = r["v"]
        else:
            px[r["asset_id"]][0].append(r["day"])
            px[r["asset_id"]][1].append(r["v"])
    px["crypto:XMR"] = (xd, xv)
    # the opening balance sits the day before an account's first posting: from then on it is known
    opened = {r["account_id"]: r["d"] for r in con.execute(
        "SELECT account_id, min(as_of) d FROM opening_balance WHERE asset_id = 'fiat:USD' GROUP BY account_id")}
    if not bal:
        return {}
    # from the first dollar account with a known start (coins alone, before that, say little)
    start = min(opened.values()) if opened else min(v[0][0] for v in bal.values())
    end = max(v[0][-1] for v in bal.values())
    end = max(end, con.execute("SELECT max(date) FROM ledger").fetchone()[0][:10])
    days, d = [], date.fromisoformat(start)
    while d.isoformat() < end:
        days.append(d.isoformat())
        d += timedelta(days=7)
    days.append(end)
    groups = ["Cash", "Investments", "Crypto"]
    series = {g: [0.0] * len(days) for g in groups}
    unpriced = [False] * len(days)
    for (acct, asset), (ds, qs) in bal.items():
        g = "Cash" if asset == "fiat:USD" or MONEY_MARKET.match(asset) else "Crypto" if asset.startswith("crypto:") else "Investments"
        for i, day_ in enumerate(days):
            q = _step(ds, qs, day_)
            if not q:
                continue
            if asset == "fiat:USD":
                series[g][i] += q
                continue
            p = _step(*px.get(asset, ([], [])), day_)
            if p is None:
                p = px.get(asset, ([], [None]))[1][0] if asset != "crypto:XMR" and px.get(asset, ([], []))[0] else None
            if p is None:
                unpriced[i] = True   # coins before the price history begins
                continue
            series[g][i] += q * p
    return {"days": days, "series": {g: [round(v, 2) for v in vs] for g, vs in series.items()},
            "full": max(opened.values()) if opened else start,
            "unpriced": next((days[i] for i in range(len(days) - 1, -1, -1) if unpriced[i]), None)}


def _recurring(spend: list, asof: str) -> list:
    """Merchants charged on a steady cadence at a steady amount: subscriptions, phone plans."""
    by = defaultdict(list)
    for d, cat, merchant, amt, *_, what in spend:
        if amt > 0 and GROUPS[cat] in ("essential", "discretionary") and what not in ("groceries", "dining", "shopping"):
            by[(merchant, cat)].append((date.fromisoformat(d), amt))
    out = []
    end = date.fromisoformat(asof)
    for (merchant, cat), xs in by.items():
        if len({d.strftime("%Y-%m") for d, _ in xs}) < 3:
            continue
        gaps = [(b[0] - a[0]).days for a, b in zip(xs, xs[1:]) if (b[0] - a[0]).days > 3]
        if not gaps:
            continue
        gap = statistics.median(gaps)
        if sum(1 for g in gaps if 25 <= g <= 36) >= len(gaps) / 3:
            gap = 30.44   # monthly with pauses: a subscription that was cancelled and restarted
        amts = [a for _, a in xs]
        typical = statistics.median(amts)
        recent = [a for d, a in xs if (end - d).days <= 400]
        steady = sum(1 for a in amts if abs(a - typical) <= 0.35 * typical) >= 0.6 * len(amts)
        if not (20 <= gap <= 40 or 80 <= gap <= 100 or 330 <= gap <= 400) or not steady:
            continue
        last = xs[-1][0]
        out.append({"merchant": merchant, "category": cat, "every": round(gap), "typical": round(typical, 2),
                    "monthly": round(typical * 30.44 / gap, 2), "charges": len(xs), "first": xs[0][0].isoformat(),
                    "last": last.isoformat(), "active": (end - last).days <= gap * 1.6 + 7,
                    "last12": round(sum(recent), 2)})
    return sorted(out, key=lambda r: (not r["active"], -r["monthly"]))


def _budget_defaults() -> dict | None:
    from .db import DATA
    p = DATA / "budget.json"
    return json.loads(p.read_text()) if p.exists() else None


def standalone(data: dict) -> str:
    """One self-contained page (the portal's dashboard code, inlined, with the data embedded): no network."""
    js = "\n".join((WEB / f).read_text() for f in ("common.js", "budget.js", "dash.js"))
    blob = re.sub(r"</", r"<\\/", json.dumps(data, separators=(",", ":")))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Spending</title><style>{(WEB / "style.css").read_text()}</style></head>
<body><div class="dash" id="app" style="padding-top:24px"></div><div id="tip"></div>
<script>{js.replace("</script", "<\\/script")}
const D = {blob};
Dash.mount(document.getElementById("app"), D, {{portal: false}});
const go = () => Dash.show(location.hash.slice(1));
addEventListener("hashchange", go); go();
</script></body></html>"""


def write(con, out: Path) -> Path:
    out.write_text(standalone(build(con)))
    out.chmod(0o600)
    return out
