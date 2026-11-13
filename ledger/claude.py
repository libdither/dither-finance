"""Asks Claude (Claude Code's `claude -p`, through whatever API Claude Code is set up to use) one question with a JSON
answer.

Locked down: no tools, no MCP servers, no settings or hooks (so no CLAUDE.md or plugins), nothing saved to
~/.claude (--no-session-persistence), run from an empty private directory with a minimal environment, the way the
systemd units run it. What a prompt holds leaves the machine for Anthropic's API: callers decide what goes in.
"""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import config

MODEL = config.get("claude", "model")   # an alias ("sonnet": the latest Sonnet) or a full model id


def _bin() -> str:
    return shutil.which("claude") or str(Path.home() / ".local/bin/claude")


def available() -> bool:
    return Path(_bin()).exists() and (bool(_proxy()) or (Path.home() / ".claude/.credentials.json").exists())


PROXY_VARS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN")


def _proxy() -> dict:
    """Where Claude Code sends requests, as set in ~/.claude/settings.json's env (e.g. a local CLIProxyAPI hub of
    pooled subscriptions). Settings aren't loaded for these calls (they carry hooks), so these two are passed
    on by hand; without them claude signs in as the one account in ~/.claude/.credentials.json."""
    try:
        env = json.loads((Path.home() / ".claude/settings.json").read_text()).get("env", {})
    except (OSError, ValueError):
        env = {}
    return {k: str(env.get(k) or os.environ.get(k)) for k in PROXY_VARS if env.get(k) or os.environ.get(k)}


def ask(system: str, prompt: str, schema: dict, model: str = MODEL, timeout: int = 600) -> dict:
    """The structured answer (matching schema); raises RuntimeError on any failure."""
    env = {k: os.environ[k] for k in ("HOME", "USER", "LANG", "TZ", "XDG_RUNTIME_DIR") if k in os.environ}
    env["PATH"] = f"{Path(_bin()).parent}:/run/current-system/sw/bin:/usr/bin:/bin"
    env |= _proxy()
    with tempfile.TemporaryDirectory(prefix="ledger-claude-") as cwd:
        try:
            p = subprocess.run(
                [_bin(), "-p", "--model", model, "--tools", "", "--strict-mcp-config", "--setting-sources", "",
                 "--no-session-persistence", "--system-prompt", system, "--output-format", "json",
                 "--json-schema", json.dumps(schema)],
                input=prompt, capture_output=True, text=True, cwd=cwd, env=env, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise RuntimeError(f"claude: {e}") from e
    try:
        out = json.loads(p.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude exited {p.returncode}: {(p.stderr or p.stdout)[-300:]}") from None
    if out.get("is_error") or not isinstance(out.get("structured_output"), dict):
        raise RuntimeError(f"claude: {out.get('subtype')} {str(out.get('result'))[:300]}")
    return out["structured_output"]
