"""Workspace settings for the live e2e drivers, read from the environment.

Nothing here has a default: the drivers act on a real workspace, so the profile, warehouse,
catalog and external base must be named explicitly (see .env.e2e.example at the repo root).
"""
import os


def require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is not set. Export it (see .env.e2e.example) before running this driver.")
    return value


def optional(name: str) -> str:
    return os.environ.get(name, "").strip()
