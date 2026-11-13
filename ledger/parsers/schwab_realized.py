"""Schwab web 'Realized Gain / Loss' page printed to PDF: one row per sale, per fund.

Each row states what Schwab itself counted as sold, so lots.py can check against it. The page's
summary (record count, account totals, disallowed loss) is checked here against the parsed rows.
"""
import datetime as dt
import re
from decimal import Decimal
from pathlib import Path

import pdfplumber

from ..model import AccountRef, Parsed, Realized, dec
from .schwab_pdf import _lines

NAME, VERSION, FEED = "schwab_realized_pdf", 1, "schwab_web"
ROW = re.compile(r"^([A-Z][A-Z0-9.]{0,6}) (\d\d/\d\d/\d{4}) ([\d,.]+) \$([\d,.]+) (.+?) \$([\d,.]+) \$([\d,.]+) ([+-]\$[\d,.]+)")
SIGNED = re.compile(r"^[+-]\$[\d,.]+$")
DISALLOWED = re.compile(r"Disallowed Loss: \$([\d,.]+)")


def detect(path: Path, head: bytes) -> bool:
    if path.suffix.lower() != ".pdf":
        return False
    with pdfplumber.open(path) as pdf:
        t = pdf.pages[0].extract_text() or ""
    return "Realized Gain / Loss" in t and "…" in t


def parse(path: Path) -> Parsed:
    out = Parsed(NAME, VERSION, FEED)
    with pdfplumber.open(path) as pdf:
        pages = [_lines(p) for p in pdf.pages]
        alltext = "\n".join(p.extract_text() or "" for p in pdf.pages)

    m = re.search(r"^\s*(.+?)\s*…(\d+)", alltext, re.M)
    if not m:
        raise ValueError("no account header (name …1234) found")
    ref = AccountRef(FEED, f"schwab:{m.group(2)}", "schwab", m.group(2), m.group(1).strip(), "brokerage")
    if c := re.search(r"^(\d\d/\d\d/\d{4}) to (\d\d/\d\d/\d{4})$", alltext, re.M):
        out.meta["search_range"] = [_date(c.group(1)), _date(c.group(2))]
        out.coverage = tuple(out.meta["search_range"])

    long_x = short_x = None
    for pno, lines in enumerate(pages):
        for i, (y, ws) in enumerate(lines):
            text = " ".join(w["text"] for w in ws)
            if "Method" in text and "Proceeds" in text:   # header: the two term columns are right-aligned
                terms = [w for w in ws if w["text"] == "Term"]
                long_x, short_x = terms[0]["x1"], terms[1]["x1"]
                continue
            r = ROW.match(text)
            if not r:
                continue
            if long_x is None:
                raise ValueError("row found before the table header")
            sym, day, qty, px, method, proceeds, cost, gain = r.groups()
            terms = {"long": None, "short": None}
            signed = [w for w in ws if SIGNED.match(w["text"])][1:]   # the first is the row's total
            for w in signed:
                terms["long" if abs(w["x1"] - long_x) < abs(w["x1"] - short_x) else "short"] = dec(w["text"])
            below = " ".join(w["text"] for w in lines[i + 1][1]) if i + 1 < len(lines) else ""
            lost = DISALLOWED.search(below)
            asset = f"sec:{sym}"
            out.assets.setdefault(asset, {"symbol": sym})
            out.realized.append(Realized(f"p{pno}:y{round(y)}", ref, asset, _date(day), dec(qty), dec(px), method,
                                         dec(proceeds), dec(cost), dec(gain), terms["long"], terms["short"],
                                         dec(lost.group(1)) if lost else Decimal(0)))
    _check_summary(out, alltext)
    return out


def _date(s: str) -> str:
    return dt.datetime.strptime(s, "%m/%d/%Y").date().isoformat()


def _check_summary(out: Parsed, text: str):
    rows = out.realized
    if (n := re.search(r"Chart based on (\d+) records", text)) and int(n.group(1)) != len(rows):
        out.warnings.append(f"page says {n.group(1)} records but {len(rows)} rows were parsed")
    if t := re.search(r"^Account Total \$([\d,.]+) \$([\d,.]+) ([+-]\$[\d,.]+)", text, re.M):
        if dec(t.group(1)) != sum(r.proceeds for r in rows) or dec(t.group(2)) != sum(r.cost for r in rows) \
                or dec(t.group(3)) != sum(r.gain for r in rows):
            out.warnings.append("rows do not sum to the page's Account Total")
    else:
        out.warnings.append("no Account Total line found")
    if d := re.search(r"^Disallowed Loss -\$([\d,.]+)", text, re.M):
        if dec(d.group(1)) != sum(r.disallowed for r in rows):
            out.warnings.append("disallowed losses do not sum to the page's total")
    bad = sum(1 for r in rows if r.proceeds - r.cost + r.disallowed != r.gain)
    if bad:
        out.warnings.append(f"{bad} row(s) where proceeds - cost + disallowed != gain")
