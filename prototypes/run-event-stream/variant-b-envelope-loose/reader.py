"""Reader for variant B. Identical surface to variant A's reader; the only
parser variation is what we pass in.

We make a deliberate distinction:

- A `CorruptLine` is *envelope* corruption: JSON parse failure or an envelope
  that violates the on-disk contract (missing event_id, etc.).
- A *payload* schema violation for a known kind is also a `CorruptLine` —
  the kind says "this is a verb_completed and here is what verb_completed
  means"; if the payload doesn't match, that's drift, not forward-compat.
- An unknown kind is NOT corruption — the envelope decoded cleanly. The
  TypedEvent is returned with `known=False` and the raw payload preserved
  on `envelope.payload`.
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
                        yield CorruptLine(line_no, offset, buf, "unterminated_line_at_eof")
                    return
                time.sleep(poll_interval)
                continue
            buf += chunk
            if not buf.endswith("\n"):
                continue
            line_no += 1
            yield from _parse_line(buf, parser, line_no, offset)
            offset += len(buf.encode("utf-8"))
            buf = ""


def _parse_line(raw: str, parser, line_no: int, offset: int) -> Iterator[Any]:
    text = raw.rstrip("\n")
    if not text:
        return
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        yield CorruptLine(line_no, offset, text, f"json_decode: {exc.msg}")
        return
    try:
        yield parser(obj)
    except Exception as exc:  # noqa: BLE001
        yield CorruptLine(line_no, offset, text, f"schema: {exc.__class__.__name__}: {exc}")
