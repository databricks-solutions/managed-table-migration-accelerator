# Privileges: tiered least-privilege recipe + preflight

What the **profiler `run_as` identity** needs to see the estate, why coverage is bounded, and
the exact `GRANT` statements to close each gap. The profiler's `run_preflight=true` cell
(off by default) probes this identity and prints these same statements; it **never executes a
GRANT**. Rationale: [`../design-deltas.md`](../design-deltas.md) ("Privilege contract (tiered)",
D3/D4). Column contract (unchanged by this page): [`../contracts/control-tables.md`](../contracts/control-tables.md).

> **A human runs GRANTs.** The preflight only prints them. Never run `ALTER TABLE ... SET
> MANAGED` from an editor, and never put credentials/tokens/secrets in code; read them from
> Databricks secret scopes.

## The tiers

| Tier | Profiler reads | Minimum privilege | Preflight probe |
|---|---|---|---|
| **Discovery** | `information_schema.tables` (system, then per-catalog fallback) | `BROWSE` per catalog (auto-filters metadata without `USE CATALOG`/`USE SCHEMA`/`SELECT`) | `can_read_system_information_schema` **or** `can_read_catalog_information_schema` |
| **Enrichment** | `DESCRIBE DETAIL`, `DESCRIBE HISTORY`; `information_schema.row_filters`/`column_masks` | `USE CATALOG` + `USE SCHEMA` + `SELECT` per catalog or schema. `READ METADATA` does **not** substitute (tested, below) | `can_describe_detail` **and** `can_describe_history` |
| **System tables** | `system.access.table_lineage`, `system.compute.clusters`, `system.lakeflow.job_task_run_timeline` | `USE CATALOG` on `system` + explicit `SELECT` on each schema (these do **not** auto-filter) | `can_read_system_lineage` **and** `can_read_system_compute` **and** `can_read_system_lakeflow` |
| **Workspace reach** *(advisory)* | all of the above, per workspace | run **in each workspace** whose bound catalogs you must cover | none (cannot be proven from one run) |

Delta-sharing discovery (`SHOW SHARES` / `SHOW ALL IN SHARE`) and `SHOW CATALOGS` degrade
honestly when denied: the run continues and the coverage report records the gap.

## Exact GRANTs per tier

Replace `<principal>` with the profiler SP's **application id** (or a group). Backtick any
principal/identifier with special characters. `<catalog>` is each in-scope catalog.

```sql
-- Discovery (per catalog, or estate-wide to All account users)
GRANT BROWSE ON CATALOG `<catalog>` TO `<principal>`;
GRANT BROWSE ON CATALOG `<catalog>` TO `All account users`;   -- recommended estate-wide

-- Enrichment (granular, least-privilege, per catalog; inherit to schemas/tables)
GRANT USE CATALOG ON CATALOG `<catalog>` TO `<principal>`;
GRANT USE SCHEMA  ON CATALOG `<catalog>` TO `<principal>`;
GRANT SELECT      ON CATALOG `<catalog>` TO `<principal>`;
-- Grant and owner visibility for the convert-permission check (NOT enrichment; sensitive)
GRANT READ METADATA ON METASTORE TO `<principal>`;            -- METASTORE takes no name

-- System tables (do NOT auto-filter; grant explicitly)
GRANT USE CATALOG ON CATALOG `system`        TO `<principal>`;
GRANT SELECT      ON SCHEMA  `system`.`access`   TO `<principal>`;
GRANT SELECT      ON SCHEMA  `system`.`compute`  TO `<principal>`;
GRANT SELECT      ON SCHEMA  `system`.`lakeflow` TO `<principal>`;
```

`READ METADATA` exposes sensitive metadata (grants, masks, credential names) and is a child
privilege of `MANAGE`; grant it only to a trusted profiler SP.

**`READ METADATA` is not an enrichment grant (tested 2026-10-26).** With `READ METADATA` held on
the metastore or the catalog, plus `USE CATALOG` and `USE SCHEMA`, a service principal could read
`system.information_schema.tables`, the owner and `*_privileges` views and `SHOW GRANTS`, but
`DESCRIBE DETAIL`, `DESCRIBE HISTORY`, `DESCRIBE TABLE` and `SHOW TBLPROPERTIES` were each refused
for missing `SELECT` on the table. Enrichment needs `USE CATALOG` + `USE SCHEMA` + `SELECT`. A table
the profiler can discover but not describe is held `NEEDS_REVIEW` (`convert_identity_cannot_read`)
rather than reported as corrupted. The full matrix and its limits are in
[`../install-and-permissions.md`](../install-and-permissions.md#3-what-a-read-only-profiler-identity-actually-needs-tested).

## Recommended setup: one profiler service principal, wired via `run_as`

1. Create/choose a **single profiler service principal** and set `job_00_profiler` to
   `run_as` that SP (the bundle's jobs run as the deployer on every target; the installer's
   `jobs_identity=sp:<application-id>` moves every job to the SP, then redeploy). One identity = one attributable
   `discovered_by_principal` in the manifest (WS1 provenance) and one `principal` per
   `profiler_run_ledger` row.
2. Grant the SP the tiers above. For the widest one-shot **discovery**, use the estate-wide
   `BROWSE` to `All account users` + a metastore `READ METADATA`, then the `system.*` grants;
   enrichment still needs `USE CATALOG` + `USE SCHEMA` + `SELECT` on each schema you assess.
3. Run `job_00_profiler` with `run_preflight=true` once to print the report; close any GAP
   with the printed statements; re-run until Discovery/Enrichment/System tables read **OK**.

## The wall you cannot grant around: workspace-catalog bindings

Bindings **override grants**: a catalog not bound to this workspace stays invisible *even to
a metastore admin*. So **Workspace reach is advisory**: the preflight never emits a grant for
it. To cover a bound catalog you must run the profiler **in a workspace it is bound to** (D3),
and the manifest MERGE unions runs across workspaces (never deletes unseen rows). Mutating
stages (convert/rollback/cleanup) get exactly one owning workspace per catalog (D4). "No rows
from a catalog" is never proof it is empty; it may be unbound here or invisible to this SP.
The opt-in `scan_bindings=true` profiler scan enumerates this blind-spot matrix
(`coverage_matrix`); see [`../design-deltas.md`](../design-deltas.md) (D6).

## Convert permission: what the convert identity needs

The tiers above are what the **profiler** needs to *read* the estate. Converting is different:
`ALTER TABLE ... SET MANAGED` (and the in-window `UNSET MANAGED` rollback) runs as the
**executing identity** (`job_04_convert`'s `run_as`: the deployer by default on every target, or
the service principal chosen with the installer's `jobs_identity`), and that identity needs, per
table:

- `USE CATALOG` on the catalog and `USE SCHEMA` on the schema (a catalog owner has the first
  implicitly but not the second; a schema owner has `USE SCHEMA`);
- **OWNER or `MANAGE`** on the table, its schema or its catalog — direct, inherited, or through a
  group (`ALL PRIVILEGES` does not include `MANAGE`);
- `SELECT` and `MODIFY` on the table, unless it owns the table.

Ownership is **not** required, and the tooling never changes it. A metastore admin is not
authorized by that alone, but can grant itself what is missing. When a privilege is missing,
`SET MANAGED` fails with a misleading "not an owner" message; `authority.explain_failure`
translates it.

One rule (`migration.authority.evaluate`) reads these facts as the identity itself and writes a
verdict per table into `convert_authority`: `AUTHORIZED`, `SELF_AUTHORIZABLE` (the identity can
grant itself the rest), `NEEDS_GRANT` (someone else must; the verdict names who, narrowest owner
first, with the exact statement), or `UNKNOWN` (facts unreadable; treated as not authorized). The
app, MCP tools and Genie only read that table.

1. The profiler, the dry run (Check 4), the conductor and the bulk cockpit refresh the verdicts.
2. `job_03d_lease_ownership` (**Authorize**, PRE_CONVERT, gated, dry-run by default) grants a
   `SELF_AUTHORIZABLE` table only its missing privileges and records each in `ownership_ledger`
   (`op_kind = GRANT`, with `prior_held` / `added`). A `NEEDS_GRANT` table stays MANUAL: the run
   prints a grant script grouped by grantor.
3. `job_04_convert`'s **convert permission** gate re-reads the verdict live and blocks every table
   that is not `AUTHORIZED` (`require_ownership_leased`, default `true`).
4. `job_07_post_convert_cleanup` (gated by `confirm_execute`) revokes only the privileges
   Authorize added, once the table is `CLEANED` or `ROLLED_BACK` — never one held before, and a
   shared schema/catalog grant only when no other table still needs it. It is at cleanup, **not**
   unfreeze, because the 14-day `UNSET MANAGED` rollback needs the same privileges.

> **A human runs the grants the SP cannot.** The tool surfaces the exact statement and who can run
> it, never works around a missing privilege, and never runs `ALTER TABLE ... SET MANAGED` from an
> editor.
