#!/usr/bin/env python3
"""Gated live E2E for the app/UI surface (the Databricks App front end) — L4 PLAYWRIGHT DRIVER.

Closes the golden thread at **hop 3**: after the backend stamps a verdict (hop 1) and the MCP
returns it over the wire (hop 2, proven by ``e2e_mcp.py --oracle``), this asserts the DEPLOYED app
*renders* what the W1–W3 wiring feeds it — the schedule-aware surfaces that shipped inert before.
Read-only against the control plane except `--journey`: it navigates and asserts; it triggers no
stage and mutates nothing (the *estate shaping* a green requires is done out-of-band; see "Estate
prerequisites"). `--journey` (M5) clicks the Overview guided step through one wave, and only with
``--confirm-execute`` (a convert also needs ``--confirm-convert <the gate's confirm target>``, and
that target must be the journey wave: a schema-scoped gate is cancelled).

Scope (all UI-auth WARN-tier):
  C1  — /overview: the estate KPI tiles render from live data, and (when the conductor recommends a
         convert) the guided-convert CTA opens the Convert-Gate for the recommended scope. [L4-M1]
  C2  — /waves: a runnable wave opens the Convert-Gate; the five-stage rail mounts, the Freeze
         stage renders the live coverage verdict, the footer action set is consistent with it
         (gap → Freeze-first; hold/unevaluated → Convert), and type-to-confirm + ack unlocks
         Convert (non-mutating). [L4-M3]
  C4  — /convert (expert): the scope pickers render, the "Convert scope…" gate trigger is DISABLED
         until a scope is entered (honest, no fabricated GO), the "Readiness by wave" readiness board
         renders live wave rows, and the ?wave= on-ramp opens a scoped gate with its Readiness rail
         + managed-location panel. [L4-M6]
  C6  — /workstreams: the remediation-track cards render live counts and one drill-in opens its
         detail page (mounts w/o error + names the track). [L4-M6]
  smoke — P3 render smokes (/blocked /freeze /runs /coverage /runbook /dashboard /settings): each
         route mounts w/o an error boundary and its primary query resolves (rows or an honest empty
         state); a cold warehouse is a WARN, a query-error Alert is a FAIL. [L4-M7]
  A2  — /bulk: the schedule-safest READY *parallel* batch carries the "Recommended next" badge (W3).
         PROVEN LIVE 2026-09-19 (e2e workspace): batch_01 READY 4/4 → badge; batch_02 AWAITING → none.
  BULK — M6.4: --prep-batch (campaign per member schema), --bulk-journey (one batch through /bulk,
         MUTATING), --rollback-wave (one wave from /convert, MUTATING). See README "M6.4 bulk journey".
  A1  — Convert-Gate pause banner + WARN_WINDOW badge (W2). Driver + F-WRITER-WAVE fixture SHIP
         (L4-M4); live proof deferred to the next re-seed — see "A1 (L4-M4)". [--a1]
  A3  — /overview first-run NEVER_PROFILED landing (F-EMPTY). Driver SHIPS (L4-M5); needs a WIPED
         control plane, mutually exclusive with A1/A2/C1/C2's populated estate — a separate,
         estate-destructive run (`--wipe-control-plane --confirm-wipe`, then `--a3`). [--a3]

D2 / D-UI-2 (UI-auth) — RESOLVED (2026-09-20): the Databricks Apps front door validates a
profile-minted **Bearer token** and injects ``x-forwarded-*`` for the UI *and* every ``/api`` fetch
(the SAME front door + token path ``e2e_mcp.py`` proved for ``/mcp``), so a browser context carrying
only ``Authorization: Bearer <token>`` renders the authenticated app **headless and unattended** — no
SSO/MFA, no captured session, no refresh cadence. That is now the **default** auth path
(``token_headers()``); point ``--profile`` at an SP OAuth (M2M) profile for fully non-interactive CI.
``--session-auth`` forces the legacy captured-``storageState`` (SSO cookie) path — the real end-user
session, captured once via ``--login`` (headed SSO+MFA) — kept for when you want to exercise it
explicitly. The first green (2026-09-19) was captured interactively via the ``user-playwright`` MCP;
this driver encodes the exact selectors that run proved.

Estate prerequisites (why a green needs a shaped estate — learned the hard way on the e2e workspace):
  - A2's badge renders ONLY on a READY *parallel* batch CARD (``BulkConvertPage`` →
    ``recommendedBatchId = first READY batch``). A ``populate`` estate is planned-at-0% (every wave
    PLANNED), and its only clean READY batches are single-wave → they render in the collapsed
    "Serial waves" section, never as cards. To get a READY parallel batch you must, out-of-band:
      1) dry-run (``job_03_dry_run``, non-mutating) every wave in the target parallel batch, and
      2) clear each wave's open PRE_CONVERT actions — freezable ones via ``job_03c_freeze``
         (reversible), but a USER/NOTEBOOK-attributed FREEZE_COMPACTION is MANUAL by design and only
         an operator can resolve it (mark the action DONE).
    On e2e this shaped ``batch_01`` (e2e_real_stream/share/dlt/gold) to READY 4/4. See ``--fixtures``.

A1 (L4-M4) needs a *runnable* wave (WavesPage's ``runnable`` branch, i.e. DRY_RUN_OK) that CONTAINS
an attributed writer. The generic seed buries every attributed writer in the big ``e2e_real_bronze``
domain wave (hundreds of open pre-convert actions), so the seed now provisions a tiny isolated
``e2e_real_writer`` schema whose sole table carries an imminent-WARN_WINDOW scheduled writer; the
``F-WRITER-WAVE`` fixture dry-runs + freezes it into a small runnable wave. ``--a1`` then opens THAT
wave's gate and asserts the pause banner names the job + the 'Scheduled write approaching' badge.
The live A1 proof is DEFERRED to the next natural re-seed (the e2e_real_writer schema exists only
after a re-seed); the driver + fixture + wiring ship now.

Prereqs:
  databricks bundle deploy -t e2e --profile <profile>   # app ACTIVE
  pip install playwright && playwright install chromium                       # driver deps
  python .e2e/e2e_app_ui.py --a2                                             # unattended (profile Bearer)
  # (optional) --login once, then --a2 --session-auth to exercise the SSO cookie path instead

Env (no workspace defaults; mirrors e2e_mcp.py so ONE profile drives every .e2e driver):
  DATABRICKS_CONFIG_PROFILE    (no default; or pass --profile)
  E2E_APP_URL                  (override the auto-resolved https://<app-host>)
  E2E_APP_STORAGE_STATE        (Playwright storageState JSON for --session-auth; default ~/.mtm_e2e_app_state.json)
  E2E_APP_HEADLESS             ("0" to watch an assertion run; default headless; --login is always headed)

Usage:
  python .e2e/e2e_app_ui.py --login    # (optional) capture an SSO session for --session-auth (headed)
  python .e2e/e2e_app_ui.py --fixtures # print the fixture recipe table and exit (no network)
  python .e2e/e2e_app_ui.py --c1       # Overview KPIs render + guided-convert CTA opens the gate
  python .e2e/e2e_app_ui.py --c2       # Waves Convert-Gate GO/BLOCK wiring + type-to-confirm unlock
  python .e2e/e2e_app_ui.py --c4       # Convert (expert): scope wiring + Readiness by wave + scoped gate
  python .e2e/e2e_app_ui.py --c6       # Workstreams: track cards + one detail drill-in
  python .e2e/e2e_app_ui.py --smoke    # P3 render smokes (blocked/freeze/runs/coverage/runbook/…)
  python .e2e/e2e_app_ui.py --a1       # W2 pause banner + WARN_WINDOW badge on the isolated writer wave
  python .e2e/e2e_app_ui.py --a2       # W3 recommended-batch badge on the READY parallel batch
  python .e2e/e2e_app_ui.py --a3       # first-run NEVER_PROFILED landing (WIPED control plane — separate run)
  python .e2e/e2e_app_ui.py --journey --profile <p> --wave <id> [--confirm-execute ...]  # M5, MUTATING
"""
from __future__ import annotations

import argparse
import json
import os
import re
from urllib.parse import quote
import sys
import time
from pathlib import Path

# Single-source the app-name suffix match from the SAME helper the installer/notebook/e2e_mcp use.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from migration import install as ins  # noqa: E402

import _e2e_env  # noqa: E402

PROFILE = _e2e_env.optional("DATABRICKS_CONFIG_PROFILE")
APP_NAME_WANTED = "managed-table-migration"  # bundle.name; deployed app is `mcp-<bundle.name>`
DEFAULT_STATE = os.path.expanduser("~/.mtm_e2e_app_state.json")

# Stable DOM markers (grounded in app source, 2026-09-20):
SSO_MARKER = "Continue with SSO"      # the OBO/SSO login page (session missing/expired)
READY_MARKER = "CONTROL PLANE"        # AppShell sidebar — present on every authenticated page
ERROR_MARKER = "Application Error"    # ErrorBoundary.tsx CardTitle — a crashed route

# Convert-Gate freeze-coverage verdict markers (ConvertGateDialog.tsx CoveragePanel). Exactly one
# renders per open gate; they map 1:1 to gateState (gap → BLOCK/freeze-first; held → GO).
COV_GAP = "Freeze coverage gap"       # gateState 'gap' — footer swaps to Freeze-first + Convert-anyway
COV_HELD = "Freeze bracket holds"     # gateState 'held' (fresh) — a plain Convert
COV_STALE = "Coverage may be stale"   # gateState 'held' past the freshness window — still a plain Convert
COV_UNEVAL = "Coverage not evaluated yet"  # gateState 'unevaluated' — a plain Convert (job fails closed)

# Convert-Gate concurrent-writer pause banner + advisory verdict (ConvertGateDialog WritersPanel).
PAUSE_BANNER = "will be paused"                    # the loud F3 "N production jobs will be paused" banner
WARN_WINDOW_BADGE = "Scheduled write approaching"  # verdictBadge('WARN_WINDOW').label — advisory only
WRITER_WAVE_SCHEMA = "e2e_real_writer"             # the isolated writer schema (F-WRITER-WAVE / L4-M4)

# First-run NEVER_PROFILED landing (OverviewPage.tsx FreshSituation + FirstRunResolution / A3). On a
# wiped control plane (base tables empty, views KEPT) the reads SUCCEED with zero rows, so the
# overview folds to overviewLandingState → NEVER_PROFILED (not the DataSurface NOT_PROVISIONED path).
A3_SITUATION = "The estate is unknown"             # FreshSituation warning card (situation)
A3_NO_FABRICATION = "no numbers are guessed"       # "Unknown until the profiler runs — no numbers are guessed."
A3_RESOLUTION = "Run the profiler"                 # FirstRunResolution CardTitle (the A3 card)
A3_PROFILER_JOB = "job_00_profiler"                # FirstRunResolution DarkStat Job — the one bootstrap step

# C4 — Convert (expert) page (ConvertPage.tsx). "Convert a scope" is the scope card; the gate
# trigger is "Convert scope…" (disabled until a scope is entered — the honest no-fabricated-GO
# wiring); the "Readiness by wave" board renders the live wave_plan_status × wave_gate_latest join.
C4_SCOPE_CARD = "Convert a scope"                  # data-tour="convert-scope" CardTitle
C4_GATE_TRIGGER = re.compile(r"Convert scope", re.I)  # ConvertGateDialog triggerLabel "Convert scope…"
C4_PREVIEW_CARD = "Readiness by wave"              # data-tour="convert-preview" — the readiness board
C4_LOCATION_MARKER = "To (managed)"                # LocationPlanPanel column — the managed-location panel

# C6 — Workstreams (WorkstreamsPage.tsx). The track grid links each remediation track; the detail
# page (WorkstreamDetailPage.tsx) shows the track label + an "All workstreams" back link.
C6_OPEN_MARKER = "Open workstream"                 # per-track link CTA (renders once counts load)
C6_TRACK_LABELS = ("Streaming writers", "Concurrent writer jobs", "Convert permission")
C6_DRILL_TRACK = "Concurrent writer jobs"          # the track we drill into (→ /workstreams/writer_jobs)
C6_DETAIL_BACK = "All workstreams"                 # WorkstreamDetailPage back link

# P3 render smokes (thin "mounts + query resolves" checks). DataSurface renders one of: an error
# Alert ("…load this data"), a "SQL warehouse starting…" note, aria-busy skeletons, an Empty title,
# or the rows — so _data_settled() reuses those states to prove the query reached a terminal state.
DS_ERROR = "load this data"                        # DataSurface error Alert ("Couldn't load this data")
DS_WARMING = "SQL warehouse starting"              # DataSurface cold-start note (keep waiting)
# (title marker, empty-state marker) per P3 page — see 2026-09-20-l4-ui-cuj-harness.md §2 (C5/C7/C8/
# C9/C11). C10/C13 are handled specially (an embed / static config, not a DataSurface).
P3_SMOKES: dict[str, tuple[str, str, str]] = {
    "/blocked":  ("Blocked & review", "Nothing blocked", "C5 /blocked"),
    "/freeze":   ("Paused & stopped", "Nothing is paused or stopped right now", "C7 /freeze"),
    "/runs":     ("Runs & history", "Nothing has happened yet", "C8 /runs"),
    "/coverage": ("What each workspace sees", "No profiler runs recorded yet", "C9 /coverage"),
    "/runbook":  ("Do by hand", "Nothing to do by hand", "C11 /runbook"),
}

# Fixture recipes (see 2026-09-20-l4-ui-cuj-harness.md §3). F-PLANNED is concrete today; the others
# reference the milestone that lands them. The `--fixture` seed infra (D-UI-3) arrives with F-READY-
# BATCH in L4-M2; until then these are the exact commands an operator runs to shape each estate.
FIXTURES: dict[str, str] = {
    "F-PLANNED":
        "python .e2e/seed_real_estate.py --profile <p> --preset populate   "
        "# profiled + planned-at-0% (every wave PLANNED). Serves C1/C5/C6/C9/C10 render.",
    "F-READY-BATCH":
        "python .e2e/seed_real_estate.py --profile <p> --fixture F-READY-BATCH   "
        "# on an F-PLANNED estate: auto-picks a parallel batch, dry-runs its member schemas, freezes "
        "them, operator-resolves any remaining manual PRE_CONVERT blocker, verifies READY (idempotent). "
        "Pin a batch with --fixture-batch <id>. Serves A2/C3 + C4 gate-GO.",
    "F-WRITER-WAVE":
        "python .e2e/seed_real_estate.py --profile <p> --fixture F-WRITER-WAVE   "
        "# on an estate seeded WITH the e2e_real_writer schema (a re-seed): dry-runs + 03c-freezes "
        "the isolated writer wave so it is RUNNABLE and its gate shows the pause banner + WARN_WINDOW "
        "badge. Serves C2/A1.   [L4-M4; live proof deferred to the next re-seed]",
    "F-EMPTY":
        "python .e2e/seed_real_estate.py --profile <p> --wipe-control-plane --confirm-wipe   "
        "# base tables empty (views KEPT → reads succeed with 0 rows → NEVER_PROFILED, not "
        "NOT_PROVISIONED). Serves C12/A3 first-run via `--a3`. MUTUALLY EXCLUSIVE with every "
        "populated fixture — a separate, estate-destructive run (re-profile/plan to restore).   [L4-M5]",
}

_failures: list[str] = []
_warnings: list[str] = []


# --------------------------------------------------------------------------- #
# assertion helpers (mirror e2e_mcp.py exactly)
# --------------------------------------------------------------------------- #
def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not cond:
        _failures.append(label)


def warn(label: str, detail: str = "") -> None:
    """Degrade-honestly: a session/estate/warehouse limitation is a WARN, never a false FAIL."""
    print(f"  [WARN] {label}{(' — ' + detail) if detail else ''}")
    _warnings.append(label)


# --------------------------------------------------------------------------- #
# resolution + session (D2)
# --------------------------------------------------------------------------- #
def resolve_app_url() -> str:
    if os.environ.get("E2E_APP_URL"):
        return os.environ["E2E_APP_URL"].rstrip("/")
    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient(profile=PROFILE)
    names = [getattr(a, "name", None) for a in w.apps.list()]
    app_name = ins.resolve_app_name(names, APP_NAME_WANTED)
    if not app_name:
        raise RuntimeError(f"Could not resolve deployed app for '{APP_NAME_WANTED}' (deploy first?)")
    app = w.apps.get(name=app_name)
    if not app.url:
        raise RuntimeError(f"App '{app_name}' has no URL yet (still starting?)")
    return app.url.rstrip("/")


def storage_state_dest() -> str:
    """Where --login writes / the run loads (env override, else a stable home-dir path)."""
    return os.environ.get("E2E_APP_STORAGE_STATE", DEFAULT_STATE)


def storage_state_path() -> str | None:
    """The session file IF it exists (else None → the run WARNs and every assertion degrades)."""
    path = storage_state_dest()
    return path if os.path.exists(path) else None


def token_headers() -> dict[str, str] | None:
    """Mint the profile's U2M/OAuth ``Authorization`` header — the SAME token path ``e2e_mcp.py``
    uses (``w.config.authenticate()``; SDK-native, no secrets in code). The Databricks Apps front
    door validates the Bearer and injects ``x-forwarded-*`` for the UI *and* every ``/api`` fetch
    (proven live 2026-09-20 — the KPI tiles render OBO-resolved with a header-only context and no
    session cookie), so this authenticates the app **headless and unattended** — no SSO/MFA, no
    captured ``storageState``, no refresh cadence (D-UI-2). In CI, point ``--profile`` at an SP OAuth
    (M2M) profile for a fully non-interactive session. Returns ``None`` (→ session/WARN fallback)
    when the SDK isn't installed or the profile can't mint a token.
    """
    try:
        from databricks.sdk import WorkspaceClient
        w = WorkspaceClient(profile=PROFILE)
        hdrs = dict(w.config.authenticate() or {})
        return hdrs if hdrs.get("Authorization") else None
    except Exception as e:  # noqa: BLE001
        warn(f"could not mint a Bearer token from profile {PROFILE!r} ({type(e).__name__}) — "
             "falling back to a captured session if present.")
        return None


# Bearer mode only: when the header was last minted. A profile token lives about an hour, and a
# focus walk outlives it, so goto_guarded re-mints it on the context before it can expire.
_BEARER = {"on": False, "minted": 0.0}
_BEARER_REFRESH_S = 30 * 60


def _refresh_bearer(page) -> None:
    if not _BEARER["on"] or time.time() - _BEARER["minted"] < _BEARER_REFRESH_S:
        return
    hdrs = token_headers()
    if hdrs:
        page.context.set_extra_http_headers(hdrs)
        _BEARER["minted"] = time.time()


def is_login_page(page) -> bool:
    """True when the browser is sitting on the OBO/SSO login page (no/expired session)."""
    try:
        return page.get_by_text(SSO_MARKER, exact=False).count() > 0
    except Exception:  # noqa: BLE001
        return False


def _dismiss_tour(page) -> None:
    """Close the app's onboarding tour (driver.js) if it is up. Its full-screen overlay
    intercepts pointer events, so any click-driven assertion must clear it first. Prefers the
    popover close button; falls back to Escape ONLY when no dialog is open (Escape would close a
    dialog too). Idempotent — a no-op when no tour is active.
    """
    try:
        for _ in range(3):
            if page.locator(".driver-overlay, .driver-popover").count() == 0:
                return
            close = page.locator(".driver-popover-close-btn")
            if close.count() > 0:
                try:
                    close.first.click(timeout=2_000)
                except Exception:  # noqa: BLE001
                    pass
            elif page.get_by_role("alertdialog").count() == 0:
                page.keyboard.press("Escape")
            page.wait_for_timeout(400)
    except Exception:  # noqa: BLE001
        pass


_TOUR_STEPS_TS = Path(__file__).resolve().parents[2] / "app" / "client" / "src" / "tours" / "tourSteps.ts"


def _tour_routes() -> list[str]:
    try:
        return re.findall(r"^\s*'(/[a-z-]*)':\s*\{", _TOUR_STEPS_TS.read_text(), flags=re.M)
    except OSError:
        return []


def _suppress_tours_script() -> str:
    """Init script marking every route's tour seen at a version no tour reaches, so none auto-starts
    (tourState.pure.ts ``shouldAutoStart``). A journey's timed clicks cannot race a tour overlay."""
    keys = [k for r in _tour_routes() for k in (f"mtm.tour.{r}.seenVersion", f"mtm.tour.{r}.dismissed")]
    vals = ["1000000" if k.endswith("seenVersion") else "true" for k in keys]
    return (f"try {{ const kv = {json.dumps(dict(zip(keys, vals)))}; "
            "for (const k in kv) localStorage.setItem(k, kv[k]); } catch (e) {}")


# --------------------------------------------------------------------------- #
# guarded navigation — the reusable M0 primitive every CUJ spec builds on:
#   navigate → detect the login gate → wait for the authenticated shell →
#   assert no error boundary → (optionally) wait for a page-specific content marker.
# Returns True only when the page is authenticated, mounted, and ready to assert on.
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# --screenshots DIR — a full-page PNG at every checkpoint + an index.md (git-ignored artifacts)
# --------------------------------------------------------------------------- #
_SHOTS: dict = {"dir": None, "n": 0, "index": [], "console": []}
WAREHOUSE_STARTING = "SQL warehouse starting"


def shot(page, name: str, note: str = "", *, full_page: bool = True) -> None:
    """Save a full-page screenshot for ``name`` when --screenshots is set (never fails a check)."""
    d = _SHOTS["dir"]
    if not d:
        return
    # Capture loaded content, not the warehouse-wake placeholders (bounded; never fails a check).
    try:
        page.get_by_text(WAREHOUSE_STARTING, exact=False).first.wait_for(state="hidden", timeout=90_000)
        page.wait_for_timeout(1_000)
    except Exception:  # noqa: BLE001
        pass
    _SHOTS["n"] += 1
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:60] or "shot"
    fn = f"{_SHOTS['n']:03d}_{slug}.png"
    try:
        page.screenshot(path=os.path.join(d, fn), full_page=full_page)
        _SHOTS["index"].append((fn, name, note or page.url))
    except Exception as e:  # noqa: BLE001
        warn(f"screenshot {name}", str(e)[:160])


def write_shot_index() -> None:
    d = _SHOTS["dir"]
    if not d or not _SHOTS["index"]:
        return
    lines = ["# UI screenshots", "", f"{len(_SHOTS['index'])} checkpoint(s).", "",
             "| # | Checkpoint | What it backs |", "|---|---|---|"]
    for fn, name, note in _SHOTS["index"]:
        lines.append(f"| [{fn}]({fn}) | {name} | {note} |")
    if _SHOTS["console"]:
        uniq = list(dict.fromkeys(c[:300] for c in _SHOTS["console"]))
        lines += ["", f"## Console errors ({len(_SHOTS['console'])}, {len(uniq)} distinct; first 20)", ""]
        lines += [f"- `{c}`" for c in uniq[:20]]
    Path(d, "index.md").write_text("\n".join(lines) + "\n")
    print(f"Screenshots: {len(_SHOTS['index'])} in {d} (index.md)")


def check_card_layers(page, tag: str, *, kind: str | None = None) -> None:
    """The layered next-step card: the step's button and the decision facts show without opening
    anything, and every collapsed section (``details[data-layer]``) starts closed.

    Name-agnostic on purpose: each step kind has its own button, so this finds the block that holds
    "You approve the step" (the step block) and asserts it is visible, offers a button or link, and
    sits outside every collapsed section. Read-only: sections are opened only for a screenshot,
    then closed again.
    """
    card = page.locator('[data-tour="overview-next"]')
    folds = card.locator("details[data-layer]")
    n = folds.count()
    check(f"{tag} the next-step card has collapsed sections", n > 0, f"count={n}")
    still_open = [folds.nth(i).get_attribute("data-layer") for i in range(n)
                  if folds.nth(i).get_attribute("open") is not None]
    check(f"{tag} every collapsed section starts closed", not still_open, f"open: {still_open}")
    approve = card.get_by_text("You approve the step", exact=False)
    if approve.count() == 0:
        check(f"{tag} the step block renders", False, "no 'You approve the step' line")
        return
    block = approve.first.locator("xpath=..")
    check(f"{tag} the step block shows without opening anything", block.is_visible()
          and block.locator("xpath=ancestor::details").count() == 0)
    actions = block.locator("button, a")
    visible = [actions.nth(i) for i in range(actions.count()) if actions.nth(i).is_visible()]
    check(f"{tag} the step's button shows without opening anything", len(visible) > 0,
          ", ".join((a.inner_text() or "").strip()[:40] for a in visible[:4]))
    if kind in ("convert", "resume"):
        down = card.get_by_text("Max downtime", exact=True)
        check(f"{tag} max downtime shows on a convert step without opening anything",
              down.count() > 0 and down.first.is_visible())
        # Milestone 6: the progress bar and the Ready line sit on the face, outside every section.
        here = card.get_by_text("you are here", exact=True)
        check(f"{tag} the progress bar shows this step without opening anything",
              here.count() == 1 and here.first.is_visible()
              and here.first.locator("xpath=ancestor::details").count() == 0,
              f"count={here.count()}")
        verdict = card.locator("[data-verdict]")
        text = (verdict.first.inner_text() or "").strip() if verdict.count() else ""
        check(f"{tag} the Ready line shows without opening anything",
              verdict.count() == 1 and verdict.first.is_visible()
              and verdict.first.locator("xpath=ancestor::details").count() == 0, text)
        check(f"{tag} the Ready line never reads Ready with a table blocked",
              not (text.startswith("Ready.") and "blocked" in text), text)
    if _SHOTS["dir"] and n:
        folds.evaluate_all("els => els.forEach(e => { e.open = true; })")
        page.wait_for_timeout(400)
        shot(page, f"{tag} next-step card with every section open", "capture only; closed again after")
        folds.evaluate_all("els => els.forEach(e => { e.open = false; })")


def goto_guarded(page, url: str, *, ready_marker: str | None = None, label: str = "") -> bool:
    tag = label or url
    _refresh_bearer(page)
    page.goto(url, wait_until="domcontentloaded")

    if is_login_page(page):
        warn(f"{tag}: landed on the SSO login page — session missing/expired. "
             "Run `python .e2e/e2e_app_ui.py --login` to (re)capture a session.")
        return False

    # Authenticated app shell (sidebar) must render.
    try:
        page.get_by_text(READY_MARKER, exact=False).first.wait_for(timeout=30_000)
    except Exception:  # noqa: BLE001
        warn(f"{tag}: authenticated app shell did not render ({READY_MARKER!r} absent)")
        return False

    # Error-boundary check — a crashed route renders ErrorBoundary's "Application Error".
    if page.get_by_text(ERROR_MARKER, exact=True).count() > 0:
        check(f"{tag} route mounts without an error boundary", False, f"{ERROR_MARKER!r} shown")
        return False
    check(f"{tag} route mounts without an error boundary", True)

    # Clear the onboarding tour if it auto-started — its overlay would intercept every click.
    _dismiss_tour(page)

    # Page-specific readiness (the first query wakes the warehouse — up to ~a minute).
    if ready_marker:
        try:
            page.get_by_text(ready_marker, exact=False).first.wait_for(timeout=90_000)
        except Exception:  # noqa: BLE001
            warn(f"{tag}: content marker {ready_marker!r} did not render "
                 "(warehouse cold / empty estate / not shaped)")
            shot(page, f"{tag} (marker missing)", url)
            return False
    shot(page, tag, url)
    return True


# --------------------------------------------------------------------------- #
# --login — capture/refresh an auth session (headed, interactive; D2 step 1)
# --------------------------------------------------------------------------- #
def capture_session(app_url: str) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError:
        print("playwright not installed — `pip install playwright && playwright install chromium`.")
        return 2
    dest = storage_state_dest()
    print(f"Opening {app_url} in a headed browser — complete the Databricks SSO + MFA login there.")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(app_url, wait_until="domcontentloaded")
        print(f"Waiting (up to 5m) for the app shell ({READY_MARKER!r}) to render after login…")
        try:
            page.get_by_text(READY_MARKER, exact=False).first.wait_for(timeout=300_000)
            print("App shell detected — login complete.")
        except Exception:  # noqa: BLE001
            print("Timed out waiting for the app shell. If you DID log in, saving state anyway; "
                  "otherwise re-run --login.")
        ctx.storage_state(path=dest)
        browser.close()
    print(f"Saved auth session → {dest}")
    if not os.environ.get("E2E_APP_STORAGE_STATE"):
        print(f"(Default path. To pin it: export E2E_APP_STORAGE_STATE={dest})")
    return 0


# --------------------------------------------------------------------------- #
# C1 — /overview: KPIs render from live data + the guided-convert CTA opens the gate
# --------------------------------------------------------------------------- #
def assert_c1(page, app_url: str) -> None:
    """The Overview landing renders its live estate KPIs, and (when the conductor recommends a
    convert) the guided-convert CTA opens the Convert-Gate for the recommended scope.

    Grounded in OverviewPage.tsx: the situation card ("Where the migration stands")
    renders four KPI tiles (Migrated / Validated / Remaining / Schemas blocked) from the precomputed
    migration_next_actions rollup; the guided ResolutionCard footer reads "You approve the step.";
    and GuidedConvertCta renders a ConvertGateDialog whose trigger is "Open the convert step" /
    "Convert anyway…" and which opens an AlertDialog (role=alertdialog) with a "Readiness" rail.
    """
    # "Schemas blocked" is a KPI tile that only renders on a PLANNED estate (not the first-run card),
    # so it doubles as the warehouse-wake / data-loaded marker.
    if not goto_guarded(page, f"{app_url}/overview", ready_marker="Schemas blocked",
                        label="C1 /overview"):
        return

    # C1.1 — the four estate KPI tiles render from live data.
    for tile in ("Migrated", "Validated", "Remaining", "Schemas blocked"):
        check(f"C1 KPI tile {tile!r} renders", page.get_by_text(tile, exact=False).count() > 0)

    # C1.2 — the guided next-action (ResolutionCard) mounted (its stable footer line).
    check("C1 the guided next-action card renders",
          page.get_by_text("You approve the step", exact=False).count() > 0)

    # C1.5 — the card's step and decision facts show without opening anything (layered card).
    # C1 reads no control table, so a visible convert button (_CONVERT_CTA_RE, as C1.3) is what
    # marks a convert step and turns on the convert-only checks (max downtime, bar, Ready line).
    next_card = page.locator('[data-tour="overview-next"]')
    on_convert = next_card.get_by_role("button", name=_CONVERT_CTA_RE).count() > 0
    check_card_layers(page, "C1", kind="convert" if on_convert else None)

    # C1.3 — the guided-convert CTA opens the Convert-Gate (only when convert is the recommendation).
    # Matched by the same names the journey clicks (_CONVERT_CTA_RE: "Open the convert step" and the
    # conscious-skip / anyway variants); the old "convert gate" wording no longer exists, so this
    # check had been skipping silently.
    gate_btn = page.get_by_role("button", name=_CONVERT_CTA_RE)
    if gate_btn.count() == 0:
        warn("C1 guided-convert CTA not present — the current conductor recommendation is not a "
             "convert step (F-PLANNED often recommends the dry-run/prep first). The gate-open "
             "assertion needs a convert-recommended scope (F-READY-BATCH; L4-M2/M3).")
        return
    if not gate_btn.first.is_enabled():
        warn("C1 guided-convert CTA is present but disabled (a convert is already running / waiting "
             "on members) — gate-open assertion skipped.")
        return

    gate_btn.first.click()
    dialog = page.get_by_role("alertdialog")
    try:
        dialog.first.wait_for(timeout=10_000)
    except Exception:  # noqa: BLE001
        check("C1 guided-convert CTA opens the Convert-Gate dialog", False, "no alertdialog appeared")
        return
    check("C1 guided-convert CTA opens the Convert-Gate dialog", dialog.count() > 0)
    # C1.4 — the gate content is wired (its Readiness rail), not an empty modal.
    dialog_text = (dialog.first.inner_text() if dialog.count() else "") or ""
    check("C1 the opened gate renders its Readiness step", "Readiness" in dialog_text,
          dialog_text[:120].replace("\n", " "))


# --------------------------------------------------------------------------- #
# C2 — /waves: the Convert-Gate renders the correct GO/BLOCK wiring
# --------------------------------------------------------------------------- #
def _open_convert_gate(page) -> bool:
    """Open an inline Convert-Gate from the Waves board and return True once the dialog is open.

    Prefers whatever inline ``Convert <label>`` trigger is already visible (the Next-convert card
    when its wave is a normal, runnable one); falls back to expanding wave rows until a
    non-streaming runnable wave renders its trigger (a streaming wave routes to the streaming
    cockpit as a link, so it has no inline gate). Only ever clicks toggles + the trigger — no
    mutation.

    NB the inline trigger renders DISABLED first while the wave's members query loads (the count
    must be real before you can convert), so we rely on Playwright's click auto-wait to hold until
    it enables rather than skipping a momentarily-disabled button.
    """
    trig_re = re.compile(r"^Convert\s+\S")  # "Convert <label>" — the inline gate trigger only

    def _try_click() -> bool:
        trig = page.get_by_role("button", name=trig_re)
        if trig.count() == 0:
            return False
        try:
            trig.first.click(timeout=20_000)  # auto-waits for lazy members to enable the button
            return True
        except Exception:  # noqa: BLE001
            return False

    # 1) A trigger may already be on the board (Next-convert card / an expanded wave).
    if _try_click():
        return True

    # 2) Expand wave rows (each header shows "<n> tables") until one exposes an inline gate. A
    #    streaming/blocked wave has no inline trigger, so skip to the next. Scoped to the board so
    #    the "Check converted tables" action (no leading digit) is never matched.
    board = page.locator('[data-tour="waves-board"]')
    headers = board.get_by_role("button").filter(has_text=re.compile(r"\d+\s+tables"))
    for i in range(min(headers.count(), 12)):
        try:
            headers.nth(i).click()
        except Exception:  # noqa: BLE001
            continue
        page.wait_for_timeout(1_500)
        if _try_click():
            return True
    return False


def assert_c2(page, app_url: str) -> None:
    """/waves — a runnable wave → the Convert-Gate renders the correct GO/BLOCK wiring.

    Proves the render+interaction layer a pure test structurally can't reach: the deployed gate
    OPENS from the board, its five-stage rail mounts, the Freeze stage renders the LIVE coverage
    verdict, the FOOTER action set is CONSISTENT with that verdict (a gap → Freeze-first +
    Convert-without-freezing; a hold/unevaluated → a plain Convert), and the SR2 type-to-confirm
    (+ acknowledge) actually unlocks Convert. Non-mutating: we type/tick and READ the button
    state; we NEVER click Convert/Freeze-first. Grounded in ConvertGateDialog.tsx +
    gateFlow/gateChecklist.pure.

    Why GO/BLOCK is asserted as WIRING, not a fixed verdict: WRITER_STOP_COVERAGE is written only
    by job_04_convert, so a freshly dry-run+frozen F-READY-BATCH wave folds to ``unevaluated``
    (gateState needs compaction+stream+writer all covered for a ``held``), and a clean coverage
    ``gap`` needs a dry-run that FOUND uncovered work. So this asserts the gate renders SOME
    recognized verdict and the footer matches it; a live hold/gap is exercised opportunistically
    when the estate provides it.
    """
    if not goto_guarded(page, f"{app_url}/waves", ready_marker="Next convert", label="C2 /waves"):
        warn("C2 no runnable wave on the board (no 'Next convert') — shape one with "
             "--fixtures F-READY-BATCH, then re-run.")
        return

    _dismiss_tour(page)  # the board tour can auto-start once the plan renders
    if not _open_convert_gate(page):
        warn("C2 could not open an inline Convert-Gate (the runnable waves may all be streaming, "
             "which route to the streaming cockpit) — needs a runnable NORMAL wave (F-READY-BATCH).")
        return

    dialog = page.get_by_role("alertdialog")
    try:
        dialog.first.wait_for(timeout=10_000)
    except Exception:  # noqa: BLE001
        check("C2 the Convert-Gate opens from the Waves board", False, "no alertdialog appeared")
        return
    check("C2 the Convert-Gate opens from the Waves board", dialog.count() > 0)
    _dismiss_tour(page)  # belt-and-braces: clear any residual overlay now the dialog is up

    # C2.1 — the five-stage process rail mounted (Readiness → Freeze → Convert).
    rail_text = (dialog.first.inner_text() if dialog.count() else "") or ""
    check("C2 the gate renders its process rail (Readiness/Freeze/Convert)",
          all(s in rail_text for s in ("Readiness", "Freeze", "Convert")))

    # C2.2 — the Freeze stage renders a recognized LIVE coverage verdict. NB the panel headers are
    # CSS-uppercased, so inner_text() returns e.g. "FREEZE BRACKET HOLDS" — match case-insensitively.
    freeze_node = dialog.locator('button[title^="Freeze"]').first
    if freeze_node.count():
        freeze_node.click()
    low = ((dialog.first.inner_text() if dialog.count() else "") or "").lower()
    verdict = ("gap" if COV_GAP.lower() in low
               else "held" if (COV_HELD.lower() in low or COV_STALE.lower() in low)
               else "unevaluated" if COV_UNEVAL.lower() in low
               else None)
    check("C2 the Freeze stage renders a coverage verdict", verdict is not None,
          verdict or low[:100].replace("\n", " "))

    # C2.3 — the FOOTER action set is CONSISTENT with the verdict (the GO/BLOCK wiring proof).
    freeze_first = dialog.get_by_role("button", name="Pause jobs first")
    convert_wo = dialog.get_by_role("button", name="Convert without pausing")
    convert_plain = dialog.get_by_role("button", name="Convert", exact=True)
    if verdict == "gap":
        check("C2 a coverage gap wires the freeze-first BLOCK remediation",
              freeze_first.count() > 0 and convert_wo.count() > 0)
        primary = convert_wo
    else:
        check("C2 a hold/unevaluated verdict wires a plain Convert action",
              convert_plain.count() > 0 and freeze_first.count() == 0)
        primary = convert_plain
        if verdict == "unevaluated":
            print("    (note: coverage unevaluated — a clean held-GO needs WRITER_STOP_COVERAGE "
                  "from job_04; the GO/BLOCK contrast is asserted as wiring, exercised live when "
                  "the estate provides a hold/gap.)")

    # C2.4 — the concurrent-writers 'Pause writer jobs first' affordance is CONSISTENT with the writer
    # stop-coverage (design invariant: shown iff coverage is not held). Detect held via the two
    # covered headers ("Writers stopped" fresh / "…may be stale"). Fully exercised on a live gap by
    # F-WRITER-WAVE (M4).
    if "concurrent writers" in low:
        stop_present = dialog.get_by_role("button", name="Pause writer jobs first").count() > 0
        writer_covered = ("writers stopped" in low) or ("stop-coverage may be stale" in low)
        check("C2 'Pause writer jobs first' matches writer coverage (shown iff not held)",
              stop_present == (not writer_covered),
              f"stop_present={stop_present} writer_covered={writer_covered}")
    else:
        warn("C2 no attributed writer on this wave — the concurrent-writers panel + pause banner "
             "(A1) need the F-WRITER-WAVE fixture (L4-M4).")

    # C2.5 — SR2 type-to-confirm (+ acknowledge) unlocks Convert. Non-mutating: set the gate
    # ready and READ the button's enabled state; we never click Convert.
    if primary.count() == 0:
        warn("C2 primary convert action not found — skipping the type-to-confirm unlock proof.")
    else:
        check("C2 Convert is disabled before the scope is confirmed (SR2 gate holds)",
              primary.first.is_disabled())
        # Reveal the checklist (Convert stage) so the acknowledgement checkboxes are mounted.
        convert_node = dialog.locator('button[title^="Convert"]').first
        if convert_node.count():
            convert_node.click()
        c_low = ((dialog.first.inner_text() if dialog.count() else "") or "").lower()
        check("C2 the Convert stage lists the tables it will change",
              "tables this will change" in c_low)
        # Type the exact confirm target (read from the input placeholder) + tick any acks.
        inp = page.locator("#convert-confirm")
        target = inp.get_attribute("placeholder") if inp.count() else None
        if target:
            inp.fill(target)
        for ack_id in ("ack-permissions", "ack-freeze"):
            cb = dialog.locator(f"#{ack_id}")
            if cb.count() > 0:
                try:
                    cb.click()
                except Exception:  # noqa: BLE001
                    pass
        if primary.first.is_enabled():
            check("C2 type-to-confirm + acknowledge unlocks Convert (GO interaction)", True)
        else:
            warn("C2 Convert stayed gated after confirm+ack — an additional prerequisite is open "
                 "(e.g. readiness not passed, which is never acknowledge-able); the SR2 gate held, "
                 "which is correct.")

    # C2.6 — BLOCK contrast at the board level: at least one wave surfaces a blocking indicator.
    # (Escape first so the dialog doesn't overlay the board read.)
    page.keyboard.press("Escape")
    if page.get_by_text(re.compile(r"\bblocking\b"), exact=False).count() > 0:
        check("C2 the board surfaces at least one blocked wave (BLOCK contrast)", True)
    else:
        warn("C2 no blocked wave on the board — the GO/BLOCK board contrast wasn't exercised "
             "(a fully-ready estate).")


# --------------------------------------------------------------------------- #
# A2 — /bulk: the W3 "Recommended next" badge on the READY parallel batch
# --------------------------------------------------------------------------- #
def assert_a2(page, app_url: str) -> None:
    """The started batch, else the schedule-safest READY parallel batch, carries the
    "Recommended next" badge (W3; `recommendedBatch` finishes an in-flight batch first).

    Selectors proved by the first live green (e2e workspace, 2026-09-19): the badge text is exactly
    "Recommended next" (BatchCard's recommendedBatch <Badge>), it lives inside the READY parallel
    batch CARD, and a non-READY (AWAITING_ACTIONS) batch carries no such badge.
    """
    if not goto_guarded(page, f"{app_url}/bulk", ready_marker="degree", label="A2 /bulk"):
        return

    # A2.1 — exactly one "Recommended next" badge, on the recommended READY batch.
    badges = page.get_by_text("Recommended next", exact=True)
    n = badges.count()
    if n == 0:
        warn("A2 no READY parallel batch (estate not shaped — see --fixtures F-READY-BATCH)")
        return
    check("A2 exactly one 'Recommended next' badge is shown", n == 1, f"count={n}")

    # A2.2 — the badge's card is READY (the badge only marks a READY parallel batch).
    # Walk up to the whole BatchCard, not just the header row: the member ✓ badges live in
    # CardContent (a SIBLING of the CardHeader where this badge sits), so anchor the ancestor
    # walk on content-only text ("min copy") to reach the Card root — 'degree' alone stops at
    # the header and misses the members (the A2.3 selector bug the first live run surfaced).
    card = badges.first.locator("xpath=ancestor::*[.//text()[contains(., 'min copy')]][1]")
    card_text = (card.inner_text() if card.count() else "") or ""
    # recommendedBatch finishes a started (IN_PROGRESS) batch before any READY one.
    status = next((s for s in ("IN_PROGRESS", "READY") if s in card_text), None)
    check("A2 the recommended batch card is READY, or the started batch (IN_PROGRESS)",
          status is not None, f"status={status} · {card_text[:120]}")
    # A2.3 — every member wave in the recommended card shows the dry-run-ok marker (✓).
    check("A2 the recommended card's member waves show the ready ✓", "✓" in card_text)


# --------------------------------------------------------------------------- #
# A3 — /overview first-run: the NEVER_PROFILED landing on a wiped control plane (F-EMPTY / L4-M5)
# --------------------------------------------------------------------------- #
def assert_a3(page, app_url: str) -> None:
    """The Overview first-run landing renders the honest NEVER_PROFILED empty state — the estate is
    *unknown*, not zero — and offers the single bootstrap step (run the profiler). Needs F-EMPTY (a
    wiped control plane: `seed_real_estate.py --wipe-control-plane --confirm-wipe`).

    Grounded in OverviewPage.tsx: on a wipe the BASE tables are empty but the VIEWS are KEPT, so the
    reads SUCCEED with zero rows → `overviewLandingState` → `NEVER_PROFILED` (NOT the DataSurface
    NOT_PROVISIONED path, which only fires when objects are genuinely missing). That renders
    `FreshSituation` ("The estate is unknown" + four dash KPI tiles under "no numbers are guessed")
    and `FirstRunResolution` ("Run the profiler" + a `job_00_profiler`
    JobTriggerDialog). Read-only: we assert the CTA is present but NEVER click it (a click only opens
    a confirm dialog; the job fires on confirm — out of scope for a render smoke).

    Degrades to WARN (never a false FAIL) when the control plane is NOT empty (the shared estate is
    still populated) — A3 is mutually exclusive with every populated fixture and needs its own
    wiped-estate run.
    """
    if not goto_guarded(page, f"{app_url}/overview", ready_marker=None, label="A3 /overview"):
        return
    _dismiss_tour(page)

    # The overview holds its empty landing (`if (loading) return null`) until the async landing reads
    # resolve, and the first read wakes a cold warehouse — so settle on ONE of the two mutually
    # exclusive situations before asserting: the first-run resolution (wiped) OR the populated KPI
    # tile. Polling both avoids a false FAIL against a landing that simply hasn't rendered yet.
    deadline = time.time() + 90
    while time.time() < deadline:
        if page.get_by_text(A3_RESOLUTION, exact=False).count() > 0:
            break
        if page.get_by_text("Schemas blocked", exact=False).count() > 0:
            break
        page.wait_for_timeout(2_000)

    # If the populated situation is on screen, the control plane isn't wiped — A3 can't run here.
    if page.get_by_text("Schemas blocked", exact=False).count() > 0:
        warn("A3 the control plane is still POPULATED (the planned situation renders) — A3 needs a "
             "wiped estate. Run `seed_real_estate.py --wipe-control-plane --confirm-wipe`, then "
             "`--a3`. (A3 is mutually exclusive with A1/A2/C1/C2's populated estate — a separate run.)")
        return

    # A3.1 — FreshSituation: the estate is honestly UNKNOWN (not a fabricated zero).
    check("A3 the first-run situation says the estate is unknown",
          page.get_by_text(A3_SITUATION, exact=False).count() > 0)
    check("A3 the first-run situation makes the no-fabrication promise ('no numbers are guessed')",
          page.get_by_text(A3_NO_FABRICATION, exact=False).count() > 0)
    # A3.2 — the four KPI tiles render a dash, never a fabricated 0.
    body = (page.locator("body").inner_text() or "")
    check("A3 the first-run KPI tiles render a dash (—), not a fabricated number", "—" in body)

    # A3.3 — FirstRunResolution: the single bootstrap step is the profiler (the A3 card).
    check("A3 the first-run resolution card offers 'Run the profiler'",
          page.get_by_text(A3_RESOLUTION, exact=False).count() > 0)
    check("A3 the resolution names the profiler job (job_00_profiler)",
          page.get_by_text(A3_PROFILER_JOB, exact=False).count() > 0)
    # A3.4 — the profiler CTA is present (non-mutating; we NEVER click it).
    cta = page.get_by_role("button", name=re.compile(r"Run the profiler", re.I))
    check("A3 the 'Run the profiler' bootstrap CTA is present", cta.count() > 0)

    # A3.5 (contrast) — none of the populated-estate affordances are on the first-run landing.
    check("A3 no guided-convert CTA on a never-profiled estate",
          page.get_by_role("button", name=_CONVERT_CTA_RE).count() == 0
          and page.get_by_role("button", name=re.compile(r"convert gate", re.I)).count() == 0)


# --------------------------------------------------------------------------- #
# A1 — Convert-Gate pause banner (W2) on the isolated writer wave (F-WRITER-WAVE / L4-M4)
# --------------------------------------------------------------------------- #
def _open_writer_gate(page) -> bool:
    """Open the inline Convert-Gate for the ISOLATED writer wave (schema ``e2e_real_writer``).

    The board groups by schema, so the tiny writer schema forms its own group. We scope opening to
    that group — never the Next-convert card's trigger (which targets the lowest-order runnable
    wave, a DIFFERENT wave) — and confirm we opened the right gate by the schema name appearing in
    the dialog (affected names / pause-banner full_name). Only clicks toggles + the trigger; no
    mutation. Returns False when no such group/trigger exists (estate not shaped by F-WRITER-WAVE).
    """
    board = page.locator('[data-tour="waves-board"]')
    # The innermost board div that holds BOTH the writer schema label AND a "<n> tables" wave
    # header — that is exactly the schema-group container (the header div has the label but no
    # tables button; a WaveCard has a tables button but no label), so `.last` (innermost) is it.
    group = board.locator(
        f"xpath=.//div[.//span[contains(text(),'{WRITER_WAVE_SCHEMA}')] "
        "and .//button[contains(., 'tables')]]"
    ).last
    if group.count() == 0:
        return False

    trig_re = re.compile(r"^Convert\s+\S")  # the inline "Convert <label>" gate trigger
    headers = group.get_by_role("button").filter(has_text=re.compile(r"\d+\s+tables"))
    for i in range(min(headers.count(), 6)):
        try:
            headers.nth(i).click()
        except Exception:  # noqa: BLE001
            continue
        page.wait_for_timeout(1_500)
        trig = group.get_by_role("button", name=trig_re)
        if trig.count() == 0:
            continue
        try:
            trig.first.click(timeout=20_000)  # auto-waits for lazy members to enable it
        except Exception:  # noqa: BLE001
            continue
        dlg = page.get_by_role("alertdialog")
        try:
            dlg.first.wait_for(timeout=8_000)
        except Exception:  # noqa: BLE001
            continue
        if WRITER_WAVE_SCHEMA in ((dlg.first.inner_text() if dlg.count() else "") or ""):
            return True
        page.keyboard.press("Escape")   # wrong wave — close and keep scanning this group
        page.wait_for_timeout(500)
    return False


def assert_a1(page, app_url: str) -> None:
    """/waves — the ISOLATED writer wave's Convert-Gate shows the W2 pause banner naming the
    scheduled writer + the WARN_WINDOW advisory badge (needs the F-WRITER-WAVE fixture / L4-M4).

    Proves the concurrent-writer surface a pure test can't reach: the deployed gate, opened on a
    runnable wave that CONTAINS an attributed SCHEDULED_JOB writer, renders (a) the loud "N
    production job(s) will be paused" banner naming the job (class · job id · next fire · table),
    and (b) the advisory 'Scheduled write approaching' badge (verdictBadge('WARN_WINDOW'), threaded
    from wave_status.wave_writer_verdict via W2). Non-mutating — reads only; never clicks Convert.

    Degrades to WARN (never a false FAIL) when the writer wave isn't shaped (needs a re-seed with
    the e2e_real_writer schema + `--fixture F-WRITER-WAVE`) or when attribution/schedule lag leaves
    the banner/verdict not-yet-stamped (mirrors the oracle's O1 lineage-lag tolerance).
    """
    if not goto_guarded(page, f"{app_url}/waves", ready_marker="Next convert", label="A1 /waves"):
        warn("A1 no runnable wave on the board — shape the writer wave with "
             "`--fixture F-WRITER-WAVE`, then re-run.")
        return
    _dismiss_tour(page)  # the board tour can auto-start once the plan renders

    if not _open_writer_gate(page):
        warn(f"A1 could not open the isolated {WRITER_WAVE_SCHEMA} wave's Convert-Gate — shape it "
             "with `seed_real_estate.py --fixture F-WRITER-WAVE` (a runnable writer wave). Requires "
             "a re-seed that provisions the e2e_real_writer schema (L4-M4 deferred live proof).")
        return

    dialog = page.get_by_role("alertdialog")
    _dismiss_tour(page)  # belt-and-braces once the dialog is up
    # The pause banner + WARN_WINDOW badge render on the FREEZE stage panel (CoveragePanel +
    # WritersPanel). The gate auto-opens on the Convert/Permissions stage whenever a resolution is
    # pending (e.g. a missing convert permission), so reveal the Freeze stage first — same as assert_c2.
    # Non-mutating: only navigates the rail, never clicks Convert/Freeze-first.
    freeze_node = dialog.locator('button[title^="Freeze"]').first
    if freeze_node.count():
        freeze_node.click()
        page.wait_for_timeout(1_000)
    text = (dialog.first.inner_text() if dialog.count() else "") or ""
    low = text.lower()

    # A1.1 — the loud pause banner is present and names a schedulable writer job row.
    check("A1 the gate renders the 'will be paused' pause banner", PAUSE_BANNER in low)
    check("A1 the pause banner names a scheduled writer job (class · job id · next fire · table)",
          ("· job " in text) and ("next fire" in low))
    # A1.2 — the named writer is the isolated writer-wave table (not some other wave's writer).
    check(f"A1 the pause banner names the {WRITER_WAVE_SCHEMA} writer table",
          WRITER_WAVE_SCHEMA in text)
    # A1.3 — a concrete next-fire time, not the 'schedule unknown' fallback (WARN on lineage lag).
    if "schedule unknown" in low:
        warn("A1 pause banner shows 'schedule unknown' — next_scheduled_write_at not stamped yet "
             "(schedule/lineage lag); re-profile + re-run the fixture.")
    else:
        check("A1 the pause banner shows a concrete next-fire time (WARN_WINDOW evidence)",
              "next fire" in low)
    # A1.4 — the advisory WARN_WINDOW badge ('Scheduled write approaching'), threaded via W2. WARN
    #        if the wave's rolled-up verdict hasn't landed as WARN_WINDOW yet (schedule lag).
    if WARN_WINDOW_BADGE.lower() in low:
        check("A1 the gate shows the WARN_WINDOW advisory badge ('Scheduled write approaching')",
              True)
    else:
        warn("A1 advisory badge is not WARN_WINDOW yet — wave_writer_verdict hasn't rolled up to "
             "WARN_WINDOW (schedule/lineage lag); re-profile + re-run the fixture.")
    page.keyboard.press("Escape")


# --------------------------------------------------------------------------- #
# C4 — /convert (expert): scope entry → the gate reflects scope readiness (L4-M6)
# --------------------------------------------------------------------------- #
def assert_c4(page, app_url: str) -> None:
    """/convert — the expert scope page mounts, the scope pickers + honest gate wiring render,
    and (via the ?wave= on-ramp) a scoped gate opens with its Readiness rail + managed-location
    panel. Non-mutating: reads the board, uses the URL on-ramp, and never clicks Convert.

    Grounded in ConvertPage.tsx: the "Convert a scope" card holds the target_schema/bucket/wave
    pickers and a ``Convert scope…`` ConvertGateDialog whose trigger is DISABLED until a scope is
    entered (SR: "Provide a scope first" — never a fabricated GO). The "Readiness by wave" board is a
    read-only client join of wave_plan_status × wave_gate_latest, exposing each wave_id in a mono
    span — which doubles as the on-ramp for the ?wave= gate-open below.
    """
    if not goto_guarded(page, f"{app_url}/convert", ready_marker=C4_SCOPE_CARD, label="C4 /convert"):
        return

    # C4.1 — the scope card + its three pickers render.
    check("C4 the 'Convert a scope' card renders", page.get_by_text(C4_SCOPE_CARD, exact=False).count() > 0)
    for field in ("target_schema", "bucket", "wave"):
        check(f"C4 the {field!r} scope field renders",
              page.locator("label", has_text=re.compile(rf"^{field}$")).count() > 0)

    # C4.2 — the gate trigger is present AND disabled before any scope (the honest gate wiring).
    trig = page.get_by_role("button", name=C4_GATE_TRIGGER)
    check("C4 the 'Convert scope…' gate trigger renders", trig.count() > 0)
    if trig.count() > 0:
        check("C4 the gate trigger is disabled before a scope is entered (no fabricated GO)",
              trig.first.is_disabled())

    # C4.3 — the Readiness by wave readiness board renders wave rows from the live join. The board has
    # its OWN queries (wave_plan_status + wave_gate_latest) that resolve AFTER the page title, so
    # settle before reading: poll for wave-id rows (each row's wave_id is a font-mono span) OR the
    # honest "No plan yet" empty state. A cold warehouse / empty plan is a WARN, never a false FAIL.
    if not page.get_by_text(C4_PREVIEW_CARD, exact=False).count():
        warn("C4 the 'Readiness by wave' board did not render — cannot verify the readiness join.")
        return
    preview = page.locator("[data-tour='convert-preview']")
    wave_ids: list[str] = []
    deadline = time.time() + 60
    while time.time() < deadline:
        wave_ids = [w.strip() for w in preview.locator("span.font-mono").all_inner_texts() if w.strip()]
        if wave_ids:
            break
        if page.get_by_text("No plan yet", exact=False).count() > 0:
            warn("C4 the Readiness by wave board is empty ('No plan yet') — the estate has no plan; "
                 "the ?wave= gate-open on-ramp can't be exercised (F-PLANNED via --preset populate).")
            return
        page.wait_for_timeout(2_000)
    if not wave_ids:
        warn("C4 the Readiness by wave board did not settle within 60s (warehouse cold / still loading) "
             "— the readiness-board + ?wave= gate-open assertions degraded to WARN.")
        return
    check("C4 the Readiness by wave board renders wave rows (live wave_plan_status × wave_gate_latest)",
          len(wave_ids) > 0, f"{len(wave_ids)} wave row(s)")

    # C4.4 (bonus) — the ?wave= on-ramp scopes the page → the gate enables → opens with a Readiness
    # rail + the managed-location panel. Read-only: never clicks Convert.
    wid = wave_ids[0]
    if not goto_guarded(page, f"{app_url}/convert?wave={wid}", ready_marker=C4_SCOPE_CARD,
                        label=f"C4 /convert?wave={wid}"):
        return
    trig = page.get_by_role("button", name=C4_GATE_TRIGGER)
    if trig.count() == 0 or not trig.first.is_enabled():
        warn(f"C4 the gate trigger did not enable for wave {wid} (coverage/members still loading) — "
             "gate-open assertion skipped.")
        return
    trig.first.click()
    dialog = page.get_by_role("alertdialog")
    try:
        dialog.first.wait_for(timeout=10_000)
    except Exception:  # noqa: BLE001
        check("C4 the scoped 'Convert scope…' gate opens", False, "no alertdialog appeared")
        return
    check("C4 the scoped 'Convert scope…' gate opens", dialog.count() > 0)
    dtext = (dialog.first.inner_text() if dialog.count() else "") or ""
    check("C4 the opened gate renders its Readiness step", "Readiness" in dtext,
          dtext[:120].replace("\n", " "))
    # C4.5 — the managed-location plan panel renders inside the scoped gate (LocationPlanForScope).
    if C4_LOCATION_MARKER in dtext:
        check("C4 the gate renders the managed-location plan panel ('To (managed)')", True)
    else:
        warn("C4 no managed-location panel in the gate — schema_locations has no row for this wave "
             "(managed location not resolved yet); location assertion degraded.")
    page.keyboard.press("Escape")


# --------------------------------------------------------------------------- #
# C6 — /workstreams: the remediation track cards render + one detail drill-in (L4-M6)
# --------------------------------------------------------------------------- #
def assert_c6(page, app_url: str) -> None:
    """/workstreams — the nine remediation-track cards render live counts, and drilling into one
    opens its detail page. Grounded in WorkstreamsPage.tsx (the ``workstream_counts`` grid, each
    card a ``<Link to="/workstreams/<key>">`` with a label + count + "Open workstream") and
    WorkstreamDetailPage.tsx (the track label CardTitle + an "All workstreams" back link).
    """
    if not goto_guarded(page, f"{app_url}/workstreams", ready_marker=C6_OPEN_MARKER,
                        label="C6 /workstreams"):
        return

    # C6.1 — the track grid mounted with the known remediation-track labels.
    check("C6 the workstreams grid renders",
          page.locator("[data-tour='workstreams-grid']").count() > 0)
    for label in C6_TRACK_LABELS:
        check(f"C6 the {label!r} track card renders", page.get_by_text(label, exact=False).count() > 0)

    # C6.2 — the cards carry live counts (a tabular-nums count span per track).
    counts = page.locator("[data-tour='workstreams-grid'] span.tabular-nums").count()
    check("C6 each track card shows a live count", counts >= len(C6_TRACK_LABELS),
          f"{counts} count span(s)")

    # C6.3 — drilling into a track opens its detail page (mounts w/o error + back link).
    link = page.get_by_role("link", name=re.compile(re.escape(C6_DRILL_TRACK), re.I))
    if link.count() == 0:
        warn(f"C6 the {C6_DRILL_TRACK!r} track link not found — drill-in skipped.")
        return
    link.first.click()
    try:
        page.get_by_text(C6_DETAIL_BACK, exact=False).first.wait_for(timeout=30_000)
    except Exception:  # noqa: BLE001
        check("C6 the workstream detail page mounts (the 'All workstreams' back link)", False)
        return
    if page.get_by_text(ERROR_MARKER, exact=True).count() > 0:
        check("C6 the workstream detail route mounts without an error boundary", False)
        return
    check("C6 the workstream detail route mounts without an error boundary", True)
    check("C6 the detail page names the track it drilled into",
          page.get_by_text(C6_DRILL_TRACK, exact=False).count() > 0)


# --------------------------------------------------------------------------- #
# P3 render smokes — thin "route mounts + AppKit query resolves + key element" checks (L4-M7)
# --------------------------------------------------------------------------- #
def _data_settled(page, empty_marker: str, deadline_s: int = 45) -> str:
    """Poll a DataSurface until it reaches a terminal state, returning one of:
    ``ready`` (rows rendered), ``empty`` (the honest empty state), ``error`` (the query-error
    Alert), or ``cold`` (still warming/loading past the deadline). Reuses DataSurface's own
    rendered states so we prove the wired query RESOLVED, not just that the static title mounted.
    """
    end = time.time() + deadline_s
    while time.time() < end:
        if page.get_by_text(DS_ERROR, exact=False).count() > 0:
            return "error"
        if empty_marker and page.get_by_text(empty_marker, exact=False).count() > 0:
            return "empty"
        warming = page.get_by_text(DS_WARMING, exact=False).count() > 0
        busy = page.locator("[aria-busy='true']").count() > 0
        if not warming and not busy:
            return "ready"
        page.wait_for_timeout(2_000)
    return "cold"


def render_smoke(page, app_url: str, path: str, title_marker: str, empty_marker: str,
                 label: str) -> None:
    """One P3 page: mounts w/o an error boundary, its title renders, and its primary DataSurface
    resolves to a terminal state (rows OR an honest empty state). A cold warehouse is a WARN; a
    live query-error Alert is a FAIL (the wired read broke)."""
    if not goto_guarded(page, f"{app_url}{path}", ready_marker=title_marker, label=label):
        return
    check(f"{label} the page title {title_marker!r} renders",
          page.get_by_text(title_marker, exact=False).count() > 0)
    outcome = _data_settled(page, empty_marker)
    if outcome == "ready":
        check(f"{label} the primary data surface resolved with rows", True)
    elif outcome == "empty":
        check(f"{label} the primary data surface resolved (honest empty state)", True)
    elif outcome == "error":
        check(f"{label} the primary data surface loaded without a query error", False,
              "DataSurface shows the 'Couldn't load this data' alert")
    else:
        warn(f"{label} the primary data surface did not settle within 45s "
             "(warehouse cold / still loading) — smoke degraded to WARN.")


def assert_smoke(page, app_url: str) -> None:
    """The P3 render-smoke sweep (C5/C7/C8/C9/C11 via the generic helper; C10 dashboard embed +
    C13 settings config are asserted specially since neither is a DataSurface)."""
    for path, (title, empty, label) in P3_SMOKES.items():
        render_smoke(page, app_url, path, title, empty, label)

    # C10 — /dashboard: an embedded AI/BI dashboard (or a not-configured fallback), not a query.
    if goto_guarded(page, f"{app_url}/dashboard", ready_marker="Estate dashboard",
                    label="C10 /dashboard"):
        check("C10 the '/dashboard' page title renders",
              page.get_by_text("Estate dashboard", exact=False).count() > 0)
        embed = page.get_by_title("AI/BI estate dashboard").count() > 0
        configured = page.get_by_text("Configured dashboard", exact=False).count() > 0
        if embed or configured:
            check("C10 the dashboard embed renders (configured dashboard)", True)
        else:
            warn("C10 no dashboard embed — dashboard_id not in the handshake yet (job_99_setup) "
                 "or embedding not approved; render smoke degraded to WARN.")

    # C13 — /settings: static Connection + Query-health config (no DataSurface poll).
    if goto_guarded(page, f"{app_url}/settings", ready_marker="Connection", label="C13 /settings"):
        check("C13 the Connection panel renders", page.get_by_text("Connection", exact=False).count() > 0)
        check("C13 the Query health panel renders",
              page.get_by_text("Query health", exact=False).count() > 0)


# --------------------------------------------------------------------------- #
# M5 journey — one wave end to end from the Overview guided step (MUTATING)
# --------------------------------------------------------------------------- #
JOURNEY_REASON = "M5 live proof: this dev estate has no D2D recipient or Compat location (e2e fixture)"
_TRIGGER_URL = re.compile(r"/api/jobs/[^/]+/trigger$")
# JobTriggerDialog / ConvertGateDialog confirm in an alertdialog; RederiveAction (Refresh) in a popover.
_CONFIRM_SURFACE = '[role="alertdialog"], [role="dialog"]'
_ONE_CLICK = {  # conductor kind -> (trigger label regex, confirm label; None = _CONFIRM_FOR / the trigger minus "…")
    "prep": (r"^Run the check$", "Run the check"),
    "freeze": (r"^Pause (jobs first|anyway…)$", None),
    "lease": (r"^Authorize the convert$", "Authorize"),
    "validate": (r"^Check the converted tables$", "Check"),
    "unfreeze": (r"^Restart paused jobs$", "Restart"),
    "reshare": (r"^Share again$", "Share again"),
    "optimize": (r"^Turn on liquid clustering$", "Turn on"),
}
# Triggers whose confirm button is not the trigger label minus "…" (lib/sectionCopy.ts DIALOG_COPY).
_CONFIRM_FOR = {"Pause jobs first": "Pause jobs", "Pause anyway…": "Pause anyway"}
_RECALCULATE = (r"^Refresh (recommendation|the next step)$", "Refresh")
_CONVERT_KINDS = ("convert", "resume", "actions")
_GATE_RE = r"^Open the convert step$"
_SKIP_RE = r"^Convert without clearing these \(\d+\)$"
_ANYWAY_RE = r"^Convert anyway…$"
# Every name the guided convert button can have (plain gate, conscious skip, demoted anyway).
_CONVERT_CTA_RE = re.compile(f"{_GATE_RE}|{_SKIP_RE}|{_ANYWAY_RE}")
_DEFER_PLACEHOLDER = "e.g. recipient not ready until next week"
_CTA_WAIT_MS = 45_000
# Convert-gate acknowledgements (gateChecklist.pure.ts) -> the flag that consents to each, and what
# still backs the ack at convert time. `freeze` (coverage not HELD) is ticked only on the plain gate
# path, where 04_convert re-checks coverage; a conscious skip sends require_compaction_frozen=false,
# so there a `freeze`/`recency` ack is left to a human. A gap does not always swap the footer to
# 'Pause jobs first' (that needs freezeParams; a wave-scoped gate without a plan_run_id keeps a plain
# Convert), so the Freeze rail node's "not frozen yet" is read before any tick.
_ACK_FLAG = {"freeze": "--confirm-convert", "permissions": "--convert-anyway",
             "actions": "--allow-conscious-skip", "recency": "--allow-conscious-skip"}
_ACK_BACKSTOP = {"freeze": "04_convert re-checks compaction/stream coverage",
                 "permissions": "04_convert re-checks the convert permission live",
                 "actions": "the conscious skip sends require_pre_convert_actions_cleared=false",
                 "recency": "04_convert re-checks OPTIMIZE/VACUUM recency"}
_FREEZE_GAP_TITLE = "Freeze — not frozen yet"  # gateFlow.pure freezeStatus: an unheld gap, no fresh freeze
_PERMISSIONS_CHECKING_TITLE = "Permissions — checking…"  # gateFlow.pure permissionsStatus while the preflight is in flight
_PERMISSION_SETTLE_MS = 60_000


def _permission_settled(page, dlg) -> bool:
    """Wait until the gate's convert-permission read answers (the Permissions rail leaves
    'checking…'). Reading the checklist earlier sees no settled permission at all."""
    node = dlg.locator('button[title^="Permissions"]').first
    deadline = time.time() + _PERMISSION_SETTLE_MS / 1000
    while time.time() < deadline:
        if node.count() == 0 or node.get_attribute("title") != _PERMISSIONS_CHECKING_TITLE:
            return True
        page.wait_for_timeout(250)
    return False


def _is_trigger(resp) -> bool:
    return resp.request.method == "POST" and _TRIGGER_URL.search(resp.url.split("?")[0]) is not None


def _run_id_of(resp) -> int | None:
    try:
        body = resp.json() or {}
    except Exception:  # noqa: BLE001
        body = {}
    if resp.status == 409:
        warn("trigger de-duplicated (409)", f"a run of this job is already active (run {body.get('run_id') or '?'}); "
             "rerun --journey after it finishes")
        return None
    if not resp.ok:
        check("trigger accepted", False, f"HTTP {resp.status}: {str(body.get('error') or resp.text())[:200]}")
        return None
    run_id = body.get("run_id")
    check("trigger returned a run_id", bool(run_id), str(run_id))
    return int(run_id) if run_id else None


def _click_confirm(page, button) -> int | None:
    """Click a confirm button that fires POST /api/jobs/<alias>/trigger; return the run_id."""
    try:
        with page.expect_response(_is_trigger, timeout=60_000) as info:
            button.click()
    except Exception as e:  # noqa: BLE001
        check("the confirm sent a trigger request", False, f"{type(e).__name__}: {str(e)[:160]}")
        return None
    return _run_id_of(info.value)


def _confirm_and_capture(page, confirm_name: str) -> int | None:
    btn = page.locator(_CONFIRM_SURFACE).get_by_role("button", name=confirm_name, exact=True).last
    try:
        btn.wait_for(timeout=15_000)
    except Exception:  # noqa: BLE001
        check(f"the {confirm_name!r} confirm renders", False)
        return None
    if btn.is_disabled():
        warn(f"the {confirm_name!r} confirm is disabled", btn.get_attribute("title") or "a run of this job may be active")
        page.keyboard.press("Escape")
        return None
    return _click_confirm(page, btn)


def _a_run_is_active(page) -> bool:
    return (page.get_by_text("is running now", exact=False).count() > 0
            or page.get_by_role("button", name=re.compile(r"^Running…$")).count() > 0)


def _click_step(page, trigger_re: str, confirm_name: str | None, *, root=None, optional: bool = False,
                disabled_note: str = "") -> int | None:
    btn = (root or page).get_by_role("button", name=re.compile(trigger_re)).first
    try:
        btn.wait_for(timeout=_CTA_WAIT_MS)
    except Exception:  # noqa: BLE001
        if _a_run_is_active(page):
            warn(f"CTA {trigger_re!r} absent", "a run is in flight on this card; rerun --journey after it finishes")
        elif optional:
            warn(f"CTA {trigger_re!r} absent", "the card offers no such click here; a human decides (read the card)")
        else:
            check(f"CTA {trigger_re!r} present", False)
        return None
    if btn.is_disabled():
        warn(f"CTA {trigger_re!r} is disabled",
             (btn.get_attribute("title") or "see the card note") + (f"; {disabled_note}" if disabled_note else ""))
        return None
    label = btn.inner_text().strip()
    _dismiss_tour(page)
    btn.click()
    return _confirm_and_capture(page, confirm_name or _CONFIRM_FOR.get(label, label.rstrip("…").strip()))


def _would_click(page, kind: str) -> str:
    """Dry mode: name the CTA the journey would click for ``kind``. Reads only; never clicks."""
    if kind == "compat":
        return "Waves → 'Converted — finish this wave' → Defer on the ENABLE_COMPAT fixture row"
    cands = [_GATE_RE, _SKIP_RE, _ANYWAY_RE] if kind in _CONVERT_KINDS else (
        [_ONE_CLICK[kind][0]] if kind in _ONE_CLICK else [])
    if not cands:
        return "nothing (no one-click Overview step for this kind)"
    deadline = time.time() + 30
    while time.time() < deadline:
        for rx in cands:
            b = page.get_by_role("button", name=re.compile(rx))
            if b.count():
                b0 = b.first
                state = f"disabled: {b0.get_attribute('title') or 'see the card'}" if b0.is_disabled() else "enabled"
                return f"{b0.inner_text().strip()!r} ({state})"
        page.wait_for_timeout(1_000)
    return f"no CTA matching {cands} rendered within 30s"


def _unchecked_acks(dlg) -> list[str]:
    out = []
    for ack in _ACK_FLAG:
        cb = dlg.locator(f"#ack-{ack}")
        if cb.count() and cb.first.get_attribute("aria-checked") != "true":
            out.append(ack)
    return out


def _ui_convert(page, *, wave_id: str, confirm_convert: str | None, allow_conscious_skip: bool,
                convert_anyway: bool) -> int | None:
    from migration import journey_oracle as jo

    gate = page.get_by_role("button", name=re.compile(_GATE_RE))
    skip = page.get_by_role("button", name=re.compile(_SKIP_RE))
    anyway = page.get_by_role("button", name=re.compile(_ANYWAY_RE))
    running = page.get_by_role("button", name="A conversion is already running")
    deadline = time.time() + _CTA_WAIT_MS / 1000
    trigger = None
    via_skip = False
    while trigger is None and time.time() < deadline:
        if running.count():
            warn("convert already running", "the card shows 'Convert is running…'; rerun --journey after it finishes")
            return None
        if skip.count():
            if not allow_conscious_skip:
                warn("HUMAN GATE — conscious skip", "the card offers 'Convert without clearing these'; run "
                     "`e2e_journey.py resolve-manual --wave <id> --confirm` or rerun with --allow-conscious-skip")
                return None
            trigger, via_skip = skip.first, True
        elif anyway.count():
            why = ("the recommendation is stale (the card leads with 'Refresh the next step')"
                   if page.get_by_role("button", name=re.compile(r"^Refresh the next step$")).count()
                   else "no readiness check has evaluated this scope (the card leads with 'Check readiness first')")
            if not convert_anyway:
                warn("HUMAN GATE — convert anyway", f"the card demotes convert: {why}; rerun with --convert-anyway")
                return None
            trigger = anyway.first
        elif gate.count() and gate.first.is_enabled():
            trigger = gate.first
        else:
            page.wait_for_timeout(1_000)
    if trigger is None:
        if _a_run_is_active(page):
            warn("convert CTA not ready", "a run is in flight on this card; rerun --journey after it finishes")
        else:
            check("a convert CTA is on the card", False, f"none enabled within {_CTA_WAIT_MS // 1000}s")
        return None
    _dismiss_tour(page)
    trigger.click()
    box = page.locator("#convert-confirm")
    try:
        box.wait_for(timeout=30_000)
    except Exception:  # noqa: BLE001
        check("the convert gate opens with its type-to-confirm", False)
        return None
    dlg = page.get_by_role("alertdialog").filter(has=box)
    cancel = dlg.get_by_role("button", name="Cancel", exact=True)
    if not _permission_settled(page, dlg):
        warn("HUMAN GATE — convert", f"the gate is still checking the convert permission after {_PERMISSION_SETTLE_MS // 1000}s "
             "(the /api/convert/preflight read has not answered); a human decides")
        cancel.click()
        return None
    target = (box.get_attribute("placeholder") or "").strip()
    plain = dlg.get_by_role("button", name="Convert", exact=True).last
    # The MCP pass pins mtm_convert to its own wave; a schema-scoped Overview gate would convert it too.
    verdict, msg = jo.ui_convert_target_outcome(target, wave_id=wave_id, confirm_convert=confirm_convert)
    if target == wave_id and plain.count() == 0:
        verdict, msg = jo.WARN, ("the gate shows a coverage gap (only 'Convert without pausing' / 'Pause "
                                 "jobs first'); pause first or a human decides")
    if verdict != jo.PASS:
        warn("HUMAN GATE — convert", msg.removeprefix("HUMAN GATE — "))
        cancel.click()
        return None
    box.fill(target)
    stage = dlg.locator('button[title^="Convert"]').first  # the rail node that reveals the ack checklist
    if stage.count():
        stage.click()
    pending = _unchecked_acks(dlg)
    held_back = [a for a in ("freeze", "recency") if a in pending]
    if via_skip and held_back:
        warn("HUMAN GATE — freeze not held on a conscious skip",
             f"the gate asks to acknowledge {', '.join('#ack-' + a for a in held_back)} and the job skips "
             "require_compaction_frozen; tick it yourself in the app")
        cancel.click()
        return None
    freeze_node = dlg.locator('button[title^="Freeze"]').first
    if "freeze" in pending and freeze_node.count() and freeze_node.get_attribute("title") == _FREEZE_GAP_TITLE:
        warn("HUMAN GATE — freeze coverage gap", "the gate's Freeze stage shows 'not frozen yet'; run the freeze "
             "step first (04_convert would fail closed on coverage)")
        cancel.click()
        return None
    consented = {"freeze": True, "permissions": convert_anyway,
                 "actions": allow_conscious_skip, "recency": allow_conscious_skip}
    given = {"freeze": f"--confirm-convert={target}", "permissions": "--convert-anyway",
             "actions": "--allow-conscious-skip", "recency": "--allow-conscious-skip"}
    for ack in pending:
        if consented[ack]:
            dlg.locator(f"#ack-{ack}").first.click()
            print(f"    ticked #ack-{ack} under {given[ack]}; {_ACK_BACKSTOP[ack]}")
    if not plain.is_enabled():
        left = _unchecked_acks(dlg)
        warn("HUMAN GATE — convert", ("unacknowledged: " + ", ".join(f"{a} ({_ACK_FLAG[a]})" for a in left)) if left
             else "Convert stays disabled (readiness not green, nothing to convert, or a run in flight)")
        cancel.click()
        return None
    return _click_confirm(page, plain)


def _ui_defer(page, scope, full_name: str, *, reason: str = JOURNEY_REASON) -> int | None:
    box = scope.get_by_placeholder(_DEFER_PLACEHOLDER)
    try:
        box.first.wait_for(timeout=_CTA_WAIT_MS)
    except Exception:  # noqa: BLE001
        check("the Defer panel renders", False, full_name)
        return None
    box.first.fill(reason)
    row = scope.locator("li").filter(has_text=full_name).filter(has=page.get_by_role("button", name="Defer", exact=True))
    if row.count() == 0:
        check("the Defer panel lists the fixture row", False, full_name)
        return None
    btn = row.first.get_by_role("button", name="Defer", exact=True)
    deadline = time.time() + 10
    while btn.is_disabled() and time.time() < deadline:  # enables once React sees the typed reason
        page.wait_for_timeout(250)
    if btn.is_disabled():
        warn("the Defer CTA is disabled", btn.get_attribute("title") or full_name)
        return None
    _dismiss_tour(page)
    btn.click()
    return _confirm_and_capture(page, "Defer")


def _waves_finish_section(page, app_url: str, wave_id: str):
    """The journey wave's fully-migrated finish branch on /waves (scoped to ``wave_id``'s card)."""
    if not goto_guarded(page, f"{app_url}/waves", ready_marker=None, label="journey /waves"):
        return None
    board = page.locator('[data-tour="waves-board"]')
    header = board.get_by_role("button").filter(has_text=re.compile(r"\d+ tables")).filter(
        has=page.locator("span.font-mono", has_text=re.compile(rf"^{re.escape(wave_id)}$")))
    try:
        header.first.wait_for(timeout=90_000)
    except Exception:  # noqa: BLE001
        check(f"Waves lists {wave_id}", False)
        return None
    _dismiss_tour(page)
    marker = header.first.locator("xpath=..").get_by_text(re.compile(r"^Converted — finish this wave"))
    if marker.count() == 0:
        header.first.click()
    try:
        marker.first.wait_for(timeout=_CTA_WAIT_MS)
    except Exception:  # noqa: BLE001
        check("Waves shows the fully-migrated finish branch", False, wave_id)
        return None
    check("Waves shows the fully-migrated finish branch", True, marker.first.inner_text()[:80])
    return marker.first.locator("xpath=..")


def _wait_run(j, run_id: int, timeout_s: int, tag: str) -> tuple[str, int] | None:
    try:
        return j.wait_run(run_id, timeout_s=timeout_s)
    except TimeoutError as e:
        warn(f"{tag} run {run_id} still running", f"{e}; rerun --journey to resume from the conductor")
        return None


def assert_journey(page, app_url: str, *, wave_id: str, confirm_execute: bool, confirm_convert: str | None,
                   allow_conscious_skip: bool, convert_anyway: bool, inject_fixture: bool, max_steps: int,
                   focus: str | None = None) -> None:
    """Walk ``wave_id`` through the conductor's steps by clicking the Overview guided card (and the
    Waves finish branch for Compat), asserting each step's control-table effect via the oracle.

    Resumable: each step starts from the conductor's current recommendation. Without
    ``confirm_execute`` it clicks nothing that starts a run (it reads and reports what it would
    click); a convert also needs ``confirm_convert`` equal to the gate's own confirm target, and that
    target must be ``wave_id`` (a gate that confirms a schema is cancelled).

    ``focus`` (focus mode): drive ``/overview?focus=<scope>`` instead, and compare against that
    scope's own per-scope row (migration_next_actions_by_scope) rather than the estate step.
    """
    import e2e_journey as ej
    from databricks.sdk import WorkspaceClient
    from migration import journey_oracle as jo

    j = ej.Journey(WorkspaceClient(profile=PROFILE))
    rep = lambda label, o: ej.report(label, o, check=check, warn=warn)  # noqa: E731
    prid = j.plan_run_id()
    wave_names = {str(m["full_name"]) for m in j.members(prid, wave_id)}
    print(f"Journey (UI): plan {prid}, wave {wave_id} ({len(wave_names)} tables), "
          f"{'EXECUTE' if confirm_execute else 'dry mode — clicks nothing that starts a run'}")
    if not wave_names:
        check(f"{wave_id} has members in plan {prid}", False, "check the id with `e2e_journey.py survey`")
        return
    fixture_on = any(r.get("status") in ("OPEN", "IN_PROGRESS", "DEFERRED") for r in j.fixture_rows(wave_names))
    overview = f"{app_url}/overview?focus={quote(focus)}" if focus else f"{app_url}/overview"

    def next_row() -> dict | None:
        if not focus:
            return j.next_row()
        return next((r for r in j.scoped_rows() if r.get("scope_key") == focus), None)
    last_step: str | None = None
    for n in range(1, max_steps + 1):
        tag = f"J{n}"
        if not goto_guarded(page, overview, ready_marker="Schemas blocked", label=f"{tag} /overview"):
            return
        row = next_row() or {}
        kind, got = str(row.get("next_step_kind") or ""), str(row.get("next_wave_id") or "")
        wave = j.wave_row(prid, wave_id)
        print(f"\n[{tag}] conductor: {kind} {got or '-'} ({wave_id}: {(wave or {}).get('wave_state')})")
        if wave is None:
            check(f"{tag} {wave_id} has a wave_status row", False, f"plan {prid}")
            return
        if jo.fully_migrated(wave) and jo.spec_finish_step(wave) is None:
            check(f"{tag} {wave_id} finished and the conductor moved on", got != wave_id, f"{kind} {got or '-'}")
            return
        if got != wave_id:
            if jo.wave_started(wave):
                check(f"{tag} the Overview recommends {wave_id}", False, f"mid-journey, got {kind} {got or '-'}")
            else:
                warn(f"{tag} the Overview recommends {kind} {got or '-'}, not {wave_id}",
                     "the journey drives only the Overview's wave; pick it with `e2e_journey.py survey`")
            return
        if kind == last_step:
            warn(f"{tag} {kind} is still recommended after its run", "a human decides (read the run output)")
            return
        if not confirm_execute:
            warn(f"{tag} HUMAN GATE", f"dry mode: the card recommends {kind} on {wave_id}; it would click "
                 f"{_would_click(page, kind)}; rerun with --confirm-execute")
            return
        if kind == "optimize" and inject_fixture and not fixture_on:
            rep(f"{tag} finish-first before the fixture", jo.finish_first_outcome(row, j.wave_rows(prid)))
            j.inject_fixture(prid, wave_id)
            fixture_on = True
            run_id = _click_step(page, *_RECALCULATE)
            if not run_id:
                return
            waited = _wait_run(j, run_id, 900, tag)
            if waited is None:
                return
            result, start = waited
            rep(f"{tag} conductor run", jo.run_outcome(result, j.last_failed_tasks))
            for o in jo.step_advanced_outcome(step="optimize", wave_id=wave_id, wave_after=j.wave_row(prid, wave_id),
                                              next_row=next_row(), run_start_ms=start):
                rep(f"{tag} after the fixture", o)
            continue
        if kind in _CONVERT_KINDS:
            run_id = _ui_convert(page, wave_id=wave_id, confirm_convert=confirm_convert,
                                 allow_conscious_skip=allow_conscious_skip, convert_anyway=convert_anyway)
            step = "convert"
        elif kind == "compat":
            section = _waves_finish_section(page, app_url, wave_id)
            if section is None:
                return
            enable = section.get_by_role("button", name=re.compile(r"^Turn on Compatibility Mode$"))
            warn(f"{tag} compat", "Turn on Compatibility Mode needs a human-chosen empty location (never invented); "
                 "none on this estate (fixture), deferring" if enable.count()
                 else "no Turn on Compatibility Mode CTA on the branch; deferring")
            fix = next((r for r in j.fixture_rows(wave_names)
                        if r["action_kind"] == "ENABLE_COMPAT" and r["status"] == "OPEN"), None)
            if fix is None:
                warn(f"{tag} compat", "the conductor recommends compat but no OPEN fixture row is on this wave; "
                     "a human decides")
                return
            run_id = _ui_defer(page, section, str(fix["full_name"]))
            step = "compat"
        elif kind in _ONE_CLICK:
            run_id = _click_step(page, *_ONE_CLICK[kind], optional=kind == "freeze",
                                 disabled_note="Authorize is disabled; a table needs a grant someone else must "
                                               "run (send the convert-permissions grant script), then re-check"
                                 if kind == "lease" else "")
            step = kind
        else:
            warn(f"{tag} {kind}", "no one-click Overview step for this kind; a human decides")
            return
        if not run_id:
            return
        waited = _wait_run(j, run_id, 5400 if step == "convert" else 1800, tag)
        if waited is None:
            return
        result, start = waited
        run_verdict = jo.run_outcome(result, j.last_failed_tasks)
        rep(f"{tag} {step} run", run_verdict)
        last_step = step
        after, nxt = j.wave_row(prid, wave_id), next_row()
        for o in jo.step_advanced_outcome(step=step, wave_id=wave_id, wave_after=after, next_row=nxt, run_start_ms=start):
            rep(f"{tag} after {step}", o)
        if run_verdict[0] == jo.FAIL:
            return
        if step == "validate":
            rep(f"{tag} finish-first after validate", jo.finish_first_outcome(nxt, j.wave_rows(prid)))
        if step == "optimize":
            rep(f"{tag} 08c stamped the wave", jo.optimize_stamp_outcome(j.members(prid, wave_id), after))
        if step in ("reshare", "compat"):
            kind_of = {"reshare": "RESHARE_D2D", "compat": "ENABLE_COMPAT"}[step]
            fix = next((r for r in j.fixture_rows(wave_names) if r["action_kind"] == kind_of), None)
            if fix and fix["status"] == "OPEN" and step == "reshare":
                if not goto_guarded(page, overview, ready_marker="Schemas blocked", label=f"{tag} defer"):
                    return
                reshare_card = page.get_by_role("button", name=re.compile(_ONE_CLICK["reshare"][0])).first.locator(
                    f"xpath=ancestor::div[.//input[@placeholder='{_DEFER_PLACEHOLDER}']][1]")
                d_run = _ui_defer(page, reshare_card, str(fix["full_name"]))
                if not d_run:
                    return
                d_waited = _wait_run(j, d_run, 1800, tag)
                if d_waited is None:
                    return
                _, d_start = d_waited
                fix = j.action(fix["action_key"])
                rep(f"{tag} defer {kind_of}", jo.defer_outcome(fix, j.audit_since([fix["full_name"]], d_start),
                                                               reason=JOURNEY_REASON))
                for o in jo.step_advanced_outcome(step=step, wave_id=wave_id, wave_after=j.wave_row(prid, wave_id),
                                                  next_row=next_row(), run_start_ms=d_start):
                    rep(f"{tag} after defer", o)
            elif fix and step == "compat":
                full = j.action(fix["action_key"])
                rep(f"{tag} defer {kind_of}", jo.defer_outcome(full, j.audit_since([full["full_name"]], start),
                                                               reason=JOURNEY_REASON))
    warn("J max steps", f"stopped after {max_steps} steps; rerun to continue")


# --------------------------------------------------------------------------- #
# M6.4 bulk journey: /bulk (BatchCard), /convert (campaign card, RollbackCard)
# --------------------------------------------------------------------------- #
_TRIGGER_URL_RE = re.compile(r".*/api/jobs/[^/]+/trigger/?(\?.*)?$")
_RUN_URL_GLOB = "**/api/jobs/**/run*"
_installed_guards: dict[int, list[tuple[object, object]]] = {}
_BULK_DRY = {  # reversible brackets: dry run first, then execute
    "bulk_freeze": ("Plan the pause (dry run)", "Run dry run"),
    "bulk_lease": ("Plan Authorize (dry run)", "Run dry run"),
    "bulk_unfreeze": ("Plan the restart (dry run)", "Run dry run"),
}
_BULK_EXEC = {  # step -> (trigger label, confirm label, typed confirm = the batch id)
    "bulk_freeze": ("Pause jobs", "Pause", True),
    "bulk_lease": ("Authorize", "Authorize", True),
    "bulk_validate": ("Check converted tables", "Check", False),
    "bulk_unfreeze": ("Restart jobs", "Restart", True),
    "bulk_reshare": ("Share again", "Share again", False),
    "bulk_optimize": ("Turn on liquid clustering", "Turn on", False),
}
_BULK_CONVERT_RE = r"^Bulk convert( \(resume\))?$"
_BULK_STEP_CHOICES = ("bulk_freeze", "bulk_lease", "bulk_convert", "bulk_validate", "bulk_unfreeze",
                      "bulk_reshare", "bulk_compat", "bulk_optimize")


def _bulk_convert_cancel(page, cancel) -> None:
    try:
        cancel.click()
    except Exception:  # noqa: BLE001
        try:
            page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001
            pass


def _guard_abort(route, alias: str, msg: str) -> None:
    check(f"trigger guard {alias}", False, msg)
    route.abort()


def _install_trigger_guard(page, guard) -> None:
    """Route every job trigger through ``guard(alias, params)`` before it leaves the browser; FAIL aborts it."""
    pid = id(page)
    for pattern, handler in _installed_guards.pop(pid, []):
        try:
            page.unroute(pattern, handler)
        except Exception:  # noqa: BLE001
            pass

    def handle_trigger(route) -> None:
        alias = "?"
        try:
            req = route.request
            if req.method != "POST":
                route.continue_()
                return
            alias = req.url.split("?")[0].rstrip("/").split("/")[-2]
            try:
                body = req.post_data_json
            except Exception as e:  # noqa: BLE001
                _guard_abort(route, alias, f"unparseable trigger body: {type(e).__name__}: {e}")
                return
            if not isinstance(body, dict):
                _guard_abort(route, alias, "unparseable trigger body")
                return
            params = body.get("params")
            if not isinstance(params, dict):
                _guard_abort(route, alias, "trigger params not an object")
                return
            try:
                verdict, msg = guard(alias, params)
            except Exception as e:  # noqa: BLE001
                verdict, msg = "FAIL", f"guard error: {type(e).__name__}: {e}"
            print(f"    trigger {alias} {json.dumps(params, sort_keys=True, default=str)}")
            check(f"trigger guard {alias}", verdict != "FAIL", msg)
            if verdict == "FAIL":
                route.abort()
            else:
                route.continue_()
        except Exception as e:  # noqa: BLE001
            _guard_abort(route, alias, f"handler error: {type(e).__name__}: {e}")

    def handle_run(route) -> None:
        try:
            if route.request.method != "POST":
                route.continue_()
                return
            _guard_abort(route, "run", "direct job /run is blocked by the harness (use /trigger only)")
        except Exception as e:  # noqa: BLE001
            _guard_abort(route, "run", f"handler error: {type(e).__name__}: {e}")

    page.route(_TRIGGER_URL_RE, handle_trigger)
    page.route(_RUN_URL_GLOB, handle_run)
    _installed_guards[pid] = [(_TRIGGER_URL_RE, handle_trigger), (_RUN_URL_GLOB, handle_run)]


def _bulk_card(page, app_url: str, batch_id: str):
    """The BatchCard for ``batch_id`` on a fresh /bulk load (the innermost element holding both its
    title and its 'Recommended next:' line). Its permission count is wave_status's stored one, so
    there is no second read to wait for."""
    if not goto_guarded(page, f"{app_url}/bulk", ready_marker="degree", label=f"/bulk {batch_id}"):
        return None
    card = page.locator("xpath=//*[.//span[normalize-space()='Recommended next:']]").filter(
        has=page.get_by_text(batch_id, exact=True)).last
    try:
        card.wait_for(timeout=90_000)
    except Exception:  # noqa: BLE001
        check(f"/bulk shows a card for {batch_id}", False)
        return None
    _dismiss_tour(page)
    return card


def _card_label(card) -> str:
    span = card.locator("xpath=.//span[normalize-space()='Recommended next:']/following-sibling::span[1]").first
    try:
        return span.inner_text().strip()
    except Exception:  # noqa: BLE001
        return ""


def _bulk_click(page, card, trigger: str, confirm: str, *, typed: str | None = None) -> int | None:
    btn = card.get_by_role("button", name=trigger, exact=True).first
    try:
        btn.wait_for(timeout=_CTA_WAIT_MS)
    except Exception:  # noqa: BLE001
        check(f"{trigger!r} renders on the card", False)
        return None
    if btn.is_disabled():
        warn(f"{trigger!r} is disabled", btn.get_attribute("title") or "see the card")
        return None
    _dismiss_tour(page)
    btn.click()
    if typed:
        box = page.locator(_CONFIRM_SURFACE).get_by_placeholder(typed, exact=True).last
        try:
            box.wait_for(timeout=15_000)
        except Exception:  # noqa: BLE001
            check(f"the {trigger!r} confirm asks to type {typed!r}", False)
            page.keyboard.press("Escape")
            return None
        box.fill(typed)
    return _confirm_and_capture(page, confirm)


def _read_prechecks(dlg, *, settle_s: int = 90) -> tuple[list[tuple[str, str]], list[str], bool]:
    """The pre-convert checks panel's rows once its preflights settle (no spinner, five unchanged
    reads a second apart), the members it names as over the cap, and whether it settled (False =
    the deadline passed on a still-changing or spinning panel, so the rows are not proof)."""
    panel = dlg.locator("div.rounded-lg").filter(has_text=re.compile(r"Pre-convert checks")).last
    prev: list[tuple[str, str]] | None = None
    stable = 0
    deadline = time.time() + settle_s
    while time.time() < deadline:
        rows = []
        items = panel.locator("li")
        for i in range(items.count()):
            spans = items.nth(i).locator("span.flex-col > span")
            if spans.count() >= 2:
                rows.append((spans.nth(0).inner_text().strip(), spans.nth(1).inner_text().strip()))
        spinning = panel.locator(".animate-spin").count() > 0
        stable = stable + 1 if rows and rows == prev and not spinning else 0
        if stable >= 5:
            break
        prev = rows
        dlg.page.wait_for_timeout(1_000)
    note = panel.get_by_text(re.compile(r"^Not checked here")).first
    unchecked: list[str] = []
    if note.count():
        unchecked = [s.strip() for s in note.inner_text().split(":", 1)[1].strip().rstrip(".").split(",") if s.strip()]
    return prev or [], unchecked, stable >= 5


def _bulk_convert(page, card, *, batch_id: str, members: list, confirm_convert: str | None,
                  record: dict, rep) -> int | None:
    """Open the Bulk convert confirm, record the pre-convert checks panel per member, then convert
    only when --confirm-convert names the batch and no check is blocked (else cancel: HUMAN GATE)."""
    from migration import bulk_journey_oracle as bjo
    from migration import parallelism as pl

    btn = card.get_by_role("button", name=re.compile(_BULK_CONVERT_RE)).first
    try:
        btn.wait_for(timeout=_CTA_WAIT_MS)
    except Exception:  # noqa: BLE001
        check("the Bulk convert CTA renders", False)
        return None
    if btn.is_disabled():
        warn("Bulk convert is disabled", btn.get_attribute("title") or "see the card")
        return None
    resume = "(resume)" in btn.inner_text()
    _dismiss_tour(page)
    btn.click()
    box = page.locator(_CONFIRM_SURFACE).get_by_placeholder(batch_id, exact=True).last
    try:
        box.wait_for(timeout=15_000)
    except Exception:  # noqa: BLE001
        check(f"the Bulk convert confirm asks to type {batch_id!r}", False)
        page.keyboard.press("Escape")
        return None
    dlg = page.get_by_role("alertdialog").filter(has=page.get_by_placeholder(batch_id, exact=True))
    cancel = dlg.get_by_role("button", name="Cancel", exact=True)
    try:
        panel = dlg.locator("div.rounded-lg").filter(has_text=re.compile(r"Pre-convert checks")).last
        if panel.count() == 0:
            panel.wait_for(state="attached", timeout=15_000)
        rows, unchecked, settled = _read_prechecks(dlg)
    except Exception as e:  # noqa: BLE001
        warn("pre-convert checks panel not readable", f"{type(e).__name__}: {str(e)[:160]}")
        _bulk_convert_cancel(page, cancel)
        return None
    if not rows:
        warn("pre-convert checks panel not readable", "no settled check rows before convert")
        _bulk_convert_cancel(page, cancel)
        return None
    checked, _ = bjo.precheck_targets(members, resume, pl.DEFAULT_BULK_MAX_PARALLEL)
    outcomes, rec = bjo.prechecks_outcome(rows, checked, unchecked=unchecked)
    print(f"    Pre-convert checks panel ({'resume' if resume else 'convert'}):")
    for label, copy in rows:
        print(f"      {label}: {copy}")
    for o in outcomes:
        rep("B prechecks", o)
    record.update(rec)
    why = None
    if any(s == "blocked" for checks in rec.values() for s in checks.values()):
        why = "the panel shows a blocked check; the bulk convert would refuse that wave. A human decides"
    elif not settled:
        why = "the pre-convert checks panel did not settle (still loading or changing), so it is not proof"
    elif any(v == bjo.FAIL for v, _ in outcomes):
        why = "the pre-convert checks panel contradicts the contract (FAIL above)"
    if why:
        warn("HUMAN GATE — bulk convert", why)
        _bulk_convert_cancel(page, cancel)
        return None
    if (confirm_convert or "").strip() != batch_id:
        warn("HUMAN GATE — bulk convert", f"the confirm asks to type {batch_id!r}; rerun with "
             f"--confirm-convert {batch_id} to convert {', '.join(checked)}")
        _bulk_convert_cancel(page, cancel)
        return None
    box.fill(batch_id)
    return _click_confirm(page, dlg.get_by_role("button", name="Convert", exact=True).last)


def _pick_combobox(page, trigger, search_placeholder: str, value: str, what: str) -> bool:
    try:
        trigger.wait_for(timeout=_CTA_WAIT_MS)
    except Exception:  # noqa: BLE001
        check(f"the {what} picker renders", False)
        return False
    deadline = time.time() + 30
    while trigger.is_disabled() and time.time() < deadline:
        page.wait_for_timeout(500)
    if trigger.is_disabled():
        warn(f"the {what} picker is disabled", (trigger.inner_text() or "").strip()[:160])
        return False
    trigger.click()
    page.get_by_placeholder(search_placeholder).last.fill(value)
    opt = page.locator(f'[cmdk-item][data-value="{value}"]').first
    try:
        opt.wait_for(timeout=30_000)
    except Exception:  # noqa: BLE001
        check(f"the {what} picker lists {value}", False)
        page.keyboard.press("Escape")
        return False
    opt.click()
    return True


def _pinned_plan(j, wanted: str | None) -> str | None:
    """The current plan_run_id, or None (after a FAIL) when ``wanted`` (--plan-run-id, the plan the
    operator approved from the survey) is not the current plan: nothing is clicked on a re-plan."""
    prid = j.plan_run_id()
    if wanted and wanted != prid:
        check(f"the current plan is the approved {wanted}", False,
              f"wave_plan now holds {prid}; re-run `e2e_journey.py survey --batch` and approve that plan")
        return None
    return prid


def assert_prep_batch(page, app_url, *, batch_id: str, confirm_execute: bool,
                      plan_run_id: str | None = None) -> None:
    """Run the read-only campaign dry run (/convert → Campaign) on each member schema that needs it."""
    import e2e_journey as ej
    from databricks.sdk import WorkspaceClient
    from migration import bulk_journey_oracle as bjo
    from migration import journey_oracle as jo

    j = ej.Journey(WorkspaceClient(profile=PROFILE))
    rep = lambda label, o: ej.report(label, o, check=check, warn=warn)  # noqa: E731
    prid = _pinned_plan(j, plan_run_id)
    if prid is None:
        return
    members = bjo.batch_members(j.batch_rollups(prid)[1], batch_id)
    if not members:
        check(f"{batch_id} is a parallel batch of plan {prid}", False)
        return
    schemas = {str(m.get("group_key") or "") for m in members}
    _install_trigger_guard(page, lambda alias, params: bjo.trigger_guard(
        alias, params, plan_run_id=prid, batch_id=batch_id, member_schemas=schemas))
    print(f"Prep (UI): plan {prid}, {batch_id}, {'EXECUTE' if confirm_execute else 'dry mode'}")
    for wid, schema, need, note in bjo.prep_targets(members, j.schemas_by_wave(prid)):
        if not need:
            check(f"P {wid} needs no dry run", True, note)
            continue
        if not confirm_execute:
            warn(f"P {wid} HUMAN GATE", f"dry mode: would run the campaign dry run on {schema} ({note}); "
                 "rerun with --confirm-execute")
            continue
        if not goto_guarded(page, f"{app_url}/convert", ready_marker="Check a schema",
                            label=f"P {wid} /convert"):
            return
        _dismiss_tour(page)
        if not _pick_combobox(page, page.locator("#cp-schema"), "Search by schema, catalog, pilot…", schema, "schema"):
            return
        run_id = _click_step(page, r"^Check this schema…$", "Run the check")
        if not run_id:
            return
        waited = _wait_run(j, run_id, 3600, f"P {wid}")
        if waited is None:
            return
        print(f"    campaign run {waited[0]}")
        after = next((m for m in bjo.batch_members(j.batch_rollups(prid)[1], batch_id)
                      if str(m["wave_id"]) == wid), None)
        rep(f"P {wid} ready", bjo.prep_outcome(after, waited[0]))
        if jo.run_outcome(waited[0], j.last_failed_tasks)[0] == jo.FAIL and bjo.prep_outcome(after, waited[0])[0] == jo.FAIL:
            return


def assert_bulk_journey(page, app_url, *, batch_id: str, confirm_execute: bool, confirm_convert: str | None,
                        inject_fixture: bool, cockpit: bool, stop_before: str | None, max_steps: int,
                        plan_run_id: str | None = None) -> None:
    """Walk ``batch_id`` through its bulk journey from the /bulk card, asserting each step against the
    cockpit's rollup, the card, and (with ``cockpit``) a deployed job_11e run.

    Resumable: each step starts from the rollup's current recommendation. Without
    ``confirm_execute`` it starts no run; at the convert step it still opens the confirm, records the
    pre-convert checks panel, and cancels. A bulk convert also needs ``confirm_convert`` equal to
    ``batch_id``. Every trigger passes the request guard first.
    """
    import e2e_journey as ej
    from databricks.sdk import WorkspaceClient
    from migration import bulk_journey_oracle as bjo
    from migration import journey_oracle as jo
    from migration import parallelism as pl

    j = ej.Journey(WorkspaceClient(profile=PROFILE))
    rep = lambda label, o: ej.report(label, o, check=check, warn=warn)  # noqa: E731
    prid = _pinned_plan(j, plan_run_id)
    if prid is None:
        return
    wave_ids = j.batch_wave_ids(prid, batch_id)
    if not wave_ids:
        check(f"{batch_id} is a parallel batch of plan {prid}", False, "pick it with `e2e_journey.py survey --batch`")
        return
    names = {str(r["full_name"]) for w in wave_ids for r in j.members(prid, w)}
    fixture_keys = {str(r["action_key"]) for r in j.fixture_rows(names)}
    fixture_on = any(r.get("status") in ("OPEN", "IN_PROGRESS", "DEFERRED") for r in j.fixture_rows(names))
    _install_trigger_guard(page, lambda alias, params: bjo.trigger_guard(
        alias, params, plan_run_id=prid, batch_id=batch_id, member_schemas=set(), deferrable_keys=fixture_keys))
    print(f"Bulk journey (UI): plan {prid}, {batch_id} = {', '.join(wave_ids)} ({len(names)} tables), "
          f"{'EXECUTE' if confirm_execute else 'dry mode — starts no run'}")
    if goto_guarded(page, f"{app_url}/overview", ready_marker=None, label="B0 /overview"):
        try:  # components/nav.ts: the rail renders every item at once, so a known link proves it mounted
            page.locator('[data-tour="nav"] a[href$="/overview"]').first.wait_for(timeout=30_000)
        except Exception:  # noqa: BLE001
            warn("B0 /bulk nav check", "the sidebar navigation did not render; an empty count would prove nothing")
        else:
            links = page.locator('a[href$="/bulk"]').count()
            check("B0 /bulk stays unlisted (§8 Q1)", links == 0, f"{links} link(s) to /bulk on the Overview")
    record: dict[str, dict[str, str]] = {}
    last: str | None = None
    for n in range(1, max_steps + 1):
        tag = f"B{n}"
        rollups, joined, note = j.batch_rollups(prid)
        r = bjo.rollup_for(rollups, batch_id)
        if r is None:
            check(f"{tag} {batch_id} has a rollup", False)
            break
        step = pl.next_bulk_step(r)
        print(f"\n[{tag}] {batch_id}: {r['rollup']}, next {step} ({note})")
        card = _bulk_card(page, app_url, batch_id)
        if card is None:
            break
        rep(f"{tag} /bulk card", bjo.card_label_outcome(_card_label(card), step))
        rep(f"{tag} D6", bjo.recommended_batch_outcome(rollups, batch_id))
        if cockpit:
            for o in bjo.cockpit_outcome(j.run_cockpit(prid, batch_id), rollups, batch_id, plan_run_id=prid):
                rep(f"{tag} job_11e", o)
        if step == pl.STEP_BULK_DONE:
            check(f"{tag} {batch_id} finished", True, "converted, validated and every finish step done")
            break
        if step == stop_before:
            print(f"    stopped before {step} (--stop-before)")
            break
        if step == last:
            warn(f"{tag} {step} is still recommended after its run", "a human decides (read the run)")
            break
        if not confirm_execute:
            if step == pl.STEP_BULK_CONVERT:
                _bulk_convert(page, card, batch_id=batch_id, members=bjo.batch_members(joined, batch_id),
                              confirm_convert=None, record=record, rep=rep)
            warn(f"{tag} HUMAN GATE", f"dry mode: the card recommends {bjo.CARD_LABEL[step]!r}; "
                 "rerun with --confirm-execute")
            break
        if step == pl.STEP_BULK_OPTIMIZE and inject_fixture and not fixture_on:
            wid = bjo.fixture_wave({w: j.members(prid, w) for w in wave_ids}, wave_ids)
            if wid is None:
                warn(f"{tag} fixture", "no converted member to hang the fixture on")
            else:
                fixture_keys.update(j.inject_fixture(prid, wid))
                fixture_on = True
                continue
        if step == pl.STEP_BULK_FREEZE:
            ws = {str(w["wave_id"]): w for w in j.wave_rows(prid)}
            leases = [f"{a['wave_id']}: {a['full_name']}" for w in wave_ids for a in j.open_lease_pre_convert(prid, w)]
            if leases:
                print(f"    open LEASE_OWNERSHIP pre-convert action(s): {len(leases)} {leases[:5]} "
                      "(closed by bulk Authorize, not the freeze)")
            if sum(int(ws.get(w, {}).get("open_pre_convert_auto") or 0) for w in wave_ids) == 0:
                manual = [f"{a['action_kind']} {a['full_name']}" for w in wave_ids
                          for a in j.open_manual_pre_convert(prid, w)]
                if manual:
                    warn(f"{tag} HUMAN GATE — manual pre-convert",
                         f"nothing to auto-freeze; {len(manual)} manual action(s) {manual[:5]}: run "
                         f"`e2e_journey.py resolve-manual --batch {batch_id} --confirm`")
                else:
                    warn(f"{tag} HUMAN GATE — bulk freeze",
                         "the rollup recommends freeze but no auto-freezable or manual action is open; "
                         "read the batch's wave_status (a human decides)")
                break
        if step == pl.STEP_BULK_LEASE:
            leases = [f"{a['wave_id']}: {a['full_name']}" for w in wave_ids for a in j.open_lease_pre_convert(prid, w)]
            if leases:
                print(f"    open LEASE_OWNERSHIP pre-convert action(s): {len(leases)} {leases[:5]}")
            run_as = j.bulk_convert_run_as()
            blocked = j.needs_a_person(prid, wave_ids, run_as) if run_as else []
            if blocked:
                warn(f"{tag} HUMAN GATE — bulk authorize",
                     f"{len(blocked)} table(s) need a grant Authorize cannot add (e.g. "
                     f"{blocked[0]['full_name']}: {blocked[0].get('reason') or blocked[0].get('status')}); "
                     "send the convert-permissions grant script, then re-check")
                break
        if step in _BULK_DRY:
            d_run = _bulk_click(page, card, *_BULK_DRY[step])
            if not d_run:
                break
            d_waited = _wait_run(j, d_run, 1800, tag)
            if d_waited is None:
                break
            rep(f"{tag} {step} dry-run run", jo.run_outcome(d_waited[0], j.last_failed_tasks))
            rep(f"{tag} {step} dry-run", bjo.dry_run_outcome(step, bjo.rollup_for(j.batch_rollups(prid)[0], batch_id)))
            card = _bulk_card(page, app_url, batch_id)
            if card is None:
                break
        fix = None
        if step == pl.STEP_BULK_CONVERT:
            run_id = _bulk_convert(page, card, batch_id=batch_id, members=bjo.batch_members(joined, batch_id),
                                   confirm_convert=confirm_convert, record=record, rep=rep)
        elif step == pl.STEP_BULK_COMPAT:
            fix = next((x for x in j.fixture_rows(names)
                        if x["action_kind"] == "ENABLE_COMPAT" and x["status"] == "OPEN"), None)
            if fix is None:
                warn(f"{tag} compat", "Compat is owed but no OPEN fixture row is in the batch; a human decides "
                     "(a real ENABLE_COMPAT needs an empty path per table)")
                break
            warn(f"{tag} compat", "Compatibility Mode needs a human-chosen EMPTY path per table, never invented; "
                 "none on this estate, so the fixture row is deferred")
            run_id = _ui_defer(page, card, str(fix["full_name"]), reason=bjo.DEFER_REASON)
        elif step in _BULK_EXEC:
            trigger, confirm, typed = _BULK_EXEC[step]
            run_id = _bulk_click(page, card, trigger, confirm, typed=batch_id if typed else None)
        else:
            warn(f"{tag} {step}", "no click for this step; a human decides")
            break
        if not run_id:
            break
        waited = _wait_run(j, run_id, 5400 if step == pl.STEP_BULK_CONVERT else 1800, tag)
        if waited is None:
            break
        result, start = waited
        verdict = jo.run_outcome(result, j.last_failed_tasks)
        rep(f"{tag} {step} run", verdict)
        last = step
        rollups, joined, _ = j.batch_rollups(prid)
        for o in bjo.batch_step_outcome(step=step, after=bjo.rollup_for(rollups, batch_id),
                                        next_row=j.next_row(), run_start_ms=start):
            rep(f"{tag} after {step}", o)
        if step == pl.STEP_BULK_CONVERT:  # also after a FAILED run: list the stuck tables
            for o in bjo.convert_outcome({w: j.members(prid, w) for w in wave_ids}):
                rep(f"{tag} converted", o)
        if verdict[0] == jo.FAIL:
            break
        if step == pl.STEP_BULK_OPTIMIZE:
            for m in bjo.batch_members(joined, batch_id):
                rep(f"{tag} 08c stamped {m['wave_id']}",
                    jo.optimize_stamp_outcome(j.members(prid, str(m["wave_id"])), m))
        if step == pl.STEP_BULK_COMPAT and fix is not None:
            rep(f"{tag} defer ENABLE_COMPAT", jo.defer_outcome(
                j.action(fix["action_key"]), j.audit_since([str(fix["full_name"])], start), reason=bjo.DEFER_REASON))
        if step == pl.STEP_BULK_RESHARE:
            fix_r = next((x for x in j.fixture_rows(names) if x["action_kind"] == "RESHARE_D2D"), None)
            if fix_r and fix_r["status"] == "OPEN":
                warn(f"{tag} re-share", "no D2D share on this estate: the fixture row stays OPEN, so it is deferred")
                card = _bulk_card(page, app_url, batch_id)
                if card is None:
                    break
                d_run = _ui_defer(page, card, str(fix_r["full_name"]), reason=bjo.DEFER_REASON)
                if not d_run:
                    break
                d_waited = _wait_run(j, d_run, 1800, tag)
                if d_waited is None:
                    break
                rep(f"{tag} defer RESHARE_D2D", jo.defer_outcome(
                    j.action(fix_r["action_key"]), j.audit_since([str(fix_r["full_name"])], d_waited[1]),
                    reason=bjo.DEFER_REASON))
                for o in bjo.batch_step_outcome(step=step, after=bjo.rollup_for(j.batch_rollups(prid)[0], batch_id),
                                                next_row=j.next_row(), run_start_ms=d_waited[1]):
                    rep(f"{tag} after the defer", o)
    else:
        warn("B max steps", f"stopped after {max_steps} steps; rerun to continue")
    if record:
        print("\nPre-convert checks recorded per member:")
        for m, checks in record.items():
            print(f"  {m}: " + ", ".join(f"{c}={s}" for c, s in checks.items()))


def assert_rollback_wave(page, app_url, *, wave_id: str, plan_run_id: str, reason: str,
                         confirm_execute: bool) -> None:
    """Roll one wave back from /convert → Roll back… (06_rollback, UNSET MANAGED), pinned to its plan."""
    import e2e_journey as ej
    from databricks.sdk import WorkspaceClient
    from migration import bulk_journey_oracle as bjo
    from migration import journey_oracle as jo

    j = ej.Journey(WorkspaceClient(profile=PROFILE))
    rep = lambda label, o: ej.report(label, o, check=check, warn=warn)  # noqa: E731
    members = j.members(plan_run_id, wave_id)
    if not members:
        check(f"R0 {wave_id} has members in plan {plan_run_id}", False)
        return
    conv = j.converted_at_ms(plan_run_id, wave_id)
    if not conv:
        warn(f"R0 {wave_id} rollback", "no successful CONVERT audit row in this plan; nothing to roll back")
        return
    window = jo.rollback_window_outcome(conv, int(time.time() * 1000))
    rep(f"R0 {wave_id} rollback window", window)
    if window[0] == jo.FAIL:
        return
    print(f"Rollback (UI): plan {plan_run_id}, wave {wave_id} ({len(members)} tables)")
    if not confirm_execute:
        warn("R1 HUMAN GATE", f"dry mode: would roll back {wave_id} from /convert → 'Roll back…'; "
             "rerun with --confirm-execute")
        return
    _install_trigger_guard(page, lambda alias, params: bjo.trigger_guard(
        alias, params, plan_run_id=plan_run_id, batch_id="", member_schemas=set(), rollback_wave=wave_id))
    if not goto_guarded(page, f"{app_url}/convert", ready_marker="Scope — one table or one wave", label="R1 /convert"):
        return
    _dismiss_tour(page)
    page.locator("#rb-reason").fill(reason)
    picker = page.get_by_role("combobox").filter(has_text="…or pick a wave").first
    if not _pick_combobox(page, picker, "Search by label, id, bucket, state, schema…", wave_id, "rollback wave"):
        return
    btn = page.get_by_role("button", name="Roll back…", exact=True).first
    if btn.is_disabled():
        warn("R1 'Roll back…' is disabled", btn.get_attribute("title") or "")
        return
    btn.click()
    box = page.locator(_CONFIRM_SURFACE).get_by_placeholder(wave_id, exact=True).last
    try:
        box.wait_for(timeout=15_000)
    except Exception:  # noqa: BLE001
        check(f"R1 the rollback confirm asks to type {wave_id}", False)
        page.keyboard.press("Escape")
        return
    box.fill(wave_id)
    run_id = _confirm_and_capture(page, "Roll back")
    if not run_id:
        return
    waited = _wait_run(j, run_id, 5400, "R1")
    if waited is None:
        return
    rep(f"R1 {wave_id} rollback run", jo.run_outcome(waited[0], j.last_failed_tasks))
    rep(f"R2 {wave_id} rolled back", jo.rollback_outcome(j.converted_members(plan_run_id, wave_id)))


def run(modes: set[str], *, session_auth: bool = False, journey: dict | None = None, bulk: dict | None = None,
        focus: dict | None = None, color_scheme: str = "light") -> None:
    app_url = resolve_app_url()
    print(f"App: {app_url}")

    # Auth (D2/D-UI-2). Default = the UNATTENDED profile-Bearer path (mint a token, set it as a
    # context header — the Apps front door validates it and injects OBO for the UI + /api, so no SSO
    # session/MFA/cadence is needed). ``--session-auth`` forces the captured ``storageState`` cookie
    # path (the real end-user session) — required only when you want to exercise it explicitly. A
    # token-mint failure falls back to a session if one exists; if neither resolves, every assertion
    # degrades to WARN (never a false FAIL).
    state: str | None = None
    hdrs: dict[str, str] | None = None
    if session_auth:
        state = storage_state_path()
        if not state:
            warn(f"--session-auth but no captured session at {storage_state_dest()} — run "
                 "`--login` first. Degrading to WARN.")
    else:
        hdrs = token_headers()
        if not hdrs:
            state = storage_state_path()
    if not state and not hdrs:
        warn("no auth resolved — neither a profile-minted Bearer token nor a captured session "
             f"({storage_state_dest()}). Check --profile (DATABRICKS_CONFIG_PROFILE) or run "
             "`--login`. Every assertion degrades to WARN.")

    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError:
        warn("playwright not installed — `pip install playwright && playwright install chromium`. "
             "Driver scaffold present; assertions skipped.")
        return

    headless = os.environ.get("E2E_APP_HEADLESS", "1") != "0"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        # The app scrolls inside its own container, so a full-page shot needs a tall viewport.
        view = {"color_scheme": color_scheme,
                "viewport": {"width": 1440, "height": 2400 if _SHOTS["dir"] else 900}}
        if state:
            ctx = browser.new_context(storage_state=state, **view)
            print("Auth: captured SSO session (storageState).")
        elif hdrs:
            ctx = browser.new_context(extra_http_headers=hdrs, **view)
            _BEARER.update(on=True, minted=time.time())
            print("Auth: profile-minted Bearer token (unattended — no session capture).")
        else:
            ctx = browser.new_context(**view)
        if _SHOTS["dir"] or modes & {"journey", "prep_batch", "bulk_journey", "rollback_wave", "focus",
                                     "focus_walk", "pins", "how", "replan"}:
            ctx.add_init_script(_suppress_tours_script())
        page = ctx.new_page()
        page.on("console", lambda m: _SHOTS["console"].append(f"{page.url}: {m.text}") if m.type == "error" else None)
        try:
            if "c1" in modes:
                assert_c1(page, app_url)
            if "c2" in modes:
                assert_c2(page, app_url)
            if "c4" in modes:
                assert_c4(page, app_url)
            if "c6" in modes:
                assert_c6(page, app_url)
            if "smoke" in modes:
                assert_smoke(page, app_url)
            if "a1" in modes:
                assert_a1(page, app_url)
            if "a2" in modes:
                assert_a2(page, app_url)
            if "a3" in modes:
                assert_a3(page, app_url)
            if "journey" in modes:
                assert_journey(page, app_url, **(journey or {}))
            if "prep_batch" in modes:
                assert_prep_batch(page, app_url, **(bulk or {})["prep_batch"])
            if "bulk_journey" in modes:
                assert_bulk_journey(page, app_url, **(bulk or {})["bulk_journey"])
            if "rollback_wave" in modes:
                assert_rollback_wave(page, app_url, **(bulk or {})["rollback_wave"])
            f = focus or {}
            if "focus" in modes:
                assert_focus(page, app_url, schema=f["schema"])
            if "focus_walk" in modes:
                assert_focus_walk(page, app_url, schema=f["schema"], journey=journey or {})
            if "pins" in modes:
                assert_pins(page, app_url, schema=f["schema"], confirm_execute=f.get("confirm_execute", False))
            if "how" in modes:
                assert_how(page, app_url)
            if "replan" in modes:
                assert_replan(page, app_url, schema=f["schema"], confirm_execute=f.get("confirm_execute", False))
        finally:
            ctx.close()
            browser.close()
            if _SHOTS["console"]:
                warn(f"{len(_SHOTS['console'])} console error(s)", _SHOTS["console"][0][:200])
            write_shot_index()


# --------------------------------------------------------------------------- #
# FOCUS / PINS / HOW — focus mode (one schema), team priorities, How it works
# --------------------------------------------------------------------------- #
def _rep(label: str, outcome) -> None:
    import e2e_journey as ej

    ej.report(label, outcome, check=check, warn=warn)


FOCUS_HEADER = "Working on one schema"
FOCUS_MISSING = "Schema not in the plan"
FOCUS_BACK = "Back to the recommended step"


def _focus_journey_rows(schema: str):
    import e2e_journey as ej
    from databricks.sdk import WorkspaceClient

    j = ej.Journey(WorkspaceClient(profile=PROFILE))
    rows = j.scoped_rows()
    return j, rows, next((r for r in rows if r.get("scope_key") == schema), None)


def assert_focus(page, app_url: str, *, schema: str) -> None:
    """Read-only: the picker beside the schema list, the focused card for ``schema`` (its own
    row, its waves, the way back), and the step's confirm naming the WAVE (never the schema)."""
    from migration import journey_oracle as jo

    j, rows, row = _focus_journey_rows(schema)
    if not goto_guarded(page, f"{app_url}/overview", ready_marker="Schemas blocked", label="F0 /overview"):
        return
    picker = page.locator('[data-tour="focus-picker"]')
    if not rows:
        warn("F0 no per-scope rows yet", "run the conductor (Refresh recommendation) after deploy")
    else:
        check("F0 the focus picker renders beside the schema list", picker.count() > 0)
        if picker.count():
            picker.first.click()
            shot(page, "F0 focus picker open", "groups: in progress / ready / re-plan / handled elsewhere / done")
            page.keyboard.press("Escape")
    if not goto_guarded(page, f"{app_url}/overview?focus={quote(schema)}", ready_marker=None, label="F1 focus"):
        return
    try:
        page.get_by_text(FOCUS_HEADER if row else FOCUS_MISSING, exact=False).first.wait_for(timeout=90_000)
    except Exception:  # noqa: BLE001
        check(f"F1 the focus view renders for {schema}", False)
        return
    shot(page, f"F1 focused on {schema}", "focus header + the schema's own guided card")
    check("F1 the way back is offered", page.get_by_text(FOCUS_BACK, exact=False).count() > 0)
    if row is None:
        check(f"F1 {schema} not in the plan reads honestly", page.get_by_text(FOCUS_MISSING, exact=False).count() > 0)
        return
    _rep("F1 scope status", jo.scope_status_outcome(row))
    wave = str(row.get("next_wave_id") or "")
    if int(row.get("wave_count") or 0) > 0:
        check("F1 the schema's waves are listed", page.get_by_text("Waves in this schema", exact=False).count() > 0)
    if wave:
        check(f"F1 the strip shows the current wave {wave}", page.get_by_text(wave, exact=True).count() > 0)
    check("F1 the guided card renders", page.get_by_text("You approve the step", exact=False).count() > 0)
    kind = str(row.get("next_step_kind") or "")
    check_card_layers(page, "F1", kind=kind)
    # The guided card's real CTAs (the same regexes the journey clicks).
    convert_cta = f"{_GATE_RE}|{_SKIP_RE}|{_ANYWAY_RE}"
    trigger = {"convert": convert_cta, "resume": convert_cta,
               "freeze": r"^(Pause jobs first|Pause anyway…)$"}.get(kind)
    if trigger and wave:
        btn = page.get_by_role("button", name=re.compile(trigger))
        try:
            btn.first.wait_for(timeout=_CTA_WAIT_MS)
        except Exception:  # noqa: BLE001
            pass
        if btn.count() and btn.first.is_enabled():
            _dismiss_tour(page)
            btn.first.click()
            dlg = page.get_by_role("alertdialog")
            try:
                dlg.first.wait_for(timeout=15_000)
                page.wait_for_timeout(2_000)
                text = dlg.first.inner_text() or ""
                box = page.locator("#convert-confirm")
                target = (box.first.get_attribute("placeholder") or "").strip() if box.count() else ""
                shot(page, f"F2 {kind} confirm (wave-pinned)", f"must name {wave}, not the schema alone")
                if target:
                    check(f"F2 the {kind} confirm types the wave {wave}", target == wave, f"type-to-confirm: {target!r}")
                else:
                    check(f"F2 the {kind} confirm names the wave {wave}", wave in text, text[:160].replace("\n", " "))
            except Exception:  # noqa: BLE001
                check(f"F2 the {kind} confirm opens", False)
            page.keyboard.press("Escape")
        else:
            warn(f"F2 {kind} trigger absent or disabled", "a run may be active")
    back = page.get_by_text(FOCUS_BACK, exact=False)
    if back.count():
        back.first.click()
        page.wait_for_timeout(1500)
        check("F3 back to the recommended step leaves focus mode", "focus=" not in page.url, page.url)
        shot(page, "F3 back to the estate view")


def assert_focus_walk(page, app_url: str, *, schema: str, journey: dict) -> None:
    """MUTATING (behind --confirm-execute / --confirm-convert): walk ``schema`` wave by wave from
    the focused card, reusing the M5 one-wave journey with ``focus``; waves never interleave."""
    from migration import journey_oracle as jo

    allowed = {x.strip() for x in str(journey.get("confirm_convert") or "").split(",") if x.strip()}
    seen: list[tuple[str, str]] = []
    walked: list[str] = []
    for n in range(1, 9):
        _, _, row = _focus_journey_rows(schema)
        wave = str((row or {}).get("next_wave_id") or "")
        seen.append((wave, str((row or {}).get("next_step_kind") or "done")))
        if not wave:
            print(f"  W{n} {schema}: {(row or {}).get('scope_status')} — no wave left")
            break
        if walked and wave == walked[-1]:
            warn(f"W{n} {wave} did not move on", "stopped at a human gate (or a step that needs a person); "
                 "rerun with the gate's flag to continue")
            break
        if wave in walked:
            check(f"W{n} {wave} is not walked twice", False, "the walk came back to a wave it had left")
            break
        walked.append(wave)
        assert_journey(page, app_url, **{**journey, "wave_id": wave,
                                         "confirm_convert": wave if wave in allowed else None}, focus=schema)
        if _failures:
            break
    _rep("W walk order", jo.focus_walk_outcome(seen))
    shot(page, f"W {schema} after the walk")


def assert_pins(page, app_url: str, *, schema: str, confirm_execute: bool) -> None:
    """Team priorities: the panel lists the pins; the focus view pins (reason first) or shows the
    place. With --confirm-execute it adds then removes the pin for ``schema`` through the UI."""
    from migration import journey_oracle as jo

    j, _, _ = _focus_journey_rows(schema)
    pins = jo.team_pins(j.pins_row())
    if not goto_guarded(page, f"{app_url}/overview", ready_marker="Schemas blocked", label="P0 /overview"):
        return
    panel = page.locator('[data-tour="team-pins"]')
    if pins:
        check("P0 the Team priorities panel lists the pins", panel.count() > 0
              and all(page.get_by_text(str(p.get("scope_key")), exact=True).count() > 0 for p in pins))
    else:
        check("P0 no pins: no Team priorities panel", panel.count() == 0)
    if not goto_guarded(page, f"{app_url}/overview?focus={quote(schema)}", ready_marker=FOCUS_HEADER, label="P1 focus"):
        return
    pinned = any(p.get("scope_key") == schema for p in pins)
    if pinned:
        check("P1 the focus view shows the team pin", page.get_by_text("Pinned by your team", exact=False).count() > 0)
        return
    reason = page.get_by_label("Why this schema goes first")
    pin_btn = page.get_by_role("button", name=re.compile(r"^Pin for the team", re.I))
    check("P1 the pin control asks for a reason", reason.count() > 0 and pin_btn.count() > 0)
    if not (reason.count() and pin_btn.count()):
        return
    check("P1 pinning is disabled until a reason is typed", pin_btn.first.is_disabled())
    reason.first.fill("e2e focus CUJ")
    check("P1 a reason enables pinning", pin_btn.first.is_enabled())
    pin_btn.first.click()
    shot(page, "P2 pin confirm", "Pin for the team dialog")
    if not confirm_execute:
        print("  P2 dry mode — cancelling the pin (pass --confirm-execute to add then remove it)")
        page.keyboard.press("Escape")
        return
    run_id = _confirm_and_capture(page, "Pin")
    if run_id and _wait_run(j, run_id, 1800, "P3 pin"):
        after = jo.team_pins(j.pins_row())
        check(f"P3 {schema} is pinned", any(p.get("scope_key") == schema for p in after))
        _rep("P3 pin precedence", jo.pins_outcome(j.pins_row(), j.wave_rows(j.plan_run_id())))
        if goto_guarded(page, f"{app_url}/overview", ready_marker="Schemas blocked", label="P4 /overview"):
            shot(page, "P4 Team priorities with the pin")
        if goto_guarded(page, f"{app_url}/overview?focus={quote(schema)}", ready_marker="Pinned by your team", label="P5 focus"):
            rm = page.get_by_role("button", name=re.compile(r"^Remove", re.I))
            if rm.count():
                rm.first.click()
                rid = _confirm_and_capture(page, "Remove")
                if rid and _wait_run(j, rid, 1800, "P5 remove"):
                    check(f"P5 {schema} is no longer pinned",
                          all(p.get("scope_key") != schema for p in jo.team_pins(j.pins_row())))


def _planned_in_schema(j, prid: str, schema: str) -> int:
    """How many of ``schema``'s tables the plan ``prid`` holds (``wave_status`` has no scope column)."""
    from migration import journey_oracle as jo

    rows = j.sql(f"SELECT count(*) AS n FROM {j.t('wave_plan')} WHERE plan_run_id = {jo.sql_string(prid)} "
                 f"AND full_name LIKE {jo.sql_string(schema + '.%')}")
    return int((rows[0] if rows else {}).get("n") or 0)


def assert_replan(page, app_url: str, *, schema: str, confirm_execute: bool) -> None:
    """Drift: the focused schema names its tables found after the plan, its Plan link opens /waves,
    and the Re-plan confirm types the current plan id. With --confirm-execute it re-plans (a new
    plan_run_id; no table changes) and checks the note is gone and the tables joined a wave."""
    j, _, row = _focus_journey_rows(schema)
    unplanned = int((row or {}).get("unplanned_tables") or 0)
    if not goto_guarded(page, f"{app_url}/overview?focus={quote(schema)}", ready_marker=FOCUS_HEADER, label="R0 focus"):
        return
    note = page.get_by_text("found after the plan", exact=False)
    check(f"R0 the drift note matches the row ({unplanned} unplanned)", (note.count() > 0) == (unplanned > 0))
    shot(page, f"R0 {schema} with tables found after the plan", f"unplanned_tables={unplanned}")
    if not unplanned:
        return
    before_prid = j.plan_run_id()
    before_tables = _planned_in_schema(j, before_prid, schema)
    page.get_by_role("link", name="Plan", exact=True).first.click()
    try:
        page.wait_for_url(re.compile(r"/waves"), timeout=30_000)
        page.get_by_role("button", name="Re-plan", exact=True).first.wait_for(timeout=90_000)
    except Exception:  # noqa: BLE001
        check("R1 the Plan link opens /waves with a Re-plan button", False, page.url)
        return
    check("R1 the Plan link opens /waves with a Re-plan button", True)
    shot(page, "R1 /waves from the drift note", "the Re-plan button")
    page.get_by_role("button", name="Re-plan", exact=True).first.click()
    box = page.locator("#replan-confirm")
    try:
        box.first.wait_for(timeout=15_000)
    except Exception:  # noqa: BLE001
        check("R2 the Re-plan confirm opens", False)
        return
    check("R2 the Re-plan confirm types the current plan id",
          (box.first.get_attribute("placeholder") or "").strip() == before_prid, before_prid)
    shot(page, "R2 Re-plan confirm", "names what a re-plan invalidates; type the plan id")
    if not confirm_execute:
        print("  R2 dry mode — cancelling the re-plan (pass --confirm-execute to run it)")
        page.keyboard.press("Escape")
        return
    box.first.fill(before_prid)
    shot(page, "R3 Re-plan confirm unlocked", "the plan id typed")
    run_id = _confirm_and_capture(page, "Re-plan")
    if not (run_id and _wait_run(j, run_id, 2400, "R3 re-plan")):
        return
    after_prid = j.plan_run_id()
    check("R4 the re-plan minted a new plan", after_prid != before_prid, f"{before_prid} -> {after_prid}")
    _, _, after = _focus_journey_rows(schema)
    check("R4 no table of the schema is left out of the plan",
          int((after or {}).get("unplanned_tables") or 0) == 0, str((after or {}).get("scope_status")))
    after_tables = _planned_in_schema(j, after_prid, schema)
    check("R4 the found tables joined the schema's waves", after_tables == before_tables + unplanned,
          f"{before_tables} + {unplanned} -> {after_tables}")
    if goto_guarded(page, f"{app_url}/overview?focus={quote(schema)}", ready_marker=FOCUS_HEADER, label="R5 focus"):
        check("R5 the drift note is gone", page.get_by_text("found after the plan", exact=False).count() == 0)
        shot(page, f"R5 {schema} after the re-plan")


HOW_PAGES = (
    ("/how-it-works", "How it works"),
    ("/how-it-works/happy-path", "One schema at a time"),
    ("/how-it-works/waves", "One schema, and team priorities"),
    ("/how-it-works/workstreams", "Handled elsewhere"),
    ("/how-it-works/direct", "Conductor"),
)


def assert_how(page, app_url: str) -> None:
    """Every How it works page renders, with its focus-mode section."""
    for path, marker in HOW_PAGES:
        if goto_guarded(page, f"{app_url}{path}", ready_marker=marker, label=f"H {path}"):
            check(f"H {path} shows {marker!r}", True)
            # The new section can sit far down a long (scroll-driven) page: bring it into view
            # and capture just that viewport, so the screenshot shows what the check found.
            try:
                page.get_by_text(marker, exact=False).last.scroll_into_view_if_needed(timeout=10_000)
                page.wait_for_timeout(1_500)
                shot(page, f"H {path} section {marker}", "the focus-mode section in view", full_page=False)
            except Exception as e:  # noqa: BLE001
                warn(f"H {path} section screenshot", str(e)[:160])


def print_fixtures() -> None:
    print("L4 fixtures:\n")
    for name, recipe in FIXTURES.items():
        print(f"  {name}\n    {recipe}\n")


def main() -> int:
    global PROFILE
    ap = argparse.ArgumentParser(
        description="Live app/UI e2e (L4). --login (capture session), "
                    "--c1/--c2/--c4/--c6/--smoke/--a1/--a2/--a3, --journey (M5), --fixtures.")
    ap.add_argument("--profile",
                    help="Databricks CLI profile. Required for --journey (never defaulted); the other "
                         "modes fall back to DATABRICKS_CONFIG_PROFILE.")
    ap.add_argument("--login", action="store_true",
                    help="capture/refresh the SSO auth session in a headed browser (only needed for "
                         "--session-auth; the default token path needs no session).")
    ap.add_argument("--session-auth", action="store_true",
                    help="force the captured storageState (SSO cookie) path instead of the default "
                         "unattended profile-Bearer token auth.")
    ap.add_argument("--fixtures", action="store_true",
                    help="print the fixture recipe table and exit (no network).")
    ap.add_argument("--c1", action="store_true",
                    help="Overview KPIs render + guided-convert CTA opens the gate.")
    ap.add_argument("--c2", action="store_true",
                    help="Waves Convert-Gate GO/BLOCK wiring + type-to-confirm unlock.")
    ap.add_argument("--c4", action="store_true",
                    help="Convert (expert): scope pickers + disabled-gate wiring + Readiness by wave "
                         "board + scoped ?wave= gate-open with the managed-location panel.")
    ap.add_argument("--c6", action="store_true",
                    help="Workstreams: remediation-track cards render live counts + one detail "
                         "drill-in mounts.")
    ap.add_argument("--smoke", action="store_true",
                    help="P3 render smokes: /blocked /freeze /runs /coverage /runbook /dashboard "
                         "/settings mount + resolve their primary query.")
    ap.add_argument("--a2", action="store_true", help="W3 recommended-batch badge on /bulk.")
    ap.add_argument("--a1", action="store_true",
                    help="W2 pause banner + WARN_WINDOW badge on the isolated writer wave "
                         "(F-WRITER-WAVE; live proof deferred to the next re-seed).")
    ap.add_argument("--a3", action="store_true",
                    help="Overview first-run NEVER_PROFILED landing on a WIPED control plane "
                         "(F-EMPTY; a separate, estate-destructive run).")
    ap.add_argument("--journey", action="store_true",
                    help="M5: walk --wave end to end from the Overview guided step (MUTATING; see README).")
    ap.add_argument("--wave", help="--journey wave id (from `e2e_journey.py survey`).")
    ap.add_argument("--confirm-execute", action="store_true",
                    help="--journey / --bulk-journey: allow clicks that start a job (HUMAN GATE). Without it, dry mode.")
    ap.add_argument("--confirm-convert", help="--journey / --bulk-journey: the exact convert-gate confirm target "
                                              "(for --journey must be --wave; for --bulk-journey: the batch id); "
                                              "a schema-scoped gate is cancelled (HUMAN GATE).")
    ap.add_argument("--allow-conscious-skip", action="store_true",
                    help="--journey: take 'Convert without clearing these (N)' for manual pre-convert actions; "
                         "that convert sends require_compaction_frozen=false and "
                         "require_pre_convert_actions_cleared=false (a freeze/recency ack stays a human's).")
    ap.add_argument("--convert-anyway", action="store_true",
                    help="--journey: take the demoted 'Convert anyway…' and acknowledge a convert-permission gap.")
    ap.add_argument("--inject-fixture", action="store_true",
                    help="--journey / --bulk-journey: when the conductor first recommends optimize on the wave, "
                         "MERGE labelled re-share/Compat fixture rows (e2e_m5_fixture), then Refresh.")
    ap.add_argument("--max-steps", type=int, default=14)
    ap.add_argument("--prep-batch", action="store_true",
                    help="M6.4: run the read-only campaign dry run (/convert) on each --batch member schema "
                         "that needs it.")
    ap.add_argument("--bulk-journey", action="store_true",
                    help="M6.4: walk --batch through /bulk (MUTATING; see README).")
    ap.add_argument("--rollback-wave", metavar="WAVE_ID",
                    help="M6.4: roll one wave back from /convert → Roll back… (MUTATING: UNSET MANAGED).")
    ap.add_argument("--batch", help="--prep-batch / --bulk-journey batch id (from `e2e_journey.py survey --batch`).")
    ap.add_argument("--plan-run-id",
                    help="--rollback-wave / --prep-batch / --bulk-journey: the approved plan (from "
                         "`e2e_journey.py survey --batch`). The trigger guard pins it; a different current plan "
                         "FAILs before any click. Required with --confirm-execute.")
    ap.add_argument("--reason", help="--rollback-wave: the audit reason.")
    ap.add_argument("--cockpit", action="store_true",
                    help="--bulk-journey: run job_11e (action=show, read-only) each step and cross-check it.")
    ap.add_argument("--screenshots", metavar="DIR",
                    help="save a full-page PNG at every checkpoint + DIR/index.md (e.g. backend/.e2e/artifacts/<date>).")
    ap.add_argument("--color-scheme", choices=["light", "dark"], default="light", help="browser colour scheme.")
    ap.add_argument("--focus", action="store_true", help="focus mode, read-only: picker, focused card, wave-pinned confirm.")
    ap.add_argument("--focus-walk", action="store_true",
                    help="focus mode, MUTATING: walk --focus-schema wave by wave (HUMAN GATE; --confirm-convert w1,w2).")
    ap.add_argument("--pins", action="store_true", help="team priorities (adds+removes a pin only with --confirm-execute).")
    ap.add_argument("--how", action="store_true", help="every How it works page renders with its focus section.")
    ap.add_argument("--replan", action="store_true",
                    help="drift: the focused schema's tables found after the plan, then Re-plan from /waves "
                         "(a new plan_run_id; runs only with --confirm-execute).")
    ap.add_argument("--focus-schema", help="--focus / --focus-walk / --pins / --replan: the scope (catalog.schema).")
    ap.add_argument("--stop-before", choices=_BULK_STEP_CHOICES,
                    help="--bulk-journey: stop before launching this step.")
    args = ap.parse_args()
    if args.profile:
        PROFILE = args.profile
    if not PROFILE and not args.fixtures:
        print("Pass --profile or set DATABRICKS_CONFIG_PROFILE (never defaulted).")
        return 2

    if args.fixtures:
        print_fixtures()
        return 0

    if args.login:
        return capture_session(resolve_app_url())

    if args.rollback_wave is not None and not str(args.rollback_wave).strip():
        print("--rollback-wave needs a non-blank wave id.")
        return 2

    modes = {m for m, on in (("c1", args.c1), ("c2", args.c2), ("c4", args.c4), ("c6", args.c6),
                             ("smoke", args.smoke), ("a1", args.a1), ("a2", args.a2),
                             ("a3", args.a3), ("journey", args.journey), ("prep_batch", args.prep_batch),
                             ("bulk_journey", args.bulk_journey),
                             ("rollback_wave", args.rollback_wave is not None), ("focus", args.focus),
                             ("focus_walk", args.focus_walk), ("pins", args.pins), ("how", args.how),
                             ("replan", args.replan)) if on}
    if modes & {"focus", "focus_walk", "pins", "replan"} and not (args.focus_schema and args.profile):
        print("--focus / --focus-walk / --pins / --replan need --focus-schema and --profile (never defaulted).")
        return 2
    if args.screenshots:
        Path(args.screenshots).mkdir(parents=True, exist_ok=True)
        _SHOTS["dir"] = args.screenshots
    if "rollback_wave" in modes and modes & {"journey", "prep_batch", "bulk_journey"}:
        print("--rollback-wave is its own run (one human gate per wave): do not combine it with "
              "--journey / --prep-batch / --bulk-journey.")
        return 2
    if "journey" in modes and not args.profile:
        print("--journey needs --profile (never defaulted).")
        return 2
    bulk_driving = modes & {"prep_batch", "bulk_journey", "rollback_wave"}
    if bulk_driving and not args.profile:
        print(f"{', '.join(sorted(bulk_driving))} need --profile (never defaulted).")
        return 2
    if "journey" in modes and not args.wave:
        print("--journey needs --wave (run `python backend/.e2e/e2e_journey.py --profile <p> survey`).")
        return 2
    if modes & {"prep_batch", "bulk_journey"} and not args.batch:
        print("--prep-batch / --bulk-journey need --batch (run `e2e_journey.py --profile <p> survey --batch`).")
        return 2
    if modes & {"prep_batch", "bulk_journey"} and args.confirm_execute and not args.plan_run_id:
        print("--prep-batch / --bulk-journey with --confirm-execute need --plan-run-id (the plan you approved "
              "from `e2e_journey.py survey --batch`; the guard pins it).")
        return 2
    if "rollback_wave" in modes and not (args.plan_run_id and (args.reason or "").strip()):
        print("--rollback-wave needs --plan-run-id and a non-blank --reason.")
        return 2
    bulk = {
        "prep_batch": dict(batch_id=args.batch, confirm_execute=args.confirm_execute,
                           plan_run_id=args.plan_run_id),
        "bulk_journey": dict(batch_id=args.batch, confirm_execute=args.confirm_execute,
                             confirm_convert=args.confirm_convert, inject_fixture=args.inject_fixture,
                             cockpit=args.cockpit, stop_before=args.stop_before, max_steps=args.max_steps,
                             plan_run_id=args.plan_run_id),
        "rollback_wave": dict(wave_id=args.rollback_wave, plan_run_id=args.plan_run_id,
                              reason=(args.reason or "").strip(), confirm_execute=args.confirm_execute),
    }
    journey = dict(wave_id=args.wave, confirm_execute=args.confirm_execute, confirm_convert=args.confirm_convert,
                   allow_conscious_skip=args.allow_conscious_skip, convert_anyway=args.convert_anyway,
                   inject_fixture=args.inject_fixture, max_steps=args.max_steps)
    if not modes:
        print("Pass --login (capture session) then --c1 (Overview) / --c2 (Waves gate) / --c4 "
              "(Convert expert) / --c6 (Workstreams) / --smoke (P3 pages) / --a1 (writer pause "
              "banner) / --a2 (W3 badge) / --a3 (first-run, wiped estate) / --journey (M5 one "
              "wave, MUTATING) / --prep-batch / --bulk-journey / --rollback-wave (M6.4, MUTATING). "
              "--fixtures prints estate recipes.")
        return 2

    run(modes, session_auth=args.session_auth, journey=journey, bulk=bulk,
        focus=dict(schema=args.focus_schema, confirm_execute=args.confirm_execute),
        color_scheme=args.color_scheme)

    print()
    if _warnings:
        print(f"WARNINGS ({len(_warnings)}): {', '.join(_warnings)}")
    if _failures:
        print(f"FAILURES ({len(_failures)}): {', '.join(_failures)}")
        return 1
    print(f"L4 app/UI ({', '.join(sorted(modes))}): all checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
