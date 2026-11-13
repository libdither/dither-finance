"""`ledger classify`: Claude picks the kind of each transaction the rules can't place.

The kind (categories.KINDS: Essential, Discretionary, Giving, Taxes, Investing, Own transfer, Fronted for others,
and for money in Earned, Steady income, Investment returns, Windfall) is what every number in the app is computed
from; what a purchase was (groceries, dining…) is a tag from the rules, not the model's job.

Asked about: rows whose rule is a placeholder (FALLBACK: Unsorted, a person's money, an unexplained transfer…) and
shopping purchases (need or want is a judgment about the items). Coins arriving aren't asked about (nearly always the
owner's own, and they count only once labeled). Each is described with everything the ledger knows: description,
account, amount and direction, date; the Venmo note and the other person; Privacy's merchant category; for an Amazon
charge, the items (and whether it shipped to someone else); your note on it; and how you labeled others from the same
merchant (unless the description only names how money moved: GENERIC). Claude (ledger/claude.py: `claude -p` on the
owner's subscription, no tools, nothing saved) answers each row's kind, how sure it is and why, BATCH rows a call, with
the owner's own labels as examples and data/classify-notes.txt (facts the owner wrote for it) in its instructions.
This sends those rows to Anthropic.

Results live in data/classify.db, outside the ledger (the ledger stays a function of data/raw and data/state.jsonl).
A result is reused while both the instructions (model, prompt, notes: RUN_KEY) and the transaction's context (ctx
hash) are unchanged, so editing a note re-runs exactly what it affects.

How results are used (effective): a kind you set wins; then the kind of a rule that names the merchant; then Claude's
answer unless it was guessing; then the rule's fallback (Unsorted, Windfall from a person, Unexplained…). Rows that
end on a fallback, or that Claude was only fairly sure of, are what the portal's Review page asks you about.
"""
import datetime as dt
import hashlib
import json
import sqlite3
import time

from . import claude, config, merchants, prices
from .categories import CATEGORIES, KINDS, categorize, kind_of
from .db import DATA

DB = DATA / "classify.db"
NOTES = DATA / "classify-notes.txt"   # the owner's facts for the model ("ACME FUND is a charity"), optional
PROMPT_VERSION = 11  # 7: kinds; 8-10: jev tuning; 11: Claude
BATCH = 40
# rule categories that are placeholders: their kind is a guess, so the model may do better, and you are asked
FALLBACK = {"Uncategorized", "People (Venmo/PayPal)", "From people", "Transfer in (unlabeled)", "Checks deposited",
            "Unlabeled transfer out"}
SURE = {"high": 0.95, "medium": 0.75, "low": 0.4}   # stored as p
USE_AT, SURE_AT = 0.7, 0.9   # an answer is used from medium up; below high it is still reviewed
# descriptions that name only how money moved; how you labeled one says nothing about the next
GENERIC = {"Monero Sent", "Monero Received", "Mobile check deposit", "Paypal Inst Xfer", "Paypal Transfer",
           "Outgoing Wire"}
# what each kind means, for the model (categories.HELP is what the page shows)
ASK = {
    "Essential": "a need: rent or a room for the night, groceries, utilities, phone and internet, insurance, rides and "
                 "transit, medical care, medication and medical supplies, tuition and school costs, basic clothing, "
                 "toiletries, household basics and repairs",
    "Discretionary": "a want: eating out, entertainment, events, games, media, gadgets and upgrades, hobbies, decor, "
                     "trips for fun, apps, AI tools and subscriptions that could be dropped",
    "Giving": "a donation to a charity, nonprofit or cause, or a present or money given to someone as a gift",
    "Unsorted": "money out whose purpose the record doesn't show",
    "Taxes": "income tax payments",
    "Investing": "money into or out of an investment: a brokerage, a crypto exchange, coins bought or sold, a prediction "
                 "market",
    "Own transfer": "money moved between the owner's own accounts, exchanges or wallets",
    "Fronted for others": "paying for something on another person's behalf or lending them money; and money in from a "
                          "person paying the owner back (the note often names the items, the meal or the bill)",
    "Earned": "pay for work: salary, wages, contracting, stipends",
    "Steady income": "regular income besides work: financial aid, education savings paying school costs, solar credits, "
                     "benefits",
    "Investment returns": "interest and dividends",
    "Windfall": "one-off money given to the owner: birthday or holiday money, a gift, a tax refund, a prize",
    "Unexplained": "money in whose source the record doesn't show",
}
assert set(ASK) == set(KINDS)
ABOUT = config.get("claude", "about")   # a sentence or two about the owner (config [claude] about), optional
SYSTEM = ("You sort one person's transactions into kinds for their own budgeting app. " + (ABOUT.strip() + " " if ABOUT.strip() else "")
          + "Each transaction has exactly one kind:\n"
          + "\n".join(f"- {k}: {v}" for k, v in ASK.items()) + """

How to judge:
- A purchase is Essential or Discretionary by what was bought, not by the store: read the items and notes. Judge as
  a sensible, frugal person would: everyday necessities are Essential even when bought online; comfort, hobby and
  nice-to-have things are Discretionary.
- An order shipped to someone else is Giving unless the record says they paid for it (then Fronted for others).
- Money in from a person: Fronted for others when it pays the owner back for something (items, a meal, a bill, a
  thank-you for paying); Windfall when it's a gift.
- Follow how the owner labeled similar transactions; their labels are their own judgment. Similar means the same
  merchant or person, or the same kind of item or note; a description that only says how money moved ("Monero sent",
  "Outgoing Wire", a mobile check deposit) doesn't make two transactions similar.
- When the record gives nothing to go on (a bare "Monero sent", an unexplained transfer or check), answer Unsorted
  for money out or Unexplained for money in, with sure "low". Don't guess from the amount alone.
- sure: "high" when the record makes it plain, "medium" for a reasonable reading, "low" when guessing.
- why: one short plain sentence naming the evidence.""")


def _notes() -> str:
    try:
        return NOTES.read_text().strip()
    except OSError:
        return ""


def _system() -> str:
    n = _notes()
    return SYSTEM + (f"\n\nFacts the owner wrote for you:\n{n}" if n else "")


SCHEMA = {"type": "object", "required": ["rows"], "properties": {"rows": {"type": "array", "items": {
    "type": "object", "required": ["id", "kind", "sure", "why"],
    "properties": {"id": {"type": "string"}, "kind": {"type": "string", "enum": list(KINDS)},
                   "sure": {"type": "string", "enum": list(SURE)}, "why": {"type": "string"}}}}}}
RUN_KEY = hashlib.sha256(json.dumps([claude.MODEL, PROMPT_VERSION, _system(), SCHEMA]).encode()).hexdigest()[:16]


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(DB, timeout=30)
    con.execute("""CREATE TABLE IF NOT EXISTS result (txn_id TEXT PRIMARY KEY, run_key TEXT NOT NULL, ctx TEXT NOT NULL,
                   probs TEXT NOT NULL, top TEXT NOT NULL, p REAL NOT NULL, at TEXT NOT NULL)""")
    if "why" not in {r[1] for r in con.execute("PRAGMA table_info(result)")}:
        con.execute("ALTER TABLE result ADD COLUMN why TEXT")
    DB.chmod(0o600)
    return con


def _rows(con, ids=None):
    q = """SELECT l.txn_id, l.date, l.account_id, l.description, l.kind, l.amount_f, l.category, l.note, l.counterparty,
                  coalesce(n.name, l.account_id) acct, a.base_asset, a.kind acct_kind
           FROM ledger l JOIN account a ON a.id = l.account_id LEFT JOIN account_annotation n ON n.account_id = l.account_id
           WHERE l.flow = 'external' AND (a.base_asset = 'fiat:USD' OR a.base_asset LIKE 'crypto:%') AND NOT l.pending
             AND a.kind IS NOT 'brokerage'
             AND NOT EXISTS (SELECT 1 FROM posting px WHERE px.txn_id = l.txn_id AND px.account_id = 'ext:unreconciled')"""
    rows = con.execute(q).fetchall()
    return [r for r in rows if ids is None or r["txn_id"] in ids]


def _extras(con) -> dict:
    """Merchant category codes Privacy reported, by txn."""
    out = {}
    for tid, extra in con.execute("""SELECT l.txn_id, o.extra FROM txn_link l JOIN observation o ON o.id = l.obs_id
                                     WHERE o.feed = 'privacy' AND o.extra IS NOT NULL"""):
        e = json.loads(extra)
        if e.get("category"):
            out[tid] = e["category"]
    return out


def context(r, rule: str, merchant: str, extras: dict, orders: dict, owner: str | None, labeled: dict) -> str:
    amt = r["amount_f"]
    d = dt.date.fromisoformat(r["date"][:10])
    if r["base_asset"].startswith("crypto:"):   # coins: the amount, and what it was worth that day
        sym = r["base_asset"].split(":")[1]
        px = prices.price(sym, d.isoformat())
        money = f"{abs(amt):,.4f} {sym}" + (f" (about ${abs(amt) * px:,.2f} that day)" if px else "")
    else:
        money = f"${abs(amt):,.2f}"
    lines = [f"Date: {d.isoformat()} ({d.strftime('%A')})", f"Account: {r['acct']}",
             f"Money {'out' if amt < 0 else 'in'}: {money}", f"Description: {r['description'] or ''}"]
    if r["account_id"].startswith("venmo"):
        party, _, note = (r["description"] or "").partition(":")
        lines[-1] = f"Venmo {'payment to' if amt < 0 else 'payment from'}: {party.strip()}" + (f"; note: {note.strip()}" if note.strip() else "")
    if mcc := extras.get(r["txn_id"]):
        lines.append(f"Card network's merchant category: {mcc}")
    for o in orders.get(r["txn_id"], [])[:3]:
        items = "; ".join(f"{i['title'][:90]}" + (f" (${i['price']})" if i.get("price") else "") for i in o.get("items", [])[:6])
        lines.append(f"Amazon order: {items}" + (" — shipped to someone else, not the owner" if o.get("for_other") else ""))
    if r["note"]:
        lines.append(f"The owner's note: {r['note']}")
    if merchant not in GENERIC and (seen := labeled.get(merchant)):
        lines.append("The owner labeled other transactions from this merchant: " + ", ".join(f"{c} ({n}×)" for c, n in seen[:3]))
    return "Transaction:\n" + "\n".join(lines)


def _examples(con, n: int = 60) -> str:
    """The owner's own labels, newest first: their judgment, for the model to follow."""
    rows = con.execute("""SELECT l.date, l.account_id, l.description, l.amount_f, l.note, l.category FROM ledger l
                          WHERE l.category IS NOT NULL AND l.flow = 'external' ORDER BY l.date DESC LIMIT ?""", (n,))
    return "\n".join(f"- {r['description']}" + (f" (note: {r['note']})" if r["note"] else "")
                     + f", money {'out' if r['amount_f'] < 0 else 'in'}: {r['category']}" for r in rows)


def _labeled(con) -> dict:
    out: dict[str, dict] = {}
    for r in con.execute("""SELECT l.description, l.account_id, l.kind, l.amount_f, l.category, a.kind acct_kind
                            FROM ledger l JOIN account a ON a.id = l.account_id WHERE l.category IS NOT NULL"""):
        _, m = categorize(r["description"], r["account_id"], r["kind"], r["amount_f"], r["acct_kind"])
        out.setdefault(m, {}).setdefault(r["category"], 0)
        out[m][r["category"]] += 1
    return {m: sorted(cs.items(), key=lambda kv: -kv[1]) for m, cs in out.items()}


def asked(r, rule: str) -> bool:
    """Whether the model is asked about this row (see the module's docstring)."""
    if r["amount_f"] > 0 and r["base_asset"].startswith("crypto:"):
        return False
    return rule in FALLBACK or (rule == "Shopping" and r["amount_f"] < 0)


def run(con, ids: set | None = None, force: bool = False, limit: int | None = None, log=print) -> dict:
    if not claude.available():
        raise RuntimeError("claude isn't installed or signed in (~/.local/bin/claude, ~/.claude/.credentials.json)")
    m = merchants.match(con)
    orders, owner, extras, labeled = m["by_txn"], m["owner"], _extras(con), _labeled(con)
    cdb = _db()
    have = {r[0]: (r[1], r[2]) for r in cdb.execute("SELECT txn_id, run_key, ctx FROM result")}
    todo = []
    for r in _rows(con, ids):
        rule, merchant = categorize(r["description"], r["account_id"], r["kind"], r["amount_f"], r["acct_kind"])
        if not asked(r, rule):
            continue
        ctx = context(r, rule, merchant, extras, orders, owner, labeled)
        h = hashlib.sha256(ctx.encode()).hexdigest()[:16]
        if force or have.get(r["txn_id"]) != (RUN_KEY, h):
            todo.append((r, ctx, h))
    todo.sort(key=lambda x: x[0]["date"], reverse=True)   # newest first: they matter most
    if limit:
        todo = todo[:limit]
    examples = _examples(con)
    t0, n = time.time(), 0
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        prompt = ((f"How the owner labeled some transactions themselves:\n{examples}\n\n" if examples else "")
                  + "Give the kind of each transaction below.\n\n"
                  + "\n\n".join(f"[{j}] {ctx.removeprefix('Transaction:').strip()}" for j, (_, ctx, _) in enumerate(batch)))
        got = {a["id"].strip("[] "): a for a in claude.ask(_system(), prompt, SCHEMA)["rows"]}
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        for j, (r, _, h) in enumerate(batch):
            a = got.get(str(j))
            if not a or a["kind"] not in KINDS:
                continue   # left out: asked again next run
            p = SURE.get(a["sure"], SURE["low"])
            cdb.execute("INSERT OR REPLACE INTO result VALUES (?,?,?,?,?,?,?,?)",
                        (r["txn_id"], RUN_KEY, h, json.dumps({a["kind"]: p}), a["kind"], p, now, a["why"][:300]))
            n += 1
        cdb.commit()
        log(f"  {n}/{len(todo)} ({time.time() - t0:.0f}s)")
    return {"classified": n, "asked": len(todo), "seconds": round(time.time() - t0, 1), "run_key": RUN_KEY,
            "model": claude.MODEL}


def results() -> dict[str, dict]:
    """txn_id -> {probs, top, p, why} for the current run key."""
    if not DB.exists():
        return {}
    cdb = _db()
    return {t: {"probs": json.loads(p), "top": top, "p": pv, "why": why}
            for t, p, top, pv, why in cdb.execute("SELECT txn_id, probs, top, p, why FROM result WHERE run_key=?",
                                                  (RUN_KEY,))}


def effective(rule: str, merchant: str, manual: str | None, model: dict | None, amount: float) -> tuple[str, str]:
    """(kind, source): source is 'you', 'rule', 'model' or 'fallback'. `rule` is the rule's category; `manual` the
    kind you set. The model's answer counts unless it was guessing (USE_AT)."""
    if manual in KINDS:
        return manual, "you"
    if manual in CATEGORIES:   # a label from before kinds (2026-11-12), until it is converted
        return kind_of(manual), "you"
    shopping = rule == "Shopping" and amount < 0
    if not (rule in FALLBACK or shopping):
        return kind_of(rule), "rule"
    if model and model["top"] in KINDS and model["p"] >= USE_AT:
        return model["top"], "model"
    return kind_of(rule), "fallback"


def needs_review(rule: str, source: str, model: dict | None) -> bool:
    """Ask the user when the kind is a placeholder's (Unsorted, a person's money…) or the model was only fairly sure.
    Shopping isn't asked about: need or want is a cheap call to get wrong, and easy to flip by hand."""
    if rule == "Shopping":
        return False
    return (source == "fallback" and rule in FALLBACK) or (source == "model" and model["p"] < SURE_AT)
