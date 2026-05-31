"""Tail reader. Yields parsed events as they arrive.

Corrupt lines (truncated JSON, schema violation that is NOT a forward-compat
discriminator miss) surface as `CorruptLine` records. The caller MUST handle
them; INV-NO-CORRUPT-FORWARD forbids silent skipping.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator


@dataclass(frozen=True)
class CorruptLine:
    line_no: int
    byte_offset: int
    raw: str
    error: str


def read_all(path: Path, parser: Callable[[dict[str, Any]], Any]) -> Iterator[Any]:
    """Read the whole file once. Yields parsed events or CorruptLine markers."""
    with open(path, "r", encoding="utf-8") as fh:
        offset = 0
        for line_no, raw in enumerate(fh, start=1):
            yield from _parse_line(raw, parser, line_no, offset)
            offset += len(raw.encode("utf-8"))


def tail(
    path: Path,
    parser: Callable[[dict[str, Any]], Any],
    *,
    stop: Callable[[], bool] = lambda: False,
    poll_interval: float = 0.02,
) -> Iterator[Any]:
    """Yield events as they appear. Stops when `stop()` returns True AND no
    more bytes are available. Recovers a torn final line by buffering until \\n.
    """
    with open(path, "r", encoding="utf-8") as fh:
        line_no = 0
        offset = 0
        buf = ""
        while True:
            chunk = fh.readline()
            if chunk == "":
                if stop():
                    if buf:
                        line_no += 1
                        yield CorruptLine(
                            line_no=line_no,
                            byte_offset=offset,
                            raw=buf,
                            error="unterminated_line_at_eof",
                        )
                    return
                time.sleep(poll_interval)
                continue
            buf += chunk
            if not buf.endswith("\n"):
                # Torn write; wait for the rest.
                continue
            line_no += 1
            yield from _parse_line(buf, parser, line_no, offset)
            offset += len(buf.encode("utf-8"))
            buf = ""


def _parse_line(
    raw: str,
    parser: Callable[[dict[str, Any]], Any],
    line_no: int,
    offset: int,
) -> Iterator[Any]:
    text = raw.rstrip("\n")
    if not text:
        return
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        yield CorruptLine(
            line_no=line_no,
            byte_offset=offset,
            raw=text,
            error=f"json_decode: {exc.msg}",
        )
        return
    try:
        yield parser(obj)
    except Exception as exc:  # noqa: BLE001 — schema violation surfaces here.
        yield CorruptLine(
            line_no=line_no,
            byte_offset=offset,
            raw=text,
            error=f"schema: {exc.__class__.__name__}: {exc}",
        )
