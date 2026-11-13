"""This machine's settings: $FINANCE_HOME/config.toml (never in the repo; config.example.toml lists the keys).

An environment variable, where one is named in ENV, overrides the file; the defaults suit one machine on localhost.
"""
import os
import tomllib

from .db import HOME

PATH = HOME / "config.toml"
DEFAULTS = {
    "portal": {"host": "127.0.0.1", "port": 5191},
    # the dither-accounts manager: url is its API (this machine), page what a browser opens (defaults to url)
    "accounts": {"url": "http://127.0.0.1:8486", "page": ""},
    "jev": {"url": "http://127.0.0.1:7862"},
    "claude": {"model": "sonnet", "about": ""},
    "dashboard": {"full_from": ""},
    "ledger": {"timezone": "UTC"},
}
ENV = {("accounts", "url"): "DITHER_ACCOUNTS_URL", ("accounts", "page"): "DITHER_ACCOUNTS_PAGE",
       ("jev", "url"): "JEV_URL", ("claude", "model"): "FINANCE_CLAUDE_MODEL"}


def _file() -> dict:
    try:
        return tomllib.loads(PATH.read_text())
    except FileNotFoundError:
        return {}


def get(section: str, key: str):
    if (var := ENV.get((section, key))) and os.environ.get(var):
        return os.environ[var]
    return _file().get(section, {}).get(key, DEFAULTS[section][key])
