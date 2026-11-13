"""Amazon order history, as saved by a puller.

Orders aren't money movements, so this yields no observations: each order becomes a
merchant_order row (what was bought, for whom, the total, the card), and merchants.py links
it to the card charges that paid for it. A pull restates the orders it saw; the newest report
of each order wins, and the newest one read from its details page supplies item prices.
"""
import json
from decimal import Decimal
from pathlib import Path

from ..model import MerchantOrder, Parsed

NAME, VERSION, FEED = "amazon_orders", 1, "amazon"


def detect(path: Path, head: bytes) -> bool:
    return path.suffix == ".json" and b'"source": "amazon-orders"' in head


def parse(path: Path) -> Parsed:
    doc = json.loads(path.read_text())
    out = Parsed(NAME, VERSION, FEED)
    for o in doc["orders"]:
        if not o.get("placed"):
            out.warnings.append(f"order without a date skipped")
            continue
        out.orders.append(MerchantOrder(
            merchant="amazon", order_id=o["id"], placed=o["placed"], items=o.get("items") or [],
            total=Decimal(o["total"]) if o.get("total") else None, status=o.get("status"),
            ship_to=o.get("ship_to"), payment=o.get("payment"),
            detail=o["summary"] if o.get("summary") is not None else None, pulled_at=doc["pulled_at"]))
    days = sorted(o.placed for o in out.orders)
    out.coverage = (days[0], days[-1]) if days else None
    return out
