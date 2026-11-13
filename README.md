# dither-finance

A personal finance ledger that you run yourself. It reads the files your accounts export, builds a double-entry
ledger from them, and serves a small private web portal over it: a dashboard of spending and income, a budget model,
a transaction editor and a review queue. Everything stays on your machine, except the transactions the classifier
sends to Claude (see below).

## What it does
- **Evidence in, ledger out.** Each export is kept byte for byte under `data/raw/` and parsed into observations. The
  observations are matched into transactions that may span accounts: a card purchase, the bank's payment of it, a
  transfer seen from both sides. Each transaction is then resolved into postings. The database is a function of
  `data/raw/` plus your annotations (`data/state.jsonl`), and `ledger verify` rebuilds it from scratch to prove it.
- **Checks.** Every balance a source reports is a balance assertion. Investment lots are checked against the cost
  basis and realized gains the brokerage reports.
- **Kinds.** Every transaction gets one kind: Essential, Discretionary, Giving, Taxes, Investing, Own transfer,
  Fronted for others, and for money in Earned, Steady income, Investment returns, Windfall. Kinds come from your
  labels first, then keyword rules, then a classifier for what the rules can't place. Tags are free-form facets on
  top.
- **Who it was for.** Payments from people (Venmo) are linked to the purchases they repay, so money you fronted for
  others isn't counted as your spending. Durable purchases can be spread over their life.
- **Budget model.** A lifetime consumption plan (CRRA utility over career scenarios and uncertain income streams,
  with Monte Carlo bands) suggests a sustainable spending rate.

Supported sources: SimpleFIN (banks and brokerages), Schwab's History and Realized Gain/Loss exports (CSV, JSON or
printed PDF), Privacy.com cards, Venmo statement CSVs, Amazon order history, a Monero view-only wallet, and Cake
Wallet's history CSV. Coin prices come from Kraken's public API.

## Getting started
Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
export FINANCE_HOME=~/.local/share/finance-tracker   # where data/, secrets/ and config.toml live (default: the repo)
mkdir -p "$FINANCE_HOME/data/raw" && chmod 700 "$FINANCE_HOME/data"
cp config.example.toml "$FINANCE_HOME/config.toml"      # optional: every setting has a default
cp ledger/rules.toml "$FINANCE_HOME/data/rules.toml"    # optional: your own keyword rules

# put exports under $FINANCE_HOME/data/raw/<source>/, then:
uv run ledger ingest              # parse, match, resolve, check
uv run ledger status              # coverage and match stats per account
uv run ledger portal-password     # set the portal's password
uv run ledger serve               # the portal, on 127.0.0.1:5191 by default
```

To try it on invented data first, give it the sample: six months of a made-up checking account and credit card.

```sh
export FINANCE_HOME=$(mktemp -d) && mkdir -p "$FINANCE_HOME/data/raw/simplefin"
cp examples/simplefin-sample.json "$FINANCE_HOME/data/raw/simplefin/"
uv run ledger ingest && uv run ledger serve --dev     # --dev: no sign-in, loopback only
```

`uv run ledger --help` lists every command: `verify`, `report`, `account`, `merge-account`, `dashboard` (a
self-contained HTML page), `classify`, `prices`, `sql` and more.

## Configuration
- `$FINANCE_HOME/config.toml`: the portal's address, your timezone, the classifier's model and a sentence about you,
  and the optional services below. `config.example.toml` lists every key.
- `$FINANCE_HOME/data/rules.toml`: keyword rules (regexes over the description: category, merchant, tags). When it
  is missing, the generic starter set in `ledger/rules.toml` is used.
- `$FINANCE_HOME/data/classify-notes.txt`: facts for the classifier, one per line ("ACME FUND is a charity").
- `$FINANCE_HOME/data/people.json`: your household members and your own name aliases, for splitting shared purchases.

## The classifier and your privacy
`ledger classify` asks Claude, through Claude Code's `claude -p` with no tools, no settings and nothing saved, for the
kind of each transaction the rules can't place. Those transactions' details (description, amount, date, notes,
Amazon item titles) and your latest labels as examples **are sent to Anthropic**. Rule-placed transactions are never
sent. Without Claude Code installed, nothing is sent and those transactions stay on their rule's fallback kind for you
to label.

## Optional services
- **dither-accounts**, an accounts manager that signs in to each account and pulls its exports on a schedule.
  `ledger pull` fetches what it pulled. Without it, drop exports into `data/raw/` yourself and run `ledger ingest`.
- **jev**, a local yes/no model service used for repayment and durable-goods suggestions. Without it, those
  suggestions are skipped.
- The portal's "Pull now" buttons start `finance-pull@SOURCE` systemd user units.

## Development
See [AGENTS.md](AGENTS.md) for how the code is laid out and the rules for contributing: above all, no personal data
in the repo. Lint with `uv run ruff check ledger`.

## License
MIT. See [LICENSE](LICENSE).
