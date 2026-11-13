"""Writes examples/simplefin-sample.json: six months of an invented person's checking account and credit card, in
SimpleFIN's /accounts format. Every name, merchant and amount is made up; it exists to try the ledger and portal.

  uv run python examples/make_sample.py
"""
import datetime as dt
import json
import random
from pathlib import Path

rnd = random.Random(7)
START = dt.date(2025, 1, 1)
DAYS = 181
NOON = lambda d: int(dt.datetime(d.year, d.month, d.day, 12, tzinfo=dt.UTC).timestamp())   # date-only


def txns(prefix, events):
    return [{"id": f"{prefix}-{i:04d}", "posted": NOON(d), "amount": f"{amt:.2f}", "description": desc}
            for i, (d, amt, desc) in enumerate(sorted(events))]


checking, card = [], []
for m in range(6):
    first = dt.date(2025, 1 + m, 1)
    checking.append((first + dt.timedelta(days=14), 2450.00, "ACME CORP PAYROLL DIR DEP"))
    checking.append((first + dt.timedelta(days=27), 2450.00, "ACME CORP PAYROLL DIR DEP"))
    checking.append((first, -1400.00, "ZELLE TO ANN LEE RENT"))
    checking.append((first + dt.timedelta(days=4), -62.10, "PG&E WEB ONLINE PAYMENT"))
    checking.append((first + dt.timedelta(days=9), -45.00, "VERIZON WIRELESS PAYMENT"))
    checking.append((first + dt.timedelta(days=20), -200.00, "COINBASE.COM BUY"))
    checking.append((first + dt.timedelta(days=27), 1.12, "INTEREST PAYMENT"))
    card.append((first + dt.timedelta(days=11), -15.49, "NETFLIX.COM"))
    card.append((first + dt.timedelta(days=17), -20.00, "CLAUDE.AI SUBSCRIPTION"))
for i in range(DAYS):
    d = START + dt.timedelta(days=i)
    if d.weekday() == 5:
        card.append((d, -round(rnd.uniform(45, 110), 2), "TRADER JOE S #123 SPRINGFIELD"))
    if rnd.random() < 0.18:
        card.append((d, -round(rnd.uniform(9, 38), 2), rnd.choice(["TST* NOODLE HOUSE", "SQ *CORNER CAFE", "CHIPOTLE 0456"])))
    if rnd.random() < 0.06:
        card.append((d, -round(rnd.uniform(12, 140), 2), "AMAZON MKTPL*AB12CD34"))
    if rnd.random() < 0.05:
        card.append((d, -round(rnd.uniform(8, 30), 2), "LYFT *RIDE"))
for m in range(1, 6):   # pay the card from checking each month: a transfer between your own accounts
    d = dt.date(2025, 1 + m, 3)
    owed = -sum(a for day, a, _ in card if dt.date(2025, m, 1) <= day < dt.date(2025, m + 1, 1))
    checking.append((d, -round(owed, 2), "ONLINE TRANSFER TO CARD ...5678"))
    card.append((d, round(owed, 2), "ONLINE TRANSFER FROM CHK ...1234"))

end = START + dt.timedelta(days=DAYS)
as_of = int(dt.datetime(end.year, end.month, end.day, 18, tzinfo=dt.UTC).timestamp())
accounts = []
for aid, name, opening, events in (("ex-chk", "Everyday Checking ...1234", 3000.00, checking),
                                   ("ex-card", "Rewards Credit Card ...5678", 0.00, card)):
    accounts.append({"id": aid, "name": name, "conn_id": "ex-conn", "currency": "USD",
                     "balance": f"{opening + sum(a for _, a, _ in events):.2f}", "balance-date": as_of,
                     "transactions": txns(aid, events)})
doc = {"errlist": [], "connections": [{"conn_id": "ex-conn", "org_name": "Acme Bank"}], "accounts": accounts}
out = Path(__file__).with_name("simplefin-sample.json")
out.write_text(json.dumps(doc, indent=1) + "\n")
print(f"wrote {out} ({sum(len(a['transactions']) for a in accounts)} transactions)")
