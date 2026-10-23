#!/usr/bin/env python3
"""Apply the dashboard rationalization to backend/resources/dashboard_scope.yml in place.

Pure logic lives in migration.dashboard_layout; this is the thin edge (read → transform →
write), modeled on backend/.e2e/_fix_dashboard_pages.py. Idempotent: `rationalize` is safe
to re-run — on an already-rationalized file this is a no-op (never raises).
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from migration import dashboard_layout as dl  # noqa: E402

YML = pathlib.Path(__file__).resolve().parents[1] / "resources" / "dashboard_scope.yml"


def main() -> int:
    head, dash = dl.split_serialized(YML.read_text())
    before = len(dash["pages"])
    dash = dl.rationalize(dash)
    problems = dl.check_invariants(dash)
    if problems:
        print("INVARIANT VIOLATIONS:", *problems, sep="\n  ")
        return 1
    YML.write_text(dl.join_serialized(head, dash))
    print(f"pages {before} -> {len(dash['pages'])}; wrote {YML}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
