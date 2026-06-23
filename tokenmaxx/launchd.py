from __future__ import annotations

import plistlib
from pathlib import Path


def build_launchd_plist(program: str, queue_path: Path, log_path: Path, interval_seconds: int) -> str:
    payload = {
        "Label": "com.local.tokenmaxx",
        "ProgramArguments": [
            program,
            "watch",
            "--queue",
            str(Path(queue_path).expanduser()),
            "--sleep-seconds",
            str(interval_seconds),
        ],
        "StartInterval": int(interval_seconds),
        "StandardOutPath": str(Path(log_path).expanduser()),
        "StandardErrorPath": str(Path(log_path).expanduser()),
        "RunAtLoad": False,
    }
    return plistlib.dumps(payload, sort_keys=False).decode()
