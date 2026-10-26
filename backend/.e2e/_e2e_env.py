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


def deployment() -> str:
    """The ``[<target>] <bundle.name>`` segment of this target's job names (see install.py)."""
    return f"[{optional('E2E_BUNDLE_TARGET') or 'e2e'}] managed-table-migration"


def app_name_wanted() -> str:
    """The deployed app to drive: ``E2E_APP_NAME`` (exact, for a second isolated deployment), else
    the bundle name, which matches the default ``mcp-<bundle.name>`` by unique suffix."""
    return optional("E2E_APP_NAME") or "managed-table-migration"
