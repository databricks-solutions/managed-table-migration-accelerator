# Usage telemetry (opt-out)

The toolkit reports **how it is used and where migrations get stuck**, never what is being
migrated. It uses the same mechanism as Databricks Labs projects (DQX, UCX) and Genie Workbench:
signals ride as `key/value` extras on the Databricks SDK `User-Agent` of one cheap call to **this
workspace's own control plane**. Nothing is sent to any other endpoint, no row or file leaves the
account, and nothing is written to the control tables.

Source of truth: `backend/src/migration/telemetry.py` (pure, unit-tested) and
`app/server/telemetry/`. This page is the reviewer's catalogue; the code's closed vocabularies win
if they ever disagree.

## Turning it off

| Switch | Scope |
|---|---|
| Bundle variable `telemetry_enabled: "false"` (default `"true"`), then `bundle deploy` | Every stage job (deploy-fixed `base_parameter` on 20 notebook tasks) and the app (`MTM_TELEMETRY_ENABLED`) |
| `UC_EXT2MANAGED_TELEMETRY=false` in the environment | Backend notebooks |
| `DO_NOT_TRACK=1` in the environment | Backend notebooks and the app (the same switch AppKit honours) |
| The `telemetry_enabled` widget on `install.py` | The hand-run installer notebook |

`false`, `0`, `no` and `off` disable it. **If the setting cannot be read, nothing is sent**, and the
app sends nothing when `MTM_TELEMETRY_ENABLED` is unset. When it is off there are no pings, no
product stamp, and the profiler / planner skip the four aggregate queries behind the estate signals.

## What is sent

Every ping carries the product token `uc-ext2managed/<version>` (the SDK adds its own
`databricks-sdk-py/…`, `python/…`, `os/…` and `runtime/…` tokens). Everything else is namespaced
`e2m.` and is either a value the toolkit defines itself or a band:

- **Counts** are bands only: `0`, `1-9`, `10-99`, `100-999`, `1k-9k`, `10k+`. Token counts of `0`
  are not sent.
- **Estate bytes** are a TB band: `lt1tb`, `1-10tb`, `10-100tb`, `100tb-1pb`, `1pb+`.
- A value outside its vocabulary is counted as `other`, never sent as-is.

### Every stage ping

| Key | Values |
|---|---|
| `e2m.stage` | `00_profiler` · `02_wave_planner` · `03_dry_run` · `03c_freeze` · `03d_lease_ownership` · `04_convert` · `05_validate` · `05b_unfreeze_verify` · `06_rollback` · `07_post_convert_cleanup` · `08_reshare` · `08b_enable_compat` · `08c_enable_optimization` · `11_bulk_cockpit` · `99_setup` · `install` |
| `e2m.run` | a random 8-hex id per stage run (stitches the chunks of one run; carries no meaning) |
| `e2m.chunk` | `<i>of<n>` (a run sends at most 6 calls) |
| `e2m.mode` | `dry_run` · `execute` |
| `e2m.outcome` | `ok` · `failed` (an item failed; the fail-loud raise follows) · `error` (the stage died before reporting) · `noop` · `refused` |
| `e2m.sel` | `schema` · `bucket` · `wave` · `batch` · `table` · `none` (the selector *kind*, never its value) |
| `e2m.n.<metric>` | band; metrics `targets`, `converted`, `failed`, `skipped`, `reconciled`, `validated`, `pending`, `rolled_back`, `refused`, `waves`, `batches`, `ops`, `manual`, `verified`, `restored`, `frozen`, `planned`, `discovered`, `hinted`, `switched`, `hint_fallback` (`08c`: tables clustered with ZORDER hints, manual keys switched to AUTO, hints rejected → plain AUTO) |
| `e2m.f.<flag>` | `true`/`false` for `streaming`, `truncate_uniform`, `enable_po`, `force_vacuum`, `restore_prior_state`, `location_supplied` (never the location), `open_apis`, `genie`, `warehouse_set`, `app_bound`, `confirm_convert`, `discovery_error` |
| `e2m.bucket` · `e2m.finish_action` · `e2m.step` · `e2m.cockpit_action` | wave bucket (`lineage.ALL_BUCKETS`) · `run`/`defer`/`reopen` · conductor / bulk step kinds · `show`/`advance` |

### Pain points

| Key | Source vocabulary |
|---|---|
| `e2m.blk.<token>` | Blocker tokens: `locations.DRY_RUN_OWNED_BLOCKERS`, the `BLOCKER_*` constants in `eligibility` / `external_writes` / `reconcile` / `path_access` / `convert`, the literal recency blockers (`recent_vacuum_activity`, `recent_compaction_activity`, `recent_write_activity`, `non_delta_format`) and the failure markers (`VERIFICATION_FAILED`, `ROLLBACK_FAILED`, …) |
| `e2m.err.<CLASS>` | The **structured** Databricks error class of a failed convert (`getCondition()` / `getErrorClass()` / `error_code`, e.g. `DELTA_ALTER_TABLE_SET_MANAGED_FAILED.FILE_VALIDATION_FAILED`) or `classify_convert_error`'s class. Never parsed from, and never accompanied by, the message. |
| `e2m.chk.<n>.<FAIL\|WARN>` | Dry-run check number and status |
| `e2m.gate.<kind>` | The convert gate that refused: `gate_eval.ALL_GATE_KINDS` plus `RUNTIME`, `PLAN_DRIFT`, `STREAMING_LEAK`, `PRE_CONVERT_ACTIONS`, `MAX_TABLES`, `OWNING_WORKSPACE`, `STREAMING_PLAN_UNAVAILABLE` |
| `e2m.op.<kind>` | Failed freeze / restore op kinds (`executor.OP_*`, `RESTORE_*`) |
| `e2m.exc` | The Python exception class name when a stage dies before reporting (e.g. `RuntimeError`) |
| `e2m.rdr.<state>` · `e2m.wsv.<verdict>` · `e2m.auth.<status>` | External-reader readiness · writer-schedule verdict · convert-permission verdict |

### Estate shape (profiler and planner only)

`e2m.est.tables`, `e2m.est.tb`, `e2m.st.<STATE>` (manifest state mix), `e2m.blk.<token>`,
`e2m.act.<KIND>.<auto|manual>` (open actions by kind, split by whether the freeze executor can
operate them), `e2m.auth.<status>` and `e2m.bkt.<bucket>`, all banded.

### App

Each successful job launch from the app or its MCP server sends `e2m.trigger/<alias>` (a key of the
app's own job map), `e2m.surface/app|mcp` and `e2m.mcp_tier/v1|v2`. Job parameters, the operator and
the request are never read. The app's own SDK calls carry the `uc-ext2managed/<version>` product.

## What is never sent

Catalog, schema or table names (hashed or not); storage paths and locations; share and recipient
names; job, pipeline or notebook names and ids; any identity (the `operator` parameter, users,
groups, service principals); SQL text; error messages; `grant_sql`; and every free-text field
(`defer_reason`, `path_redirect_reason`, rollback `reason`, `ask_text`). The workspace is
identified by the control plane that receives the call, so the toolkit never sends it.

## Guarantees

- **Never changes a stage.** Every edge is best-effort and never raises; the notebook-side
  aggregation goes through `tm.tally()` / `tm.size()`, which cannot raise either. A telemetry
  failure is logged as one line and swallowed; gates, verdicts and fail-loud raises are untouched.
- **Bounded.** Each call has a 5 s retry window and HTTP timeout, a stage stops at its first failed
  call, and the whole send has a 10 s deadline. At most 6 calls per stage run, one per app launch.
- **Reports failures too.** A stage reports before its fail-loud raise and before every
  `dbutils.notebook.exit`; an IPython `post_run_cell` hook reports a stage that dies anywhere else
  (`outcome=error`, the exception type, the error class and the gate it tripped). A
  `dbutils.notebook.exit` surfaces in that hook as `NotebookExit` and is ignored.
- **Runtime note.** Serverless jobs ship an old Databricks SDK without `databricks.sdk.useragent`,
  so the process-wide product stamp is skipped there; the explicit pings still carry it.

## Maintaining it

- **New signal:** add its key to a closed vocabulary in `telemetry.py` (import it from the owning
  module rather than copying it) and a case to `test_telemetry.py`. The leak tests feed names,
  paths, emails, newlines and messages through every builder.
- **New blocker token:** if it has no constant in its owning module, add it to
  `telemetry._LITERAL_BLOCKERS`; `test_notebook_wiring_telemetry.py` fails on any unregistered
  blocker literal.
- **New stage notebook:** add it to `telemetry.STAGES`, insert the three-line start block after its
  last `from migration import`, call `TM.finish(...)` before its fail-loud raise and every exit, and
  give its job task the `telemetry_enabled: ${var.telemetry_enabled}` base parameter. The wiring
  guard checks all four.
- **New app trigger path:** route it through `triggerAlias`; the ping is fired there.
