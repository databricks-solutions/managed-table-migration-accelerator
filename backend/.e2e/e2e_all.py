#!/usr/bin/env python3
"""Top-level e2e conductor — chain L0→L5 into one run with a single rollup.

Runs each layer as its EXISTING driver (no reimplementation) and aggregates their pass/warn/fail:

  L0  pytest backend/src/tests + vitest (app)        offline, HARD gate — abort on red
  L1  seed_real_estate.py --preset populate          seed the one estate every layer targets
  L2  seed_real_estate.py --stage e2e [--dry-run|--confirm-execute]   the mutating backbone
  L3  e2e_mcp.py --probe --oracle --cuj               MCP transport + cross-surface oracle + CUJ reads
  L4  e2e_app_ui.py --c1                              app UI render (Playwright) — soft, --skip-able
  L5  e2e_notify.py --all                             notify/comms surfaces (digest/comms/runbook)

The pure planning + rollup logic lives in ``migration.e2e_conductor`` (unit-tested, no subprocess);
this file is the thin edge that spawns the drivers and streams their output.

Design rules (2026-09-19-e2e-everything.md §5): idempotent + resumable; degrade honestly (a layer
that can't run is a WARN, never a false FAIL); gated mutation (L2 mutates ONLY under
``--confirm-execute`` — the default is a non-mutating dry-run); ``--through`` stops early; one estate;
non-zero exit ONLY on a HARD failure.

Usage (never auto-selects a profile — always pass one):
  python .e2e/e2e_all.py --profile <profile>                 # read-mostly (L2 dry-run)
  python .e2e/e2e_all.py --profile <p> --through L3                              # L0..L3 only
  python .e2e/e2e_all.py --profile <p> --skip L4                                 # headless (no UI auth)
  python .e2e/e2e_all.py --profile <p> --confirm-execute                        # full mutating backbone
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from migration import e2e_conductor as ec  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _run_step(step: dict, env: dict) -> dict:
    """Spawn one driver, stream its output live while capturing it, and classify the result."""
    cwd = os.path.join(REPO_ROOT, step["cwd"])
    print(f"\n$ ({step['cwd']}) {' '.join(step['argv'])}", flush=True)
    captured: list[str] = []
    try:
        proc = subprocess.Popen(
            step["argv"], cwd=cwd, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1)
    except FileNotFoundError as e:  # e.g. npm not installed → soft WARN for vitest
        detail = f"command not found ({e})"
        print(f"  [skip] {detail}")
        return {"status": "WARN", "warns": 1, "fails": 0, "detail": detail}
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        captured.append(line)
    code = proc.wait()
    status, warns, fails = ec.classify_output(code, "".join(captured))
    return {"status": status, "warns": warns, "fails": fails,
            "detail": f"exit {code}" if status == "FAIL" else ""}


def main() -> int:
    ap = argparse.ArgumentParser(description="e2e conductor — chain L0→L5 with one rollup.")
    ap.add_argument("--profile", required=True,
                    help="Databricks CLI profile (required — never auto-selected).")
    ap.add_argument("--through", default=None, help="Inclusive cutoff layer (e.g. L3).")
    ap.add_argument("--skip", action="append", default=[], help="Layer to skip (repeatable).")
    ap.add_argument("--confirm-execute", action="store_true",
                    help="Run L2 as the real mutating lifecycle (default: non-mutating dry-run).")
    args = ap.parse_args()

    try:
        layers = ec.resolve_layers(through=args.through, skip=args.skip)
    except ValueError as e:
        print(f"error: {e}")
        return 2

    env = dict(os.environ)
    env["DATABRICKS_CONFIG_PROFILE"] = args.profile

    mode = "MUTATING (--confirm-execute)" if args.confirm_execute else "read-mostly (L2 dry-run)"
    print(f"e2e-all — profile={args.profile}  layers={layers}  mode={mode}")

    results: list[dict] = []
    aborted = False
    for layer in layers:
        if aborted:
            break
        print(f"\n===== {layer}: {ec.LAYER_TITLES.get(layer, layer)} =====")
        for step in ec.steps_for_layer(layer, profile=args.profile,
                                       confirm_execute=args.confirm_execute):
            outcome = _run_step(step, env)
            results.append({
                "layer": layer, "label": step["label"], "hard": step["hard"],
                **outcome,
            })
            if step.get("abort") and outcome["status"] == "FAIL":
                print(f"\n!! {layer} {step['label']} FAILED (hard gate) — aborting the run.")
                aborted = True
                break

    text, code = ec.build_rollup(results)
    if aborted:
        text += "\n  (aborted early on the L0 hard gate — later layers not run)"
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
