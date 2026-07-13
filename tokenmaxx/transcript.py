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
