"""Schwab web 'Transaction History' pages printed to PDF (bank checking and brokerage layouts).

Rows are found by their MM/DD/YYYY date cell; wrapped cell text above/below the date line is
attached to the nearest row. Columns come from the header word positions, so layout drift
between exports is tolerated. The browser can print a row cut by a page break twice; such
duplicates are dropped, and checking rows are verified against the running balance.
"""
import datetime as dt
import re
from decimal import Decimal
from pathlib import Path

import pdfplumber

from ..model import AccountRef, Leg, Obs, Parsed, Snap, dec, day, qstr

NAME, VERSION, FEED = "schwab_web_pdf", 2, "schwab_web"
TZ = "America/New_York"
DATE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
MONEYISH = re.compile(r"^\(?-?\$?-?[\d,]*\.?\d+\)?$|^-$")   # lone "-": a wide amount wraps its sign onto the line above
WRAP_DY = 10          # max vertical distance from a wrapped line to its row's date line

# column name, header token, alignment. Right-aligned columns match on the token's right edge.
LAYOUTS = {
    "checking": [("date", "Date", "L"), ("type", "Transaction", "L"), ("description", "Description", "L"),
                 ("deposit", "Deposit", "R"), ("withdrawal", "Withdrawal", "R"), ("balance", "Balance", "R")],
    "brokerage": [("date", "Date", "L"), ("type", "Transaction", "L"), ("symbol", "Symbol", "L"),
                  ("description", "Description", "L"), ("quantity", "Quantity", "R"), ("price", "Price", "R"),
                  ("fees", "Comm", "R"), ("amount", "Amount", "R")],
}


def detect(path: Path, head: bytes) -> bool:
    if path.suffix.lower() != ".pdf":
        return False
    with pdfplumber.open(path) as pdf:
        t = pdf.pages[0].extract_text() or ""
    return "Transaction History" in t and "…" in t


def _lines(page):
    words = sorted(page.extract_words(x_tolerance=1.5), key=lambda w: (w["top"], w["x0"]))
    lines, cur = [], []
    for w in words:
        if cur and w["top"] - cur[0]["top"] > 2:
            lines.append(cur)
            cur = []
        cur.append(w)
    if cur:
        lines.append(cur)
    return [(min(w["top"] for w in ln), sorted(ln, key=lambda w: w["x0"])) for ln in lines]


def _date(s: str) -> dt.date:
    return dt.datetime.strptime(s, "%m/%d/%Y").date()


def parse(path: Path) -> Parsed:
    out = Parsed(NAME, VERSION, FEED)
    with pdfplumber.open(path) as pdf:
        pages = [_lines(p) for p in pdf.pages]
        alltext = "\n".join(p.extract_text() or "" for p in pdf.pages)

    m = re.search(r"^\s*(.+?)\s*…(\d+)", alltext, re.M)
    if not m:
        raise ValueError("no account header (name …1234) found")
    acct_name, mask = m.group(1).strip(), m.group(2)
    if c := re.search(r"found from (\d\d/\d\d/\d{4}) to (\d\d/\d\d/\d{4})", alltext):
        # the search range, not what was printed: the page lazy-loads, so a print may hold a slice
        out.meta["search_range"] = [_date(c.group(1)).isoformat(), _date(c.group(2)).isoformat()]
    if u := re.search(r"Updated: (.+? ET), (\d\d/\d\d/\d{4})", alltext):
        out.meta["updated"] = f"{_date(u.group(2)).isoformat()} {u.group(1)}"

    # header row: first line containing both 'Description' and a layout-specific token
    layout = cols = None
    for pno, lines in enumerate(pages):
        for y, ws in lines:
            texts = [w["text"] for w in ws]
            if "Description" in texts:
                layout = "checking" if "Deposit" in texts else "brokerage" if "Quantity" in texts else None
                if layout:
                    cols = []
                    for name, tok, align in LAYOUTS[layout]:
                        w = next(w for w in ws if w["text"] == tok)
                        cols.append((name, align, w["x0"], w["x1"]))
                    start = (pno, y)
                    break
        if layout:
            break
    if not layout:
        raise ValueError("no transaction table header found")
    out.meta["layout"] = layout
    kind = "checking" if layout == "checking" else "brokerage"
    ref = AccountRef(FEED, f"schwab:{mask}", "schwab", mask, acct_name, kind)

    left = [c for c in cols if c[1] == "L"]
    right = [c for c in cols if c[1] == "R"]

    def column(w) -> str:
        if MONEYISH.match(w["text"]):
            near = min(right, key=lambda c: abs(c[3] - w["x1"]))
            if abs(near[3] - w["x1"]) <= 8:
                return near[0]
        best = left[0][0]
        for name, _, x0, _ in left:
            if w["x0"] >= x0 - 4:
                best = name
        return best

    # rows per page: date-anchored line + nearby wrapped lines
    rows, stray = [], []
    for pno, lines in enumerate(pages):
        if pno < start[0]:
            continue
        body = [(y, ws) for y, ws in lines if pno > start[0] or y > start[1] + 3]
        if end := next((i for i, (_, ws) in enumerate(body) if any(w["text"].startswith("Today") for w in ws)), None):
            body = body[:end]   # print footer: "Today's Date: 06:00 PM ET, 01/02/2025"
        # a row's date line; but "MM/DD/YYYY as of" puts the as-of date on the next line
        anchors, asof = [], False
        for y, ws in body:
            dcol = [w["text"] for w in ws if column(w) == "date"]
            if dcol and DATE.match(dcol[0]) and not asof:
                anchors.append((y, ws))
            if dcol:
                asof = dcol[-2:] == ["as", "of"]
        if not anchors:
            stray += [(pno, round(y), " ".join(w["text"] for w in ws)) for y, ws in body]
            continue
        blocks = {y: [] for y, _ in anchors}
        for y, ws in body:
            ay = min(blocks, key=lambda a: abs(a - y))
            if abs(ay - y) <= WRAP_DY:
                blocks[ay].append((y, ws))
            else:
                stray.append((pno, round(y), " ".join(w["text"] for w in ws)))
        for ay, _ in anchors:
            rows.append((pno, ay, blocks[ay], next(ws for y, ws in anchors if y == ay)))
    if stray:
        out.meta["ignored_lines"] = stray   # kept for auditing what the parser skipped
        out.warnings.append(f"{len(stray)} text line(s) outside any row were ignored")

    def cells_of(block):
        cells: dict[str, list[str]] = {}
        for _, ws in sorted(block, key=lambda b: b[0]):
            for w in ws:
                cells.setdefault(column(w), []).append(w["text"])
        return {k: " ".join(v) for k, v in cells.items()}

    rows = [(pno, ay, cells_of(block), anchor) for pno, ay, block, anchor in rows]

    # The browser prints a row cut by a page break on both pages, each copy missing the
    # wrapped lines that fell on the other page. Same values across the break => one row.
    def same_row(a: dict, b: dict) -> bool:
        return all(a.get(k) == b.get(k) for k in set(a) | set(b) if k not in ("type", "description"))

    def join(top: str, bottom: str) -> str:   # top = copy on the earlier page (has the upper wraps)
        return top if bottom in top else bottom if top in bottom else f"{top} {bottom}"

    deduped, merged = [], 0
    for i, r in enumerate(rows):
        prev = deduped[-1] if deduped else None
        first_on_page = i > 0 and rows[i - 1][0] != r[0]
        if first_on_page and r[0] == prev[0] + 1 and same_row(prev[2], r[2]):
            cell = {k: join(prev[2].get(k, ""), r[2].get(k, "")).strip() for k in set(prev[2]) | set(r[2])}
            deduped[-1] = (prev[0], prev[1], cell, prev[3])
            merged += 1
            continue
        deduped.append(r)
    if merged:
        out.warnings.append(f"{merged} row(s) split across a page break were merged")

    for pno, ay, cell, anchor in deduped:
        posted = _date(next(w["text"] for w in anchor if DATE.match(w["text"])))
        times = [day(posted, TZ, "posted")]
        asof = re.search(r"as of (\d\d/\d\d/\d{4})", cell.get("date", ""))
        if asof:
            times.append(day(_date(asof.group(1)), TZ, "transacted"))
        raw = {"page": pno, "y": round(ay, 1), **cell}
        if note := re.sub(r"\s*\d\d/\d\d/\d{4}\s*", " ", cell.get("date", "")).strip():
            raw["date_note"] = note
        loc = f"p{pno}:y{round(ay)}"
        typ, desc = cell.get("type"), cell.get("description")
        if layout == "checking":
            dep, wd, bal = dec(cell.get("deposit")), dec(cell.get("withdrawal")), dec(cell.get("balance"))
            amt = dep if dep is not None else -abs(wd) if wd is not None else Decimal(0)
            key = (posted.isoformat(), typ, desc, qstr(amt), qstr(bal) if bal is not None else None)
            out.obs.append(Obs(loc, ref, key, times, [Leg("fiat:USD", amt, "cash")], typ, desc,
                               extra={"running_balance": qstr(bal)} if bal is not None else {}, raw=raw))
            if bal is not None:
                out.snaps.append(Snap(loc, ref, "fiat:USD", "running", times[0], bal))
        else:
            sym = cell.get("symbol")
            qty, price, fees = dec(cell.get("quantity")), dec(cell.get("price")), dec(cell.get("fees"))
            amt = dec(cell.get("amount")) or Decimal(0)
            legs = [Leg("fiat:USD", amt, "cash")]
            if sym and qty is not None:
                if qty > 0 and (amt > 0 or amt == 0 and re.search(r"sell|withdraw|out|deliver", typ or "", re.I)):
                    qty = -qty
                legs.append(Leg(f"sec:{sym}", qty, "position", price, "fiat:USD" if price is not None else None))
                out.assets.setdefault(f"sec:{sym}", {"symbol": sym, "name": desc})
            key = (posted.isoformat(), typ, sym, desc, cell.get("quantity"), cell.get("price"), qstr(amt))
            out.obs.append(Obs(loc, ref, key, times, legs, typ, desc,
                               extra={"fees": qstr(fees)} if fees else {}, raw=raw))

    _check_footer(out, stray)
    if out.obs:
        days = [o.times[0].value for o in out.obs]
        out.coverage = (min(days), max(days))   # rows on the edge days may be partial
    if layout == "checking":
        _check_running(out)
    return out


def _check_running(out: Parsed):
    """Rows are newest-first: balance[i] - amount[i] must equal balance[i+1]."""
    bad = 0
    obs = [o for o in out.obs if "running_balance" in o.extra]
    for a, b in zip(obs, obs[1:]):
        if Decimal(a.extra["running_balance"]) - a.legs[0].quantity != Decimal(b.extra["running_balance"]):
            bad += 1
    out.meta["running_balance_breaks"] = bad
    if bad:
        out.warnings.append(f"running balance breaks between {bad} adjacent row pair(s) (missing/duplicate rows?)")


def _check_footer(out: Parsed, ignored: list):
    """The page footer says 'Showing A - B' and (brokerage) 'Total <fees> <amount>'."""
    text = [t for _, _, t in ignored]
    if m := next((re.search(r"Showing (\d+) - (\d+)", t) for t in text if t.startswith("Showing")), None):
        want = int(m.group(2)) - int(m.group(1)) + 1
        out.meta["showing"] = [int(m.group(1)), int(m.group(2))]
        if want != len(out.obs):
            out.warnings.append(f"page says {want} rows but {len(out.obs)} were parsed")
    if t := next((t for t in text if t.startswith("Total ")), None):
        *_, amount = t.split()
        got = sum((l.quantity for o in out.obs for l in o.legs if l.role == "cash"), Decimal(0))
        if dec(amount) != got:
            out.warnings.append("amount column does not sum to the page's Total line")
