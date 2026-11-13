import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # the code
# FINANCE_HOME holds data/ and secrets/ (e.g. ~/.local/share/finance-tracker); unset = the repo.
HOME = Path(os.environ.get("FINANCE_HOME") or ROOT).expanduser()
DATA = HOME / "data"
SECRETS = HOME / "secrets"
DB_PATH = DATA / "ledger.db"
STATE_PATH = DATA / "state.jsonl"

# Append-only. Each entry upgrades user_version N-1 -> N. Entry 1 is the base schema;
# never edit an applied entry, add a file to migrations/ instead.
MIGRATIONS: list[str] = [
    (Path(__file__).parent / "schema.sql").read_text(),
    *(p.read_text() for p in sorted((Path(__file__).parent / "migrations").glob("*.sql"))),
]


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    v = con.execute("PRAGMA user_version").fetchone()[0]
    for i, sql in enumerate(MIGRATIONS[v:], start=v + 1):
        with con:
            con.executescript(sql)
            con.execute(f"PRAGMA user_version = {i}")
    return con
