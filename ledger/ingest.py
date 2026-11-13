"""Ingest raw files -> evidence; link evidence -> txns; resolve txns -> facts.

Determinism: given the same db state and the same raw files, every step produces the same
rows. Links are only ever added for unlinked observations, so existing txn ids (and anything
the user hung on them) survive re-ingests. `build_scratch` + state.jsonl reproduces a db.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

from .db import DATA
from .model import Parsed, asset_kind, h, local_day, pstr, qstr, ulp
from .parsers import detect

# Earlier feeds mint txns first and win field resolution. 'manual' is reserved for hand entries.
FEED_PRIORITY = ["manual", "schwab_web", "schwab_csv", "simplefin", "privacy", "cake", "monero"]
MAX_DAYS = 4          # posted-date slack when matching the same txn across feeds
XFER_DAYS = 4         # max days between the two sides of an own-account transfer
# Both sides of an own-account transfer must say so; equal-and-opposite amounts alone collide.
XFER = re.compile(r"\b(transfer|xfer|journal|moneylink|wire|funds received)\b", re.I)
# Two exports of the same rows (Schwab's printed history pages and its CSV exports): a row of the first feed is
# paired with its own row of the second (same account, posted day, cash, positions and running balance), one to one,
# before any fuzzy match. Fuzzy matching takes one new row per txn and account, so it cannot pair two rows of one
# account in one txn (the halves of a share journal), and would mint the second.
SAME_ROWS = {"schwab_csv": "schwab_web"}
# Feeds that name a chain transaction by its txid (ext_id): a row of one joins the txn that holds another's row with
# the same txid on the same account, before any fuzzy match. A wallet app's export (cake) and the view-only wallet
# (monero) see one transaction differently: the view-only wallet, without key images, reports a payment's change as
# received; the app reports the payment. Linked, the better feed speaks for the account (FEED_PRIORITY).
TXID_FEEDS = {"cake", "monero"}
DECIMALS = {"fiat:USD": 2}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pri(feed: str) -> tuple:
    return (FEED_PRIORITY.index(feed) if feed in FEED_PRIORITY else 99, feed)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _j(d) -> str | None:
    return json.dumps(d, sort_keys=True, default=str) if d else None


# ---------------------------------------------------------------- evidence

def ingest(con, paths: list[Path], reparse: bool = False, full_scan: bool = False, log=print) -> None:
    seen = set()
    for path in sorted(paths):
        mod = detect(path)
        if mod is None:
            log(f"skip (no parser): {path}")
            continue
        sha = _sha(path)
        seen.add(sha)
        # stored as data/raw/... wherever FINANCE_HOME puts data/ (resolve() follows a symlinked data/)
        rp, dr = path.resolve(), DATA.resolve()
        rel = str(Path("data") / rp.relative_to(dr)) if rp.is_relative_to(dr) else str(path)
        row = con.execute("SELECT parser_version FROM source_file WHERE sha256=?", (sha,)).fetchone()
        if row and row[0] == mod.VERSION and not reparse:
            with con:
                con.execute("UPDATE source_file SET path=?, present=1 WHERE sha256=?", (rel, sha))
            continue
        parsed = mod.parse(path)
        with con:
            _store(con, sha, rel, path.stat().st_size, parsed)
        log(f"parsed {rel}: {len(parsed.obs)} observations, {len(parsed.snaps)} snapshots"
            + "".join(f"\n  warning: {w}" for w in parsed.warnings))
    if full_scan:
        with con:
            q = ",".join("?" * len(seen))
            con.execute(f"UPDATE source_file SET present=0 WHERE sha256 NOT IN ({q})", list(seen))


def _asset(con, aid: str, info: dict | None = None) -> None:
    info = info or {}
    con.execute("INSERT OR IGNORE INTO asset(id, kind, symbol, decimals) VALUES (?,?,?,?)",
                (aid, asset_kind(aid), info.get("symbol") or aid.split(":", 1)[-1], DECIMALS.get(aid)))
    if info.get("name"):
        con.execute("UPDATE asset SET name=? WHERE id=? AND name IS NULL", (info["name"], aid))


def _account(con, ref, cache: dict) -> str:
    k = (ref.feed, ref.ext_key)
    if k in cache:
        return cache[k]
    r = con.execute("SELECT account_id FROM account_alias_pin WHERE feed=? AND ext_key=?", k).fetchone() or \
        con.execute("SELECT account_id FROM account_alias WHERE feed=? AND ext_key=?", k).fetchone()
    if r:
        acc = r[0]
        con.execute("INSERT OR IGNORE INTO account(id, institution) VALUES (?, 'unknown')", (acc,))  # filled in later
        con.execute("INSERT OR REPLACE INTO account_alias VALUES (?,?,?)", (*k, acc))
    else:
        r = ref.mask and con.execute(
            "SELECT id FROM account WHERE institution=? AND json_extract(meta,'$.mask')=?",
            (ref.institution, ref.mask)).fetchone()
        acc = r[0] if r else f"{ref.institution}-{ref.mask or h(ref.feed, ref.ext_key, n=8)}"
        if not r:
            _asset(con, ref.base_asset)
            con.execute("""INSERT INTO account(id, institution, name, kind, base_asset, meta) VALUES (?,?,?,?,?,?)
                           ON CONFLICT(id) DO UPDATE SET institution=excluded.institution, name=excluded.name,
                             kind=excluded.kind, base_asset=excluded.base_asset, meta=excluded.meta""",
                        (acc, ref.institution, ref.name, ref.kind, ref.base_asset, _j({"mask": ref.mask})))
        con.execute("INSERT INTO account_alias VALUES (?,?,?)", (*k, acc))
    cache[k] = acc
    return acc


def _store(con, sha: str, rel: str, size: int, p: Parsed) -> None:
    con.execute("""INSERT INTO source_file(sha256, path, size, parser, parser_version, coverage_lo, coverage_hi,
                                           meta, first_seen, parsed_at, present)
                   VALUES (?,?,?,?,?,?,?,?,?,?,1)
                   ON CONFLICT(sha256) DO UPDATE SET path=excluded.path, parser=excluded.parser,
                     parser_version=excluded.parser_version, coverage_lo=excluded.coverage_lo,
                     coverage_hi=excluded.coverage_hi, meta=excluded.meta, parsed_at=excluded.parsed_at, present=1""",
                (sha, rel, size, p.parser, p.version, *(p.coverage or (None, None)),
                 _j({**p.meta, "warnings": p.warnings}), _now(), _now()))
    for aid, info in p.assets.items():
        _asset(con, aid, info)
    accts: dict = {}

    # fingerprint = feed + account + natural key + occurrence index of that key in this file,
    # so identical rows within a file stay distinct but overlapping files agree.
    occ: Counter = Counter()
    new: dict[str, tuple] = {}
    for o in p.obs:
        base = (p.feed, o.account.ext_key, o.key)
        fp = f"{p.feed}:{h(*base, occ[base])}"
        occ[base] += 1
        new[h(sha, o.locator, n=24)] = (o, fp)

    old = dict(con.execute("SELECT id, fingerprint FROM observation WHERE source_sha=?", (sha,)).fetchall())
    gone = [i for i in old if i not in new]
    con.executemany("DELETE FROM observation WHERE id=?", [(i,) for i in gone])
    # A re-parse that changes what an observation says (fingerprint, legs or times) re-opens its
    # identity: it is unlinked to match afresh, and its old txn carries over if left empty.
    said = {}
    for oid in old:
        said[oid] = (sorted(tuple(r) for r in con.execute(
                         "SELECT asset_id, quantity, role FROM obs_leg WHERE obs_id=?", (oid,))),
                     sorted(tuple(r) for r in con.execute("SELECT role, value FROM obs_time WHERE obs_id=?", (oid,))))

    def says(o) -> tuple:
        return (sorted((l.asset, pstr(l.quantity), l.role) for l in o.legs), sorted((t.role, t.value) for t in o.times))
    refp = [i for i, (o, fp) in new.items() if i in old and (old[i] != fp or said[i] != says(o))]
    con.executemany("""INSERT OR IGNORE INTO txn_carry SELECT obs_id, txn_id FROM txn_link WHERE obs_id=?""",
                    [(i,) for i in refp])
    con.executemany("DELETE FROM txn_link WHERE obs_id=?", [(i,) for i in refp])

    for oid, (o, fp) in new.items():
        acc = _account(con, o.account, accts)
        con.execute("""INSERT INTO observation(id, source_sha, locator, feed, account_id, fingerprint, ext_id, kind,
                                               description, payee, memo, pending, extra, raw)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET feed=excluded.feed, account_id=excluded.account_id,
                         fingerprint=excluded.fingerprint, ext_id=excluded.ext_id, kind=excluded.kind,
                         description=excluded.description, payee=excluded.payee, memo=excluded.memo,
                         pending=excluded.pending, extra=excluded.extra, raw=excluded.raw""",
                    (oid, sha, o.locator, p.feed, acc, fp, o.ext_id, o.kind, o.description, o.payee, o.memo,
                     int(o.pending), _j(o.extra), json.dumps(o.raw, sort_keys=True, default=str)))
        con.execute("DELETE FROM obs_time WHERE obs_id=?", (oid,))
        con.execute("DELETE FROM obs_leg WHERE obs_id=?", (oid,))
        con.executemany("INSERT INTO obs_time VALUES (?,?,?,?,?,?,?)",
                        [(oid, t.role, t.value, t.precision, t.tz, t.lo, t.hi) for t in o.times])
        for n, l in enumerate(o.legs):
            _asset(con, l.asset)
            if l.price_asset:
                _asset(con, l.price_asset)
            con.execute("INSERT INTO obs_leg VALUES (?,?,?,?,?,?,?,?)",
                        (oid, n, _account(con, l.account, accts) if l.account else acc, l.asset, pstr(l.quantity),
                         pstr(l.unit_price) if l.unit_price is not None else None, l.price_asset, l.role))

    con.execute("DELETE FROM snapshot WHERE source_sha=?", (sha,))
    for s in p.snaps:
        _asset(con, s.asset)
        if s.value_asset:
            _asset(con, s.value_asset)
        con.execute("INSERT INTO snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sha, s.locator, _account(con, s.account, accts), s.asset, s.kind, s.as_of.value,
                     s.as_of.precision, s.as_of.lo, qstr(s.quantity) if s.quantity is not None else None,
                     qstr(s.value) if s.value is not None else None, s.value_asset, _j(s.extra)))

    con.execute("DELETE FROM merchant_order WHERE source_sha=?", (sha,))
    for o in p.orders:
        con.execute("INSERT OR REPLACE INTO merchant_order VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (sha, o.merchant, o.order_id, o.placed, qstr(o.total) if o.total is not None else None, o.status,
                     o.ship_to, o.payment, json.dumps(o.items), json.dumps(o.detail) if o.detail else None, o.pulled_at))
    con.execute("DELETE FROM realized_close WHERE source_sha=?", (sha,))
    opt = lambda d: qstr(d) if d is not None else None
    for r in p.realized:
        _asset(con, r.asset, p.assets.get(r.asset))
        con.execute("INSERT INTO realized_close VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sha, r.locator, _account(con, r.account, accts), r.asset, r.closed, qstr(r.quantity),
                     opt(r.closing_price), r.method, qstr(r.proceeds), qstr(r.cost), qstr(r.gain),
                     opt(r.long_term), opt(r.short_term), qstr(r.disallowed)))


# ---------------------------------------------------------------- loading

def _load(con) -> dict[str, dict]:
    """All observations with times, legs and derived matching facts."""
    obs = {}
    acct_base = dict(con.execute("SELECT id, base_asset FROM account").fetchall())
    for r in con.execute("""SELECT o.*, l.txn_id FROM observation o LEFT JOIN txn_link l ON l.obs_id = o.id"""):
        d = dict(r)
        text = f"{r['kind'] or ''} {r['description'] or ''} {r['payee'] or ''}"
        d.update(times=[], legs=[], amount=Decimal(0), days=set(), xfer=bool(XFER.search(text)),
                 tokens=set(re.findall(r"[a-z0-9]{2,}", f"{r['description'] or ''} {r['payee'] or ''}".lower())))
        obs[r["id"]] = d
    for r in con.execute("SELECT * FROM obs_time ORDER BY obs_id, role"):
        o = obs[r["obs_id"]]
        o["times"].append(dict(r))
        o["days"].add(dt.date.fromisoformat(r["value"][:10]).toordinal())
    for r in con.execute("SELECT * FROM obs_leg ORDER BY obs_id, n"):
        o = obs[r["obs_id"]]
        o["legs"].append(dict(r))
        if r["role"] in ("cash", "main") and r["account_id"] == o["account_id"] \
                and r["asset_id"] == acct_base.get(r["account_id"]):   # its own account's cash only
            o["amount"] += Decimal(r["quantity"])
    return obs


# ---------------------------------------------------------------- identity

def _row_key(o: dict) -> tuple:
    posted = next((t["value"][:10] for t in o["times"] if t["role"] == "posted"), None)
    pos = tuple(sorted((l["asset_id"], str(Decimal(l["quantity"]).normalize())) for l in o["legs"] if l["role"] == "position"))
    bal = json.loads(o["extra"]).get("running_balance") if o["extra"] else None
    return (o["account_id"], posted, str(o["amount"].normalize()), pos, bal)


def _same_rows(obs: dict, groups: dict, fps: list, feed: str, other: str, attach) -> list:
    """Pair `feed`'s new rows with `other`'s rows that are the same row (SAME_ROWS); returns the fps left unpaired."""
    theirs: dict[tuple, list[dict]] = defaultdict(list)
    paired: Counter = Counter()      # rows of `feed` already paired, per row key (each holds one of `other`'s rows)
    for o in sorted(obs.values(), key=lambda o: o["id"]):
        if o["feed"] == other and o["txn_id"]:
            theirs[_row_key(o)].append(o)
    seen = set()
    for o in obs.values():
        if o["feed"] == feed and o["txn_id"] and o["fingerprint"] not in seen and o["fingerprint"] not in fps:
            seen.add(o["fingerprint"])
            paired[_row_key(o)] += 1
    left = []
    for fp in fps:
        k = _row_key(groups[fp][0])
        if paired[k] < len(theirs.get(k, ())):
            attach(groups[fp], theirs[k][paired[k]]["txn_id"], "same-row")
            paired[k] += 1
        else:
            left.append(fp)
    return left


def _same_txid(by_txn: dict, groups: dict, fps: list, feed: str, attach) -> list:
    """Join `feed`'s new rows to the txn holding another TXID_FEEDS feed's row with the same txid and account."""
    where: dict[tuple, str] = {}
    for tid, os_ in sorted(by_txn.items()):
        for o in os_:
            if o["feed"] in TXID_FEEDS and o["feed"] != feed and o["ext_id"]:
                where.setdefault((o["account_id"], o["ext_id"]), tid)
    left = []
    for fp in fps:
        rep = groups[fp][0]
        tid = rep["ext_id"] and where.get((rep["account_id"], rep["ext_id"]))
        if tid:
            attach(groups[fp], tid, "txid")
        else:
            left.append(fp)
    return left


def _mint(con, fp: str) -> str:
    tid = "tx_" + h(fp, n=16)
    con.execute("INSERT OR IGNORE INTO txn(id, minted_from, minted_at) VALUES (?,?,?)", (tid, fp, _now()))
    con.execute("UPDATE txn SET orphaned=0 WHERE id=?", (tid,))
    return tid


def link(con, log=print) -> Counter:
    obs = _load(con)
    stats: Counter = Counter()
    by_txn: dict[str, list[dict]] = defaultdict(list)
    fp_txn: dict[str, str] = {}
    for o in obs.values():
        if o["txn_id"]:
            by_txn[o["txn_id"]].append(o)
            fp_txn.setdefault(o["fingerprint"], o["txn_id"])
    pins = {r[0]: r[1] for r in con.execute("SELECT fingerprint, txn_id FROM link_pin")}

    def attach(group: list[dict], tid: str, method: str, score=None):
        for o in group:
            con.execute("INSERT INTO txn_link VALUES (?,?,?,?,?)", (o["id"], tid, method, score, _now()))
            o["txn_id"] = tid
            by_txn[tid].append(o)
        fp_txn.setdefault(group[0]["fingerprint"], tid)
        stats[method] += len(group)

    unlinked = [o for o in obs.values() if not o["txn_id"]]
    with con:
        for feed in sorted({o["feed"] for o in unlinked}, key=_pri):
            groups: dict[str, list[dict]] = defaultdict(list)
            for o in sorted((o for o in unlinked if o["feed"] == feed), key=lambda o: o["id"]):
                groups[o["fingerprint"]].append(o)
            fuzzy = []
            for fp in sorted(groups):
                if fp in pins:
                    attach(groups[fp], pins[fp] or _mint(con, fp), "pin")
                elif fp in fp_txn:
                    attach(groups[fp], fp_txn[fp], "fingerprint")
                else:
                    fuzzy.append(fp)
            if feed in SAME_ROWS:
                fuzzy = _same_rows(obs, groups, fuzzy, feed, SAME_ROWS[feed], attach)
            if feed in TXID_FEEDS:
                fuzzy = _same_txid(by_txn, groups, fuzzy, feed, attach)

            # cross-feed, same account: same cash amount, nearby dates; best pairs first, one-to-one.
            # A txn may span accounts (transfers), so compare only against its obs in this account.
            index = defaultdict(set)
            for tid, os_ in by_txn.items():
                for o in os_:
                    index[(o["account_id"], o["amount"])].add(tid)
            pairs = []
            for fp in fuzzy:
                rep = groups[fp][0]
                for tid in index.get((rep["account_id"], rep["amount"]), ()):
                    tos = [t for t in by_txn[tid] if t["account_id"] == rep["account_id"]]
                    if any(t["feed"] == feed and (rep["pending"] or not t["pending"]) for t in tos):
                        continue
                    dd = min(abs(a - b) for t in tos for a in rep["days"] for b in t["days"])
                    if dd > MAX_DAYS:
                        continue
                    ttok = set().union(*(t["tokens"] for t in tos))
                    sim = len(rep["tokens"] & ttok) / (len(rep["tokens"] | ttok) or 1)
                    pairs.append((dd, -sim, fp, tid))
            used_fp, used_t = set(), set()
            for dd, nsim, fp, tid in sorted(pairs):
                # one new row per txn and account: a transfer's two sides may both join it in one run
                if fp in used_fp or (tid, groups[fp][0]["account_id"]) in used_t:
                    continue
                used_fp.add(fp)
                used_t.add((tid, groups[fp][0]["account_id"]))
                attach(groups[fp], tid, "fuzzy", round(-nsim - 0.1 * dd, 3))
            for fp in fuzzy:
                if fp not in used_fp:
                    attach(groups[fp], _mint(con, fp), "mint")
        stats["carried"] += carry_over(con)
        stats["transfer"] += merge_transfers(con, by_txn, pins)
        con.execute("""UPDATE txn SET orphaned = merged_into IS NULL
                         AND NOT EXISTS (SELECT 1 FROM txn_link l WHERE l.txn_id = txn.id)""")
    return +stats


def carry_over(con) -> int:
    """A txn emptied by fingerprint changes is merged into where its observations went."""
    n = 0
    for obs_id, old, new in con.execute("""SELECT c.obs_id, c.old_txn, l.txn_id FROM txn_carry c
                                           JOIN txn_link l ON l.obs_id = c.obs_id ORDER BY c.obs_id""").fetchall():
        alive = con.execute("SELECT 1 FROM txn_link WHERE txn_id=? LIMIT 1", (old,)).fetchone()
        merged = con.execute("SELECT merged_into FROM txn WHERE id=?", (old,)).fetchone()[0]
        if old != new and not alive and not merged:
            absorb(con, new, old, "carry")
            n += 1
    con.execute("DELETE FROM txn_carry WHERE obs_id IN (SELECT obs_id FROM txn_link)")
    return n


def merge_transfers(con, by_txn: dict[str, list[dict]], pins: dict) -> int:
    """Merge single-account txns that are two sides of one movement inside the boundary.

    What an event moves is its cash amount or, when it has no cash, its lone unpriced share
    leg. Two events pair when they move equal and opposite amounts of the same thing within
    XFER_DAYS and both are described as transfers/journals:
      * cash between two different owned accounts (checking <-> brokerage);
      * shares within one account (brokers journal shares between sub-accounts, out and back).
    An event whose best partner is tied is left alone (pin it by hand). The outgoing side's txn
    survives; the other is marked merged_into it. Runs over every txn, not just new ones, so a
    side that arrives late still pairs; it only ever merges, so ids annotations hang on stay
    valid via merged_into.
    """
    owned = {r[0] for r in con.execute("SELECT id FROM account WHERE owned")}
    never = {fp for fp, t in pins.items() if t is None}
    cands = {}
    for tid, os_ in by_txn.items():
        accts, amts = {o["account_id"] for o in os_}, {o["amount"] for o in os_}
        if len(accts) != 1 or len(amts) != 1 or not accts <= owned:
            continue
        if not any(o["xfer"] for o in os_) or any(o["fingerprint"] in never for o in os_):
            continue
        amt = next(iter(amts))
        shares = {(l["asset_id"], Decimal(l["quantity"])) for o in os_ for l in o["legs"]
                  if l["role"] == "position" and l["unit_price"] is None}
        if amt:
            move = ("cash", amt)
        elif len(shares) == 1:
            move = next(iter(shares))
        else:
            continue
        if move[1]:
            cands[tid] = (next(iter(accts)), move, {d for o in os_ for d in o["days"]})
    by_move = defaultdict(list)
    for tid, (_, move, _) in cands.items():
        by_move[move].append(tid)
    pairs = []
    for out, (a, (what, q), days) in cands.items():
        for inn in by_move.get((what, -q), []) if q < 0 else ():
            b, _, days2 = cands[inn]
            dd = min(abs(x - y) for x in days for y in days2)
            if (b != a) == (what == "cash") and dd <= XFER_DAYS:
                pairs.append((dd, out, inn))
    best: dict[str, list[int]] = defaultdict(list)
    for dd, out, inn in pairs:
        best[out].append(dd)
        best[inn].append(dd)
    tied = {t for t, ds in best.items() if ds.count(min(ds)) > 1}
    used, n = set(), 0
    for dd, out, inn in sorted(pairs):
        if out in used or inn in used or out in tied or inn in tied:
            continue
        used |= {out, inn}
        absorb(con, out, inn, "transfer")
        by_txn[out] += by_txn.pop(inn)
        n += 1
    return n


def absorb(con, keep: str, gone: str, method: str) -> None:
    """Move txn `gone`'s observations, pins and annotation onto `keep`."""
    con.execute("UPDATE txn_link SET txn_id=?, method=? WHERE txn_id=?", (keep, method, gone))
    con.execute("UPDATE link_pin SET txn_id=? WHERE txn_id=?", (keep, gone))
    con.execute("UPDATE txn SET merged_into=? WHERE id=? OR merged_into=?", (keep, gone, gone))
    a = con.execute("SELECT * FROM txn_annotation WHERE txn_id=?", (gone,)).fetchone()
    if not a:
        return
    k = con.execute("SELECT * FROM txn_annotation WHERE txn_id=?", (keep,)).fetchone()
    if not k:
        con.execute("UPDATE txn_annotation SET txn_id=? WHERE txn_id=?", (keep, gone))
        return
    note = "\n".join(x for x in (k["note"], a["note"]) if x) or None
    con.execute("UPDATE txn_annotation SET category=?, tags=?, overrides=?, note=?, updated_at=? WHERE txn_id=?",
                (k["category"] or a["category"], k["tags"] or a["tags"], k["overrides"] or a["overrides"],
                 note, _now(), keep))
    con.execute("DELETE FROM txn_annotation WHERE txn_id=?", (gone,))


# ---------------------------------------------------------------- resolved

def _rank(o: dict) -> tuple:
    return (_pri(o["feed"]), o["pending"], -len(o["legs"]), o["id"])


def _occurred(os_: list[dict]) -> dict:
    times = [t for o in os_ for t in o["times"]]
    return next((t for t in times if t["role"] == "transacted"), None) or \
        next((t for t in os_[0]["times"] if t["role"] == "posted"), None) or times[0]


def _sides(g: list[dict]) -> list[dict]:
    """The observations that speak for one account in one txn (g is ranked best first).

    Other feeds and repeated fingerprints are duplicates of the best feed's rows. Distinct
    posted rows of that feed are complementary parts of the event (fuzzy matching never puts
    two of them together; only merges do, e.g. the two halves of a share journal); a pending
    row counts only while nothing posted exists.
    """
    feed = g[0]["feed"]
    rows = [o for o in g if o["feed"] == feed and not o["pending"]] or [g[0]]
    out, fps = [], set()
    for o in rows:
        if o["fingerprint"] not in fps:
            fps.add(o["fingerprint"])
            out.append(o)
    return out


def _relayed(o: dict) -> bool:
    return bool(o["extra"]) and bool(json.loads(o["extra"]).get("relayed"))


def _day(t: dict) -> str:
    return local_day(t["value"], t["precision"], t["tz"], t["lo"])


def _asset_ulp(asset: str) -> Decimal:
    return Decimal(1).scaleb(-DECIMALS.get(asset, 8))


def _ext_account(con, cat: str | None) -> str:
    aid = f"ext:{cat or 'uncategorized'}"
    con.execute("""INSERT OR IGNORE INTO account(id, institution, name, kind, owned)
                   VALUES (?, 'external', ?, 'external', 0)""", (aid, cat or "uncategorized"))
    return aid


def resolve(con) -> int:
    """Rebuild txn_fact, entry and posting: one balanced event per txn.

    Each owned account's postings come from the best observation *from that account*, so two
    feeds that saw the same account never double its postings, and two accounts' feeds that
    saw one transfer each contribute their own side. Whatever doesn't net to zero (by value
    at the stated price, within rounding) is posted to the event's external counterparty.
    """
    obs = _load(con)
    owned = {r[0] for r in con.execute("SELECT id FROM account WHERE owned")}
    base = dict(con.execute("SELECT id, base_asset FROM account").fetchall())
    category = dict(con.execute("SELECT txn_id, category FROM txn_annotation").fetchall())
    by_txn: dict[str, list[dict]] = defaultdict(list)
    for o in obs.values():
        if o["txn_id"]:
            by_txn[o["txn_id"]].append(o)
    # days each account's own feeds cover; relayed rows (a third party reporting on the account)
    # that no own row confirms inside this window go to ext:unreconciled
    native: dict[str, tuple[str, str]] = {}
    for o in obs.values():
        if not _relayed(o) and o["times"]:
            d = _day(o["times"][0])
            lo, hi = native.get(o["account_id"], (d, d))
            native[o["account_id"]] = (min(lo, d), max(hi, d))
    with con:
        for t in ("posting", "entry", "txn_fact"):
            con.execute(f"DELETE FROM {t}")
        for tid in sorted(by_txn):
            os_ = sorted(by_txn[tid], key=_rank)
            groups: dict[str, list[dict]] = {}
            for o in os_:
                groups.setdefault(o["account_id"], []).append(o)
            when = {a: _occurred(g) for a, g in groups.items()}

            # A posting is dated when it hit the account (posted), which is what balances follow;
            # entries are dated when the event happened (transacted), which is what budgets follow.
            postings, voice = [], {}   # voice: account -> observation its postings came from
            for acct, g in list(groups.items()):
                sides = _sides(g)
                lo, hi = native.get(acct, ("", ""))
                unconfirmed = all(_relayed(p) for p in sides) and lo <= _day(when[acct]) <= hi
                for p in sides:
                    posted = _day(next((t for t in p["times"] if t["role"] == "posted"), when[acct]))
                    for l in p["legs"]:
                        if l["account_id"] != acct:
                            continue
                        if unconfirmed:
                            postings.append(dict(l, account_id=_ext_account(con, "unreconciled"), role="counter",
                                                 date=posted, obs_id=p["id"]))
                        else:
                            postings.append(dict(l, date=posted, obs_id=p["id"]))
                            voice.setdefault(acct, p)
                if unconfirmed:
                    del groups[acct]
            # legs an observation states for another account (an ACH's card-account side) count
            # when no observation from that account itself is in the event
            for p in os_:
                for l in p["legs"]:
                    if l["account_id"] == p["account_id"] or l["account_id"] in groups:
                        continue
                    if voice.setdefault(l["account_id"], p) is p:
                        posted = _day(next((t for t in p["times"] if t["role"] == "posted"), p["times"][0]))
                        postings.append(dict(l, date=posted, obs_id=p["id"]))
            for acct, g in groups.items():
                for p in _sides(g):
                    posted = _day(next((t for t in p["times"] if t["role"] == "posted"), when[acct]))
                    if fee := json.loads(p["extra"] or "{}").get("fees"):   # reported fees, already in its cash
                        postings.append({"account_id": _ext_account(con, "fees"), "asset_id": base[acct],
                                         "quantity": fee, "unit_price": None, "price_asset": None, "role": "fee",
                                         "date": posted, "obs_id": p["id"]})

            # Balance by weight. Priced legs are only as exact as their printed digits, so an
            # asset that has one tolerates the rounding those digits allow (Beancount's rule).
            resid: dict[str, Decimal] = defaultdict(Decimal)
            slop: dict[str, Decimal] = defaultdict(Decimal)
            priced = set()
            for l in postings:
                q = Decimal(l["quantity"])
                if l["unit_price"] is not None and l["price_asset"]:
                    pr = Decimal(l["unit_price"])
                    resid[l["price_asset"]] += q * pr
                    slop[l["price_asset"]] += abs(q) * ulp(l["unit_price"]) / 2 + abs(pr) * ulp(l["quantity"]) / 2
                    priced.add(l["price_asset"])
                else:
                    resid[l["asset_id"]] += q
                    slop[l["asset_id"]] += ulp(l["quantity"]) / 2
            first = min(when.values(), key=lambda t: (t["lo"], t["value"]))
            for asset, r in sorted(resid.items()):
                if abs(r) > (slop[asset] if asset in priced else 0):
                    q = -r.quantize(_asset_ulp(asset)) if asset in DECIMALS else -r
                    postings.append({"account_id": _ext_account(con, category.get(tid)), "asset_id": asset,
                                     "quantity": pstr(q), "unit_price": None, "price_asset": None,
                                     "role": "counter", "date": _day(first), "obs_id": None})

            touched = {p["account_id"] for p in postings}
            accts = [a for a in voice if a in owned]
            for a in accts:
                if a not in groups:   # spoken for by another account's observation
                    groups[a] = [voice[a]]
                    when[a] = _occurred([voice[a]])
            flow = "external" if touched - owned else "transfer" if len(touched) > 1 else "internal"
            for n, p in enumerate(postings):
                con.execute("INSERT INTO posting VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (tid, n, p["account_id"], p["asset_id"], p["quantity"], float(p["quantity"]),
                             p["unit_price"], p["price_asset"], p["role"], p["date"], p["obs_id"]))
            for acct in accts:
                g = groups[acct]
                amt = sum((Decimal(p["quantity"]) for p in postings if p["account_id"] == acct
                           and p["asset_id"] == base.get(acct) and p["role"] in ("cash", "main")), Decimal(0))
                cp = ",".join(sorted(touched - {acct}))
                con.execute("INSERT INTO entry VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    tid, acct, _day(when[acct]), when[acct]["precision"], g[0]["kind"], g[0]["description"],
                    next((o["payee"] for o in g if o["payee"]), None), qstr(amt), float(amt), cp,
                    int(all(o["pending"] for o in g)), len(g), ",".join(sorted({o["feed"] for o in g}, key=_pri))))
            p = os_[0]
            con.execute("INSERT INTO txn_fact VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                tid, _day(first), first["value"], first["precision"], first["lo"], first["hi"], p["kind"],
                p["description"], flow, ",".join(sorted(accts)), int(all(o["pending"] for o in os_)), len(os_),
                ",".join(sorted({o["feed"] for o in os_}, key=_pri))))
    return len(by_txn)


# ---------------------------------------------------------------- portable state

def export_state(con, path: Path) -> int:
    """Identity + user layers, anchored to fingerprints (stable across rebuilds). No amounts/descriptions."""
    fps = defaultdict(set)
    for tid, fp in con.execute("SELECT l.txn_id, o.fingerprint FROM txn_link l JOIN observation o ON o.id = l.obs_id"):
        fps[tid].add(fp)
    ann = {r["txn_id"]: {k: r[k] for k in ("category", "tags", "note", "overrides", "updated_at") if r[k] is not None}
           for r in con.execute("SELECT * FROM txn_annotation")}
    lines = []
    for r in con.execute("SELECT id, minted_from, merged_into FROM txn ORDER BY id"):
        if r["merged_into"]:
            lines.append({"txn": r["id"], "minted_from": r["minted_from"], "merged_into": r["merged_into"]})
        elif r["id"] in fps or r["id"] in ann:
            d = {"txn": r["id"], "minted_from": r["minted_from"], "fps": sorted(fps.get(r["id"], ()))}
            if r["id"] in ann:
                d["annotation"] = ann[r["id"]]
            lines.append(d)
    for r in con.execute("SELECT * FROM link_pin ORDER BY fingerprint"):
        lines.append({"pin": r["fingerprint"], "txn": r["txn_id"], "reason": r["reason"]})
    for r in con.execute("SELECT * FROM account_alias_pin ORDER BY feed, ext_key"):
        lines.append({"alias": [r["feed"], r["ext_key"]], "account": r["account_id"]})
    for r in con.execute("SELECT * FROM account_annotation ORDER BY account_id"):
        lines.append({"account_note": r["account_id"], **{k: r[k] for k in ("name", "institution", "closed", "note")
                                                           if r[k] is not None}})
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(d, sort_keys=True) + "\n" for d in lines))
    tmp.replace(path)
    return len(lines)


def import_aliases(con, path: Path) -> int:
    """Account pins from state.jsonl; must run before ingest so observations land right."""
    if not path.exists():
        return 0
    n = 0
    with con:
        for d in (json.loads(l) for l in path.read_text().splitlines() if l.strip()):
            if "account_note" in d:
                set_account(con, d["account_note"], **{k: d.get(k) for k in ("name", "institution", "closed", "note")})
            if "alias" in d:
                con.execute("INSERT OR REPLACE INTO account_alias_pin VALUES (?,?,?,?)", (*d["alias"], d["account"], _now()))
                n += 1
    return n


def set_account(con, account: str, **fields) -> None:
    """Upsert the user's annotation of an account; None leaves a field as it is."""
    con.execute("INSERT OR IGNORE INTO account_annotation(account_id, updated_at) VALUES (?,?)", (account, _now()))
    for k, v in fields.items():
        if v is not None:
            con.execute(f"UPDATE account_annotation SET {k}=?, updated_at=? WHERE account_id=?", (v or None, _now(), account))


def merge_account(con, src: str, dst: str) -> int:
    """Declare account `src` to be `dst`: pin src's feed keys to dst and re-file its evidence.

    Re-filed observations are unlinked so they match afresh in their new account (e.g. an ACH
    seen by Privacy pairs with the bank's own row); their old txns carry over via txn_carry.
    Derived tables are cleared; the caller re-runs link and resolve.
    """
    with con:
        keys = con.execute("SELECT feed, ext_key FROM account_alias WHERE account_id=?", (src,)).fetchall()
        for feed, key in keys:
            con.execute("INSERT OR REPLACE INTO account_alias_pin VALUES (?,?,?,?)", (feed, key, dst, _now()))
        con.execute("UPDATE account_alias SET account_id=? WHERE account_id=?", (dst, src))
        moved = [r[0] for r in con.execute("""SELECT id FROM observation WHERE account_id=?
                                              UNION SELECT obs_id FROM obs_leg WHERE account_id=?""", (src, src))]
        con.execute("UPDATE observation SET account_id=? WHERE account_id=?", (dst, src))
        con.execute("UPDATE obs_leg SET account_id=? WHERE account_id=?", (dst, src))
        con.execute("UPDATE snapshot SET account_id=? WHERE account_id=?", (dst, src))
        con.execute("UPDATE realized_close SET account_id=? WHERE account_id=?", (dst, src))
        con.executemany("INSERT OR IGNORE INTO txn_carry SELECT obs_id, txn_id FROM txn_link WHERE obs_id=?",
                        [(i,) for i in moved])
        con.executemany("DELETE FROM txn_link WHERE obs_id=?", [(i,) for i in moved])
        for t in ("lot_close", "lot", "price", "balance_check", "opening_balance", "posting", "entry", "txn_fact"):
            con.execute(f"DELETE FROM {t}")
        con.execute("UPDATE OR IGNORE account_annotation SET account_id=? WHERE account_id=?", (dst, src))
        con.execute("DELETE FROM account WHERE id=?", (src,))
    return len(moved)


def import_state(con, path: Path) -> Counter:
    stats: Counter = Counter()
    if not path.exists():
        return stats
    lines = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    txns = [d for d in lines if "txn" in d and "pin" not in d]
    with con:
        for d in txns:   # every id first, so merged_into can reference any of them
            con.execute("INSERT OR IGNORE INTO txn(id, minted_from, minted_at) VALUES (?,?,?)",
                        (d["txn"], d["minted_from"], _now()))
        for d in txns:
            if d.get("merged_into"):
                con.execute("UPDATE txn SET merged_into=? WHERE id=?", (d["merged_into"], d["txn"]))
                stats["merged"] += 1
                continue
            for fp in d["fps"]:
                n = con.execute("""INSERT INTO txn_link SELECT o.id, ?, 'state', NULL, ? FROM observation o
                                   WHERE o.fingerprint = ? AND o.id NOT IN (SELECT obs_id FROM txn_link)""",
                                (d["txn"], _now(), fp)).rowcount
                stats["linked"] += n
                stats["missing_fp"] += n == 0
            if a := d.get("annotation"):
                con.execute("""INSERT OR REPLACE INTO txn_annotation(txn_id, category, tags, note, overrides, updated_at)
                               VALUES (?,?,?,?,?,?)""", (d["txn"], a.get("category"), a.get("tags"), a.get("note"),
                                                         a.get("overrides"), a.get("updated_at") or _now()))
                stats["annotations"] += 1
        for d in (d for d in lines if "pin" in d):
            con.execute("INSERT OR REPLACE INTO link_pin VALUES (?,?,?,?)", (d["pin"], d["txn"], d["reason"], _now()))
            stats["pins"] += 1
    return stats
