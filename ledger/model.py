"""Parser output types and time/money helpers. Parsers emit these; ingest.py stores them."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from zoneinfo import ZoneInfo

from . import config

UTC = dt.timezone.utc
PRECISIONS = ("year", "month", "day", "hour", "minute", "second", "ms", "us")
# Calendar day of instants recorded in UTC (card swipes etc.) for budgeting. Day-precision
# values keep the day their source gave, in the source's own zone. The owner's zone: config [ledger] timezone.
LOCAL_TZ = config.get("ledger", "timezone")


def local_day(value: str, precision: str, tz: str | None, lo: int) -> str:
    if precision in ("year", "month", "day"):
        return value[:10]
    z = ZoneInfo(tz if tz and tz != "UTC" else LOCAL_TZ)
    return dt.datetime.fromtimestamp(lo, z).date().isoformat()


def h(*parts, n: int = 20) -> str:
    return hashlib.sha256(json.dumps(parts, default=str, separators=(",", ":")).encode()).hexdigest()[:n]


def dec(s: str | None) -> Decimal | None:
    """'$1,234.50' / '-$3.00' / '($3.00)' / '0.5' -> Decimal; '' -> None."""
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")") or "-" in s
    s = re.sub(r"[^\d.]", "", s)
    if not s:
        return None
    d = Decimal(s)
    return -d if neg else d


def qstr(d: Decimal) -> str:
    """Canonical exact decimal text: no exponent, no trailing zeros, '-0' -> '0'."""
    s = format(d.normalize(), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def pstr(d: Decimal) -> str:
    """Exact decimal text that keeps the printed precision ('1.50' stays '1.50', meaning +-0.005)."""
    s = format(d, "f")
    return s[1:] if s.startswith("-") and not d else s


def ulp(s: str) -> Decimal:
    """Unit in the last printed place: '1.50' -> 0.01, '12' -> 1."""
    return Decimal(1).scaleb(Decimal(s).as_tuple().exponent)


@dataclass(frozen=True)
class Time:
    role: str            # posted | transacted | settled | block
    value: str
    precision: str
    tz: str | None
    lo: int
    hi: int

    @property
    def local_date(self) -> str:
        return local_day(self.value, self.precision, self.tz, self.lo)


def day(d: dt.date, tz: str, role: str = "posted") -> Time:
    z = ZoneInfo(tz)
    lo = dt.datetime(d.year, d.month, d.day, tzinfo=z)
    hi = lo + dt.timedelta(days=1)
    return Time(role, d.isoformat(), "day", tz, int(lo.timestamp()), int(hi.timestamp()))


def instant(epoch: int, role: str, precision: str = "second") -> Time:
    t = dt.datetime.fromtimestamp(epoch, UTC)
    return Time(role, t.strftime("%Y-%m-%dT%H:%M:%SZ"), precision, "UTC", epoch, epoch + 1)


@dataclass(frozen=True)
class AccountRef:
    feed: str
    ext_key: str          # how this feed names the account (stable within the feed)
    institution: str      # slug: 'schwab'
    mask: str | None      # last digits, used to unify the same account across feeds
    name: str | None = None
    kind: str | None = None
    base_asset: str = "fiat:USD"


@dataclass
class Leg:
    asset: str
    quantity: Decimal
    role: str = "main"
    unit_price: Decimal | None = None
    price_asset: str | None = None
    account: AccountRef | None = None   # None = the observation's account


@dataclass
class Obs:
    locator: str
    account: AccountRef
    key: tuple            # within-feed natural key; ingest adds an occurrence index
    times: list[Time]
    legs: list[Leg]
    kind: str | None = None
    description: str | None = None
    payee: str | None = None
    memo: str | None = None
    ext_id: str | None = None
    pending: bool = False
    extra: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


@dataclass
class Snap:
    locator: str
    account: AccountRef
    asset: str
    kind: str             # balance | available | position | running
    as_of: Time
    quantity: Decimal | None = None
    value: Decimal | None = None
    value_asset: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class Realized:
    locator: str
    account: AccountRef
    asset: str
    closed: str           # day the broker closed the position
    quantity: Decimal
    closing_price: Decimal | None
    method: str | None
    proceeds: Decimal
    cost: Decimal
    gain: Decimal
    long_term: Decimal | None = None
    short_term: Decimal | None = None
    disallowed: Decimal = Decimal(0)


@dataclass
class MerchantOrder:
    merchant: str
    order_id: str
    placed: str          # YYYY-MM-DD
    items: list[dict]
    total: Decimal | None = None
    status: str | None = None
    ship_to: str | None = None
    payment: str | None = None
    detail: dict | None = None
    pulled_at: str = ""


@dataclass
class Parsed:
    parser: str
    version: int
    feed: str
    obs: list[Obs] = field(default_factory=list)
    snaps: list[Snap] = field(default_factory=list)
    realized: list[Realized] = field(default_factory=list)
    orders: list[MerchantOrder] = field(default_factory=list)
    coverage: tuple[str, str] | None = None
    meta: dict = field(default_factory=dict)
    assets: dict[str, dict] = field(default_factory=dict)   # asset id -> {name, symbol, ...}
    warnings: list[str] = field(default_factory=list)


def asset_kind(asset_id: str) -> str:
    return {"fiat": "fiat", "sec": "security", "crypto": "crypto"}.get(asset_id.split(":", 1)[0], "other")
