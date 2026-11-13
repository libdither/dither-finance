"""Monero view-only wallet pulls, as saved by a puller.

One account per wallet ('monero' feed, base asset crypto:XMR). Each transfer the wallet reports
is an observation keyed by (txid, type): 'in' adds its amount, 'out' removes amount + fee
(the wallet already nets out change). Every pull restates the whole history, so repeated
fingerprints across files collapse into one txn.

A view-only wallet without imported key images can't see spends: it lists no 'out' transfers
and counts change outputs of your own payments as 'in'. Such pulls carry a warning and no
balance snapshot, since the balance it reports would be wrong.
"""
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from ..model import AccountRef, Leg, Obs, Parsed, Snap, instant

NAME, VERSION, FEED = "monero_json", 1, "monero"
XMR, ATOMIC = "crypto:XMR", Decimal(10) ** 12
PENDING = {"pending", "pool"}


def detect(path: Path, head: bytes) -> bool:
    return path.suffix == ".json" and b'"source": "monero-wallet-rpc"' in head


def parse(path: Path) -> Parsed:
    doc = json.loads(path.read_text())
    out = Parsed(NAME, VERSION, FEED)
    out.assets[XMR] = {"symbol": "XMR", "name": "Monero"}
    ref = AccountRef(FEED, f"wallet-{doc['wallet']}", "monero", None, "Monero wallet", "wallet", XMR)
    pulled = int(datetime.strptime(doc["pulled_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())
    for t in doc["transfers"]:
        kind = t["type"]
        amt = Decimal(t["amount"]) / ATOMIC
        fee = Decimal(t.get("fee") or 0) / ATOMIC
        if kind in ("out", "pending"):
            legs = [Leg(XMR, -(amt + fee), "cash")]
        elif kind in ("in", "pool"):
            legs = [Leg(XMR, amt, "cash")]
        else:
            continue
        out.obs.append(Obs(
            locator=f"txid={t['txid']};type={kind}", account=ref, key=(t["txid"], "out" if kind in ("out", "pending") else "in"),
            times=[instant(t["timestamp"] or pulled, "block", "minute")], legs=legs,
            kind="receive" if legs[0].quantity > 0 else "send",
            description="Monero received" if legs[0].quantity > 0 else "Monero sent",
            ext_id=t["txid"], pending=kind in PENDING, extra={"height": t.get("height"), "fee": str(fee)},
            raw={k: t.get(k) for k in ("txid", "type", "height", "subaddr_index")}))
    if doc.get("key_images"):
        out.snaps.append(Snap("balance", ref, XMR, "balance", instant(pulled, "as_of"), Decimal(doc["balance"]) / ATOMIC))
    else:
        out.warnings.append("view-only pull without key images: spends are invisible and change shows as received")
    days = sorted(datetime.fromtimestamp(t["timestamp"], timezone.utc).date().isoformat()
                  for t in doc["transfers"] if t.get("timestamp"))
    out.coverage = (days[0] if days else doc["pulled_at"][:10], doc["pulled_at"][:10])
    out.meta = {"height": doc["height"], "key_images": bool(doc.get("key_images"))}
    return out
