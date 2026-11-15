"""Read-only QuestDB HTTP helper. Refuses anything that is not a single SELECT."""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

from config import QDB_URL

_FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|truncate|rename|copy|vacuum|reindex|backup|grant|revoke|snapshot)\b", re.I)


def select(query: str, timeout: float = 120.0) -> tuple[list[str], list[list]]:
    q = query.strip().rstrip(";")
    if not q.lower().startswith("select") or ";" in q or _FORBIDDEN.search(q):
        raise ValueError(f"read-only guard rejected query: {q[:80]}")
    url = QDB_URL + "?" + urllib.parse.urlencode({"query": q})
    with urllib.request.urlopen(url, timeout=timeout) as r:
        payload = json.loads(r.read())
    if "error" in payload:
        raise RuntimeError(payload["error"])
    cols = [c["name"] for c in payload["columns"]]
    return cols, payload["dataset"]
