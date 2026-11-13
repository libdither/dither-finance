"""Venmo statement CSVs, as saved by a puller (or downloaded by hand).

Venmo is modelled like Privacy.com: a pass-through account ('venmo') you hold a balance in.
  * every row is an observation on that account, keyed by Venmo's transaction ID: a payment
    out leaves it toward the person or merchant, a payment in arrives in it;
  * a payment funded from a bank or card ("Funding Source" other than the Venmo balance) also
    gets a relayed observation on that funding account, with a second leg into 'venmo'. The
    bank's own row for that debit (VENMO *Name ..., VENMO PURCHASE) pairs with it, so the bank
    line becomes a transfer and the note and person sit on the Venmo event;
  * a transfer out (cash-out) is a relayed observation on its destination account for what the
    bank receives (the amount less any instant-transfer fee); the fee is its own event.
Funding accounts appear as their own accounts until `ledger merge-account` pins them to the
real one (Venmo names a bank by its last four digits and a debit card by the card's).

Beginning and ending balances become balance assertions at the period's edges; the period
comes from the file name (venmo_<start>_<end>_..., or VenmoStatement_<Month>_<Year>).
Datetime is UTC.
"""
import calendar
import csv
import datetime as dt
import io
import re
from decimal import Decimal
from pathlib import Path

from ..model import LOCAL_TZ, AccountRef, Leg, Obs, Parsed, Snap, dec, instant

NAME, VERSION, FEED = "venmo_csv", 2, "venmo"
USD = "fiat:USD"
SKIP = {"canceled", "cancelled", "failed", "declined", "denied", "reversed"}
BALANCE = {"", "venmo balance"}
TZ = LOCAL_TZ   # statements give times in the owner's zone


def detect(path: Path, head: bytes) -> bool:
    return path.suffix.lower() == ".csv" and b"Account Statement - (@" in head and b"Funding Source" in head


def _period(path: Path) -> tuple[dt.date, dt.date] | None:
    if m := re.search(r"(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})", path.name):
        return dt.date.fromisoformat(m.group(1)), dt.date.fromisoformat(m.group(2))
    if m := re.search(r"([A-Za-z]+)_(\d{4})", path.name):
        months = {n.lower(): i for i, n in enumerate(calendar.month_name) if n}
        if (mo := months.get(m.group(1).lower())) is not None:
            y = int(m.group(2))
            return dt.date(y, mo, 1), dt.date(y, mo, calendar.monthrange(y, mo)[1])
    return None


def _funding(label: str) -> AccountRef:
    """'Charles Schwab Personal Checking *1234', 'Visa *5678' -> an account named as Venmo names it."""
    m = re.search(r"\*\s*(\d+)\s*$", label)
    inst = "schwab" if "schwab" in label.lower() else "card" if re.match(r"(visa|mastercard|amex|discover)\b", label, re.I) else "bank"
    kind = "card" if inst == "card" else "checking"
    return AccountRef(FEED, f"funding:{label}", inst, m.group(1) if m else None, label, kind)


def _end_of(d: dt.date) -> int:
    from zoneinfo import ZoneInfo
    return int(dt.datetime.combine(d + dt.timedelta(days=1), dt.time(), ZoneInfo(TZ)).timestamp()) - 1


def parse(path: Path) -> Parsed:
    text = path.read_text(encoding="utf-8-sig")
    out = Parsed(NAME, VERSION, FEED)
    rows = list(csv.reader(io.StringIO(text)))
    user = re.search(r"Account Statement - \(@([^)]+)\)", text)
    ref = AccountRef(FEED, f"user:{user.group(1) if user else 'me'}", "venmo", None, "Venmo", "wallet")
    h = next(i for i, r in enumerate(rows) if "ID" in r and "Datetime" in r)
    cols = {name.strip(): i for i, name in enumerate(rows[h])}
    get = lambda r, k: r[cols[k]].strip() if k in cols and cols[k] < len(r) else ""
    begin = end = None
    skipped = 0
    for n, r in enumerate(rows[h + 1:], start=h + 1):
        tid = get(r, "ID")
        if not tid:
            begin = dec(get(r, "Beginning Balance")) if begin is None and get(r, "Beginning Balance") else begin
            end = dec(get(r, "Ending Balance")) if get(r, "Ending Balance") else end
            continue
        status = get(r, "Status")
        if status.lower() in SKIP:
            skipped += 1
            continue
        when = dt.datetime.fromisoformat(get(r, "Datetime")).replace(tzinfo=dt.timezone.utc)
        t = instant(int(when.timestamp()), "transacted")
        amt = dec(get(r, "Amount (total)")) or Decimal(0)
        fee = abs(dec(get(r, "Amount (fee)")) or Decimal(0))
        typ, note = get(r, "Type"), get(r, "Note")
        frm, to = get(r, "From"), get(r, "To")
        src, dst = get(r, "Funding Source"), get(r, "Destination")
        pending = status.lower() == "pending"
        raw = {"type": typ, "status": status, "funding": src, "destination": dst}
        if "transfer" in typ.lower() and amt < 0:
            # cash-out: the bank gets the amount less the fee; the fee leaves to Venmo
            got = -amt - fee
            out.obs.append(Obs(
                locator=f"row={n};id={tid}", account=_funding(dst), key=("venmo-transfer", tid), times=[t],
                legs=[Leg(USD, got, "cash"), Leg(USD, -got, "cash", account=ref)], kind="transfer",
                description=f"Venmo {typ.lower()}", ext_id=tid, pending=pending, extra={"relayed": True}, raw=raw))
            if fee:   # its own event, so the transfer above stays a pure transfer
                out.obs.append(Obs(
                    locator=f"row={n};id={tid};fee", account=ref, key=("venmo-fee", tid), times=[t],
                    legs=[Leg(USD, -fee, "cash")], kind="fee", description=f"Venmo {typ.lower()} fee",
                    payee="Venmo", ext_id=tid, pending=pending, raw=raw))
            continue
        party = to if amt < 0 else frm
        out.obs.append(Obs(
            locator=f"row={n};id={tid}", account=ref, key=(tid,), times=[t], legs=[Leg(USD, amt, "cash")],
            kind=typ.lower(), description=f"{party}: {note}" if note else party, payee=party or None,
            memo=note or None, ext_id=tid, pending=pending,
            extra={k: v for k, v in (("from", frm), ("to", to), ("type", typ), ("fee", str(fee) if fee else None)) if v},
            raw=raw))
        if amt < 0 and src.lower() not in BALANCE:
            out.obs.append(Obs(
                locator=f"row={n};id={tid};funding", account=_funding(src), key=("venmo-funding", tid), times=[t],
                legs=[Leg(USD, amt, "cash"), Leg(USD, -amt, "cash", account=ref)], kind="transfer",
                description=f"Venmo: {party}", ext_id=tid, pending=pending, extra={"relayed": True}, raw=raw))
        elif amt > 0 and dst.lower() not in BALANCE:
            out.obs.append(Obs(
                locator=f"row={n};id={tid};destination", account=_funding(dst), key=("venmo-destination", tid),
                times=[t], legs=[Leg(USD, amt, "cash"), Leg(USD, -amt, "cash", account=ref)], kind="transfer",
                description=f"Venmo from {party}", ext_id=tid, pending=pending, extra={"relayed": True}, raw=raw))
    period = _period(path)
    if period:
        start, stop = period
        if begin is not None:
            out.snaps.append(Snap("begin", ref, USD, "balance", instant(_end_of(start - dt.timedelta(days=1)), "as_of"), begin))
        if end is not None:
            pulled = re.search(r"_(\d{8}T\d{6})Z", path.name)   # a period still open ends when it was pulled
            at = _end_of(stop) if not pulled else min(_end_of(stop), int(dt.datetime.strptime(pulled.group(1), "%Y%m%dT%H%M%S")
                                                                        .replace(tzinfo=dt.timezone.utc).timestamp()))
            out.snaps.append(Snap("end", ref, USD, "balance", instant(at, "as_of"), end))
        out.coverage = (start.isoformat(), stop.isoformat())
    else:
        out.warnings.append("statement period unknown (file name): no balance assertions")
    if skipped:
        out.meta["skipped"] = skipped
    return out
