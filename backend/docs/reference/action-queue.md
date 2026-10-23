# Action queue reference

`<manifest_catalog>.<manifest_schema>.migration_actions` is the **work queue**. Every
"papercut" that must be handled before or after a conversion becomes one tracked,
table- or schema-scoped row.

If you are building an external control plane, this page is the most important one to read
before writing anything. Getting the ownership split wrong is the one way an integration can
silently destroy operator progress.

Schema table: [../integration/control-tables.md#migration_actions](../integration/control-tables.md#migration_actions).

## Action kinds

`actions.ALL_KINDS`: exactly these ten, no others:

| Kind | Phase | Blocking | Raised by |
|---|---|---|---|
| `FREEZE_COMPACTION` | `PRE_CONVERT` | yes | a `compaction_sources` actor for the table/schema |
| `UNFREEZE_COMPACTION` | `POST_CONVERT` | no | paired partner of `FREEZE_COMPACTION` |
| `STOP_STREAM` | `PRE_CONVERT` | yes | a `streaming_plan` relationship |
| `RESTART_STREAM` | `POST_CONVERT` | no | a `streaming_plan` relationship (one per relationship; unattributed still emits) |
| `RESHARE_D2D` | `POST_CONVERT` | no | `table_manifest.is_delta_shared` |
| `ENABLE_COMPAT` | `POST_CONVERT` | no | `needs_compatibility_mode` + `compat_mode_sql` |
| `FIX_PATH_ACCESS` | `PRE_CONVERT` | **yes** | `has_path_based_access` |
| `UPGRADE_CONSUMER_DBR` | `PRE_CONVERT` | advisory | `has_below_floor_consumer` |
| `LEASE_OWNERSHIP` | `PRE_CONVERT` | yes | the latest `convert_authority` verdict is `SELF_AUTHORIZABLE` or `NEEDS_GRANT` (the convert identity is missing part of the convert permission) |
| `RESTORE_OWNERSHIP` | `POST_CONVERT` | no | paired partner of `LEASE_OWNERSHIP`, only when Authorize can grant the privileges itself (the revoke cleanup owes) |

### Freeze / restore pairs

`actions.FREEZE_KIND_PAIRS` maps each pre-convert freeze kind to its post-convert restore
partner. These bracket the conversion:

| `PRE_CONVERT` | `POST_CONVERT` |
|---|---|
| `FREEZE_COMPACTION` | `UNFREEZE_COMPACTION` |
| `STOP_STREAM` | `RESTART_STREAM` |

`job_03c_freeze` satisfies the pre-convert kind at freeze time; `job_05b_unfreeze_verify`
closes the paired post-convert kind at unfreeze time, and only after
[verification](#verify-before-done).

### The ownership pair

`LEASE_OWNERSHIP → RESTORE_OWNERSHIP` (kind names kept from the ownership lease) is the
**convert permission** pair, generated from `convert_authority` verdicts that are
`SELF_AUTHORIZABLE` or `NEEDS_GRANT` via `actions.generate_ownership_actions`. It brackets the
conversion the same way, with two differences from the freeze pairs:

- `LEASE_OWNERSHIP` (blocking) carries the exact `GRANT`s and who can run each. For a
  self-grantable table, `job_03d_lease_ownership` (Authorize) satisfies it by running them
  (recorded in `ownership_ledger`, `op_kind = GRANT`) and closes it once the table re-evaluates
  `AUTHORIZED`; otherwise `owner` names the first grantor and a human runs the grants. Ownership
  never changes; and
- its `RESTORE_OWNERSHIP` partner (only for self-grantable tables) carries the `REVOKE`s and is
  closed by `job_07_post_convert_cleanup` **at cleanup, not at unfreeze**, once every privilege
  Authorize added is revoked. Because the in-window `UNSET MANAGED` rollback needs the same
  privileges, they are held through the whole 14-day window.

## Enumerations

| Column | Exact domain |
|---|---|
| `scope` | `TABLE`, `SCHEMA` |
| `target_kind` | `JOB`, `PIPELINE`, `SHARE`, `CLUSTER`, `SOURCE_FILE`, `TABLE`, `NONE` |
| `phase` | `PRE_CONVERT`, `POST_CONVERT`, `POST_ROLLBACK` |
| `status` | `OPEN`, `IN_PROGRESS`, `DONE`, `OBSOLETE`, `DEFERRED` (only `RESHARE_D2D` / `ENABLE_COMPAT`; operator-set via `08`/`08b` `finish_action=defer`, returned to `OPEN` by `reopen`; never holds a wave, still listed in `migration_runbook`) |
| `confidence` | `high`, `medium`, `low` |

## Deterministic identity: `action_key`

```
action_key = "<action_kind>|<scope_ref>|<target_id or '-'>"
```

where `scope_ref` (`actions.scope_ref`) is:

- `full_name` when `scope = 'TABLE'`, and
- `catalog.schema` when `scope = 'SCHEMA'`.

It is readable and greppable rather than hashed, deliberately, so the queue is debuggable by
eye. Examples:

```
FREEZE_COMPACTION|main.sales|-
STOP_STREAM|main.sales.orders|job-4711
RESHARE_D2D|main.sales.orders|-
```

The same papercut always yields the same key. **This is what makes regeneration idempotent.**

`action_id` is a separate stable UUID assigned on first insert and never re-assigned.

## Regeneration is pure and idempotent

`actions.actions_from_manifest_row(row, ...)` is a pure function of one manifest row plus its
detail rows (`streaming_rows`, `compaction_rows`, `path_locations`) and an optional wave. It
**yields nothing for a clean row**.

The write path is:

1. Generate rows for the in-scope manifest rows.
2. `actions.dedupe_by_key(rows)`: collapse rows sharing an `action_key`, keeping the last.
   This is required: Delta MERGE errors when multiple source rows match one target, and a
   schema-scoped action generated once per table in the same schema would collide.
3. `actions.merge_action_rows(...)`: MERGE on `action_key`.
4. `actions.read_open_action_keys(..., where_predicate=...)` then `actions.mark_obsolete(...)`
   for keys that no longer appear.

**Read-only producers.** Only `00_profiler` and `03b_compaction_scan` generate actions.
Nothing in that path runs `SET MANAGED`, pauses a job, restarts a stream, re-shares, or
enables Compatibility Mode.

## The never-delete rule

Rows are **never deleted**. A cleared signal flips to `OBSOLETE` via `actions.mark_obsolete`,
which:

- only touches `OPEN` / `IN_PROGRESS` / `DEFERRED` rows (a set-aside action whose signal
  cleared is retired too), so a human-completed (`DONE`) action is never reopened or
  re-flagged, and
- is scoped by the regenerated `catalog`/`schema` slice (the `where_predicate` on
  `read_open_action_keys`), so out-of-scope actions are never marked obsolete merely because
  this run did not regenerate them.

The audit trail is therefore complete: you can always reconstruct what was raised, when, and
how it was closed.

## The three-way ownership split

This is the critical invariant for any external writer. The 30 columns of
`migration_actions` are partitioned **exactly once** into three sets, a partition guarded by
`src/tests/test_actions.py`.

| Set | Count | Columns | Lifecycle |
|---|---|---|---|
| `IDENTITY_COLUMNS` | 11 | `action_key`, `action_id`, `action_kind`, `scope`, `catalog`, `schema`, `table_name`, `full_name`, `target_kind`, `target_id`, `phase` | Set on first INSERT. Deterministic and immutable. **Preserved on UPDATE.** |
| `GENERATOR_OWNED_COLUMNS` | 12 | `title`, `detail`, `remediation_sql`, `remediation_api`, `owner`, `confidence`, `blocking`, `plan_run_id`, `wave_id`, `source`, `detected_at`, `updated_at` | Refreshed on **every** regeneration: the current truth of the signal. Anything you write here will be overwritten. |
| `OPERATOR_OWNED_COLUMNS` | 7 | `status`, `assignee`, `resolution`, `evidence`, `completed_at`, `completed_by`, `notes` | **Never clobbered by a regeneration.** This is the externally-writable surface. |

`merge_action_rows` sets only generator-owned columns on `WHEN MATCHED`. That is what lets a
re-profile run at any time without wiping human or executor progress.

The same pattern applies to the freeze ledger: `LEDGER_IDENTITY_COLUMNS` (11) and
`LEDGER_MUTABLE_COLUMNS` (17) partition the 28 `freeze_ledger` columns exactly once, guarded
by `src/tests/test_executor.py`. The `ownership_ledger` follows the identical discipline:
11 identity + 11 mutable over its 22 columns, and the captured `prior_owner` (an identity
column) is never overwritten once set.

## Writing to the queue

Use the provided helpers, or replicate their column discipline exactly.

### `actions.set_action_progress(...)`

Updates operator/executor-owned columns for a list of `action_key`s:

```python
set_action_progress(
    spark, manifest_catalog, manifest_schema, keys,
    status=...,            # OPEN | IN_PROGRESS | DONE | OBSOLETE | DEFERRED
    resolution=...,        # optional
    evidence=...,          # optional
    completed_by=...,      # optional
    completed_at=...,      # optional
    updated_at=...,        # optional
)
```

The freeze executor, unfreeze verifier, re-share executor, and compat executor all go through
this; they write nothing else.

### Assignee routing

Assignment is a **separate pass**, never part of the generator MERGE:

- `actions.resolve_assignee(owner, mapping)`: pure. `mapping` is an operator-supplied
  `{owner: assignee}` dict; a `"*"` key is a catch-all default. Returns `None` when nothing
  matches; it never guesses.
- `actions.assign_actions_from_owner_map(...)`: writes **only** the `assignee` column on
  `OPEN` / `IN_PROGRESS` rows, is idempotent (rows already carrying the resolved assignee are
  left alone), accepts an optional `where_predicate` to scope the pass, and is a no-op for an
  empty mapping.

Because `assignee ∈ OPERATOR_OWNED_COLUMNS`, a later re-profile preserves it.

### If you write SQL directly

Restrict your `UPDATE` to the operator-owned set and key on `action_key`:

```sql
UPDATE <manifest_catalog>.<manifest_schema>.migration_actions
SET status       = 'IN_PROGRESS',
    assignee     = 'data-platform-oncall',
    notes        = 'tracked in TICKET-1234',
    updated_at   = current_timestamp()
WHERE action_key = 'STOP_STREAM|main.sales.orders|job-4711'
  AND status IN ('OPEN', 'IN_PROGRESS');
```

> `updated_at` is **generator-owned**. Writing it is harmless (the next regeneration
> overwrites it), but do not rely on it to record *your* write time; use `completed_at` or
> `notes` for that.

**Never** write `action_key`, `action_id`, `action_kind`, `scope`, `catalog`, `schema`,
`table_name`, `full_name`, `target_kind`, or `phase`. Changing an identity column breaks the
MERGE and will duplicate the action on the next regeneration.

**Never** insert new rows. Action rows are derived from manifest signals; a hand-inserted row
has no generator and will be marked `OBSOLETE` on the next regeneration of its scope.

## Verify before DONE

Freeze, restart, re-share, and compat actions advance to `DONE` only when platform truth
confirms the change:

| Kind | Verification |
|---|---|
| `FREEZE_COMPACTION` (job) | the job is actually paused |
| `FREEZE_COMPACTION` (schema) | Predictive Optimization is actually disabled |
| `RESTART_STREAM` | a run started **after** `convert_ts` |
| `UNFREEZE_COMPACTION` | the prior state was restored |
| `RESHARE_D2D` | `reshare.reshare_verified`: the table is present in the share after re-add |
| `ENABLE_COMPAT` | `compat.compat_verified`, via live table properties |
| `LEASE_OWNERSHIP` | the table is actually owned by the migration group |
| `RESTORE_OWNERSHIP` | the table is actually owned by the captured `prior_owner` |

Applied-but-unverified stays `IN_PROGRESS`. An external control plane should apply the same
discipline: do not mark `DONE` on "the API call returned 200"; mark `DONE` on observed truth.

## Manual and advisory actions

Some actions can never be auto-operated, and the tooling deliberately keeps them visible
rather than silently dropping them:

- **Unattributed freeze/stop actions.** `executor.freeze_op_for_action` returns `None` for a
  null-target or unattributed action, so it stays MANUAL.
- **A table with no attributable Delta share.** Surfaced with a `<SHARE_NAME>` placeholder.
- **Compatibility Mode without a supplied location.** The empty external-location path is a
  human storage decision; the tool never invents one.
- **A convert permission someone else must grant.** A `NEEDS_GRANT` verdict keeps the
  `LEASE_OWNERSHIP` action MANUAL: it carries the exact `GRANT`s and, per grant, who can run it
  (`ask_text`, from `authority.py`); the tool never names a grantor of its own. An `UNKNOWN`
  verdict raises no action, but the `04` gate still blocks the table. Ownership never changes.
- **`FIX_PATH_ACCESS`.** Always advisory in the sense that no executor rewrites consumer
  code, but it is **blocking `PRE_CONVERT`**. Its `detail` carries a precise **per-consumer**
  remediation from the pure `actions.build_path_fix_remediation`: the exact name-based
  identifier plus, from the tier-2 `path_access_locations` scan, a WHO/WHERE/WHICH bullet per
  consumer (entity kind + name, source path / cell / line, matched path literal). It falls
  back to `table_manifest.path_access_entities` when the scan has no hit, and routes verbatim
  into the runbook.

## The manual-statement contract

Every supported action kind emits a non-empty `remediation_sql` **or** `remediation_api`, and
every freeze/restore op returns its exact statement (`executor.op_statement` /
`executor.restore_statement`). Both are guarded by tests. That guarantee is what makes the
[`migration_runbook`](../integration/control-tables.md#migration_runbook-view) view
trustworthy: it is never silently empty when work is owed.

## How the queue gates the conversion

- `wave_action_status` rolls open actions up per `(plan_run_id, wave_id)`.
- `wave_status` `LEFT JOIN`s it, so a data-ready wave with open pre-convert actions reports
  `AWAITING_ACTIONS` instead of `READY`, and a data-complete wave with open post-convert
  actions reports `AWAITING_ACTIONS` instead of `DONE`.
- `job_04_convert` fails fast on open **blocking** `PRE_CONVERT` actions, unless
  `require_pre_convert_actions_cleared=false`.
- `job_04_convert` also enforces the **convert permission** gate (parallel to the compaction /
  stream coverage gates): every table still to convert must be `AUTHORIZED`, read live as the
  convert identity, else it fails loud, prints the exact `GRANT`s grouped by grantor, and suggests
  `job_03d_lease_ownership` (Authorize). Gated by `require_ownership_leased` (default `true`).
- The conductor recommends the optional, non-mutating `freeze` / `unfreeze` / `actions` step
  for an `AWAITING_ACTIONS` wave rather than falsely reporting `done`.

## Related pages

- [../integration/control-tables.md](../integration/control-tables.md): full schemas, including the freeze ledger.
- [../integration/external-control-plane.md](../integration/external-control-plane.md): the safe-write contract in integration terms.
- [../operations/runbook.md](../operations/runbook.md): where the freeze bracket sits in the sequence.
