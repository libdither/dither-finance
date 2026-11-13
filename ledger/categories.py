"""What each dollar is (its kind), and what it was (rule categories, which become tags).

A transaction has exactly one **kind**: the thing every number in the app is computed from (spending, essentials,
the allowance, money left over, income by how certain it is). The user's kind wins (txn_annotation.category holds
the kind's name since 2026-11-12), then a rule's, then the model's (classify.py), then the rule's fallback. Kinds are
few and far apart on purpose: essential or not is a judgment about the purchase, made per transaction.

The rules below still name a **category** (Groceries, Dining, AI & software…) and a merchant from the description.
A category is only a step: it maps to one kind and one descriptive tag (CATEGORIES), the "what it was" the dashboard
breaks spending down by. Tags never partition money; kinds do.

Kinds sit in groups: 'essential' and 'discretionary' are consumption; 'other' is money that leaves without being
consumption (taxes, investing, transfers, money fronted for others); 'income' comes in, with a certainty class the
budget model uses (earned and steady are planned on; windfalls count once they arrive; investment returns are
already part of the return on savings). A kind in a non-income group also takes money in: a refund, or someone
paying you back ("Fronted for others" nets to what was never repaid).

overrides.for (flows.py) says who a purchase served; only your share counts as spending.
"""
import re
import tomllib
from pathlib import Path

from .db import DATA

# kind: (group, income certainty, what it means, for the model)
KINDS = {
    "Essential": ("essential", None, "a need: rent, groceries, utilities, phone and internet, insurance, getting to work or "
                  "school, medical care and medication, tuition, required fees, things a home can't do without"),
    "Discretionary": ("discretionary", None, "a want: eating out, entertainment, games, media, gadgets, hobbies, trips for "
                      "fun, software and services that could be dropped"),
    "Giving": ("discretionary", None, "donations to charities and causes, and presents or money given to friends and family"),
    "Unsorted": ("discretionary", None, "a purchase whose purpose can't be told yet"),
    "Taxes": ("other", None, "income tax payments"),
    "Investing": ("other", None, "money put into investments or taken out of them: a brokerage, a crypto exchange, coins "
                  "sold or bought, a prediction market, interest on money borrowed to invest; never a store or restaurant"),
    "Own transfer": ("other", None, "money moved to another account of the owner's, or out to an account whose owner is unclear"),
    "Fronted for others": ("other", None, "something bought on someone else's behalf, money lent to a person, or their "
                           "paying it back"),
    "Earned": ("income", "earned", "pay for work: salary, wages, contracting, stipends"),
    "Steady income": ("income", "steady", "regular income besides work: financial aid refunds, solar credits, benefits"),
    "Investment returns": ("income", "investment", "interest and dividends"),
    "Windfall": ("income", "windfall", "one-off money: a gift, a tax refund, a deposited check, money from a person"),
    "Unexplained": ("income", "unknown", "money arriving from an account whose owner is unclear"),
}
GROUPS = {k: v[0] for k, v in KINDS.items()}             # kind -> group: what the dashboard and the budget read
CERTAINTY = {k: v[1] for k, v in KINDS.items() if v[1]}  # income kind -> certainty class
HELP = {k: v[2] for k, v in KINDS.items()}

# rule category -> (kind, descriptive tag). Each category's kind is in the group the category had before kinds, so
# the switch (2026-11-12) moved no number; the user's old category labels became this kind and tag.
CATEGORIES = {
    "Groceries": ("Essential", "groceries"), "Rent": ("Essential", "rent"), "Utilities": ("Essential", "utilities"),
    "Insurance": ("Essential", "insurance"), "Phone & internet": ("Essential", "phone-internet"),
    "Transport": ("Essential", "transport"), "Education": ("Essential", "education"),
    "Health & personal": ("Essential", "health"), "Home": ("Essential", "home"), "Fees": ("Essential", "fees"),
    "Dining": ("Discretionary", "dining"), "Shopping": ("Discretionary", "shopping"),
    "AI & software": ("Discretionary", "software"), "Media & games": ("Discretionary", "media"),
    "Travel": ("Discretionary", "travel"), "Memberships": ("Discretionary", "membership"),
    "Giving": ("Giving", "charity"), "Gifts given": ("Giving", "gifts"),
    "People (Venmo/PayPal)": ("Unsorted", "people"), "Uncategorized": ("Unsorted", None),
    "Taxes": ("Taxes", None), "Investing": ("Investing", None), "Margin interest": ("Investing", "margin-interest"),
    "Unlabeled transfer out": ("Own transfer", None), "Bought for others": ("Fronted for others", None),
    "Loans to people": ("Fronted for others", "loan"), "Work": ("Earned", "work"),
    "Financial aid refunds": ("Steady income", "financial-aid"), "Solar credits": ("Steady income", "solar"),
    "Investment income": ("Investment returns", None), "Gifts received": ("Windfall", "gift"),
    "From people": ("Windfall", "from-people"), "Checks deposited": ("Windfall", "check"),
    "Tax refunds": ("Windfall", "tax-refund"), "Transfer in (unlabeled)": ("Unexplained", None),
}
assert all(k in KINDS for k, _ in CATEGORIES.values())


def kind_of(category: str) -> str:
    return CATEGORIES[category][0]


def what_of(category: str) -> str | None:
    """The descriptive tag a rule category stands for (None for the placeholders)."""
    return CATEGORIES[category][1]


WHATS = {t for _, t in CATEGORIES.values() if t}


def what_for(rule: str, manual_tags: list | None) -> str | None:
    """What it was, as one descriptive tag: one of the user's tags that names a category (an old category label
    became one), else the rule's; a "-tag" the user set removes the rule's."""
    for t in manual_tags or []:
        if t in WHATS:
            return t
    w = CATEGORIES[rule][1]
    return None if w and "-" + w in (manual_tags or []) else w


def categories_of(rule: str, manual_kind: str | None, manual_tags: list | None) -> set[str]:
    """The categories a transaction stands for, for checks that ask what it was (flows.py): the user's, through their
    kind and tags, when they labeled it; else the rule's."""
    if manual_kind:
        return {manual_kind} | {c for c, (_, t) in CATEGORIES.items() if t and t in (manual_tags or [])}
    return {rule}


# Keyword rules: the owner's in $FINANCE_HOME/data/rules.toml, else the repo's generic starter set (ledger/rules.toml).
# Patterns match the upper-cased description; the first match wins.
RULES_FILE = DATA / "rules.toml"
if not RULES_FILE.exists():
    RULES_FILE = Path(__file__).with_name("rules.toml")
_R = tomllib.loads(RULES_FILE.read_text())
SPEND = [(r[0], r[1], r[2] if len(r) > 2 else None) for r in _R.get("spend", [])]    # (pattern, category, merchant)
INCOME = [(r[0], r[1], r[2] if len(r) > 2 else None) for r in _R.get("income", [])]
SPEND_RX = [(re.compile(p), c, m) for p, c, m in SPEND]
INCOME_RX = [(re.compile(p), c, m) for p, c, m in INCOME]
VENMO_OUT = [(re.compile(p), c) for p, c in _R.get("venmo_out", [])]   # matched against a Venmo note
VENMO_IN = [(re.compile(p), c) for p, c in _R.get("venmo_in", [])]
TAG_RULES = [(p, tuple(ts)) for p, ts in _R.get("tags", [])]
assert all(c in CATEGORIES for _, c, _ in SPEND + INCOME) and all(c in CATEGORIES for _, c in VENMO_OUT + VENMO_IN), \
    f"{RULES_FILE}: a rule names a category not in CATEGORIES"


def merchant_of(desc: str) -> str:
    d = re.sub(r"^(SQ|TST|PY|SP|GB|WL|MCF|LS) ?\*\s*", "", desc.upper())
    d = re.sub(r"\*.*$|#\S+|\b\d[\w-]*|\.\.\.|\s+-\s+.*$", " ", d)
    words = [w for w in d.split() if w not in {"VISA", "DIRECT", "NEW", "YORK"}][:3]
    return " ".join(words).title() or "Unknown"


def _venmo(d: str, desc: str, kind: str | None, amount: float):
    party = desc.split(":", 1)[0].strip() or "Venmo"
    if (kind or "") == "fee":
        return "Fees", "Venmo"
    note = d.split(":", 1)[1] if ":" in d else ""
    if amount < 0:
        for rx, cat, merchant in SPEND_RX:   # merchant payments (eBay, DoorDash)
            if rx.search(d):
                return cat, merchant or party
        return next((c for rx, c in VENMO_OUT if rx.search(note)), "People (Venmo/PayPal)"), party
    return next((c for rx, c in VENMO_IN if rx.search(note)), "From people"), party


def categorize(desc: str | None, account: str, kind: str | None, amount: float, acct_kind: str | None):
    """-> (rule category, merchant). amount is the owned account's side: negative = money out; acct_kind is the
    account's kind (account.kind: 'checking', 'brokerage', 'wallet'…)."""
    d = (desc or "").upper().strip()
    if acct_kind == "brokerage":   # investment flows, not consumption
        if amount > 0 and not re.search(r"WIRED FUNDS|FUNDS RECEIVED|^TFR ", d):
            return "Investment income", "Dividends & interest"
        if amount < 0 and ((kind or "").lower() == "margin interest" or d.startswith("INTEREST ")):
            return "Margin interest", "Margin interest"
    if account.startswith("venmo"):   # "Person: note"; Venmo's own merchant rows are just the merchant
        return _venmo(d, desc or "", kind, amount)
    rules = INCOME_RX if amount > 0 else SPEND_RX
    for rx, cat, merchant in rules:
        if rx.search(d):
            return cat, merchant or merchant_of(d)
    if amount > 0:   # a refund from a merchant: credit it to that merchant's category
        for rx, cat, merchant in SPEND_RX:
            if rx.search(d):
                return cat, merchant or merchant_of(d)
    if acct_kind == "brokerage":
        return "Investing", merchant_of(d)
    return ("Transfer in (unlabeled)" if amount > 0 else "Uncategorized"), merchant_of(d)


# Tags: facets that cut across categories (a payment can have several: privacy, a trip, a project). A tag never partitions
# money, so nothing in the budget sums by tag. Effective tags = the rules' tags over the upper-cased description, plus
# derived ones (xmr: paid from or into a Monero wallet; subscription: the dashboard's recurring charges), plus the
# user's own (txn_annotation.tags, a JSON list), where "-name" removes a rule's or derived tag.
TAG_RX = [(re.compile(p), t) for p, t in TAG_RULES]
TAG_NAME = re.compile(r"^-?[a-z0-9][a-z0-9:_-]{0,39}$")


def clean_tags(tags) -> list[str]:
    """A user's tags as stored: lower case, trimmed, valid names only (a leading - removes a rule's tag), no repeats."""
    out = []
    for t in tags or []:
        t = str(t).strip().lower().replace(" ", "-")
        if TAG_NAME.match(t) and t not in out:
            out.append(t)
    return out


def tags_for(desc: str | None, account: str, manual: list | None = None, derived=()) -> list[str]:
    d = (desc or "").upper()
    out = set(derived)
    for rx, ts in TAG_RX:
        if rx.search(d):
            out.update(ts)
    if account.startswith("monero"):
        out.add("xmr")
    for t in manual or []:
        if t.startswith("-"):
            out.discard(t[1:])
        else:
            out.add(t)
    return sorted(out)
