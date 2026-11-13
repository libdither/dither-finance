"""Raw SimpleFIN /accounts responses, as saved by a puller."""
import json
import re
from decimal import Decimal
from pathlib import Path

from ..model import AccountRef, Leg, Obs, Parsed, Snap, dec, instant, day
import datetime as dt

NAME, VERSION, FEED = "simplefin_json", 1, "simplefin"
BANK_TZ = "America/New_York"   # SimpleFIN encodes bank dates as 12:00 UTC on that day


def detect(path: Path, head: bytes) -> bool:
    return path.suffix == ".json" and b'"accounts"' in head and (b'"errlist"' in head or b'"connections"' in head)


def _slug(s: str) -> str:
    s = s.lower()
    if "schwab" in s:
        return "schwab"
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "unknown"


def _kind(name: str) -> str | None:
    n = name.lower()
    for k, words in (("checking", ["checking"]), ("savings", ["saving"]), ("credit", ["credit", "card"]),
                     ("brokerage", ["invest", "brokerage", "ira", "401", "roth"])):
        if any(w in n for w in words):
            return k
    return None


def _time(epoch: int, role: str):
    if epoch % 86400 == 43200:   # date-only placeholder
        return day(dt.datetime.fromtimestamp(epoch, dt.timezone.utc).date(), BANK_TZ, role)
    return instant(epoch, role)


def _d(v):
    return None if v is None else dec(str(v))


def parse(path: Path) -> Parsed:
    doc = json.loads(path.read_text())
    out = Parsed(NAME, VERSION, FEED)
    orgs = {c["conn_id"]: c.get("org_name") or c.get("name") or "" for c in doc.get("connections", [])}
    out.meta["errlist"] = doc.get("errlist", [])
    for a in doc.get("accounts", []):
        name = a.get("name", "")
        m = re.search(r"\.\.\.(\d+)|\((\d+)\)\s*$", name)
        cur = a.get("currency", "USD")
        base = f"fiat:{cur}" if re.fullmatch(r"[A-Z]{3}", cur) else f"custom:{cur}"
        ref = AccountRef(FEED, a["id"], _slug(orgs.get(a.get("conn_id"), "") or a.get("org", {}).get("name", "")),
                         (m.group(1) or m.group(2)) if m else None,
                         re.sub(r"\s*\.\.\.\d+.*$", "", name) or name, _kind(name), base)
        for t in a.get("transactions", []):
            times = [_time(t["posted"], "posted")] if t.get("posted") else []
            if t.get("transacted_at"):
                times.append(_time(t["transacted_at"], "transacted"))
            out.obs.append(Obs(
                locator=f"acct={a['id']};txn={t['id']}", account=ref, key=(t["id"],), times=times,
                legs=[Leg(base, Decimal(t["amount"]), "cash")],
                description=t.get("description"), payee=t.get("payee") or None, memo=t.get("memo") or None,
                ext_id=t["id"], pending=bool(t.get("pending")),
                extra={k: v for k, v in t.items() if k in ("mcc", "extra") and v}, raw=t))
        if "balance-date" in a:
            when = instant(a["balance-date"], "as_of")
            for kind, k in (("balance", "balance"), ("available", "available-balance")):
                if a.get(k) is not None:
                    out.snaps.append(Snap(f"acct={a['id']};{kind}", ref, base, kind, when, Decimal(a[k])))
            for hld in a.get("holdings", []):
                sym = hld.get("symbol") or f"?{hld.get('id')}"
                asset = f"sec:{sym}"
                out.assets[asset] = {"symbol": hld.get("symbol"), "name": hld.get("description")}
                out.snaps.append(Snap(
                    f"acct={a['id']};holding={hld.get('id')}", ref, asset, "position",
                    when, _d(hld.get("shares")), _d(hld.get("market_value")), base,
                    {k: hld.get(k) for k in ("cost_basis", "purchase_price") if hld.get(k) is not None}))
    return out
