# FinanceTracker: notes for agents

A personal finance ledger: double-entry bookkeeping over evidence (bank, card, brokerage, payment-app, Amazon and
Monero exports), a web portal over it (dashboard, budget model, transaction editor, review queue), and a classifier
that gives each transaction a kind. It is meant to be open-sourced, so the repo holds code and generic docs only.

## Keep concrete data and local configuration out of the repo
The owner wants to publish this repo. Nothing in it may identify the owner or describe their money or machine.

- **Never commit:** real names (the owner's or anyone else's), account numbers or account ids, balances, amounts and
  dates of real transactions, postal, email or wallet addresses, hostnames, domains, IP addresses (tailnet or LAN)
  and machine-specific ports or paths, merchants, employers, schools or causes tied to the owner, and anything about
  the owner's life, health, home or people. That holds for code, comments, docstrings, docs, tests, fixtures,
  examples, commit messages and PR text.
- **Where those go instead:**
  - `$FINANCE_HOME/config.toml` for settings (ledger/config.py reads it; config.example.toml in the repo lists
    every key with a neutral default).
  - `$FINANCE_HOME/data/` for data and data-like rules: rules.toml (the owner's keyword rules, in place of the
    repo's generic ledger/rules.toml), classify-notes.txt, budget.json, people.json.
  - `$FINANCE_HOME/secrets/` for credentials.
  - `AGENTS.local.md` (untracked) for notes about this machine and the owner's accounts.
- **New code:**
  - A new setting goes in config.py with a localhost or neutral default and in config.example.toml.
  - A new owner-specific rule goes in a file under `$FINANCE_HOME`, not in code; a generic one may go in
    ledger/rules.toml.
  - Code never names an account id: ask the account's kind (account.kind: 'checking', 'brokerage', 'wallet'…).
  - Examples and tests use invented data ("Ann Lee", "ACME MARKET", `acct-123`).
- **Before every commit**, read `git diff --cached` for the above. A quick sweep:
  `git grep -nIE '[0-9]{9,}|@[a-z0-9-]+\.[a-z]{2,}|100\.64\.|192\.168\.|\.ts\.net'`. Then look for names by eye: a
  pattern can't find them.

## Working on it
- Python 3.12 with uv: `uv run ledger <command>` (ledger/cli.py: ingest, status, verify, build, report, classify,
  serve, ...). Lint: `uv run ruff check ledger`.
- State lives outside the repo, in `$FINANCE_HOME` (data/, secrets/, config.toml). Without it, it defaults to the
  repo (data/ and secrets/ are gitignored). ledger/db.py defines ROOT, HOME, DATA and SECRETS; build data paths from
  those, never from `__file__`.
- `ledger verify` must stay REPRODUCIBLE: the database is a function of data/raw and data/state.jsonl. Test risky
  changes on a copy with `--db`.
- Bump a parser's VERSION when its output changes. Schema changes go in a new ledger/migrations/NNNN_*.sql; never
  edit an applied one.
- Keyword rules (categories.py) come from `$FINANCE_HOME/data/rules.toml` if it exists, else ledger/rules.toml.
- Kinds (categories.KINDS) are what the app computes from; tags are facets and never partition money.
  `classify.effective` gives a transaction's kind (the user's, then a rule's, then the model's, then the rule's
  fallback).
- Classifier results live in data/classify.db, not the ledger. `ledger classify` sends the rows the rules can't
  place to Claude (ledger/claude.py, `claude -p`, locked down), which sends their contents to Anthropic. Keep it to
  what the classification needs.
- The portal (ledger/portal.py, ledger/web/) is one page with an in-memory router. Data endpoints are cached
  payloads with ETags, and static files are cached by content hash.
