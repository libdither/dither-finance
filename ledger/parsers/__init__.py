from pathlib import Path

from . import amazon, cake_csv, monero, privacy, schwab_csv, schwab_pdf, schwab_realized, simplefin, venmo

PARSERS = [privacy, simplefin, schwab_pdf, schwab_realized, schwab_csv, monero, cake_csv, venmo, amazon]


def detect(path: Path):
    head = path.read_bytes()[:4096]
    for p in PARSERS:
        if p.detect(path, head):
            return p
    return None
