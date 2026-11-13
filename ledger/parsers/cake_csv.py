"""Cake Wallet's transaction history export (Transactions → Export CSV), for a Monero wallet.

What a view-only wallet can't see without key images, Cake knows: which transactions you sent, with their fee, and
the note you wrote on each. It is data you hand over yourself, so it is given on the portal (Connections, the Monero
card: portal.upload_cake), which saves it byte for byte as data/raw/cake/`cake-<address's first 8>-<UTC stamp>-
<sha8>.csv`; the file itself doesn't say which wallet it is, so the name does (as Venmo's names carry their period).

The format (cake_wallet lib/core/csv_export_service.dart, since 2026-05): a UTF-8 BOM, then
`record_type,date_time,type,amount,currency,fee,tx_id,address,status,note,from_amount,from_currency,to_amount,
to_currency,trade_id,provider,confirmations`. Only `transaction` rows in XMR are read (swaps and orders are other
records; a swap's XMR leg is also a transaction row). `amount` and `fee` are XMR decimals; `type` is incoming or
outgoing (an outgoing amount excludes change and fee, as wallet2 reports it); `tx_id` is `<txid>_<amount>_<account>_
<address index>`; `date_time` is UTC ISO; fields that start like a spreadsheet formula are prefixed with ' (unless
they are plain numbers).

Each row is an observation on the same account the view-only wallet's pulls use (feed `cake`, keyed like the wallet's
(txid, in|out)). ingest links the two by txid (TXID_FEEDS) and lets this feed speak for the account, so a payment's
change output, which the view-only wallet reports as received, stops counting. Every export restates the whole
history; repeated fingerprints collapse.
"""
import csv
import io
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ..model import AccountRef, Leg, Obs, Parsed, instant

NAME, VERSION, FEED = "cake_csv", 1, "cake"
XMR = "crypto:XMR"
HEADER = "record_type,date_time,type,amount,currency,fee,tx_id"
WALLET = re.compile(r"^cake-([0-9A-Za-z]{8})-")
FORMULA = set("=+-@\t\r")


def detect(path: Path, head: bytes) -> bool:
    return path.suffix == ".csv" and head.decode("utf-8", "replace").lstrip("﻿").startswith(HEADER)


def _field(v: str | None) -> str:
    """Undo Cake's spreadsheet-formula guard (a ' before =, +, -, @, tab or CR)."""
    v = v or ""
    return v[1:] if len(v) > 1 and v[0] == "'" and v[1] in FORMULA else v


def _xmr(v: str) -> Decimal:
    try:
        return Decimal(_field(v).strip() or "0")
    except InvalidOperation:
        raise ValueError(f"not an XMR amount: {v!r}")


def parse(path: Path) -> Parsed:
    m = WALLET.match(path.name)
    if not m:
        raise ValueError(f"{path.name}: a Cake Wallet export must be named cake-<wallet's first 8>-…csv (the portal's upload names it)")
    out = Parsed(NAME, VERSION, FEED)
    out.assets[XMR] = {"symbol": "XMR", "name": "Monero"}
    # the same account as the view-only wallet's pulls (parsers/monero.py)
    ref = AccountRef("monero", f"wallet-{m.group(1)}", "monero", None, "Monero wallet", "wallet", XMR)
    days, skipped = [], 0
    for r in csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"))):
        if r.get("record_type") != "transaction" or _field(r.get("currency")).upper() != "XMR":
            skipped += r.get("record_type") != "transaction"
            continue
        txid = _field(r["tx_id"]).split("_", 1)[0].lower()
        kind = _field(r["type"]).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", txid) or kind not in ("incoming", "outgoing"):
            out.warnings.append(f"row skipped: tx_id or type not understood ({kind!r})")
            continue
        amt, fee = _xmr(r["amount"]), _xmr(r.get("fee") or "")
        when = datetime.fromisoformat(_field(r["date_time"]).replace("Z", "+00:00"))
        note, addr = _field(r.get("note")).strip(), _field(r.get("address")).strip()
        sent = kind == "outgoing"
        days.append(when.date().isoformat())
        out.obs.append(Obs(
            locator=f"txid={txid};type={kind}", account=ref, key=(txid, "out" if sent else "in"),
            times=[instant(int(when.timestamp()), "block", "minute")],
            legs=[Leg(XMR, -(amt + fee) if sent else amt, "cash")],
            kind="send" if sent else "receive",
            description=note or ("Monero sent" if sent else "Monero received"),
            memo=note or None, ext_id=txid, pending=_field(r.get("status")).lower() == "pending",
            extra={k: v for k, v in (("fee", str(fee) if fee else None), ("address", addr or None), ("note", note or None)) if v},
            raw={"tx_id": _field(r["tx_id"]), "type": kind, "confirmations": _field(r.get("confirmations"))}))
    if skipped:
        out.warnings.append(f"{skipped} non-transaction row(s) (swaps, orders) not read")
    days.sort()
    out.coverage = (days[0], days[-1]) if days else None
    out.meta = {"wallet": m.group(1), "transactions": len(out.obs)}
    return out
