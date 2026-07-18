from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path


def tail_records(path: Path, max_lines: int = 80) -> list[dict]:
    if max_lines <= 0:
        return []
    chunks: list[bytes] = []
    newline_count = 0
    try:
        with Path(path).open("rb") as handle:
            handle.seek(0, 2)
            position = handle.tell()
            while position > 0 and newline_count <= max_lines:
                size = min(8192, position)
                position -= size
                handle.seek(position)
                chunk = handle.read(size)
                chunks.append(chunk)
                newline_count += chunk.count(b"\n")
    except OSError:
        return []
    lines = b"".join(reversed(chunks)).decode(errors="replace").splitlines()[-max_lines:]
    records: list[dict] = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def record_timestamp(record: dict) -> int:
    raw = record.get("timestamp")
    if isinstance(raw, str):
        try:
            return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    return 0


def record_timestamp_ns(record: dict) -> int:
    """Return an ISO record timestamp as epoch nanoseconds for ordering."""
    raw = record.get("timestamp")
    if not isinstance(raw, str):
        return 0
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        whole_seconds = int(parsed.replace(microsecond=0).timestamp())
    except ValueError:
        return 0

    fraction_digits = ""
    if "." in raw:
        for character in raw.split(".", 1)[1]:
            if not character.isdigit():
                break
            fraction_digits += character
    fraction_ns = int((fraction_digits + "000000000")[:9]) if fraction_digits else 0
    return whole_seconds * 1_000_000_000 + fraction_ns
