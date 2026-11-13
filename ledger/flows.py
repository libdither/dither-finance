"""Who each dollar served, and over what time: repayments, beneficiaries and durable goods.

The ledger says where money went; this says what it was *for*. Three read-time layers, each with
your annotations winning over what is inferred:

Repayments. Money from a person (a Venmo payment in) is often them paying you back for something
you bought: a lamp, half a dinner. `update` matches each such inflow to the purchases it
repays: a price written in the note ("desk lamp (24.99)") that equals a purchase pins it;
otherwise candidates from the weeks before are scored on how the amounts relate (all of it, half,
a third…), how long before, and jev's judgment that the note describes the purchase. Links live
in data/classify.db; ones you confirm or reject live on the inflow's annotation
(overrides.repays = [{txn, amount}], overrides.repays_none = true) and so in state.jsonl.

Beneficiaries. Each purchase is split among "me", "household" and named people
(overrides.for = {"me": .5, "<person>": .5}). Without one, a purchase someone repaid is theirs
to the extent they repaid it. "household" is shared equally by you and data/people.json's
household members. Only your share is your spending; the rest is money fronted for others, and
each person's balance is what you fronted for them less what they paid back.

Durable goods. A purchase that keeps serving you (furniture, tools, electronics) is spending
spread over its life, not a spike in the month you bought it. jev suggests a life for larger
purchases in goods categories; overrides.life_months sets it (0 = used up at once).
"""
import hashlib
import itertools
import json
import math
import re
from datetime import date
from pathlib import Path

from . import classify, jev, merchants
from .categories import categories_of, categorize
from .db import DATA

PEOPLE = DATA / "people.json"
ME, HOUSEHOLD = "me", "household"
WINDOW = 120                        # days a repayment may come after the purchase
# rule categories, and the kinds a user's label can name (categories_of mixes both)
MAYBE_REPAYMENT = {"From people", "Bought for others", "Loans to people", "Transfer in (unlabeled)", "Fronted for others",
                   "Unexplained"}
NOT_PURCHASES = {"Taxes", "Investing", "Margin interest", "Unlabeled transfer out", "Rent", "Fees", "Own transfer"}
GOODS = {"Home", "Shopping", "Transport", "Health & personal", "Education", "Media & games", "Gifts given"}
DURABLE_MIN = 40.0                  # smaller purchases aren't worth spreading
LIVES = [("Used up within a month: food, consumables, services, subscriptions, tickets", 0),
         ("Lasts about a year: cheap gadgets, basic clothing, small accessories", 12),
         ("Lasts two to three years: electronics, shoes, small appliances, software licenses", 30),
         ("Lasts five years or more: furniture, tools, large appliances, bikes, durable home goods", 72)]
LIFE_KEY = "life-v2"
PRICE = re.compile(r"(?<![\w.])\$?(\d{1,5}\.\d{2})(?!\d)|\((\d{1,5})\s*\$?\)")


def people() -> dict:
    try:
        return json.loads(PEOPLE.read_text())
    except (OSError, ValueError):
        return {"household": []}


def _rows(con) -> list[dict]:
    rows = con.execute("""
        SELECT l.txn_id, l.date, l.account_id, l.description, l.kind, l.amount_f, l.category, l.note, l.tags, a.overrides,
               ac.kind acct_kind
        FROM ledger l JOIN account ac ON ac.id = l.account_id LEFT JOIN txn_annotation a ON a.txn_id = l.txn_id
        WHERE l.flow = 'external' AND ac.base_asset = 'fiat:USD' AND NOT l.pending AND ac.kind IS NOT 'brokerage'
          AND NOT EXISTS (SELECT 1 FROM posting px WHERE px.txn_id = l.txn_id AND px.account_id = 'ext:unreconciled')
        ORDER BY l.date""").fetchall()
    out = []
    for r in rows:
        rule, merchant = categorize(r["description"], r["account_id"], r["kind"], r["amount_f"], r["acct_kind"])
        out.append({"id": r["txn_id"], "date": r["date"][:10], "account": r["account_id"], "desc": r["description"] or "", "kind": r["kind"],
                    "amount": r["amount_f"], "rule": rule, "merchant": merchant, "manual": r["category"],
                    # what it was: the user's kind and tags when they labeled it, else the rule's category
                    "cats": categories_of(rule, r["category"], json.loads(r["tags"]) if r["tags"] else None),
                    "note": r["note"], "ov": json.loads(r["overrides"]) if r["overrides"] else {}})
    return out


def _party(r: dict) -> tuple[str | None, str]:
    """(person, note) for a payment from a person; Venmo rows read 'Name: note'."""
    if r["account"].startswith("venmo") and (r.get("kind") or "") == "payment":   # people, not merchants or fees
        name, _, note = r["desc"].partition(":")
        return name.strip() or None, note.strip()
    return None, ""


def _prices(note: str) -> list[float]:
    return [float(a or b) for a, b in PRICE.findall(note or "")]


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(a) - date.fromisoformat(b)).days


def _describe(o: dict, orders: dict) -> str:
    items = "; ".join(i.get("title", "")[:80] for od in orders.get(o["id"], []) for i in od.get("items", [])[:5])
    return f"Purchase on {o['date']}: {o['desc']}, ${-o['amount']:.2f}" + (f". Items: {items}" if items else "")


# ---------------------------------------------------------------- inference (stored)

def _db():
    con = classify._db()
    con.execute("""CREATE TABLE IF NOT EXISTS repay_link (inflow TEXT, outflow TEXT, amount REAL, conf REAL, method TEXT,
                   PRIMARY KEY (inflow, outflow))""")
    con.execute("""CREATE TABLE IF NOT EXISTS life (txn_id TEXT PRIMARY KEY, key TEXT, ctx TEXT, months INTEGER, p REAL)""")
    return con


def _judge(person, amt, day, note, outs, orders, together=False) -> float:
    """P(this payment repays these purchases): the best single one, or all of them at once."""
    q = (f"Is this what {person}'s Venmo payment of ${amt:.2f} on {day} (note: \"{note}\") pays back, fully or partly?")
    inst = "Decide whether a person's payment reimburses the owner for this purchase."
    if together:
        return jev._post("/v1/judge", {"texts": [" AND ".join(_describe(o, orders) for o in outs)], "question": q, "instruct": inst})["p"][0]
    return max(jev._post("/v1/judge", {"texts": [_describe(o, orders) for o in outs], "question": q, "instruct": inst})["p"])


def _link_repayments(rows: list[dict], orders: dict, log) -> list[tuple]:
    outs = [r for r in rows if r["amount"] < 0 and not (r["cats"] & NOT_PURCHASES)]
    left = {o["id"]: -o["amount"] for o in outs}
    links = []
    for r in rows:
        person, note = _party(r)
        if r["amount"] <= 0 or not person or r["ov"].get("repays_none") or r["ov"].get("repays"):
            continue   # not a payment from a person, or you settled it by hand
        if not (r["cats"] & MAYBE_REPAYMENT):
            continue   # a gift, pay for work...: the category already says what it was
        cands = [o for o in outs if 0 <= _days(r["date"], o["date"]) <= WINDOW and left[o["id"]] > 0.01
                 and o["id"] not in r["ov"].get("not_repays", [])]
        got, amt = [], r["amount"]
        prices = _prices(note)
        unmatched = []
        for x in prices:   # a price in the note that equals a purchase pins it
            hit = [o for o in cands if abs(-o["amount"] - x) < 0.01 and o["id"] not in [g[1] for g in got]]
            if hit:
                o = min(hit, key=lambda o: _days(r["date"], o["date"]))
                got.append((r["id"], o["id"], x, 0.95, "price in note"))
            else:
                unmatched.append(x)
        for x in unmatched:   # "~90": a purchase up to 15% above the price (tax, shipping), if jev agrees
            near = [o for o in cands if x <= -o["amount"] <= x * 1.15 + 1 and o["id"] not in [g[1] for g in got]]
            if near and _judge(person, amt, r["date"], note, near[:4], orders) >= 0.5:
                o = min(near, key=lambda o: -o["amount"] - x)
                got.append((r["id"], o["id"], round(-o["amount"], 2), 0.8, "approximate price in note"))
        if got and sum(g[2] for g in got) > amt * 1.02:   # the note overcounts: trust exact prices, scale the rest
            exact = sum(g[2] for g in got if g[3] >= 0.95)
            rest = [g for g in got if g[3] < 0.95]
            k = max(0.0, amt - exact) / max(sum(g[2] for g in rest), 0.01)
            got = [g for g in got if g[3] >= 0.95] + [(g[0], g[1], round(g[2] * k, 2), g[3], g[4]) for g in rest]
        if not got and cands:   # money you sent this same person coming back (a split, a refund, a loan)
            mine = [o for o in cands if _party(o)[0] == person and amt <= -o["amount"] + 0.01]
            if mine:
                o = min(mine, key=lambda o: _days(r["date"], o["date"]))
                got.append((r["id"], o["id"], round(min(amt, -o["amount"]), 2), 0.85, "paid back by the person you paid"))
        if not got and cands:   # one purchase it pays for, wholly or a share of
            def fit(o):
                f = amt / -o["amount"]
                if f > 1.01:
                    return 0.0
                if abs(f - 1) < 0.01:
                    return 1.0
                return 0.75 if any(abs(f - 1 / k) < 0.02 for k in range(2, 7)) else 0.3
            decay = lambda o: math.exp(-_days(r["date"], o["date"]) / 45)
            ranked = [o for o in sorted(cands, key=lambda o: -fit(o) * decay(o))[:8] if fit(o) > 0]
            if ranked:
                ps = jev._post("/v1/judge", {"texts": [_describe(o, orders) for o in ranked],
                                             "question": f"Is this what {person}'s Venmo payment of ${amt:.2f} on {r['date']} "
                                                         f"(note: \"{note}\") pays back, fully or partly?",
                                             "instruct": "Decide whether a person's payment reimburses the owner for this purchase."})["p"]
                o, p = max(zip(ranked, ps), key=lambda op: op[1] * 0.6 + fit(op[0]) * decay(op[0]) * 0.4)
                conf = p * 0.6 + fit(o) * decay(o) * 0.4
                if p >= 0.5 and conf >= 0.5:
                    got.append((r["id"], o["id"], min(amt, -o["amount"]), round(conf, 3), "amount, timing and note"))
        if not got and cands:   # two or three purchases that add up to the payment
            recent = sorted([o for o in cands if _days(r["date"], o["date"]) <= 60], key=lambda o: _days(r["date"], o["date"]))[:14]
            for k in (2, 3):
                combos = [cs for cs in itertools.combinations(recent, k) if abs(sum(-o["amount"] for o in cs) - amt) <= max(0.01, amt * 0.005)]
                best = None
                for cs in combos[:6]:
                    p = _judge(person, amt, r["date"], note, list(cs), orders, together=True)
                    if p >= 0.7 and (best is None or p > best[1]):   # sums can coincide: ask for more
                        best = (cs, p)
                if best:
                    got = [(r["id"], o["id"], round(-o["amount"], 2), round(0.5 + best[1] / 2, 3), "purchases adding up to it")
                           for o in best[0]]
                    break
        for g in got:
            left[g[1]] -= g[2]
        links += got
    log(f"repayments: {len(links)} link(s)")
    return links


def _suggest_lives(rows: list[dict], orders: dict, notes: dict, log) -> list[tuple]:
    """Ranks the lives against each other: a bare merchant name rarely convinces the model of any one
    of them, but the ranking still says which fits best. Kept when that one has at least half the
    weight among the four. A repayment note that describes the purchase is part of its context."""
    cdb = _db()
    have = {t: (k, c) for t, k, c in cdb.execute("SELECT txn_id, key, ctx FROM life")}
    out = []
    for r in rows:
        if r["amount"] > -DURABLE_MIN or not (r["cats"] & GOODS) or (r["merchant"] in ("Amazon",) and r["id"] not in orders):
            continue   # small, not goods, or an Amazon charge whose items we don't know yet
        text = _describe(r, orders) + "".join(f'. Someone repaying it wrote: "{n}"' for n in notes.get(r["id"], []))
        h = hashlib.sha256(text.encode()).hexdigest()[:16]
        if have.get(r["id"]) == (LIFE_KEY, h):
            continue
        yes = jev.choices_raw(text, "How long does this purchase keep being useful to its owner?", [l for l, _ in LIVES],
                              "Decide how long the thing bought keeps serving the person who bought it.")
        ws = [yes.get(l.split(":")[0], 0.0) for l, _ in LIVES]
        k = max(range(len(LIVES)), key=ws.__getitem__)
        out.append((r["id"], LIFE_KEY, h, LIVES[k][1], round(ws[k] / (sum(ws) or 1), 3)))
    log(f"durable goods: {len(out)} judged")
    return out


def update(con, log=print) -> dict:
    """Infer repayment links and durable lives (jev), and store them. Run by `ledger classify`."""
    rows = _rows(con)
    orders = merchants.match(con)["by_txn"]
    links = _link_repayments(rows, orders, log)
    by_id = {r["id"]: r for r in rows}
    notes = {}
    for i, o, *_ in links:
        notes.setdefault(o, []).append(_party(by_id[i])[1])
    lives = _suggest_lives(rows, orders, notes, log)
    cdb = _db()
    with cdb:
        cdb.execute("DELETE FROM repay_link")
        cdb.executemany("INSERT OR REPLACE INTO repay_link VALUES (?,?,?,?,?)", links)
        cdb.executemany("INSERT OR REPLACE INTO life VALUES (?,?,?,?,?)", lives)
    return {"links": len(links), "lives": len(lives)}


# ---------------------------------------------------------------- read time

def analyze(con) -> dict:
    """Everything the dashboard and portal need, with your annotations applied over inferences."""
    rows = _rows(con)
    by_id = {r["id"]: r for r in rows}
    cdb = _db()
    auto = [dict(zip(("inflow", "outflow", "amount", "conf", "method"), x)) for x in cdb.execute("SELECT * FROM repay_link")]
    lives = {t: (m, p) for t, m, p in cdb.execute("SELECT txn_id, months, p FROM life WHERE key=?", (LIFE_KEY,))}
    household = people().get("household", [])

    links = []
    for r in rows:   # your settlements first; auto links only where you haven't settled
        for x in r["ov"].get("repays", []):
            if x.get("txn") in by_id:
                links.append({"inflow": r["id"], "outflow": x["txn"], "amount": float(x["amount"]), "conf": 1.0,
                              "method": "confirmed by you"})
    settled = {r["id"] for r in rows if r["ov"].get("repays") or r["ov"].get("repays_none")}
    links += [l for l in auto if l["inflow"] not in settled and l["inflow"] in by_id and l["outflow"] in by_id
              and l["outflow"] not in by_id[l["inflow"]]["ov"].get("not_repays", [])]
    for l in links:
        l["person"] = _party(by_id[l["inflow"]])[0] or "someone"

    split, repaid_in = {}, {}
    for l in links:
        repaid_in[l["inflow"]] = repaid_in.get(l["inflow"], 0) + l["amount"]
    for r in rows:
        if r["amount"] >= 0:
            continue
        a = -r["amount"]
        f = r["ov"].get("for")
        if f:
            shares = {}
            for who, x in f.items():
                if who == HOUSEHOLD:   # shared equally by you and the household's members
                    for m in [ME, *household]:
                        shares[m] = shares.get(m, 0) + x / (1 + len(household))
                else:
                    shares[who] = shares.get(who, 0) + x
            z = sum(shares.values()) or 1
            shares = {k: v / z for k, v in shares.items()}
        else:
            shares = {}
            for l in links:
                if l["outflow"] == r["id"]:
                    shares[l["person"]] = shares.get(l["person"], 0) + l["amount"] / a
            if not shares:
                continue
            shares[ME] = max(0.0, 1 - sum(shares.values()))
        split[r["id"]] = shares

    balances = {}
    for tid, shares in split.items():
        a = -by_id[tid]["amount"]
        for who, x in shares.items():
            if who != ME and x > 0:
                b = balances.setdefault(who, {"person": who, "fronted": 0.0, "repaid": 0.0, "items": []})
                b["fronted"] += a * x
                b["items"].append({"txn": tid, "date": by_id[tid]["date"], "desc": by_id[tid]["desc"], "share": x,
                                   "amount": round(a * x, 2)})
    for l in links:
        b = balances.setdefault(l["person"], {"person": l["person"], "fronted": 0.0, "repaid": 0.0, "items": []})
        b["repaid"] += l["amount"]
    for b in balances.values():
        b["net"] = round(b["fronted"] - b["repaid"], 2)   # > 0: they owe you (or it was a gift)
        b["fronted"], b["repaid"] = round(b["fronted"], 2), round(b["repaid"], 2)

    life = {}
    for r in rows:
        m = r["ov"].get("life_months")
        if m is not None:
            life[r["id"]] = int(m)
        elif r["id"] in lives and lives[r["id"]][1] >= 0.5:
            life[r["id"]] = lives[r["id"]][0]
    return {"links": links, "split": split, "repaid_in": repaid_in, "life": {k: v for k, v in life.items() if v > 1},
            "people": sorted(balances.values(), key=lambda b: -abs(b["net"])), "household": household}


def spread(d: str, amount: float, months: int, until: str) -> list[tuple[str, float]]:
    """A durable purchase as equal monthly slices over its life, up to `until`."""
    out, day0 = [], date.fromisoformat(d)
    for k in range(months):
        y, m = divmod(day0.month - 1 + k, 12)
        dk = date(day0.year + y, m + 1, min(day0.day, 28))
        if dk.isoformat() > until:
            break
        out.append((dk.isoformat(), amount / months))
    return out
