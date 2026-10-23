#!/usr/bin/env python3
"""One-off: merge two action/freeze pages into siblings so the dashboard is <=16 pages.

Lakeview caps a dashboard at 16 pages; the action/freeze slices pushed it to 18. This moves
widgets (never deletes them, so every dataset stays referenced) and drops the emptied pages:

  runbook                 -> merged into action_queue   (both "action" oriented)
  compaction_sources_page -> merged into freeze_ledger  ("Freeze & actions")

Rewrites only the serialized_dashboard block scalar in resources/dashboard_scope.yml,
preserving the header/comments and the 8-space indentation of the block.
"""
from __future__ import annotations

import json
import sys

YML = "resources/dashboard_scope.yml"
MERGES = [("runbook", "action_queue"), ("compaction_sources_page", "freeze_ledger")]
INDENT = " " * 8


def page_max_y(page: dict) -> int:
    ys = [w.get("position", {}).get("y", 0) + w.get("position", {}).get("height", 0)
          for w in page.get("layout", [])]
    return max(ys) if ys else 0


def widget_names(page: dict) -> set[str]:
    out = set()
    for w in page.get("layout", []):
        nm = (w.get("widget") or {}).get("name")
        if nm:
            out.add(nm)
    return out


def main() -> int:
    raw = open(YML).read()
    marker = "serialized_dashboard: |\n"
    idx = raw.index(marker)
    head = raw[: idx + len(marker)]
    body = raw[idx + len(marker):]

    # De-indent the block scalar back to raw JSON.
    json_lines = [ln[len(INDENT):] if ln.startswith(INDENT) else ln
                  for ln in body.splitlines()]
    dash = json.loads("\n".join(json_lines))

    pages = {p["name"]: p for p in dash["pages"]}
    for src, dst in MERGES:
        if src not in pages or dst not in pages:
            print(f"skip {src}->{dst} (missing)")
            continue
        s, d = pages[src], pages[dst]
        collide = widget_names(s) & widget_names(d)
        if collide:
            print(f"ABORT: widget-name collision merging {src}->{dst}: {sorted(collide)}")
            return 1
        shift = page_max_y(d)
        for w in s.get("layout", []):
            w.setdefault("position", {})["y"] = w["position"].get("y", 0) + shift
            d.setdefault("layout", []).append(w)
        print(f"merged {src} ({len(s.get('layout', []))} widgets) into {dst} (y+{shift})")

    keep = {src for src, _ in MERGES}
    dash["pages"] = [p for p in dash["pages"] if p["name"] not in keep]
    print(f"pages now: {len(dash['pages'])}")

    # Re-emit as an 8-space-indented block scalar (matches the original style).
    pretty = json.dumps(dash, indent=2, ensure_ascii=False)
    reindented = "\n".join(INDENT + ln for ln in pretty.splitlines()) + "\n"
    open(YML, "w").write(head + reindented)
    print(f"wrote {YML}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
