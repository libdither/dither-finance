"""Privacy.com transactions, as saved by a puller.

Privacy is modelled as a pass-through card account you owe ('privacy-cards'):
  * each card charge (or refund) is an observation on that account: money goes to the merchant;
  * each ACH that settles charges is an observation on the *funding* bank account, with a
    second leg into 'privacy-cards'. Same-account matching pairs it with the bank feed's own
    row for that debit, so the bank's statement line becomes a transfer to the card account
    and the merchant detail sits on the card event.
One ACH can settle several charges (rare), so ACH observations are keyed by ACH id. They are
marked relayed: if the bank's own feed covers that day and has no matching row, resolve sends
the bank side to ext:unreconciled instead of inventing a bank posting.

Money moved iff the row is not declined or voided and its `amount` (cents; negative = refund,
which Privacy issues as its own row) is non-zero. A positive row's `refunds` is the part of a
pay-at-authorization pull that was later returned (see _pulled); otherwise refunds restate
refund rows. An ACH carrying a return code (achResult)
never reached the bank, so it is dropped while its charge stands. The nested `refunds` list restates refund rows
and is ignored. `settledAmount` differs from what the bank debits for some rows (tips), so
`amount` is used. Unsettled charges older than STALE_DAYS at pull time are expired holds.
"""
import datetime as dt
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from ..model import AccountRef, Leg, Obs, Parsed, instant

NAME, VERSION, FEED = "privacy_json", 2, "privacy"
CARDS = AccountRef(FEED, "cards", "privacy", "cards", "Privacy.com cards", "card")
UNSETTLED = {"PENDING", "AUTHORIZED", "SETTLING"}
STALE_DAYS = 30


def detect(path: Path, head: bytes) -> bool:
    return path.suffix == ".json" and b'"source": "privacy.com"' in head


def _t(s: str | None, role: str):
    if not s:
        return None
    epoch = dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    return instant(int(epoch), role)


def _pulled(t: dict) -> int:
    """Cents the ACH took from the bank for this charge."""
    auth = (t.get("originalAuth") or {}).get("amount")
    if t["amount"] > 0 and t.get("refunds") and auth == t["amount"] + sum(r["amount"] for r in t["refunds"]):
        return auth
    return t["amount"]


def _funding(t: dict) -> AccountRef:
    return AccountRef(FEED, f"bank:{t['bankAccountUuid']}", "bank", None, t.get("fundingAccount") or None,
                      "checking")


def parse(path: Path) -> Parsed:
    doc = json.loads(path.read_text())
    out = Parsed(NAME, VERSION, FEED)
    pulled = doc["pulled_at"]
    seen, skipped, stale = set(), defaultdict(int), 0
    ach: dict[str, list[dict]] = defaultdict(list)
    for t in doc["transactions"]:
        uuid = t["transactionUuid"]
        if uuid in seen:
            skipped["duplicate"] += 1
            continue
        seen.add(uuid)
        status = t.get("settlementStatus")
        if status == "DECLINED" or not t.get("amount"):
            skipped["no money moved"] += 1
            continue
        authorized = _t(t.get("dateAuthorized"), "transacted")
        if status in UNSETTLED and not t.get("achID") and authorized and pulled - authorized.lo > STALE_DAYS * 86400:
            stale += 1
            continue
        if status == "VOIDED":   # reversed: any ACH it had carries a return code and nets out
            skipped["voided"] += 1
            continue
        amt = Decimal(t["amount"]) / 100
        times = [x for x in (authorized, _t(t.get("dateSettled"), "posted")) if x]
        out.obs.append(Obs(
            locator=f"txn={uuid}", account=CARDS, key=(uuid,), times=times,
            legs=[Leg("fiat:USD", -amt, "cash")], kind="refund" if amt < 0 else "card",
            description=t.get("descriptor"), payee=t.get("descriptor"), memo=t.get("memo") or None,
            ext_id=uuid, pending=status in UNSETTLED,
            extra={k: v for k, v in (("mcc", t.get("mcc")), ("category", (t.get("mccCategory") or {}).get("name")),
                                     ("card", t.get("lastFour")), ("status", status),
                                     ("card_fees", str(Decimal(t["totalFees"]) / 100) if t.get("totalFees") else None),
                                     ("currency", t.get("currencyCode"))) if v},
            raw=t))
        if t.get("achID") and t.get("achResult"):
            skipped["ach returned"] += 1   # the charge stands; the debit never reached the bank
        elif t.get("achID"):
            ach[t["achID"]].append(t)
    for ach_id, ts in sorted(ach.items()):
        # Pay-at-authorization charges (LITHIC_ACH) pull the full authorization when it happens;
        # if the merchant then settles for less, the difference comes back as a separate credit
        # recorded in `refunds` (amount = authorization - refunds). The bank sees both.
        debit = sum((Decimal(_pulled(t)) for t in ts), Decimal(0)) / 100
        credit = sum((Decimal(_pulled(t) - t["amount"]) for t in ts), Decimal(0)) / 100
        bank = _funding(ts[0])
        pending = any(t.get("settlementStatus") in UNSETTLED for t in ts)
        raw = {"achID": ach_id, "txns": [t["transactionUuid"] for t in ts]}
        sent = min(t["achSent"] for t in ts if t.get("achSent")) if any(t.get("achSent") for t in ts) else None
        times = [x for x in (_t(sent, "transacted"),) if x] or [x for x in (_t(ts[0].get("dateSettled"), "posted"),) if x]
        if debit:
            out.obs.append(Obs(
                locator=f"ach={ach_id}", account=bank, key=("ach", ach_id), times=times,
                legs=[Leg("fiat:USD", -debit, "cash"), Leg("fiat:USD", debit, "cash", account=CARDS)],
                kind="ACH", description="Privacy.com ACH", ext_id=ach_id, pending=pending,
                extra={"charges": len(ts), "relayed": True},   # about the bank account, as Privacy reports it
                raw=raw))
        if credit:
            settled = max(t["dateSettled"] for t in ts if t.get("dateSettled"))
            out.obs.append(Obs(
                locator=f"ach={ach_id};return", account=bank, key=("ach-return", ach_id),
                times=[_t(settled, "transacted")], legs=[Leg("fiat:USD", credit, "cash"),
                                                         Leg("fiat:USD", -credit, "cash", account=CARDS)],
                kind="ACH", description="Privacy.com ACH (authorization difference returned)", ext_id=ach_id,
                pending=pending, extra={"charges": len(ts), "relayed": True}, raw=raw))
    if stale:
        out.warnings.append(f"{stale} unsettled charge(s) older than {STALE_DAYS} days skipped as expired holds")
    out.meta.update(skipped=dict(skipped), stale=stale, total=doc.get("total"), pulled_at=pulled)
    if out.obs:
        days = [o.times[0].value[:10] for o in out.obs if o.times]
        out.coverage = (min(days), max(days))
    return out
