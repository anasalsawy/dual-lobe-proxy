from __future__ import annotations

import json
import re
from typing import TypeVar, Type
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


def _balanced_objects(text: str):
    for start, ch in enumerate(text):
        if ch != "{":
            continue
        depth = 0; in_string = False; escape = False
        for i in range(start, len(text)):
            c = text[i]
            if in_string:
                if escape: escape = False
                elif c == "\\": escape = True
                elif c == '"': in_string = False
                continue
            if c == '"': in_string = True
            elif c == "{": depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    yield text[start:i+1]
                    break


def extract_json_object(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except Exception:
        pass
    for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, flags=re.I | re.S):
        for candidate in _balanced_objects(m.group(1)):
            try:
                value = json.loads(candidate)
                if isinstance(value, dict): return value
            except Exception:
                pass
    for candidate in _balanced_objects(text):
        try:
            value = json.loads(candidate)
            if isinstance(value, dict): return value
        except Exception:
            continue
    return {}


def parse_model(text: str, model: Type[T], fallback: T) -> T:
    data = extract_json_object(text)
    if not data:
        return fallback
    try:
        return model.model_validate(data)
    except Exception:
        return fallback
