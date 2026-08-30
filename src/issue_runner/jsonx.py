"""Tolerant JSON extraction from LLM replies.

Model output arrives with prose, markdown fences, or <think> blocks around the
JSON payload; local models are the worst offenders. Strip the noise, then scan
for the first balanced JSON object/array that parses.
"""

import json
import re

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class JsonExtractError(ValueError):
    pass


def extract_json(text: str):
    text = _THINK_RE.sub("", text)

    fenced = _FENCE_RE.search(text)
    candidates = [fenced.group(1)] if fenced else []
    candidates.append(text)

    for candidate in candidates:
        candidate = candidate.strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        result = _scan_balanced(candidate)
        if result is not None:
            return result

    raise JsonExtractError(f"no parseable JSON found in reply: {text[:200]!r}")


def _scan_balanced(text: str):
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch not in "{[":
            continue
        try:
            obj, _ = decoder.raw_decode(text[i:])
            return obj
        except json.JSONDecodeError:
            continue
    return None
