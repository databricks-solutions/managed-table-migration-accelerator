# Databricks notebook source
# MAGIC %md
# MAGIC # 99 — Setup: provision the admin App's consumers (Genie space + grants)
# MAGIC
# MAGIC **Install-time provisioning, split out of the profiler.** The profiler is a
# MAGIC standalone deliverable — a customer can run it against their estate with no App
# MAGIC installed — so it carries no App/Genie provisioning burden. This job does the
# MAGIC provisioning that only matters once the admin App is deployed:
# MAGIC
# MAGIC 1. `ensure_control_plane` (idempotent — makes this job self-sufficient; the Genie
# MAGIC    create API needs the control tables to EXIST, but not to have rows).
# MAGIC 2. Create/update the read-only Genie space over the control tables (SDK).
# MAGIC 3. Grant the App's service principal `CAN_RUN` on that space + the least-privilege
# MAGIC    Unity Catalog control-plane grants, verified via `SHOW GRANTS`.
# MAGIC 4. Persist `genie_space_id` + `dashboard_id` into `migration_control_config`
# MAGIC    (a PARTIAL upsert — it never blanks the `catalog`/`schema`/`warehouse_id` the
# MAGIC    profiler wrote).
# MAGIC 5. Publish the Genie knowledge docs to the volume and prune retired ones
# MAGIC    (best-effort; runs before step 2 so indexing starts on the final files).
# MAGIC
# MAGIC **Ordering: run this BEFORE or AFTER the profiler.** Because it calls
# MAGIC `ensure_control_plane` itself, the tables exist regardless; and because the config
# MAGIC write is a partial upsert, neither job blanks the other's fields. There is no
# MAGIC install-order constraint between this job and the profiler.
# MAGIC
# MAGIC Everything here is best-effort and idempotent — a Genie/permission/apps error is
# MAGIC logged, never fatal.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Widgets & configuration

# COMMAND ----------

dbutils.widgets.text("manifest_catalog", "main", "Manifest catalog")
dbutils.widgets.text("manifest_schema", "migration", "Manifest schema")
# Deployed AI/BI dashboard id — injected by the bundle (${resources.dashboards.migration_scope.id})
# so it can be persisted to migration_control_config.dashboard_id. Blank when run outside the
# bundle (e.g. interactively); the cockpit/app then fall back to their own resolution.
dbutils.widgets.text("dashboard_id", "", "AI/BI dashboard id (from bundle; blank = none)")

# Genie space (read-only Q&A over the control tables) is created/updated here via the SDK.
# Leave genie_warehouse_id blank to skip Genie creation.
dbutils.widgets.dropdown("ensure_genie", "true", ["true", "false"], "Create/update the Genie space")
dbutils.widgets.text("genie_warehouse_id", "", "Genie: SQL warehouse id (blank = skip)")
dbutils.widgets.text("genie_parent_path", "/Shared", "Genie: parent folder path")
dbutils.widgets.dropdown("genie_grant_users", "true", ["true", "false"], "Grant CAN_RUN to group `users`")
# App NAME (injected by the bundle as ${var.app_name}, default ${bundle.name}) so this job
# resolves the App's service principal at RUNTIME (w.apps.get) and provisions it:
#   (1) CAN_RUN on the Genie space it creates, and
#   (2) the least-privilege control-plane UC grants (USE CATALOG / USE SCHEMA / SELECT on
#       the schema + MODIFY on migration_actions) — the App queries the control plane AS its
#       SP, so without these its boot read of migration_control_config and all 22 queries
#       fail with INSUFFICIENT_PERMISSIONS (SQLSTATE 42501).
# The App SP's CAN_USE on the SQL warehouse is granted declaratively by the app's `sql-warehouse`
# resource binding (permission: CAN_USE) — NOT here.
# A resource ref would deadlock the deploy: the App binds the profiler job as its stage-zero
# CTA, so a plain name resolved at runtime is used, not a ${resources.apps...} substitution.
# Blank -> no App -> no grant.
dbutils.widgets.text("app_name", "", "App name to grant Genie CAN_RUN + control-plane UC grants (blank = none)")

MANIFEST_CATALOG = dbutils.widgets.get("manifest_catalog").strip()
MANIFEST_SCHEMA = dbutils.widgets.get("manifest_schema").strip()
DASHBOARD_ID = dbutils.widgets.get("dashboard_id").strip()
ENSURE_GENIE = dbutils.widgets.get("ensure_genie").strip().lower() == "true"
GENIE_WAREHOUSE_ID = dbutils.widgets.get("genie_warehouse_id").strip()
GENIE_PARENT_PATH = dbutils.widgets.get("genie_parent_path").strip() or "/Shared"
GENIE_GRANT_USERS = dbutils.widgets.get("genie_grant_users").strip().lower() == "true"
APP_NAME = dbutils.widgets.get("app_name").strip()

print(f"manifest target  = {MANIFEST_CATALOG}.{MANIFEST_SCHEMA}")
print(f"ensure_genie     = {ENSURE_GENIE} (warehouse={'set' if GENIE_WAREHOUSE_ID else 'blank -> skip'})")
print(f"app_name         = {APP_NAME or '<none>'}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Bootstrap: put `src/` on the import path
# MAGIC Works whether the notebook runs from a Databricks Asset Bundle (workspace files),
# MAGIC a Git folder, or a Repo. We walk up from common locations looking for `src/migration`.

# COMMAND ----------

import os
import sys


def _bootstrap_src() -> str:
    candidates = []
    cwd = os.getcwd()
    candidates += [cwd, os.path.dirname(cwd), os.path.dirname(os.path.dirname(cwd))]
    try:
        ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
        nb_path = ctx.notebookPath().get()  # e.g. /Workspace/.../notebooks/99_setup
        nb_dir = os.path.dirname(nb_path)
        candidates += [
            "/Workspace" + os.path.dirname(nb_dir),  # repo root (sibling of notebooks/)
            "/Workspace" + nb_dir,
        ]
    except Exception:
        pass
    for base in candidates:
        if base and os.path.isdir(os.path.join(base, "src", "migration")):
            src_root = os.path.join(base, "src")
            if src_root not in sys.path:
                sys.path.insert(0, src_root)
            return src_root
    raise RuntimeError(
        "Could not locate src/migration. Checked: " + ", ".join(c for c in candidates if c)
    )


SRC_ROOT = _bootstrap_src()
print(f"src on path: {SRC_ROOT}")

from migration import bootstrap as bs
from migration import genie_space as gs
from migration import grants as gr
from migration import metric_view as mv
import uc_ext2managed  # noqa: F401,E402  product marker (opt-out: telemetry_enabled=false)
from migration import telemetry as tm  # noqa: E402
TM = tm.start("99_setup", dbutils)  # usage telemetry: closed vocabulary only, never raises

# COMMAND ----------

# MAGIC %md
# MAGIC ## Ensure the control tables exist (idempotent — self-sufficient)
# MAGIC Create the ENTIRE control plane empty so this job can run BEFORE the profiler: the
# MAGIC Genie create API validates that referenced tables exist, but does not require rows.
# MAGIC Re-running never clobbers real data (`CREATE ... IF NOT EXISTS` / `CREATE OR REPLACE VIEW`).

# COMMAND ----------

control_fqns = bs.ensure_control_plane(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
for _label, _fqn in control_fqns.items():
    print(f"  {_label:20s} -> {_fqn}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Grant the admin App's service principal control-plane access
# MAGIC The admin App runs EVERY query as its own service principal. It needs least-privilege
# MAGIC Unity Catalog grants to read the control plane and write the one operator table:
# MAGIC `USE CATALOG`, `USE SCHEMA`, schema-level `SELECT` (covers the auto-evolving table set
# MAGIC + views), and `MODIFY` on `migration_actions` only (NOT `ALL PRIVILEGES` — the App must
# MAGIC never rewrite `audit_log` / `wave_plan` / `freeze_ledger`). Best-effort and idempotent:
# MAGIC resolves the SP at runtime from `app_name`, GRANTs, then verifies via `SHOW GRANTS` and
# MAGIC prints the exact statements for any grant it could not land (typically `USE CATALOG` on
# MAGIC a pre-existing shared catalog — see the README "App service principal grants" fallback).
# MAGIC Never fails this job.

# COMMAND ----------

if APP_NAME:
    try:
        from databricks.sdk import WorkspaceClient
        from databricks.sdk.errors import NotFound

        _w_grant = WorkspaceClient()
        # The App SP does not exist until the App has been deployed at least once. This job
        # can run before OR after the app deploy, so a NOT_FOUND here is EXPECTED on a fresh
        # workspace, not an error: re-run this job AFTER the app is deployed to apply the grants.
        try:
            _app_sp = _w_grant.apps.get(name=APP_NAME).service_principal_client_id
        except NotFound:
            _app_sp = None
            print(
                f"App '{APP_NAME}' is not deployed yet, so its service principal does not "
                f"exist -> no control-plane grants applied this run. This is expected when "
                f"setup runs before the first app deploy. Re-run this job AFTER the app is "
                f"deployed and the grants will land. Not an error."
            )
        if _app_sp:
            print(f"App '{APP_NAME}' service principal resolved: {_app_sp}")
        _missing = []
        for _priv, _sec_type, _sec_fqn, _grant_sql, _show_sql in (
            gr.app_sp_grant_plan(MANIFEST_CATALOG, MANIFEST_SCHEMA, _app_sp) if _app_sp else []
        ):
            try:
                spark.sql(_grant_sql)
            except Exception as _gex:  # noqa: BLE001 — one grant failing must not abort the rest
                print(
                    f"  [note] GRANT failed ({_priv} on {_sec_type} {_sec_fqn}): "
                    f"{type(_gex).__name__}: {str(_gex)[:140]}"
                )
            try:
                _rows = [r.asDict() for r in spark.sql(_show_sql).collect()]
            except Exception as _sex:  # noqa: BLE001 — SHOW GRANTS itself can be denied
                print(
                    f"  [note] SHOW GRANTS failed ({_sec_type} {_sec_fqn}): "
                    f"{type(_sex).__name__}: {str(_sex)[:140]}"
                )
                _rows = []
            if gr.sp_has_grant(_rows, _app_sp, _priv):
                print(f"  verified: App SP {_app_sp} has {_priv} on {_sec_type} {_sec_fqn}")
            else:
                _missing.append(_grant_sql)
        if _missing:
            print(
                "  [WARNING] App SP is MISSING required control-plane grants after the grant "
                "attempt. The admin App runs every query as this SP; without these, its boot "
                "read of migration_control_config and all downstream queries fail with "
                "INSUFFICIENT_PERMISSIONS (SQLSTATE 42501). An admin with authority on the "
                "catalog/schema must run:"
            )
            for _grant_sql in _missing:
                print(f"    {_grant_sql};")
    except Exception as _ex:  # noqa: BLE001 — job identity lacks apps/grant authority, or another error (NOT_FOUND is handled above)
        print(
            f"  [WARNING] could not resolve/grant App SP control-plane access for "
            f"app_name='{APP_NAME}' ({type(_ex).__name__}: {str(_ex)[:160]}); the job identity "
            f"may lack apps/grant authority. Run the grants manually (see README 'App service "
            f"principal grants'). The migration is unaffected."
        )
else:
    print("app_name blank -> no App SP control-plane grant (set app_name to enable).")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Publish the Genie knowledge docs to the volume (best-effort)
# MAGIC Runs BEFORE the space create/update so content search indexes the final files. Uploads
# MAGIC every registered Markdown doc (idempotent, `overwrite=True`); only when ALL uploads succeed
# MAGIC does it remove the files the library no longer ships (`gs.knowledge_prune_plan`: retired
# MAGIC doc names, old PDF copies and the Markdown probe — root files only, never a directory,
# MAGIC never anything else). Then it sets the volume description the Genie Agent reads.

# COMMAND ----------

KNOWLEDGE_PUBLISHED = False
if not ENSURE_GENIE:
    print("ensure_genie=false -> skipping the knowledge doc upload.")
else:
    import os

    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient()
    # Repo root = parent of the already-resolved SRC_ROOT (<repo>/src) — robust on
    # serverless/bundle runs where os.getcwd() is not the notebook directory.
    _repo_root = os.path.dirname(SRC_ROOT)
    _volume = gs.knowledge_volume_path(MANIFEST_CATALOG, MANIFEST_SCHEMA)
    plan = gs.knowledge_upload_plan(_repo_root, MANIFEST_CATALOG, MANIFEST_SCHEMA)
    uploaded = 0
    try:
        for local, target in plan:
            if not os.path.exists(local):
                print(f"  [skip] missing local doc {local} (redeploy the bundle)")
                continue
            with open(local, "rb") as fh:
                w.files.upload(target, fh, overwrite=True)
            uploaded += 1
        print(f"Uploaded {uploaded}/{len(plan)} knowledge docs to {_volume}")
    except Exception as ex:  # noqa: BLE001
        print(f"[note] knowledge doc upload failed ({type(ex).__name__}: {str(ex)[:200]}). "
              f"The migration is unaffected; re-run this job to retry.")
    KNOWLEDGE_PUBLISHED = uploaded == len(plan)

    # Prune only after a complete upload, so a partial run never leaves the volume without docs.
    if not KNOWLEDGE_PUBLISHED:
        print("  [note] not every doc uploaded -> leaving existing volume files in place.")
    else:
        try:
            _entries = [(e.name, bool(e.is_directory))
                        for e in w.files.list_directory_contents(_volume.rstrip("/"))]
            _remove = gs.knowledge_prune_plan(_entries, MANIFEST_CATALOG, MANIFEST_SCHEMA)
            for path in _remove:
                w.files.delete(path)
                print(f"  removed {path}")
            print(f"Removed {len(_remove)} file(s) the library no longer ships.")
        except Exception as ex:  # noqa: BLE001
            print(f"[note] knowledge volume prune failed ({type(ex).__name__}: {str(ex)[:200]}); "
                  f"old doc files may remain. Re-run this job to retry.")

    try:
        spark.sql(bs.genie_volume_comment_sql(MANIFEST_CATALOG, MANIFEST_SCHEMA))
        print("  volume description set.")
    except Exception as ex:  # noqa: BLE001
        print(f"[note] could not set the volume description ({type(ex).__name__}: "
              f"{str(ex)[:160]}); the Agent still reads the docs.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Ensure the Genie space (read-only Q&A over the control tables)
# MAGIC Created/updated here via the low-level REST API (`w.api_client.do`) — the control
# MAGIC plane already exists (ensured above), so the create endpoint never fails on missing
# MAGIC tables. Idempotent: matches an existing space by title (list -> PATCH) else POST.
# MAGIC
# MAGIC We deliberately do NOT call `w.genie.create_space` / `update_space`: those SDK
# MAGIC convenience methods only exist on newer databricks-sdk builds, and the serverless
# MAGIC runtime ships an OLDER SDK whose `GenieAPI` has no `update_space` — which previously
# MAGIC raised `AttributeError`, tripped this best-effort `try`, and silently skipped the
# MAGIC `write_control_config` below (so `genie_space_id` stayed NULL while the job reported
# MAGIC SUCCESS). `api_client.do` is present on every SDK version and hits the same
# MAGIC endpoints, so it is version-independent. Best-effort: any error is logged, never fatal.

# COMMAND ----------

if not ENSURE_GENIE:
    print("ensure_genie=false -> skipping Genie space creation.")
elif not GENIE_WAREHOUSE_ID:
    print("genie_warehouse_id is blank -> skipping Genie space creation. "
          "Set the warehouse id (bundle var warehouse_id) to enable it.")
else:
    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient()

    # Governed metric view (migration_metrics) over table_manifest — a Genie-only source that
    # gives consistent KPI aggregations (see migration.metric_view). Created HERE (not in
    # ensure_control_plane) because metric views need DBR 16.4+/17.2+ or serverless and the
    # standalone profiler may run on an older runtime. Best-effort: on failure we skip the
    # metric-view reference in the space (include_metric_view=False) so create-space never
    # fails its referenced-source validation — the tables/views still power every other answer.
    mv_ok = False
    try:
        spark.sql(mv.create_metric_view_sql(MANIFEST_CATALOG, MANIFEST_SCHEMA))
        mv_ok = True
        print(f"  metric view ready: {mv.metric_view_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)}")
    except Exception as ex:  # noqa: BLE001 — metric views need a modern runtime; degrade gracefully
        print(f"  [note] metric view create failed ({type(ex).__name__}: {str(ex)[:160]}); "
              f"the Genie space will omit it (tables/views still answer everything else).")

    # The knowledge volume rides in the payload: an update replaces the whole space config, so
    # leaving it out would drop a volume attached in the UI. A workspace without the volumes
    # Beta rejects it with a 400; step 1 then retries once without it (never on other errors).
    serialized = gs.build_serialized_space(
        MANIFEST_CATALOG, MANIFEST_SCHEMA, include_metric_view=mv_ok, include_knowledge_volume=True
    )
    serialized_without_volume = gs.build_serialized_space(
        MANIFEST_CATALOG, MANIFEST_SCHEMA, include_metric_view=mv_ok
    )

    # NARROW, INDEPENDENT best-effort steps (do NOT re-wrap them in one try/except).
    # These four steps — (1) find/create-or-update the space, (2) grant `users`, (3) grant +
    # VERIFY the App SP, (4) write the handshake — used to live in ONE best-effort try, so an
    # error in the FIRST silently skipped ALL the rest, including the read-back verification
    # built to catch exactly that and the config write that persists genie_space_id (the real
    # bug: `w.genie.update_space` AttributeError on an older SDK skipped grant+verify+write
    # while the job still reported SUCCESS). Each step now owns its own try/except and prints a
    # DISTINCT label, so one step failing can never mask another. The only legitimate
    # short-circuit is "no space id to act on": steps 2–4 need a space_id, so they are gated on
    # it (`if space_id:`), not nested under create/update. Crucially, if UPDATE fails while a
    # matching space ALREADY EXISTS, we keep that id and still run grant/verify/write against it.

    # Step 1 — find an existing space by title (list), then create-or-update it. A list failure
    # leaves the id unknown (the one case the rest cannot proceed); an update failure on an
    # already-existing space keeps `existing` so downstream steps still run.
    space_id = None
    try:
        # Paginate + normalize so an existing Agent is updated in place, never duplicated.
        # api_client.do returns a raw dict ({"spaces": [...], "next_page_token": ...}).
        _all_spaces = []
        _token = None
        while True:
            _m, _p, _q = gs.list_spaces_request(_token)
            _resp = w.api_client.do(_m, _p, query=_q)
            _all_spaces.extend(gs.spaces_from_list_response(_resp))
            _token = gs.next_page_token(_resp)
            if not _token:
                break
        existing = gs.match_space_id(_all_spaces, gs.GENIE_TITLE)
    except Exception as ex:  # noqa: BLE001
        existing = None
        print(f"[note] Genie list_spaces failed ({type(ex).__name__}: {str(ex)[:160]}); "
              f"cannot resolve the space id -> skipping Genie provisioning this run. "
              f"Re-run this job to retry.")
    else:
        # Keep the existing id up front so an update failure still leaves us a space to grant on.
        space_id = existing

        def _create_or_update(payload):
            if existing:
                _m, _p, _b = gs.update_space_request(
                    existing,
                    serialized_space=payload,
                    title=gs.GENIE_TITLE,
                    description=gs.GENIE_DESCRIPTION,
                    warehouse_id=GENIE_WAREHOUSE_ID,
                )
                w.api_client.do(_m, _p, body=_b)
                print(f"Updated Genie space {existing}")
                return existing
            _m, _p, _b = gs.create_space_request(
                GENIE_WAREHOUSE_ID,
                payload,
                title=gs.GENIE_TITLE,
                description=gs.GENIE_DESCRIPTION,
                parent_path=GENIE_PARENT_PATH,
            )
            created = gs._space_id(w.api_client.do(_m, _p, body=_b))
            print(f"Created Genie space {created}")
            return created

        try:
            try:
                space_id = _create_or_update(serialized)
                print(f"  knowledge volume attached: {gs.knowledge_volume_path(MANIFEST_CATALOG, MANIFEST_SCHEMA)}")
            except Exception as ex:  # noqa: BLE001
                # A 400 is a rejected request (nothing was created), so one retry cannot duplicate
                # a space. Any other error falls through to the note below unchanged.
                if not gs.should_retry_without_volume(ex):
                    raise
                print(f"  [note] the space rejected the knowledge volume ({type(ex).__name__}: "
                      f"{str(ex)[:160]}); retrying without it. Attach the volume in the Genie UI "
                      f"(docs/genie/00_setup.md).")
                space_id = _create_or_update(serialized_without_volume)
        except Exception as ex:  # noqa: BLE001
            # space_id stays = existing (possibly None). If a space already existed, the
            # grant/verify/write below STILL run against it despite this update error.
            print(f"[note] Genie {'update' if existing else 'create'}_space failed "
                  f"({type(ex).__name__}: {str(ex)[:160]}). "
                  + ("Proceeding with the existing space id for grants + handshake."
                     if existing else "No space id available -> skipping grants + handshake."))

    if space_id:
        # Step 2 — optional CAN_RUN grant for the `users` group, gated by genie_grant_users
        # (default true preserves prior behavior; set false for creator-only publishes).
        if GENIE_GRANT_USERS:
            try:
                w.api_client.do(
                    "PATCH",
                    f"/api/2.0/permissions/genie/{space_id}",
                    body={"access_control_list": [
                        {"group_name": "users", "permission_level": "CAN_RUN"}
                    ]},
                )
                print("  granted CAN_RUN to group `users`")
            except Exception as ex:  # noqa: BLE001
                print(f"  [note] could not set Genie `users` permission ({type(ex).__name__}: "
                      f"{str(ex)[:160]}); set it in the Genie UI if needed.")
        else:
            print("  genie_grant_users=false -> leaving Agent creator-only (no `users` grant)")

        # Step 3 — functional grant + VERIFY for the App's service principal: the App queries
        # this Genie space AS its SP, so it must hold CAN_RUN. Resolve the SP client-id UUID at
        # RUNTIME from the App name (w.apps.get) — this job cannot take the App as a resource
        # substitution without a deploy cycle (the App binds the profiler job). Blank app_name =
        # no App = skip. Independent of genie_grant_users. A PATCH is additive, so it does not
        # clobber the `users` grant above; we grant by the UUID (a display name would 200 and
        # grant nothing) and VERIFY the read-back rather than trusting the response code — the
        # read-back also catches a wrong/mismatched app_name. This is the step whose masking
        # (see above) hid a NULL genie_space_id behind a SUCCESS all week.
        if APP_NAME:
            try:
                from databricks.sdk.errors import NotFound

                try:
                    app_sp = w.apps.get(name=APP_NAME).service_principal_client_id
                except NotFound:
                    # Expected on a fresh workspace: the App SP does not exist until the App
                    # is deployed. This job can run before OR after deploy; a re-run after
                    # deploy applies the grant. Not an error.
                    app_sp = None
                    print(f"  App '{APP_NAME}' is not deployed yet -> no SP to grant CAN_RUN. "
                          f"Re-run this job AFTER the app is deployed and the grant will land. "
                          f"Not an error.")
                if app_sp:
                    w.api_client.do(
                        "PATCH",
                        gs.genie_permissions_path(space_id),
                        body=gs.sp_permission_patch_body(app_sp, "CAN_RUN"),
                    )
                    _acl = w.api_client.do("GET", gs.genie_permissions_path(space_id))
                    if gs.sp_has_permission(_acl, app_sp, "CAN_RUN"):
                        print(f"  verified: App '{APP_NAME}' SP {app_sp} has CAN_RUN on the Genie space")
                    else:
                        print(f"  [WARNING] App '{APP_NAME}' SP {app_sp} is NOT present with CAN_RUN "
                              f"after the grant — the App will not be able to query Genie. Re-run this "
                              f"job once the grant lands, or grant CAN_RUN in the Genie UI.")
            except Exception as ex:  # noqa: BLE001
                # Wrong app_name, or the job identity lacks apps access (NOT_FOUND is handled
                # above) — log and continue; never masks steps 2 or 4.
                print(f"  [WARNING] could not resolve/grant App SP CAN_RUN for app_name='{APP_NAME}' "
                      f"({type(ex).__name__}: {str(ex)[:160]}); the job identity may lack apps "
                      f"access. Grant CAN_RUN in the Genie UI, or run a separate grant job. "
                      f"The migration is unaffected.")
        else:
            print("  app_name blank -> no App SP grant (set app_name to enable).")

        # Step 4 — persist the Genie space id + dashboard id into the handshake table. A PARTIAL
        # upsert: only these fields are written; catalog/schema/warehouse_id the profiler wrote
        # are preserved. This is what makes setup order-independent from the profiler.
        try:
            bs.write_control_config(
                spark,
                MANIFEST_CATALOG,
                MANIFEST_SCHEMA,
                dashboard_id=DASHBOARD_ID or None,
                genie_space_id=space_id,
            )
            print(f"  control config updated with genie_space_id={space_id}, "
                  f"dashboard_id={DASHBOARD_ID or '<none>'}")
        except Exception as ex:  # noqa: BLE001
            print(f"  [note] could not persist genie_space_id/dashboard_id ({type(ex).__name__}: "
                  f"{str(ex)[:160]}); the App can still resolve the space by title.")

        try:
            host = w.config.host.rstrip("/")
            print(f"  open: {host}/genie/rooms/{space_id}")
        except Exception:  # noqa: BLE001
            pass

# COMMAND ----------

# MAGIC %md
# MAGIC ### Usage telemetry (opt-out: `telemetry_enabled=false`)
# MAGIC One best-effort ping marking a toolkit install/upgrade (version + which optional pieces
# MAGIC are wired). Never names, ids or identities. Never raises.

# COMMAND ----------

TM.finish(
    tm.OUTCOME_OK,
    mode=tm.MODE_EXECUTE,
    selector="none",
    extra={"genie": bool(ENSURE_GENIE), "warehouse_set": bool(GENIE_WAREHOUSE_ID),
           "app_bound": bool(APP_NAME)},
)
