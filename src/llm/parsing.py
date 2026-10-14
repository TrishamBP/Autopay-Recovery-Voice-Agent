"""Parsing helpers for model output (shared by the SQL generator and the voice agent).

Reasoning models (e.g. GLM on Fireworks) may emit `<think>...</think>` blocks and code fences;
both are stripped before parsing. Model output is untrusted: callers validate the result.
"""

import json
import re

from src.llm.client import LLMMalformedResponseError

_FENCE = re.compile(r"^```(?:json|sql)?\s*|\s*```$", re.IGNORECASE)
_THINK = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)


def strip_reasoning(content: str) -> str:
    return _THINK.sub("", content).strip()


def parse_json_object(content: str) -> dict[str, object]:
    text = _FENCE.sub("", strip_reasoning(content)).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise LLMMalformedResponseError("model output did not contain a JSON object")
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMMalformedResponseError("model output was not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise LLMMalformedResponseError("model output was not a JSON object")
    return parsed
