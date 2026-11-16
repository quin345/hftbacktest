"""Durable raw archive.

Format (same line layout as ``collector/`` in this repo, so the existing
``hftbacktest.data.utils.binancefutures.convert`` reader can consume it):

    <recv_ns> <exact websocket text frame or exact REST body>\n

One gzip file per stream per session:
    public.ndjson.gz   /public/stream frames (depth@100ms + bookTicker)
    market.ndjson.gz   /market/stream frames (aggTrade)
    rest.ndjson.gz     REST depth snapshots (raw body, same layout as collector)

Each stream file is a single gzip member written incrementally and flushed
every second; ``line`` numbers (1-based) are stored in QuestDB rows so any row
can be traced back to its exact raw envelope.
"""

from __future__ import annotations

import gzip
import os
from pathlib import Path
from typing import Iterator


class ArchiveWriter:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._raw = open(path, "ab")  # noqa: SIM115 - lifetime managed by close()
        self._gz = gzip.GzipFile(fileobj=self._raw, mode="ab", compresslevel=6)
        self.lines = 0

    @property
    def name(self) -> str:
        return self.path.name

    def append(self, recv_ns: int, text: str) -> int:
        if "\n" in text or "\r" in text:
            raise ValueError("archive payload must be a single line")
        self._gz.write(f"{recv_ns} {text}\n".encode("utf-8"))
        self.lines += 1
        return self.lines

    def flush(self) -> None:
        self._gz.flush()
        self._raw.flush()
        os.fsync(self._raw.fileno())

    def close(self) -> None:
        self._gz.close()
        self._raw.close()


def read_archive(path: Path) -> Iterator[tuple[int, int, str]]:
    """Yield (line_no, recv_ns, text). Tolerates a truncated final gzip member.

    The final line is yielded only if it is complete (newline terminated).
    """
    with gzip.open(path, "rb") as fh:
        line_no = 0
        try:
            for raw in fh:
                line_no += 1
                if not raw.endswith(b"\n"):
                    break
                text = raw[:-1].decode("utf-8")
                ts_s, _, payload = text.partition(" ")
                yield line_no, int(ts_s), payload
        except EOFError:
            # Process was killed mid-write: keep everything before the damage.
            return
