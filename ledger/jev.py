"""Client for jev, a local decision-model service (config [jev] url).

jev answers questions about text with probabilities: Qwen3-Reranker-4B, a model trained to answer
only "yes" or "no", read as P(yes). `choices` judges each option as its own yes/no and returns
one distribution including NONE (every option a no). jev caches every answer itself, so asking
again is free.
"""
import json
import urllib.request

from . import config

URL = config.get("jev", "url")
NONE = "(none of these)"


def _post(path: str, body: dict) -> dict:
    req = urllib.request.Request(URL + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=1800).read())


def model() -> str:
    return json.loads(urllib.request.urlopen(URL + "/v1/health", timeout=5).read())["model"]


def choices(text: str, question: str, options: list[str], instruct: str | None = None) -> dict[str, float]:
    """{label: p, ..., NONE: p}; options are 'Label: description'."""
    r = _post("/v1/choose", {"texts": [text], "choices": options, "question": question,
                             **({"instruct": instruct} if instruct else {})})
    return r["results"][0]["probs"]


def choices_raw(text: str, question: str, options: list[str], instruct: str | None = None) -> dict[str, float]:
    """{label: P(yes)} for each option on its own (no normalization, no NONE)."""
    r = _post("/v1/choose", {"texts": [text], "choices": options, "question": question,
                             **({"instruct": instruct} if instruct else {})})
    return r["results"][0]["yes"]


def yes(text: str, question: str, instruct: str | None = None) -> float:
    return _post("/v1/judge", {"texts": [text], "question": question, **({"instruct": instruct} if instruct else {})})["p"][0]


def up() -> bool:
    try:
        return json.loads(urllib.request.urlopen(URL + "/v1/health", timeout=5).read()).get("ok", False)
    except OSError:
        return False
