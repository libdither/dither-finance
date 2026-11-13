"""ledger: build and inspect the transactions database.

  ledger ingest [FILES…] [--reparse]   parse new/changed raw files, link, resolve, export state
  ledger build --scratch [--out DB]    rebuild a fresh db from data/raw + data/state.jsonl
  ledger verify                        scratch-build to a temp db and diff against the live one
  ledger status                        per-account coverage and match stats (no descriptions)
  ledger report [--since YYYY-MM] [--by month|counterparty]   money in/out across your boundary
  ledger account ID [--name --institution --closed --note]   annotate an account
  ledger merge-account SRC DST         declare that account SRC is DST (pinned in state.jsonl)
  ledger dashboard [--out FILE]        write a self-contained HTML page of spending and the budget model
  ledger serve [--host --port]         the finance portal (dashboard, transaction editor, connections)
  ledger portal-password [--file F]    set the portal's password
  ledger pull [SOURCE…]                fetch the finance accounts' files from dither-accounts, then ingest
  ledger prices                        fetch market prices for coins (XMR/USD from Kraken) into data/prices.json
  ledger sql "SELECT …"                run a query against the db
"""
import argparse
import os
import sys
import tempfile
from pathlib import Path

from . import config
from . import ingest as I
from . import lots as L
from . import reconcile as R
from .db import DATA, DB_PATH, STATE_PATH, connect

RAW = DATA / "raw"


def raw_files() -> list[Path]:
    return sorted(p for p in RAW.rglob("*") if p.is_file() and not p.name.startswith(".")
                  and not p.name.endswith((".crdownload", ".tmp")))   # downloads still in progress


def run_ingest(db: Path, files: list[Path] | None, reparse=False, state_in: Path | None = None, quiet=False):
    log = (lambda *a: None) if quiet else print
    con = connect(db)
    if state_in:
        log("account pins:", I.import_aliases(con, state_in))
    I.ingest(con, files or raw_files(), reparse=reparse, full_scan=files is None, log=log)
    if state_in:
        log("state import:", dict(I.import_state(con, state_in)))
    stats = I.link(con, log)
    n = I.resolve(con)
    log(f"linked {sum(stats.values())} new observation(s) {dict(stats)}; {n} txns resolved")
    r = R.reconcile(con)
    log(f"balance checks: {r['ok']}/{r['checks']} ok")
    log(f"lots: {L.build(con)}")
    return con


def cmd_ingest(a):
    from .portal import ledger_lock
    with ledger_lock():   # the portal writes annotations and state too
        _ingest(a)


def _ingest(a):
    con = run_ingest(Path(a.db), [Path(f) for f in a.files] or None, a.reparse)
    if Path(a.db).resolve() != DB_PATH.resolve():
        print("state: not exported (--db is not the live db)")
        return
    print(f"state: {I.export_state(con, STATE_PATH)} line(s) -> {STATE_PATH.relative_to(DATA.parent)}")


def cmd_build(a):
    if not a.scratch:
        sys.exit("refusing to rebuild without --scratch")
    out = Path(a.out)
    if out.exists():
        if out.resolve() == DB_PATH.resolve() and not a.force:
            sys.exit(f"{out} exists; pass --force to replace the live db, or --out elsewhere")
        for suffix in ("", "-wal", "-shm"):
            Path(str(out) + suffix).unlink(missing_ok=True)
    run_ingest(out, None, state_in=STATE_PATH)


def _snapshot(con):
    q = lambda sql: {tuple(r) for r in con.execute(sql)}
    return {
        "ledger rows": q("""SELECT txn_id, account_id, date, description, amount, flow, counterparty, category, feeds
                            FROM ledger"""),
        "links": q("SELECT obs_id, txn_id FROM txn_link"),
        "postings": q("SELECT txn_id, n, account_id, asset_id, quantity, unit_price, role, date FROM posting"),
        "txns": q("SELECT id, merged_into FROM txn WHERE NOT orphaned"),
        "lots": q("SELECT id, quantity, cost, remaining FROM lot") | q("SELECT * FROM lot_close"),
        "prices": q("SELECT * FROM price"),
        "balances": q("SELECT account_id, asset_id, day, kind, locator, ok FROM balance_check")
                    | q("SELECT account_id, asset_id, quantity, as_of, 'opening' FROM opening_balance"),
    }


def cmd_verify(a):
    live = connect(Path(a.db))
    with tempfile.TemporaryDirectory() as d:
        fresh = run_ingest(Path(d) / "scratch.db", None, state_in=STATE_PATH, quiet=True)
        s1, s2 = _snapshot(live), _snapshot(fresh)
    ok = True
    for k in s1:
        diff = len(s1[k] ^ s2[k])
        ok &= diff == 0
        print(f"{k + ':':<13} live {len(s1[k])} scratch {len(s2[k])}, differing {diff}")
    print("REPRODUCIBLE" if ok else "MISMATCH")
    sys.exit(0 if ok else 1)


def cmd_status(a):
    con = connect(Path(a.db))
    q = lambda s, *p: con.execute(s, p).fetchall()
    print("sources:")
    for r in q("""SELECT path, parser, coverage_lo, coverage_hi, present,
                         (SELECT count(*) FROM observation o WHERE o.source_sha = s.sha256) n,
                         json_extract(meta, '$.warnings') w FROM source_file s ORDER BY path"""):
        cov = f"{r['coverage_lo']}..{r['coverage_hi']}" if r["coverage_lo"] else "-"
        print(f"  {'' if r['present'] else '[missing] '}{r['path']}  {r['parser']}  obs={r['n']}  covers {cov}")
        if r["w"] and r["w"] != "[]":
            print(f"    warnings: {r['w']}")
    print("accounts:")
    for r in q("""SELECT a.id, a.kind, a.name, a.institution, a.closed, count(e.txn_id) n, min(e.date) lo,
                         max(e.date) hi, sum(e.feeds LIKE '%,%') multi
                  FROM accounts a LEFT JOIN entry e ON e.account_id = a.id
                  WHERE a.owned GROUP BY a.id ORDER BY a.id"""):
        closed = f", closed {r['closed']}" if r["closed"] else ""
        print(f"  {r['id']} ({r['kind']}; {r['institution']} {r['name'] or ''}{closed}): {r['n']} entries "
              f"{r['lo']}..{r['hi']}, {r['multi']} seen by >1 feed")
        for f in q("""SELECT feeds, count(*) n, min(date) lo, max(date) hi FROM entry WHERE account_id=?
                      GROUP BY feeds ORDER BY feeds""", r["id"]):
            print(f"      {f['feeds']:<22} {f['n']:>5}  {f['lo']}..{f['hi']}")
    print("events by flow:", dict(q("SELECT flow, count(*) FROM txn_fact GROUP BY flow")))
    print("counterparties:", dict(q("""SELECT account_id, count(DISTINCT txn_id) FROM posting
                                       WHERE role = 'counter' GROUP BY account_id""")))
    print("links:", dict(q("SELECT method, count(*) FROM txn_link GROUP BY method")))
    print("balance checks (no amounts):")
    for r in q("""SELECT c.account_id, c.asset_id, group_concat(DISTINCT c.kind) kinds, count(*) n, sum(c.ok) ok,
                         sum(c.lag) lag, min(CASE WHEN NOT c.ok THEN c.day END) first_bad,
                         o.quantity != '0' has_opening
                  FROM balance_check c LEFT JOIN opening_balance o USING (account_id, asset_id)
                  GROUP BY 1, 2 ORDER BY 1, 2"""):
        bad = f", first mismatch {r['first_bad']}" if r["first_bad"] else ""
        print(f"  {r['account_id']} {r['asset_id']:<14} {r['ok']}/{r['n']} ok ({r['kinds']})"
              f"{', ' + str(r['lag']) + ' via previous day' if r['lag'] else ''}"
              f"{', held before history' if r['has_opening'] else ''}{bad}")
    orph = q("SELECT count(*) FROM txn WHERE orphaned")[0][0]
    if orph:
        print(f"orphaned txns: {orph}")


def cmd_account(a):
    con = connect(Path(a.db))
    if not con.execute("SELECT 1 FROM account WHERE id=?", (a.id,)).fetchone():
        sys.exit(f"no account {a.id}")
    with con:
        I.set_account(con, a.id, name=a.name, institution=a.institution, closed=a.closed, note=a.note)
    print("\t".join(str(v) for v in con.execute("SELECT * FROM accounts WHERE id=?", (a.id,)).fetchone()))
    if Path(a.db).resolve() == DB_PATH.resolve():
        print(f"state: {I.export_state(con, STATE_PATH)} line(s) -> {STATE_PATH.relative_to(DATA.parent)}")


def cmd_merge_account(a):
    if Path(a.db).resolve() != DB_PATH.resolve():
        sys.exit("merge-account edits identity; run it on the live db")
    con = connect(Path(a.db))
    for acc in (a.src, a.dst):
        if not con.execute("SELECT 1 FROM account WHERE id=?", (acc,)).fetchone():
            sys.exit(f"no account {acc}")
    print(f"re-filed {I.merge_account(con, a.src, a.dst)} observation(s) from {a.src} into {a.dst}")
    con.close()
    con = run_ingest(Path(a.db), None)
    print(f"state: {I.export_state(con, STATE_PATH)} line(s) -> {STATE_PATH.relative_to(DATA.parent)}")


def cmd_report(a):
    """Monthly cash crossing the ownership boundary. Transfers, trades, journals and card
    settlements never cross it, so they can't inflate either side."""
    con = connect(Path(a.db))
    rows = con.execute("""
        SELECT substr(f.date, 1, 7) month, p.account_id cp, sum(p.quantity_f) q
        FROM posting p JOIN txn_fact f USING (txn_id)
        JOIN account a ON a.id = p.account_id AND NOT a.owned
        WHERE p.asset_id = 'fiat:USD' AND NOT f.pending AND substr(f.date, 1, 7) >= ?
        GROUP BY 1, 2 ORDER BY 1, 2""", (a.since,)).fetchall()
    months = sorted({r["month"] for r in rows})
    if a.by == "month":
        print(f"{'month':<8} {'in':>12} {'out':>12} {'net':>12}")
        for m in months:
            inn = -sum(r["q"] for r in rows if r["month"] == m and r["q"] < 0)
            out = sum(r["q"] for r in rows if r["month"] == m and r["q"] > 0)
            print(f"{m:<8} {inn:>12,.2f} {out:>12,.2f} {inn - out:>12,.2f}")
    else:
        tot: dict[str, float] = {}
        for r in rows:
            tot[r["cp"]] = tot.get(r["cp"], 0) + r["q"]
        print(f"{'counterparty':<28} {'out (+) / in (-)':>18}")
        for cp, q in sorted(tot.items(), key=lambda kv: -abs(kv[1])):
            print(f"{cp:<28} {q:>18,.2f}")


def cmd_dashboard(a):
    from . import dashboard
    out = dashboard.write(connect(Path(a.db)), Path(a.out))
    print(f"wrote {out} (holds amounts; mode 600)")


def cmd_serve(a):
    from . import portal
    portal.serve(a.host, a.port, a.dev)


def cmd_portal_password(a):
    """Reads the new password from a file (or stdin), so it never sits in shell history."""
    from . import portal
    pw = (Path(a.file).read_text() if a.file else sys.stdin.read()).strip()
    if len(pw) < 8:
        sys.exit("password too short (8+ characters)")
    portal.set_password(pw)
    print("portal password set; existing sessions signed out")


# Where each source's raw files come from. FinanceTracker runs no puller and holds no sign-in of its own: every
# source is an account in dither-accounts (an accounts manager), which signs in, pulls on its own schedule and keeps the files in its spool; `ledger pull` fetches them (ledger/accounts.py).
# "accounts" = fetched; "off" = not fetched for now (the account's files wait in its spool until it is switched).
SOURCE = {
    "simplefin": "accounts",
    "monero": "accounts",
    "privacy": "accounts",
    "venmo": "accounts",
    "amazon": "accounts",
    "schwab": "accounts",   # Schwab's CSV exports (parsers/schwab_csv.py)
}


def cmd_pull(a):
    """Fetch what the finance accounts in dither-accounts pulled, then ingest, classify and link repayments."""
    from . import accounts
    if bad := set(a.sources) - set(SOURCE):
        sys.exit(f"unknown source(s): {' '.join(bad)}")
    for n in [n for n in a.sources if SOURCE[n] == "off"]:
        print(f"== {n}: off (SOURCE in ledger/cli.py): not fetched until it is switched", flush=True)
    names = [n for n in (a.sources or SOURCE) if SOURCE[n] == "accounts"]
    failed = []
    if names and accounts.app() is None:
        print("dither-accounts: FinanceTracker is not registered (python -m ledger.accounts register)")
        failed = names
    elif names:
        print(f"== fetch {' '.join(names)} from dither-accounts", flush=True)
        try:
            # Named on the command line (the portal's pull button): pull now. The daily run: only if stale.
            res = accounts.fetch(names, refresh="always" if a.sources else "stale")
        except Exception as e:  # noqa: BLE001 - the manager is down: ingest what we have
            print(f"dither-accounts: {e}")
            res = {n: {"error": str(e)} for n in names}
        failed += [n for n, r in res.items() if r.get("error")]
    _ingest(argparse.Namespace(db=a.db, files=[], reparse=False))
    from . import prices
    try:
        prices.fetch(connect(Path(a.db)))
    except Exception as e:  # noqa: BLE001 - yesterday's prices still serve
        print(f"prices: {e}")
    from . import classify, jev
    try:   # new transactions get the model's opinion too
        print("classify:", classify.run(connect(Path(a.db)), log=lambda *x: None))
    except Exception as e:  # noqa: BLE001 - Claude unreachable or out of quota: asked again next run
        print(f"classify: {e}")
    if jev.up():   # repayments and durable goods still use the local model
        from . import flows
        print("flows:", flows.update(connect(Path(a.db)), log=lambda *x: None))
    if failed:
        sys.exit(f"failed: {' '.join(failed)}")


def cmd_classify(a):
    from . import classify, jev
    from . import flows
    con = connect(Path(a.db))
    print(classify.run(con, force=a.force, limit=a.limit))
    if jev.up():
        print(flows.update(con))   # repayments and durable goods (the local model)


def cmd_prices(a):
    from . import prices
    prices.fetch(connect(Path(a.db)))


def cmd_sql(a):
    con = connect(Path(a.db))
    cur = con.execute(a.query)
    if cur.description:
        print("\t".join(d[0] for d in cur.description))
        for r in cur:
            print("\t".join("" if v is None else str(v) for v in r))
    con.commit()


def main(argv=None):
    os.umask(0o077)   # data/ holds financial records: files 600, dirs 700
    ap = argparse.ArgumentParser(prog="ledger", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(DB_PATH))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest")
    p.add_argument("files", nargs="*")
    p.add_argument("--reparse", action="store_true", help="re-run parsers even on already-parsed files")
    p.set_defaults(fn=cmd_ingest)
    p = sub.add_parser("build")
    p.add_argument("--scratch", action="store_true")
    p.add_argument("--out", default=str(DB_PATH))
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_build)
    sub.add_parser("verify").set_defaults(fn=cmd_verify)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    p = sub.add_parser("account", help="name or annotate an account (kept in state.jsonl); '' clears a field")
    p.add_argument("id")
    p.add_argument("--name")
    p.add_argument("--institution")
    p.add_argument("--closed", metavar="YYYY-MM-DD")
    p.add_argument("--note")
    p.set_defaults(fn=cmd_account)
    p = sub.add_parser("merge-account", help="declare that account SRC is account DST (e.g. a feed's unnamed bank)")
    p.add_argument("src")
    p.add_argument("dst")
    p.set_defaults(fn=cmd_merge_account)
    p = sub.add_parser("report", help="monthly money in/out across the ownership boundary (prints amounts)")
    p.add_argument("--since", default="0000-00", help="first month, YYYY-MM")
    p.add_argument("--by", choices=["month", "counterparty"], default="month")
    p.set_defaults(fn=cmd_report)
    p = sub.add_parser("dashboard", help="write a self-contained HTML page of spending and the budget model")
    p.add_argument("--out", default=str(DATA / "dashboard.html"))
    p.set_defaults(fn=cmd_dashboard)
    p = sub.add_parser("serve", help="run the finance portal (see ledger/portal.py)")
    p.add_argument("--host", default=config.get("portal", "host"), help="default: config.toml [portal] host")
    p.add_argument("--port", type=int, default=config.get("portal", "port"))
    p.add_argument("--dev", action="store_true", help="no sign-in; loopback only")
    p.set_defaults(fn=cmd_serve)
    p = sub.add_parser("portal-password", help="set the portal password (read from --file or stdin)")
    p.add_argument("--file")
    p.set_defaults(fn=cmd_portal_password)
    p = sub.add_parser("pull", help="fetch what the finance accounts in dither-accounts pulled, then ingest")
    p.add_argument("sources", nargs="*", help="simplefin privacy monero amazon venmo")
    p.set_defaults(fn=cmd_pull)
    p = sub.add_parser("classify", help="ask the local model to categorize transactions (new or changed ones)")
    p.add_argument("--force", action="store_true", help="redo every transaction")
    p.add_argument("--limit", type=int)
    p.set_defaults(fn=cmd_classify)
    sub.add_parser("prices", help="fetch XMR/USD daily closes (Kraken public API) into data/prices.json").set_defaults(fn=cmd_prices)
    p = sub.add_parser("sql")
    p.add_argument("query")
    p.set_defaults(fn=cmd_sql)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
