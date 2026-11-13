"""Link merchant orders (merchant_order) to the ledger rows that paid for them. Read-time only.

Amazon charges a card when an order ships, once per shipment, so an order's total usually
equals one charge a few days after it was placed, and sometimes the sum of two or three. For
each order, oldest first, this takes the closest unclaimed Amazon charge of exactly that
amount within WINDOW days, else the closest combination of up to three that adds up to it.
Orders whose charges were split some other way (gift cards, partial refunds) stay unmatched.
"""
import itertools
import json
import re
from collections import Counter
from datetime import date

AMAZON = re.compile(r"AMAZON|AMZN|AMZ\*|AMZ ", re.I)
WINDOW = (-2, 21)


def orders(con, merchant: str = "amazon") -> list[dict]:
    """Newest report of each order; items and summary from the newest details read."""
    out = {}
    for r in con.execute("""SELECT * FROM merchant_order WHERE merchant=? ORDER BY pulled_at""", (merchant,)):
        o = out.setdefault(r["order_id"], {"id": r["order_id"]})
        o.update(placed=r["placed"], total=float(r["total"]) if r["total"] else None, status=r["status"],
                 ship_to=r["ship_to"] or o.get("ship_to"), payment=r["payment"] or o.get("payment"))
        items = json.loads(r["items"])
        if r["detail"]:
            o.update(items=items, detail=json.loads(r["detail"]))
        elif "detail" not in o:
            o["items"] = items
    return sorted(out.values(), key=lambda o: o["placed"])


def _charges(con) -> list[dict]:
    rows = con.execute("""SELECT l.txn_id, l.date, l.account_id, l.description, l.amount_f FROM ledger l
                          JOIN account a ON a.id = l.account_id
                          WHERE l.flow = 'external' AND a.base_asset = 'fiat:USD' AND l.amount_f < 0
                          AND NOT l.pending ORDER BY l.date""").fetchall()
    return [{"id": r["txn_id"], "date": r["date"][:10], "amount": round(-r["amount_f"], 2), "account": r["account_id"]}
            for r in rows if AMAZON.search(r["description"] or "")]


def match(con) -> dict:
    """-> {"by_txn": {txn_id: [order, ...]}, "orders": [...] (each with for_other), "owners": names, "unmatched_charges": n}"""
    os_ = orders(con)
    charges = _charges(con)
    free = {c["id"]: c for c in charges}
    by_txn: dict[str, list] = {}
    days = lambda a, b: (date.fromisoformat(a) - date.fromisoformat(b)).days
    for o in os_:
        o["txns"] = []
        if not o.get("total") or o["total"] <= 0 or re.match(r"cancel", o.get("status") or "", re.I):
            continue
        near = [c for c in free.values() if WINDOW[0] <= days(c["date"], o["placed"]) <= WINDOW[1]]
        pick = None
        exact = [c for c in near if abs(c["amount"] - o["total"]) < 0.005]
        if exact:
            pick = [min(exact, key=lambda c: abs(days(c["date"], o["placed"])))]
        else:
            for k in (2, 3):
                combos = [cs for cs in itertools.combinations(near[:24], k)
                          if abs(sum(c["amount"] for c in cs) - o["total"]) < 0.005]
                if combos:
                    pick = min(combos, key=lambda cs: sum(abs(days(c["date"], o["placed"])) for c in cs))
                    break
        for c in pick or []:
            del free[c["id"]]
            o["txns"].append(c["id"])
            by_txn.setdefault(c["id"], []).append(o)
    owners = owner_names(os_)
    for o in os_:
        o["for_other"] = bool(o.get("ship_to")) and o["ship_to"] not in owners
    return {"by_txn": by_txn, "orders": os_, "owner": sorted(owners)[0] if owners else None, "owners": sorted(owners),
            "unmatched_charges": len(free)}


def owner_names(os_: list[dict]) -> set[str]:
    """The names your own orders ship to: every ship-to sharing the most common first name ('Ann',
    'Ann Lee', 'Ann L.'), plus the aliases in data/people.json "me" (e.g. the account's display name)."""
    from .flows import people
    names = Counter(o["ship_to"] for o in os_ if o.get("ship_to"))
    if not names:
        return set(people().get("me", []))
    first = Counter(n.split()[0].lower() for n in names.elements()).most_common(1)[0][0]
    return {n for n in names if n.split()[0].lower() == first} | set(people().get("me", []))


# ---------------------------------------------------------------- crypto
CRYPTO_OUT = re.compile(r"KRAKEN|COINBASE|GEMINI|BINANCE|OUTGOING WIRE|WIRE TRANSFER|TRANSFER FUNDS TO|CASH APP|STRIKE", re.I)
PRICE_BAND = {"XMR": (40.0, 1500.0)}   # USD per coin a match may imply; wide on purpose, no price feed is used
LOOKBACK = 21                          # days between dollars leaving and coins arriving


def crypto_links(con) -> list[dict]:
    """For each coin receipt, the USD outflows in the LOOKBACK days before it whose implied price is
    plausible: candidates for 'these dollars bought those coins'. Suggestions, not ledger facts."""
    coins = con.execute("""SELECT l.txn_id, l.date, l.amount_f, a.base_asset FROM ledger l JOIN account a ON a.id = l.account_id
                           WHERE a.base_asset LIKE 'crypto:%' AND l.amount_f > 0 AND NOT l.pending ORDER BY l.date""").fetchall()
    usd = con.execute("""SELECT l.txn_id, l.date, l.amount_f, l.description, l.account_id FROM ledger l
                         JOIN account a ON a.id = l.account_id
                         WHERE a.base_asset = 'fiat:USD' AND l.amount_f < 0 AND l.flow = 'external'""").fetchall()
    usd = [u for u in usd if CRYPTO_OUT.search(u["description"] or "")]
    out = []
    for c in coins:
        sym = c["base_asset"].split(":")[1]
        lo, hi = PRICE_BAND.get(sym, (0, float("inf")))
        cands = []
        for u in usd:
            gap = (date.fromisoformat(c["date"][:10]) - date.fromisoformat(u["date"][:10])).days
            price = -u["amount_f"] / c["amount_f"]
            if 0 <= gap <= LOOKBACK and lo <= price <= hi:
                cands.append({"txn": u["txn_id"], "date": u["date"][:10], "usd": round(-u["amount_f"], 2),
                              "desc": u["description"], "days_before": gap, "implied_price": round(price, 2)})
        out.append({"txn": c["txn_id"], "date": c["date"][:10], "asset": sym, "quantity": c["amount_f"],
                    "candidates": sorted(cands, key=lambda x: x["days_before"])})
    return out
