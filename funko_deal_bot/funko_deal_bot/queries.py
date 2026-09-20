from __future__ import annotations

import csv
import io

DEFAULT_QUERIES = ["Funko Pop", "Funko", "Funko Pop!"]


def parse_search_queries(*blobs: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for blob in blobs:
        text = (blob or "").strip()
        if not text:
            continue
        reader = csv.reader(io.StringIO(text), skipinitialspace=True)
        for row in reader:
            for part in row:
                query = part.strip().strip("'")
                if not query:
                    continue
                key = query.casefold()
                if key in seen:
                    continue
                seen.add(key)
                out.append(query)
    return out or list(DEFAULT_QUERIES)


def funko_focused(queries: list[str]) -> list[str]:
    kept = [q for q in queries if "funko" in q.casefold()]
    return kept or list(DEFAULT_QUERIES)
