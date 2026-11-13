"""Schwab's own exports from schwab.com: History (brokerage and bank) and Realized Gain/Loss, as CSV (or JSON).

An accounts manager that pulls them names them
`schwab/schwab-<layout>-<mask>[-<from>-<to>]-<UTC stamp>.<csv|json>`: layout is brokerage, bank or realized, mask
the account's last digits, from/to (YYYYMMDD) the range asked for when there was one. A file saved by hand under
Schwab's own name (Individual_XXX123_Transactions_….csv) is read too: the mask comes from the name or the title line.

The same rows the printed pages gave (schwab_pdf.py, schwab_realized.py), under their own feed `schwab_csv`: the
CSV's cells are not the printed page's text, so its rows never share a fingerprint with a PDF's; where both cover
a day, the CSV row joins the PDF row's txn by the cross-feed match (same account, amount, nearby day), and the
PDF's row speaks for the account (FEED_PRIORITY: schwab_web, then schwab_csv, then simplefin).

Columns are found by name (a few spellings each), so column order and extra columns do not matter. Pending bank rows
are left out (they change when they post; SimpleFIN carries them meanwhile); total lines are skipped.
"""
import csv
import datetime as dt
import io
import json
import re
from decimal import Decimal
from pathlib import Path

from ..model import AccountRef, Leg, Obs, Parsed, Realized, Snap, dec, day, qstr

NAME, VERSION, FEED = "schwab_csv", 1, "schwab_csv"
TZ = "America/New_York"
DATE = re.compile(r"^\s*(\d\d/\d\d/\d{4})(?:\s+as of\s+(\d\d/\d\d/\d{4}))?\s*$")
OUR_NAME = re.compile(r"^schwab-(brokerage|bank|realized)-(\d{3,8})(?:-(\d{8})-(\d{8}))?-\d{8}T\d{6}Z\.(csv|json)$")
MASKS = (re.compile(r"…\s?(\d{3,4})\b"), re.compile(r"\.\.\.(\d{3,4})\b"), re.compile(r"\bX{2,}-?(\d{3,4})\b"),
         re.compile(r"\b\d{4}-(\d{4})\b"))

# column -> header spellings (compared case-insensitively, spaces squeezed)
BROKERAGE = {"date": ["Date"], "type": ["Action", "Transaction", "Type"], "symbol": ["Symbol"],
             "description": ["Description"], "quantity": ["Quantity", "Qty"], "price": ["Price"],
             "fees": ["Fees & Comm", "Fees & Commissions", "Fees", "Comm"], "amount": ["Amount"]}
BANK = {"date": ["Date"], "status": ["Status"], "type": ["Type", "Transaction Type", "Transaction"],
        "check": ["CheckNumber", "Check Number", "Check #"], "description": ["Description"],
        "withdrawal": ["Withdrawal", "Withdrawal (-)", "Withdrawals"], "deposit": ["Deposit", "Deposit (+)", "Deposits"],
        "balance": ["RunningBalance", "Running Balance", "Balance"]}
REALIZED = {"symbol": ["Symbol"], "name": ["Name", "Description"], "closed": ["Closed Date", "Date Closed"],
            "quantity": ["Quantity", "Qty"], "price": ["Closing Price", "Proceeds Per Share"],
            "method": ["Cost Basis Method", "Method"], "proceeds": ["Proceeds"],
            "cost": ["Cost Basis (CB)", "Cost Basis", "Cost Basis ($)"],
            "gain": ["Gain/Loss ($)", "Total Gain/Loss ($)", "Realized Gain/Loss ($)", "Gain/Loss", "Total"],
            "long": ["Long Term Gain/Loss ($)", "Long Term Gain/Loss", "LT Gain/Loss ($)", "Long Term"],
            "short": ["Short Term Gain/Loss ($)", "Short Term Gain/Loss", "ST Gain/Loss ($)", "Short Term"],
            "term": ["Term", "Holding Period"], "disallowed": ["Disallowed Loss", "Disallowed Loss ($)"]}
NEEDS = {"brokerage": {"date", "type", "description", "amount", "quantity"},
         "bank": {"date", "description", "withdrawal", "deposit"},
         "realized": {"symbol", "closed", "quantity", "proceeds", "cost"}}
LAYOUTS = (("realized", REALIZED), ("bank", BANK), ("brokerage", BROKERAGE))
TOTAL = re.compile(r"^\s*(transactions\s+total|account\s+total|total)\b", re.I)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("﻿", "")).strip().lower()


def _columns(row: list[str], spec: dict) -> dict[str, int]:
    have = {_norm(c): i for i, c in enumerate(row)}
    out = {}
    for col, names in spec.items():
        for n in names:
            if _norm(n) in have:
                out[col] = have[_norm(n)]
                break
    return out


def _layout(rows: list[list[str]]):
    """(layout, header row index, columns) for the first row that names a layout's needed columns."""
    for i, row in enumerate(rows[:15]):
        for name, spec in LAYOUTS:
            cols = _columns(row, spec)
            if NEEDS[name] <= set(cols) and (name != "bank" or "balance" in cols or "status" in cols):
                return name, i, cols
    return None


def _text(path: Path) -> str:
    return path.read_bytes().decode("utf-8-sig", errors="replace")


def _rows(path: Path) -> list[list[str]]:
    if path.suffix.lower() == ".json":
        doc = json.loads(_text(path))
        tx = doc.get("BrokerageTransactions") if isinstance(doc, dict) else None
        if not isinstance(tx, list):
            raise ValueError("a JSON export without BrokerageTransactions")
        head = list(BROKERAGE_JSON)
        return [head] + [[str(t.get(k) or "") for k in head] for t in tx if isinstance(t, dict)]
    return list(csv.reader(io.StringIO(_text(path))))


BROKERAGE_JSON = ("Date", "Action", "Symbol", "Description", "Quantity", "Price", "Fees & Comm", "Amount")


def detect(path: Path, head: bytes) -> bool:
    suf = path.suffix.lower()
    if suf == ".json":
        return b'"BrokerageTransactions"' in head
    if suf != ".csv" or b"Account Statement - (@" in head:   # Venmo's statements are CSV too
        return False
    try:
        rows = list(csv.reader(io.StringIO(head.decode("utf-8-sig", errors="replace"))))
    except csv.Error:
        return False
    return _layout(rows) is not None


def _mask(path: Path, rows: list[list[str]]) -> str:
    """The account's last three digits, as schwab.com shows them (…123) and schwab_pdf keys the account."""
    if m := OUR_NAME.match(path.name):
        return m.group(2)[-3:]
    if m := re.search(r"(?:^|_)X{2,}-?(\d{3,})(?:_|-|$)", path.name):   # Schwab's own name: Checking_XXX123_Transactions_…
        return m.group(1)[-3:]
    for t in [" ".join(r) for r in rows[:3]]:
        for rx in MASKS:
            if m := rx.search(t):
                return m.group(1)[-3:]
    raise ValueError("no account number (…1234) in the file's name or title")


def _date(s: str) -> dt.date:
    return dt.datetime.strptime(s, "%m/%d/%Y").date()


def _cell(row: list[str], cols: dict, k: str) -> str:
    i = cols.get(k)
    return row[i].strip() if i is not None and i < len(row) else ""


def parse(path: Path) -> Parsed:
    out = Parsed(NAME, VERSION, FEED)
    rows = _rows(path)
    lay = _layout(rows)
    if not lay:
        raise ValueError("no Schwab export header found")
    layout, hi, cols = lay
    mask = _mask(path, rows)
    out.meta["layout"] = layout
    title = " ".join(" ".join(r) for r in rows[:hi])
    if m := re.search(r"(\d\d/\d\d/\d{4})\s+to\s+(\d\d/\d\d/\d{4})", title):
        out.meta["search_range"] = [_date(m.group(1)).isoformat(), _date(m.group(2)).isoformat()]
    if (n := OUR_NAME.match(path.name)) and n.group(3):
        out.meta["search_range"] = [dt.datetime.strptime(n.group(g), "%Y%m%d").date().isoformat() for g in (3, 4)]
    kind = "checking" if layout == "bank" else "brokerage"
    ref = AccountRef(FEED, f"schwab:{mask}", "schwab", mask, None, kind)
    body = [(hi + 1 + i, r) for i, r in enumerate(rows[hi + 1:]) if any(c.strip() for c in r)]
    if layout == "realized":
        _realized(out, ref, body, cols)
    else:
        _history(out, ref, body, cols, layout)
    if "search_range" in out.meta:
        out.coverage = tuple(out.meta["search_range"])
    elif out.obs:
        days = [o.times[0].value for o in out.obs]
        out.coverage = (min(days), max(days))   # rows on the edge days may be partial
    elif out.realized:
        days = [r.closed for r in out.realized]
        out.coverage = (min(days), max(days))
    return out


def _history(out: Parsed, ref: AccountRef, body, cols, layout):
    skipped = pending = 0
    for line, r in body:
        dcell = _cell(r, cols, "date")
        m = DATE.match(dcell)
        if not m:
            skipped += 1   # a total line, a note
            continue
        posted = _date(m.group(1))
        times = [day(posted, TZ, "posted")]
        if m.group(2):
            times.append(day(_date(m.group(2)), TZ, "transacted"))
        cell = {k: _cell(r, cols, k) for k in cols}
        raw = {"line": line, **{k: v for k, v in cell.items() if v}}
        loc = f"l{line}"
        typ, desc = cell.get("type") or None, cell.get("description") or None
        if layout == "bank":
            if cell.get("status", "").lower().startswith("pend"):
                pending += 1
                continue
            dep, wd, bal = dec(cell.get("deposit")), dec(cell.get("withdrawal")), dec(cell.get("balance"))
            amt = dep if dep is not None else -abs(wd) if wd is not None else Decimal(0)
            if check := cell.get("check"):
                desc = f"{desc} #{check}" if desc else f"Check #{check}"
            key = (posted.isoformat(), typ, desc, qstr(amt), qstr(bal) if bal is not None else None)
            out.obs.append(Obs(loc, ref, key, times, [Leg("fiat:USD", amt, "cash")], typ, desc,
                               extra={"running_balance": qstr(bal)} if bal is not None else {}, raw=raw))
            if bal is not None:
                out.snaps.append(Snap(loc, ref, "fiat:USD", "running", times[0], bal))
        else:
            sym = cell.get("symbol") or None
            qty, price, fees = dec(cell.get("quantity")), dec(cell.get("price")), dec(cell.get("fees"))
            amt = dec(cell.get("amount")) or Decimal(0)
            legs = [Leg("fiat:USD", amt, "cash")]
            if sym and qty is not None:
                # as schwab_pdf: a printed quantity is unsigned unless it says otherwise; cash in means shares out
                if qty > 0 and (amt > 0 or amt == 0 and re.search(r"sell|withdraw|out|deliver", typ or "", re.I)):
                    qty = -qty
                legs.append(Leg(f"sec:{sym}", qty, "position", price, "fiat:USD" if price is not None else None))
                out.assets.setdefault(f"sec:{sym}", {"symbol": sym, "name": desc})
            key = (posted.isoformat(), typ, sym, desc, qstr(qty) if qty is not None else None,
                   qstr(price) if price is not None else None, qstr(amt))
            out.obs.append(Obs(loc, ref, key, times, legs, typ, desc,
                               extra={"fees": qstr(fees)} if fees else {}, raw=raw))
    out.meta["skipped_lines"] = skipped
    if pending:
        out.meta["pending_left_out"] = pending
    if layout == "bank":
        _check_running(out)


def _check_running(out: Parsed):
    """Rows are newest first: balance[i] - amount[i] must equal balance[i+1] (as schwab_pdf checks)."""
    obs = [o for o in out.obs if "running_balance" in o.extra]
    bad = sum(1 for a, b in zip(obs, obs[1:])
              if Decimal(a.extra["running_balance"]) - a.legs[0].quantity != Decimal(b.extra["running_balance"]))
    out.meta["running_balance_breaks"] = bad
    if bad:
        out.warnings.append(f"running balance breaks between {bad} adjacent row pair(s) (missing/duplicate rows?)")


def _realized(out: Parsed, ref: AccountRef, body, cols):
    skipped = 0
    for line, r in body:
        sym = _cell(r, cols, "symbol")
        closed = _cell(r, cols, "closed")
        m = DATE.match(closed)
        if not sym or TOTAL.match(sym) or not m:
            skipped += 1
            continue
        g = lambda k: dec(_cell(r, cols, k))
        proceeds, cost = g("proceeds"), g("cost")
        if proceeds is None or cost is None:
            skipped += 1
            continue
        disallowed = abs(g("disallowed") or Decimal(0))
        gain = g("gain")
        if gain is None:
            gain = proceeds - cost + disallowed
        long_, short = g("long"), g("short")
        term = _cell(r, cols, "term").lower()
        if long_ is None and short is None and term:
            long_, short = (gain, None) if term.startswith("long") else (None, gain) if term.startswith("short") else (None, None)
        asset = f"sec:{sym}"
        out.assets.setdefault(asset, {"symbol": sym, **({"name": n} if (n := _cell(r, cols, "name")) else {})})
        out.realized.append(Realized(f"l{line}", ref, asset, _date(m.group(1)).isoformat(), abs(g("quantity") or Decimal(0)),
                                     g("price"), _cell(r, cols, "method") or None, proceeds, cost, gain, long_, short,
                                     disallowed))
    out.meta["skipped_lines"] = skipped
    bad = sum(1 for x in out.realized if x.proceeds - x.cost + x.disallowed != x.gain)
    if bad:
        out.warnings.append(f"{bad} row(s) where proceeds - cost + disallowed != gain")
