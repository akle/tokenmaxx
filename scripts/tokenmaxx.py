#!/usr/bin/env python3
"""Compatibility wrapper for the installable tokenmaxx package."""

from tokenmaxx.cli import *  # noqa: F401,F403
from tokenmaxx.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
