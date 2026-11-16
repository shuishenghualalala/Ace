"""Small parsing helpers shared by model-facing control-plane code."""

from __future__ import annotations

import json
import re
from typing import Any


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Extract the first decodable JSON object from a model response.

    The decoder, rather than a brace-matching regular expression, handles
    nested objects and braces inside JSON strings.  ``None`` means the input
    does not contain a JSON object; schema validation remains the caller's job.
    """

    source = str(text or "").strip()
    if not source:
        return None

    candidates = []
    fenced = _JSON_FENCE_RE.search(source)
    if fenced:
        candidates.append(fenced.group(1).strip())
    candidates.append(source)

    decoder = json.JSONDecoder()
    for candidate in candidates:
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except (TypeError, ValueError):
            value = None
        if isinstance(value, dict):
            return value

        start = candidate.find("{")
        while start >= 0:
            try:
                value, _ = decoder.raw_decode(candidate[start:])
            except (TypeError, ValueError):
                start = candidate.find("{", start + 1)
                continue
            if isinstance(value, dict):
                return value
            start = candidate.find("{", start + 1)
    return None
