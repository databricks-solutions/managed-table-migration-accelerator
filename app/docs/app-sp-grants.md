# App service principal — identity, attribution, and the grants an admin must provision

**Status** Built. Companion to
[`control_plane.md`](./control_plane.md) — especially Part 0 ("the app reflects; the job
enforces") and Part 3.12 (Connection & policy). The installer that grants these sets (notebook,
CLI, Settings → Permissions, MCP) is designed and recorded as built in
[`backend/docs/install-and-permissions.md`](../../backend/docs/install-and-permissions.md).

---

## Decision

1. **The jobs' identity is one bundle variable.** Every stage job carries
   `run_as: ${var.jobs_run_as}` (a complex variable: `service_principal_name`, `group_name` or
   `user_name`). It is the identity that executes `SET MANAGED` / `UNSET MANAGED` / `VACUUM`.
   The default is whoever deploys. `prod` keeps its earlier behavior: `jobs_run_as` is set from
   `var.prod_run_as_sp`, normally the app SP, so one SP both serves the app and runs the jobs.
   The installer's `jobs_identity` option writes a different choice to
   `.databricks/bundle/<target>/variable-overrides.json` and redeploys. The app SP always stays
   the app-execution identity (control-plane reads, the single `migration_actions` write,
   `jobs.runNow`). The `run_as` sits on each job, not on the target, because a target-level
   `run_as` also applies to the app, and the Apps platform refuses a `run_as` other than the
   app's owner.
2. **`operator` is attribution only — never authorization.** App access (`CAN_USE` on the
   App) is the authorization boundary; this is an admin-only app. We do **not** gate
   triggers per-user (no OBO *authorization* step like Genie Workbench needs, because
   there arbitrary users with varying per-resource rights act on shared objects). We *do*
   record **who** clicked, for the control-plane audit trail.
3. **Day-2 operators need no admin — the *installer* does, once.** Serving the app and
   attributing the operator require no admin (an operator just needs App `CAN_USE`). But
   *deploying* the bundle and *provisioning* the SP (grants + convert authority)
   require an appropriately privileged **installer** at setup time — see "Who
   installs/deploys" below. `SET MANAGED` needs ownership **or** `MANAGE` on the table, its
   schema or its catalog, plus `SELECT` + `MODIFY` unless the identity owns the table; ownership
   never changes (see "Convert permission — who can grant what" below).

---

## Identity model (how `operator` flows)

- The signed-in user is read from the platform-injected request header
  `X-Forwarded-Email` (fallback `X-Forwarded-Preferred-Username`, which on Apps carries the display
  name). This needs **no OAuth
  scope and no admin** — it is request metadata, not an OBO call.
- The trigger route (`app/server/routes/job-trigger.ts`) threads it as an `operator` job
  parameter on `jobs.runNow`, for the mutating stages at minimum (`convert`, `rollback`,
  `cleanup`) and ideally all.
- Each notebook stamps it into `audit_log.operator` / `gate_eval.operator`, replacing the
  current `SELECT current_user()` (which, because the job runs as the SP, records the SP,
  not the human):

  ```python
  _op = dbutils.widgets.get("operator")
  OPERATOR = _op or spark.sql("SELECT current_user() AS u").collect()[0]["u"]
  ```

  The `or current_user()` fallback keeps CLI / notebook / fixture runs honest.
- The `migration_actions` write route may stamp the same identity into `assignee` / `notes`
  provenance. **Execution stays 100% SP** — attribution never changes who runs the SQL.

---

## `run_as` — who actually executes (dev vs prod), and why it can't be OBO

A trigger involves **three** identities; only one of them runs the `ALTER TABLE`:

- **Trigger identity** = the **app SP** (`jobs.runNow`, holds `CAN_MANAGE_RUN` on the jobs).
  This is *not* OBO — the SP starts the run.
- **Execution identity** = the job's **`run_as`**, which is what actually executes
  `SET MANAGED`. It is a **static principal set at deploy time**, not the caller.
- **Operator** = the human who clicked, captured from the forwarded header for *attribution
  only* (above). It never changes who executes.

Because `run_as` is static:

| Target | `mode` | `run_as` resolves to (`var.jobs_run_as`) | Convert authority satisfied by |
|---|---|---|---|
| `dev` / `e2e` | `development` | the **deploying user** (the default) | the deployer's own ownership or `MANAGE` |
| `prod` | `production` | `${var.prod_run_as_sp}` (normally the app SP) | the **SP** — needs `MANAGE` (Tier 2) plus `SELECT` + `MODIFY` |
| any | any | the installer's `jobs_identity` choice, after the redeploy | that identity |

The jobs' identity also needs `CAN_MANAGE_RUN` on every job of the deployment, so the `09` /
`11e` cockpits and the conductor tails can start other jobs. The installer plans and grants it
(a `JOBS` line, an additive Permissions API update that keeps the app SP's own run permission).
It is not a bundle `permissions` block: bundle-managed job permissions replace the whole access
list, which would remove the run permission the app's job bindings give the app SP. Deploying
as a service principal? Set `jobs_run_as`: the default names a user. The Settings grants panel
addresses Tier 2 to the identity read from the deployed jobs' `run_as`, and shows a
`<jobs-run-as>` placeholder when the jobs disagree or cannot be read.

**This is why converts "just worked" in dev/e2e:** the job ran as *you* (the deployer) and
you own the seeded estate — the SP was never the executor. The prod/SP authority path is a
*different* code path that dev testing never exercises; validate it explicitly before ship.

**Why `run_as` can't be "the OBO / current user":** a Job's `run_as` is a single fixed
principal (one user *or* one SP); `runNow` has **no `run_as` override**, and a short-lived
OBO token cannot back a long-running async job (the same reason user tokens are never
forwarded into jobs). You *could* statically set prod `run_as` to a specific admin **user**
(converts would work via that user's authority), but every run would then execute as that one
human — brittle, non-rotatable, and still needing the `operator` param for attribution — which
is exactly why prod uses an SP. A group (`run_as.group_name`) is possible too, through
`jobs_run_as`; a dedicated service principal is the recommended production choice because it
outlives the app.

---

## Two identities, four grant sets (what the installer grants)

The installer plans every grant in one reviewed plan, split by **grantee**:

| Identity | Who it is | Grant sets it receives |
|---|---|---|
| **The app's service principal** | Created with the app; reads the control plane and starts runs | **Control**: Tier 1 below (`job_99_setup` grants it) |
| **The jobs' identity** | The deployed jobs' `run_as` (`jobs_run_as`), read from the jobs, never typed | **Control** (`USE CATALOG` + `CREATE SCHEMA` on the control catalog, `CAN_READ` on the deployment folder, `CAN_MANAGE_RUN` on its jobs), **Look** (`BROWSE` or `READ METADATA`, and the `system` schemas), **Read** (`USE SCHEMA` + `SELECT` per migrated schema) and **Change** (`MANAGE` per migrated schema, all at once or per wave, handed back after cleanup) |

The installers' group (`installer_group`, falling back to the installer) gets write access to the
install records (`migration_scope`, `ownership_ledger`, `audit_log`). Grants run as the person at
the keyboard: the notebook session, the CLI profile, or the signed-in admin in Settings →
Permissions (their forwarded token, never the app's identity). The app's own service principal gains
no new authority. Tier 2 below is the same Read + Change scope, as copy-paste SQL for an admin who
prefers to run it by hand.

## The two grant tiers

### Tier 1 — control-plane access (already automated)

`job_99_setup` resolves the SP at runtime and issues the least-privilege read set + the one
write. Unchanged; documented here for the fallback when `USE CATALOG` exceeds the setup
identity's authority (README → "App service principal grants").

```sql
GRANT USE CATALOG ON CATALOG  <control_catalog>                                    TO `<app-sp-client-id>`;
GRANT USE SCHEMA  ON SCHEMA   <control_catalog>.<control_schema>                   TO `<app-sp-client-id>`;
GRANT SELECT      ON SCHEMA   <control_catalog>.<control_schema>                   TO `<app-sp-client-id>`;
GRANT MODIFY      ON TABLE    <control_catalog>.<control_schema>.migration_actions TO `<app-sp-client-id>`;
```

### Tier 2 — data-plane, to actually conduct the migration (admin-provisioned)

This is the tier the app must surface **prominently**, because it is NOT automated and is
the real gate on a new workspace. The public
[convert doc](https://learn.microsoft.com/en-us/azure/databricks/tables/convert-external-managed)
lists the external-table prerequisite as **"Permissions: Ownership of the table."** The observed
rule is wider, and it is the one this tooling enforces (`backend/src/migration/authority.py`,
pinned by the live matrix in `backend/src/tests/fixtures/convert_authority_cases.json`, where
every case records what `SET MANAGED` actually did). The convert identity needs, on each table:

- **Usage**: `USE CATALOG` and `USE SCHEMA`;
- **Authority**: ownership **or `MANAGE`** on the table, its schema, or its catalog (direct,
  inherited, or through a group). Metastore admin alone does not count, and `ALL PRIVILEGES`
  never includes `MANAGE`;
- **Data**: `SELECT` and `MODIFY`, unless it owns the table.

**Ownership never changes.** The `SET MANAGED` error "User is not an owner" is raised for a missing
authority even when ownership is not needed (`authority.explain_failure` says so).

> This only bites when `run_as` is a service principal (prod by default). With the default
> `jobs_run_as`, `run_as` is the deploying user, whose own privileges usually cover convert —
> see the `run_as` table above.

Per target catalog/schema being migrated:

```sql
-- Namespace access (so the SP can reference and read the tables)
GRANT USE CATALOG ON CATALOG <target_catalog>                 TO `<app-sp-client-id>`;
GRANT USE SCHEMA  ON SCHEMA  <target_catalog>.<target_schema> TO `<app-sp-client-id>`;
GRANT SELECT      ON SCHEMA  <target_catalog>.<target_schema> TO `<app-sp-client-id>`; -- profiler / dry-run / validate reads

-- Freeze bracket: toggle Predictive Optimization on the schema (03c / 05b)
GRANT MANAGE      ON SCHEMA  <target_catalog>.<target_schema> TO `<app-sp-client-id>`;

-- CONVERT / ROLLBACK: schema MANAGE above is the authority for every table in the schema.
-- What is still missing per table (usually table MODIFY) Authorize grants itself, and cleanup
-- revokes it. To authorize every table up front instead (a standing write grant, your call):
-- GRANT MODIFY   ON SCHEMA  <target_catalog>.<target_schema> TO `<app-sp-client-id>`;
```

#### Authorize: what the app grants itself, and the grant script for the rest

The app no longer leases ownership or takes an owner group (the in-app owner-group override and its
`GET /api/grants/group-membership` pre-check are removed; `table_owner_group` is deprecated). The
stored `convert_authority` verdict per table (written by the profiler, the dry run, the conductor and
the bulk cockpit) says what the convert identity is missing and **who can grant it**. Authorize
(`job_03d_lease_ownership`, `confirm_execute=true`) grants only what the convert identity can grant
itself, recorded in `ownership_ledger` and revoked by cleanup (`07`); the rest stays MANUAL. The app
generates the per-grantor grant script at `GET /api/grants/convert-permissions.sql` (old path
`/api/grants/ownership.sql` kept as an alias). SR12 holds — the app only triggers the job and
downloads a script; it never runs a GRANT.

Prerequisites that are **not** grants (call them out; do not try to `GRANT` them):

- **A managed location** must be configured on the target catalog or schema (the convert
  hard-gate blocks otherwise). The SP writes converted data there via that location's
  storage credential.
- **Compaction / stream freeze** needs `CAN_MANAGE` on the specific OPTIMIZE/VACUUM jobs and
  DLT / Lakeflow pipelines (workspace ACLs, not UC grants).
- **Delta Sharing re-share** (`ALTER SHARE`) needs the SP to own the D2D share (or a
  provider admin runs the two `ALTER SHARE` statements from the runbook).
- **Runtime:** `SET MANAGED` / `UNSET MANAGED` / `VACUUM` need DBR 17.3 LTS+ or Serverless.

### Convert permission — who can grant what

When the Tier 2 grants are not in place (or a table sits outside them), each `convert_authority`
row names what is missing and, per grant, **who can run it** (`grant_requests[].grantors`, then an
`ask_text` sentence every surface shows verbatim). Grantors are derived per securable level:

- a table-level grant (`MANAGE`, `SELECT`, `MODIFY`): the table owner, then the schema and catalog
  owners, then any principal with `MANAGE` on it, then a metastore admin;
- `USE SCHEMA`: the schema owner, then the catalog owner, then `MANAGE` holders, then a metastore admin;
- `USE CATALOG`: the catalog owner, then `MANAGE` holders, then a metastore admin.

If the convert identity can grant the whole gap itself (it holds `MANAGE`, owns a parent, or is a
metastore admin), the verdict is `SELF_AUTHORIZABLE` and Authorize closes it. Otherwise it is
`NEEDS_GRANT` and the app's grant script (`GET /api/grants/convert-permissions.sql`) groups the
statements by who must run them.

Prefer schema-grain grants (Tier 2) over per-table asks: one schema `MANAGE` covers every table in
the schema, including tables nobody has enumerated yet. Making the SP a metastore admin would skip
the asks but hands a standing bot metastore-wide power — least preferred.

---

## Convert permission: communication & escalation

The earlier design moved table ownership to a migration group for the conversion window and
restored it at cleanup (the older ownership model, the "ownership lease" of permissioning Plans 3–4). It worked, but an
ownership change reads as a privilege grab and the previous owner temporarily lost their implicit
owner privileges. Convert authority removes that: **ownership never changes**, so no owner loses
anything and there is nothing to restore.

- **Least privilege, bounded in time.** The SP's standing footprint stays schema-grain grants —
  **never** `ALL PRIVILEGES`, **never** SP-as-metastore-admin. Anything Authorize adds is recorded
  (`ownership_ledger`, `added=true`) and revoked at cleanup; a privilege held before is never
  revoked. An open `RESTORE_OWNERSHIP` action (the kind name is historical) is visible debt until
  the revoke lands.
- **Separation of duties.** The tool *proposes* SQL; a person with authority runs anything the SP
  cannot grant itself. The app never runs a GRANT (§0.2 "not a SQL runner"); only the gated
  Authorize job grants, and only what the SP may grant itself.
- **Ask precisely.** Each request names the principal to ask and the exact statement, so "ask a
  catalog owner or metastore admin" becomes one sentence per grant, not a hunt.
- **Full audit.** `operator`, the grant and revoke times, and each row's `added` / `prior_held`
  are captured (`ownership_ledger` + `audit_log`, `GRANT_PRIVILEGE` / `REVOKE_PRIVILEGE`).

**The CUJ, end to end:** profile → read the verdicts → Authorize what the SP can grant itself →
send the grant script for the rest → re-check → convert → validate → (past day 14) cleanup revokes
what Authorize added.

---

## Authorize as a freeze-bracket (built)

The reversibility guarantee is the **freeze bracket** (`03c_freeze` / `05b_unfreeze_verify` +
`freeze_ledger`) applied to privileges: change reversible platform state around a convert, record
what changed in a ledger, undo exactly that, verify, never delete.

- **One rule, one table.** `authority.py` computes the verdict; the profiler, the dry run
  (Check 4), the conductor, the bulk cockpit, Authorize and the convert store it in
  `convert_authority`. The app, MCP and Genie only read it.
- **Paired actions.** `LEASE_OWNERSHIP` (`PRE_CONVERT`, blocking) carries the missing `GRANT`s and
  who can run each; `RESTORE_OWNERSHIP` (`POST_CONVERT`) carries the `REVOKE`s, only when Authorize
  can grant the privileges itself. Both come from `SELF_AUTHORIZABLE` / `NEEDS_GRANT` verdicts.
- **`ownership_ledger`.** One `GRANT` row per privilege; MERGE-idempotent; **never deleted**
  (`PLANNED → GRANTED → REVOKED`). `prior_held` is the exact-undo input: a privilege held before is
  never revoked.
- **Timing differs from freeze.** Freeze restores at `05b`. The added grants are held **until
  `CLEANED` (or `ROLLED_BACK`)**, because `UNSET MANAGED` needs the same permission for 14 days.
  A schema or catalog grant shared by several tables is revoked only after the last of them.
- **Gated and verified.** Authorize (`job_03d_lease_ownership`) and the cleanup revoke default to a
  dry run that persists the exact statements; execute verifies with `SHOW GRANTS` before a row
  advances. `04_convert` re-reads the permission live and stops before any change unless every
  table is `AUTHORIZED` (`require_ownership_leased`, default `true`).
- **Legacy drain.** Leases taken before convert authority (`LEASE` rows, `LEASED`) are still
  restored to their captured `prior_owner` at cleanup; no new lease is ever taken.

The contract is authoritative in
[`control-tables.md`](../../backend/docs/contracts/control-tables.md) (`convert_authority`,
`ownership_ledger`, the action kinds) and [`jobs.md`](../../backend/docs/contracts/jobs.md)
(§6b, §7, §11). The operator guide is
[`docs/07b-convert-permissions.md`](../../docs/07b-convert-permissions.md).

---

## Who installs/deploys — the rights the installer needs

"The installer should have admin rights" is right, but it's **two different hats**, and you
usually don't need the top of both. These are **one-time, install-time** requirements — day-2
operators need only App `CAN_USE`.

### Hat 1 — platform (workspace) side, to `bundle deploy`

Deploying the bundle creates the stage Jobs, the AI/BI dashboard, the Genie handshake, **and the
Databricks App**. The binding constraint is usually **app creation**, which typically requires
**workspace admin** (or an explicitly granted Apps capability). Also needed: create/manage
Jobs & the dashboard, `CAN_USE` (or `CAN_MANAGE` to bind) on `var.warehouse_id`, and write
access to the deploy path. → **Practically: workspace admin.**

### Hat 2 — governance (Unity Catalog) side, to provision the SP (and, in dev, to convert)

This is where real authority is required, because you must **grant** to the SP and give the
convert identity its **convert authority**:

- Create the control schema + issue Tier 1 grants → **owner of / `MANAGE` on the control
  catalog** (or metastore admin). `USE CATALOG` on a *pre-existing shared* catalog often needs
  the catalog owner / metastore admin — this is the documented `job_99_setup` fallback.
- Issue Tier 2 namespace grants on each target → **owner of / `MANAGE` on** those
  catalogs/schemas. Schema-grain `MANAGE` plus `SELECT` + `MODIFY` is the convert authority
  for every table in the schema; nobody's ownership changes.
- Any grant the convert identity cannot issue itself → the principal `authority.grantors`
  names (table owner, then schema and catalog owners, then `MANAGE` holders, then a metastore
  admin), listed per table in the Settings page's `convert-permissions.sql`.
- **Dev/e2e only:** since `run_as` = the deployer, the deployer's *own* identity must also
  satisfy every stage at run time — convert authority, `MANAGE` for the PO freeze,
  `CAN_MANAGE` on compaction jobs / pipelines, and ownership of any D2D shares.

→ **Practically:** for a **scoped** migration, *workspace admin* + *owner (or `MANAGE`) on the
control + target catalogs*. For a **whole-estate** migration, the simplest single answer is
**workspace admin + metastore admin**. **Account admin is not required** unless you're also
creating or rotating the SP.

The honest framing for customers: *you designate **one** privileged installer for a one-time
setup; you do **not** hand admin to everyone who uses the app.*

---

## First-run bootstrap & estate discovery (the chicken-and-egg)

On a brand-new workspace the SP starts with nothing, which creates a bootstrap ordering
problem the app must handle honestly:

**Detecting "fresh / ungranted."** The app's boot read of `migration_control_config`
fails with `INSUFFICIENT_PERMISSIONS` (SQLSTATE `42501`) when Tier 1 is absent. That signal
already exists verbatim via `GET /api/diag/control-read` (`app/server/routes/diag.ts`).
The first-run experience keys off it: control-plane read fails ⇒ show the **Tier 1** grant
snippet; control-plane reads succeed but `table_manifest` is empty ⇒ "run the profiler";
profiler under-discovers ⇒ the SP is missing **Tier 2** on some namespaces.

**The discovery gap.** `job_00_profiler` runs **as the SP**, so it can only discover the
estate the SP can already see. `system.information_schema` is itself permission-filtered, so
SP-side discovery cannot enumerate namespaces the SP has no grant on. That means the app
cannot, by itself, tell the admin the *full* list of what to grant before the grants exist.

**Three discovery layers, in increasing authority.** OBO is a convenience pre-fill; it is
**never** the ceiling on what gets migrated. The authoritative estate is always whatever the
SP can see *after* grants, discovered by the profiler.

1. **OBO pre-scan (adopted) — the admin's visibility, a hint.** Read the admin's forwarded
   access token (`X-Forwarded-Access-Token`) and, with catalog read scopes, enumerate the
   EXTERNAL tables the **admin** can see (admins/owners typically see far more than a fresh
   SP). Use it to pre-fill the target scope and generate the exact `GRANT` statements that
   bring the SP up to the admin's visibility.
   - Requires adding to `app/resources/app.yml` `user_api_scopes`:
     `catalog.catalogs:read`, `catalog.schemas:read`, `catalog.tables:read`.
   - **How it lists (built):** the paged catalog list, then one paged
     `GET /api/2.1/unity-catalog/table-summaries?catalog_name=…` per catalog (full name + table
     type, up to 10k per page), eight catalogs at a time, inside a 20-second deadline and a
     2,000-call cap (`estate-discovery.ts` `walkCatalogs`). A catalog the admin cannot read is
     counted as `catalogsDenied`, never fatal. When the deadline or the cap stops the walk, the
     response carries `truncated: true` and `coverage` (catalogs total / scanned / denied) and the
     panel says "Scanned X of Y catalogs" — a partial list is never presented as complete. Results
     are cached per user (keyed by the forwarded email, never the token) for 5 minutes;
     `?refresh=1` (the panel's Refresh button) rescans.
   - **OBO is used for discovery/attribution only — never to trigger jobs.** Authorization
     stays App `CAN_USE`; execution stays the SP.
   - **A grant the SP can't give itself still needs a person.** OBO can *enumerate* tables,
     but a grant on a securable the SP has no `MANAGE` on must be run by its owner, a `MANAGE`
     holder or a metastore admin (the `convert_authority` grantors). The app only *generates*
     the SQL; it never executes a grant (consistent with §0.2 "not a SQL runner").
2. **Admin-typed scope — explicit intent, no scope cap.** The admin can always add
   catalog(s)/schema(s) manually. This is the escape hatch when OBO sees little (below).
3. **SP profiler → `table_manifest` — authoritative, post-grant.** After grants land,
   `job_00_profiler` (as the SP) builds `table_manifest`. *That* is the real migration
   scope, and it can be **broader than what OBO saw** — e.g. the admin granted the SP at the
   catalog/schema level, or via a group, covering tables the admin could not individually
   enumerate. The Settings panel regenerates from `table_manifest` once it exists.

### What if OBO discovers little (the admin has narrow visibility)?

This is a UX degradation, **not** a correctness problem — and it must be surfaced honestly
(SR7: "no evidence" is never "safe"; never fabricate a count).

- **Never present the OBO list as "the estate."** Label it exactly as *"external tables you
  (the signed-in admin) can currently see"* — the admin's visibility, not the workspace's.
  If it is small, say so and lead with the manual-scope escape hatch (layer 2).
- **Schema-level grants already cover unseen tables.** The Tier 2 namespace grants
  (`USE CATALOG` / `USE SCHEMA` / `SELECT` / `MANAGE` on a *schema*) apply to every table in
  that schema — including ones OBO never enumerated. So granting at catalog/schema grain, not
  per-table, is the recommended default and closes most of the visibility gap in one grant.
- **Per-table asks are the exception.** The convert-permission grant script lists only tables
  with a stored `convert_authority` verdict. A table the SP cannot see yet reads `UNKNOWN` (or has
  no row) and blocks the convert; once the namespace grants land, the profiler enumerates it and
  the script fills in.
- **A genuinely narrow admin is a real permissions limit, not a tooling gap.** If the admin
  can neither see nor grant broadly (not a catalog owner / metastore admin), the honest
  outcome is: they migrate only what they can own/grant, and the panel names who must act —
  *"ask a catalog owner or metastore admin to grant/own on `<catalog>` for `<app-sp>`."*
  This mirrors Genie Workbench: the SP must be provisioned by someone who can see the
  resource. The tool makes the ask precise; it does not pretend the estate is only what one
  under-privileged admin can see.

---

## In-app surface (Settings → Connection & policy)

> **Built — Plan 2 (`permissioning-02`).** The **"Grants the App SP needs"** panel
> (`app/client/src/pages/settings/GrantsPanel.tsx`) is implemented: it calls
> `GET /api/grants/plan` (Tier 1/2 SQL + `SHOW GRANTS` status + ownership summary),
> `GET /api/estate/obo-discovery` (admin-visibility scope pre-fill), and
> `GET /api/grants/convert-permissions.sql` (the per-grantor convert-permission grant script; the
> old `ownership.sql` path is an alias). The app generates and verifies; it never executes a grant.
>
> **Now titled "Permissions the app needs"**, and it sits below the **Permissions** panel
> (`app/client/src/pages/settings/PermissionsPanel.tsx`, `GET/POST /api/permissions*`). That panel
> is the installer's Settings front door: it runs the shared rights check and grant plan, and its
> **Grant** and **Hand back** run as the signed-in admin (their forwarded token), never as the app
> SP. This panel stays read-only.

A prominent, read-only **"Grants the App SP needs"** panel. The app **generates** the SQL;
it **never executes** it (consistent with `control_plane.md` §0.2 and SR12 download-not-send).

- **Parameterized from what the app already knows:** the SP identity (resolve via
  `current_user()` — the app runs as the SP — or `w.apps.get(...).service_principal_client_id`;
  fall back to a `<app-sp-client-id>` placeholder with a one-line "how to find it"), the
  control catalog/schema (env `MIGRATION_CATALOG` / `MIGRATION_SCHEMA`), and the **distinct
  target catalogs/schemas** (from `table_manifest` in-scope EXTERNAL rows post-profiler, or
  the admin's OBO-discovered/typed scope pre-profiler).
- **OBO list is labelled as the admin's visibility, never "the estate,"** and always sits
  next to a manual "add catalog/schema" input (discovery layer 2) so narrow OBO visibility
  never caps the scope.
- **Selection is opt-in (built).** Discovered schemas render as unticked suggestion chips with
  their external-table count; a typed scope is ticked on add. Only ticked scopes are sent as
  `GET /api/grants/plan?scopes=cat.sch,…`, and the plan returns Tier 2 for the manifest targets
  plus those scopes (each group labelled `source: scan | selected`; a scope already in the
  manifest stays one `scan` group). Tier 2 includes `MANAGE`, so nothing is pre-selected; "Select
  all" is an explicit click. A schema whose plan group is `source: scan` renders as a read-only
  "already covered by the scan" chip instead of a toggle: its grants are in Tier 2 whatever the
  selection, so "Select all" skips it and the "N selected" count excludes it. The server re-validates every scope against a strict
  `catalog.schema` pattern (letters, digits, underscores) and caps the list at 50
  (`grants.pure.ts` `parseScopeParam`), because the names are interpolated into `SHOW GRANTS`
  statements the SP executes; the client mirrors the pattern (golden cases in
  `app/server/routes/fixtures/scope_cases.json`) and shows why a typed name was rejected.
  The selection is saved in the browser (`localStorage`), never in a control table, and
  `targets_known` still means "the profiler has listed targets".
- **Prefer schema-grain grants** in the generated SQL (they cover tables OBO didn't list);
  the convert-permission grant script is the only per-table artifact, grouped by who must run
  each statement.
- **Copy button** for Tier 1 + the schema-level Tier 2 grants (short, static-shaped).
- **Built for large estates.** Tier 2 opens on a summary (schemas, landed, missing, not
  checked) and a **Missing only** view by default; schemas whose schema grants have all landed
  collapse into one "N schemas with every schema grant landed" line. Grants are grouped by
  catalog with `USE CATALOG` once per catalog; groups start closed past three catalogs and show
  ten schemas at a time. **Copy all missing**, **Copy all** and **Download .sql** always take every
  grant in the view, never only the open groups. The scope picker is grouped by catalog (a
  three-state tick-all per catalog), filterable, defaults to the schemas the scan does not cover,
  and scrolls inside a fixed height; a catalog tick or "Select all" that would pass the
  50-scope limit is disabled with the reason shown. Server side, `buildGrantsPlan` reads each
  catalog and schema once (`USE SCHEMA` / `SELECT` / `MANAGE` share one `SHOW GRANTS ON SCHEMA`),
  at most 8 statements at a time, so N schemas over C catalogs cost N + C reads instead of 4N.
  Successful reads are reused across plan loads for 60 s (a failed read is never kept, so it
  still reads `unknown`); the panel's **Refresh** sends `?refresh=1` to check again.
- **Download `convert-permissions.sql`** for the per-table grants the SP still needs, generated
  from the stored `convert_authority` verdicts (mirrors the runbook / comms-feed download). This
  handles "thousands of tables" without a giant on-screen block.
- **Honest states (SR7):** before the profiler runs the target list is unknown — say so,
  don't emit an empty grant block; the Tier 2 notice points at the scope picker instead. The
  picker shows one state per situation: checking (spinner only), unavailable (with the reason),
  nothing visible, partial scan (coverage text), and the listed schemas. A `SHOW GRANTS` verification read (like `job_99_setup`)
  can flag which Tier 1 grants have / haven't landed.

---

## Why this is enough (and why not more)

- A second operator needs **zero** per-user grants — execution is the SP. Onboarding a user
  = add them to the App's `CAN_USE`. Nothing else.
- The only friction is one-time Tier 2 provisioning per target namespace, which is exactly
  what the Settings panel makes copy-paste-able.
- We deliberately do NOT adopt Genie Workbench's per-user OBO *authorization*: it exists
  there because arbitrary users with varying per-space rights act on shared resources. Here,
  App access is the boundary and the SP is uniform, so **attribution** (not authorization)
  is the right and simpler contract.

---

## Not in this note (separate follow-up: code wiring)

This note settles the **decision and the grant contract**. The implementation is a distinct
change set:

- ✅ **Operator attribution — done (permissioning Plan 1).** `app/server/routes/job-trigger.ts`
  resolves `X-Forwarded-Email` (fallback `X-Forwarded-Preferred-Username`) via the pure
  `app/server/jobs/operator.ts` and injects `operator` into `job_parameters` **after** the
  `.strict()` Zod parse — deliberately **not** a per-alias schema field, so it is never
  client-spoofable. The 8 mutating job YAMLs declare the `operator` parameter and their
  notebooks stamp it into `audit_log.operator` / `wave_gate_evaluation.operator` with the
  `or current_user()` fallback.
- ✅ **Grants panel + OBO estate discovery — done (permissioning Plan 2).** The Settings panel
  generates/verifies Tier 1/2 grant SQL + the convert-permission grant script, and `GET /api/estate/obo-discovery`
  pre-fills scope from the forwarded token (`user_api_scopes` now includes the three
  `catalog.*:read` scopes). Discovery-only; never triggers a job; execution stays the SP.
  Ticked or typed scopes feed `GET /api/grants/plan?scopes=…`, so Tier 2 SQL exists before the
  first profiler run.
- ✅ **Ownership classification + owner notices — done (permissioning Plan 3).** The profiler
  captures `table_manifest.table_owner`; `migration.ownership.classify_change` buckets each table
  `NONE`/`JOIN_GROUP`/`LEASE`; `migration.comms.build_owner_notice` drafts `comms_feed.owner_notice`.
  Read-only foundation for the lease bracket. *(Superseded by convert authority: new plans write
  `ownership_change = NONE`.)*
- ✅ **Convert authority — done (replaces the Plan 4 lease bracket).** `authority.py` +
  `convert_authority`, Authorize (`03d`) granting only what is missing, the live `04` permission
  gate, and the `07` revoke of added grants. Ownership never changes. See "Authorize as a
  freeze-bracket (built)" above.
- ✅ **Ownership lease/restore execute bracket — done (permissioning Plan 4; superseded by convert
  authority, legacy restore kept).** `ownership.py` +
  `ownership_ledger` + the `03d_lease_ownership` stage (the older ownership model: temporary, verified, reversible `OWNER TO`),
  the fail-closed `require_ownership_leased` gate in `04_convert`, and the verified restore hook in
  `07_post_convert_cleanup` (restore held until `CLEANED`). Dry-run default; nothing auto-executes.
  The `lease_ownership` app trigger schema is wired and the app **binding** is live: `app.yml` binds
  19 jobs + 1 warehouse = 20 (exactly at the cap), with `job_03d_lease_ownership` taking the slot
  freed by folding the source scan into the profiler discovery chunk. `03d` is app-triggerable
  (and still Jobs-UI/CLI-runnable).
