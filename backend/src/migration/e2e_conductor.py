"""Pure logic for the top-level e2e conductor (``backend/.e2e/e2e_all.py``).

The conductor chains the existing per-layer drivers (L0 offline tests → L5 notify) into one run with
a single rollup. It does NOT reimplement any layer: it shells each driver as a subprocess and
aggregates their pass/warn/fail. This module holds the *pure* pieces so they are unit-testable with
no cluster, no network, and no subprocess:

  * :func:`resolve_layers`   — which layers to run given ``--through`` / ``--skip``;
  * :func:`steps_for_layer`  — the exact argv/cwd/hard flags for each layer's driver invocation;
  * :func:`classify_output`  — map a finished step's ``(exit_code, stdout)`` to PASS/WARN/FAIL by
    reusing each driver's own ``WARNINGS (n)`` / ``FAILURES (n)`` rollup lines + exit code;
  * :func:`build_rollup`     — the final one-line-per-step summary + the process exit code
    (non-zero only on a HARD FAIL — the design rule "only HARD rows fail the run").

The thin edge (``e2e_all.py``) runs the steps, calls ``classify_output`` on each, and prints
``build_rollup``. Everything here is a pure function of its inputs.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

# Ordered layers (the run order). Mirrors §2/§5 of 2026-09-19-e2e-everything.md.
LAYER_ORDER: tuple[str, ...] = ("L0", "L1", "L2", "L3", "L4", "L5")

LAYER_TITLES: dict[str, str] = {
    "L0": "offline unit tests (pytest + vitest)",
    "L1": "seed real estate (populate)",
    "L2": "mutating backbone lifecycle",
    "L3": "MCP transport + cross-surface oracle + CUJ read rows",
    "L4": "app UI (Playwright)",
    "L5": "notify / comms surfaces",
}

# The schema L3's ``--cuj`` reads (seeded by L1's ``provision`` stage).
CUJ_SCHEMA = "e2e_real_pathaccess"

# The status precedence for aggregating (worst wins).
_STATUS_RANK = {"PASS": 0, "SKIP": 0, "WARN": 1, "FAIL": 2}


def resolve_layers(through: Optional[str] = None, skip: Optional[Iterable[str]] = None) -> list[str]:
    """The ordered list of layers to run.

    * ``through`` — inclusive cutoff (e.g. ``"L3"`` runs L0..L3), blank = all;
    * ``skip``    — layers to drop (repeatable), e.g. ``["L4"]`` for a headless run.

    Raises ``ValueError`` on an unknown layer id (a typo must fail loud, not silently no-op a layer).
    """
    skip_set = {s.strip().upper() for s in (skip or []) if s and s.strip()}
    through_n = through.strip().upper() if through and through.strip() else None
    unknown = (skip_set | ({through_n} if through_n else set())) - set(LAYER_ORDER)
    if unknown:
        raise ValueError(f"unknown layer(s) {sorted(unknown)}; choose from {list(LAYER_ORDER)}")
    chosen = list(LAYER_ORDER)
    if through_n:
        chosen = list(LAYER_ORDER[: LAYER_ORDER.index(through_n) + 1])
    return [layer for layer in chosen if layer not in skip_set]


def steps_for_layer(layer: str, *, profile: str, confirm_execute: bool) -> list[dict]:
    """The concrete driver invocation(s) for one layer.

    Each step is a dict:
      * ``label``  — human label for the rollup;
      * ``cwd``    — working dir RELATIVE to the repo root (``"."``, ``"backend"``, ``"app"``);
      * ``argv``   — the command as an argv list (no shell);
      * ``hard``   — whether a FAIL fails the whole run (only HARD rows set a non-zero exit);
      * ``abort``  — whether a FAIL aborts the remaining steps (only the L0 gate does).

    ``profile`` is threaded to ``seed_real_estate.py`` (which requires ``--profile``); every driver
    also honours ``DATABRICKS_CONFIG_PROFILE`` from the env, which the edge sets. ``confirm_execute``
    toggles L2 between the non-mutating dry-run (default) and the real mutating lifecycle.
    """
    layer = layer.upper()
    if layer == "L0":
        return [
            {"label": "pytest backend", "cwd": ".",
             "argv": ["python", "-m", "pytest", "backend/src/tests/", "-q"],
             "hard": True, "abort": True},
            # vitest needs node_modules; treat its absence as a soft WARN, not a hard gate.
            {"label": "vitest app", "cwd": "app", "argv": ["npm", "test", "--silent"],
             "hard": False, "abort": False},
        ]
    if layer == "L1":
        return [{"label": "seed --preset populate", "cwd": "backend",
                 "argv": ["python", ".e2e/seed_real_estate.py", "--profile", profile,
                          "--preset", "populate"],
                 "hard": True, "abort": False}]
    if layer == "L2":
        gate = "--confirm-execute" if confirm_execute else "--dry-run"
        mode = "mutating" if confirm_execute else "dry-run"
        return [{"label": f"seed --stage e2e ({mode})", "cwd": "backend",
                 "argv": ["python", ".e2e/seed_real_estate.py", "--profile", profile,
                          "--stage", "e2e", gate],
                 "hard": True, "abort": False}]
    if layer == "L3":
        # --cuj reads the path-access schema: it holds BLOCKED tables, so the blockers row has
        # something to assert (the bronze default has none and would WARN on every run).
        # --guardrails stays opt-in: it calls mutating tools and relies on the server refusing.
        # --pins (focus mode / team priorities) runs READ-ONLY here: no --pin-schema and no
        # --confirm-execute, so it only checks the v2 listing, the confirm gate and pin precedence.
        # Soft: the pin tool exists only on a v2 deploy.
        return [{"label": "e2e_mcp --probe --oracle --cuj", "cwd": "backend",
                 "argv": ["python", ".e2e/e2e_mcp.py", "--probe", "--oracle",
                          "--cuj", "--cuj-schema", CUJ_SCHEMA],
                 "hard": True, "abort": False},
                {"label": "e2e_mcp --pins (read-only)", "cwd": "backend",
                 "argv": ["python", ".e2e/e2e_mcp.py", "--pins", "--profile", profile],
                 "hard": False, "abort": False}]
    if layer == "L4":
        # The non-destructive spec sweep over the populated estate (M6/M7): Overview, Waves gate,
        # W3 badge, Convert-expert, Workstreams, and the P3 render smokes. --a1 (needs the
        # F-WRITER-WAVE re-seed) and --a3 (a destructive control-plane wipe) stay opt-in — the
        # conductor never mutates the shared estate. Runs UNATTENDED via the profile-Bearer auth
        # (D-UI-2 resolved — no captured session needed); DATABRICKS_CONFIG_PROFILE flows from the
        # edge. Kept soft/--skip-able so browser/warehouse/deploy flakiness WARNs, never fails the run.
        # --how (every How it works page with its focus-mode section) is read-only too.
        return [{"label": "e2e_app_ui --c1 --c2 --a2 --c4 --c6 --smoke --how", "cwd": "backend",
                 "argv": ["python", ".e2e/e2e_app_ui.py", "--c1", "--c2", "--a2",
                          "--c4", "--c6", "--smoke", "--how"],
                 "hard": False, "abort": False}]
    if layer == "L5":
        return [{"label": "e2e_notify --all", "cwd": "backend",
                 "argv": ["python", ".e2e/e2e_notify.py", "--all"],
                 "hard": True, "abort": False}]
    raise ValueError(f"unknown layer {layer!r}")


def _count(text: str, keyword: str) -> int:
    """Parse a driver rollup line like ``WARNINGS (3): ...`` → ``3`` (0 when absent)."""
    m = re.search(rf"{keyword} \((\d+)\)", text or "")
    return int(m.group(1)) if m else 0


def classify_output(exit_code: int, stdout: str) -> tuple[str, int, int]:
    """Map a finished step to ``(status, warn_count, fail_count)``.

    Reuses each driver's own rollup: a non-zero exit OR a ``FAILURES (n)`` line ⇒ FAIL; else a
    ``WARNINGS (n)`` line ⇒ WARN; else PASS. ``status`` is one of ``PASS`` / ``WARN`` / ``FAIL``.
    """
    warns = _count(stdout, "WARNINGS")
    fails = _count(stdout, "FAILURES")
    if exit_code != 0 or fails > 0:
        return ("FAIL", warns, max(fails, 1 if exit_code != 0 else 0))
    if warns > 0:
        return ("WARN", warns, 0)
    return ("PASS", 0, 0)


def aggregate_layer_status(step_statuses: Iterable[str]) -> str:
    """Worst-wins status across a layer's steps (FAIL > WARN > PASS/SKIP)."""
    worst = "PASS"
    for s in step_statuses:
        if _STATUS_RANK.get(s, 0) > _STATUS_RANK.get(worst, 0):
            worst = s
    return worst


def exit_code_for(results: Iterable[dict]) -> int:
    """1 iff any HARD step FAILed (soft-layer failures — e.g. L4 — never fail the run)."""
    return 1 if any(r.get("status") == "FAIL" and r.get("hard") for r in results) else 0


def build_rollup(results: list[dict]) -> tuple[str, int]:
    """Render the final rollup and compute the process exit code.

    ``results`` is a list of finished step dicts, each with: ``layer``, ``label``, ``status``,
    ``warns``, ``fails``, ``hard`` (and optional ``detail``). Returns ``(text, exit_code)``.
    """
    lines = ["", "==== e2e-all rollup ===="]
    total_warn = total_fail = 0
    for r in results:
        status = r.get("status", "PASS")
        soft = "" if r.get("hard") else " (soft)"
        badge = status + soft
        if status == "WARN" and r.get("warns"):
            badge = f"WARN({r['warns']}){soft}"
        elif status == "FAIL" and r.get("fails"):
            badge = f"FAIL({r['fails']}){soft}"
        detail = f" — {r['detail']}" if r.get("detail") else ""
        lines.append(f"  {r.get('layer','?'):<3} {r.get('label',''):<34} {badge}{detail}")
        total_warn += int(r.get("warns") or 0)
        total_fail += int(r.get("fails") or 0)
    code = exit_code_for(results)
    hard_fail = sum(1 for r in results if r.get("status") == "FAIL" and r.get("hard"))
    soft_fail = sum(1 for r in results if r.get("status") == "FAIL" and not r.get("hard"))
    verdict = "FAIL" if code else ("WARN" if (total_warn or soft_fail) else "PASS")
    lines.append("  " + "-" * 20)
    lines.append(
        f"  RESULT: {verdict} — {hard_fail} hard failure(s), {soft_fail} soft failure(s), "
        f"{total_warn} warning(s) across {len(results)} step(s)")
    return ("\n".join(lines), code)
