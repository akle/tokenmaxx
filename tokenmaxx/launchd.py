from __future__ import annotations

import os
import plistlib
import subprocess
from dataclasses import dataclass
from pathlib import Path


LABEL = "com.local.tokenmaxx"


@dataclass(frozen=True)
class LaunchdState:
    installed: bool
    loaded: bool | None
    detail: str = ""


def build_launchd_plist(
    program: str,
    claude_bin: str | None,
    codex_bin: str | None,
    queue_path: Path,
    log_path: Path,
    interval_seconds: int,
    sessions_dir: Path | None = None,
    codex_sessions_dir: Path | None = None,
    projects_dir: Path | None = None,
    lock_timeout_seconds: int | None = None,
    path_env: str | None = None,
) -> str:
    arguments = [program, "watch"]
    if claude_bin:
        arguments.extend(["--claude-bin", str(Path(claude_bin).expanduser())])
    if codex_bin:
        arguments.extend(["--codex-bin", str(Path(codex_bin).expanduser())])
    arguments.extend(
        ["--queue", str(Path(queue_path).expanduser()), "--sleep-seconds", str(interval_seconds)]
    )
    if sessions_dir is not None:
        arguments.extend(["--sessions-dir", str(Path(sessions_dir).expanduser())])
    if codex_sessions_dir is not None:
        arguments.extend(["--codex-sessions-dir", str(Path(codex_sessions_dir).expanduser())])
    if projects_dir is not None:
        arguments.extend(["--projects-dir", str(Path(projects_dir).expanduser())])
    if lock_timeout_seconds is not None:
        arguments.extend(["--lock-timeout-seconds", str(lock_timeout_seconds)])

    payload = {
        "Label": LABEL,
        "ProgramArguments": arguments,
        "StartInterval": int(interval_seconds),
        "StandardOutPath": str(Path(log_path).expanduser()),
        "StandardErrorPath": str(Path(log_path).expanduser()),
        "RunAtLoad": False,
    }
    if path_env:
        # launchd starts agents with a bare system PATH; version-manager shims
        # (asdf, mise) exec their manager binary and die without the user PATH.
        payload["EnvironmentVariables"] = {"PATH": path_env}
    return plistlib.dumps(payload, sort_keys=False).decode()


def launchd_service_target(label: str = LABEL) -> str:
    return f"gui/{os.getuid()}/{label}"


def launchd_state(plist_path: Path, label: str = LABEL) -> LaunchdState:
    installed = Path(plist_path).expanduser().exists()
    try:
        result = subprocess.run(
            ["launchctl", "print", launchd_service_target(label)],
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return LaunchdState(installed=installed, loaded=None, detail="launchctl not found")

    if result.returncode == 0:
        return LaunchdState(installed=installed, loaded=True, detail="loaded")
    detail = (result.stderr or result.stdout or "not loaded").strip()
    return LaunchdState(installed=installed, loaded=False, detail=detail)


def launchctl_load(plist_path: Path) -> subprocess.CompletedProcess[str]:
    command = ["launchctl", "load", str(Path(plist_path).expanduser())]
    try:
        return subprocess.run(command, text=True, capture_output=True, check=False)
    except FileNotFoundError:
        return subprocess.CompletedProcess(command, 127, "", "launchctl not found")


def launchctl_unload(plist_path: Path) -> subprocess.CompletedProcess[str]:
    command = ["launchctl", "unload", str(Path(plist_path).expanduser())]
    try:
        return subprocess.run(command, text=True, capture_output=True, check=False)
    except FileNotFoundError:
        return subprocess.CompletedProcess(command, 127, "", "launchctl not found")
