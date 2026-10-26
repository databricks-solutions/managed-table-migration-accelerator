# 15 - Genie Code MCP

A third front door onto the same engine: expose the control plane to **Genie Code** (and the AI
Playground) as a custom **MCP server**, so an LLM can drive the migration through governed tool calls
instead of the notebooks or the app UI. Same model as the other surfaces — *read almost everything,
write almost nothing, trigger jobs (never SQL)* — and the **job still enforces every gate**. Every
mutating tool additionally requires `confirm=true` at the MCP layer.

This page is the operator narrative. The **design of record and the exact registration commands** live
in [`backend/docs/integration/genie-code-mcp.md`](../backend/docs/integration/genie-code-mcp.md);
this page links into it rather than restating it.

Genie Code is not the app's **Ask Genie** button. Ask Genie is a Genie space docked in the app that
answers questions about the control tables (see [Dashboard and Genie](./16-dashboard-and-genie.md)).
Genie Code is the workspace assistant; through this MCP server it calls the migration's own tools and
can launch its jobs.

## What you can ask

You ask in plain language; Genie Code picks the tool. Some examples, with the tools behind them:

| Ask | Tools it calls | Changes anything? |
|---|---|---|
| "Where does the migration stand? What should I do next?" | `mtm_get_estate_status`, `mtm_get_next_step` | No |
| "What is blocking `main.sales`, and who has to fix it?" | `mtm_list_blockers`, `mtm_list_open_actions`, `mtm_get_convert_permissions` | No |
| "Show me the waves and what is in wave 3." | `mtm_list_waves`, `mtm_get_wave_members`, `mtm_get_readiness` | No |
| "Which tables are read by path, and from where?" | `mtm_get_path_access` | No |
| "Give me the runbook of manual steps." | `mtm_get_runbook` | No |
| "Rescan the estate", "Re-plan the waves", "Dry-run `main.sales`" | `mtm_run_profiler`, `mtm_plan_waves`, `mtm_dry_run` | No tables (these stages only read and record) |
| "Can I convert `main.sales`?" | `mtm_conversion_preflight`, always first for any convert request | No |
| "Freeze, authorize and convert wave 3." | `mtm_freeze`, `mtm_authorize_convert`, `mtm_convert` | Yes, each behind `confirm=true` |
| "Finish wave 3: restart streams, re-share, set up external readers, turn on clustering." | `mtm_unfreeze`, `mtm_reshare`, `mtm_enable_compat`, `mtm_enable_optimization` | Yes, each behind `confirm=true` |
| "Roll back `main.sales.orders`; the reason is ..." | `mtm_rollback` | Yes, behind `confirm=true`, within 14 days |
| "Did that run finish?" | `mtm_get_run_status` | No |
| "What permissions does the migration need, and which can I grant?" | `mtm_get_permissions` | No |
| "Grant them", "Hand back the convert rights on `main.sales`" | `mtm_grant_permissions`, `mtm_hand_back_convert_rights` (v2) | Yes, as **you**, behind `confirm=true` and the previewed plan |

Three rules hold for every answer. Genie Code signs in as **you** (so you need `CAN_USE` on the app)
and you are recorded as the operator, while the reads and job launches themselves run as the app's
service principal, exactly as when you click in the app. A tool that changes something only returns
a "needs confirmation" preview until it is called again with `confirm=true`, and the job it launches
re-checks every gate. The permissions tools are the exception to the service-principal rule: they
read and grant with **your** sign-in, as the app's Settings → Permissions page does, so Unity
Catalog checks your rights, and a grant also needs the `plan_id` you previewed. And
`mtm_convert` accepts only a selector: it rejects every gate-downgrade parameter, so the assistant
can never weaken a safety gate. With `mcp_tier=v1` only the "No" rows are available.

A permissions question, then a grant called without `confirm`, which returns a preview and runs
nothing:

![Genie Code calling mtm_get_permissions: the rights check, the plan counts and the lines not yet in place with who can grant them, then mtm_grant_permissions without confirm, which asks for confirmation](images/install/mcp/permissions.png)

## What ships in the bundle

The MCP server is part of the app (`app/server/mcp/`), mounted at `POST /mcp`. The bundle wires up
everything the app controls:

- **`MCP_TIER=v2` by default** — reads + non-mutating stages + `mtm_conversion_preflight` **plus** the
  gated mutating tools (freeze / authorize / convert / unfreeze / rollback / cleanup / reshare / compat /
  optimize / campaign; the bulk family — the four `mtm_bulk_*` tools and the `mtm_get_bulk_status` read — is built but withheld until customers ask for it). Set `MCP_TIER=v1` to hide the mutating tools.
- **`mcp-` app-name prefix** (`var.app_name` = `mcp-${bundle.name}`) so Databricks recognizes it as a
  custom MCP server.
- **`ai-gateway` user API scope** so the forwarded user token flows through Unity Gateway.
- **Streamable HTTP transport** — stateless, `enableJsonResponse: true`, CORS locked to the workspace
  host, and a normalized `Accept` header for maximum client tolerance.

## Connecting Genie Code — start here (no account admin needed)

**Recommended path: add the app directly as a custom MCP server in Genie Code.** Genie Code can point
straight at the app's `/mcp` URL as a *Custom MCP server*. It authenticates with **your own in-platform
Databricks identity**, so there is **no account-admin step, no custom OAuth app, and no Unity Catalog
MCP Service** to create. This is the fastest path and the one to use unless you specifically need
centralized UC governance of the tool surface.

1. Genie Code → **Settings → Custom MCP server** → search for `mcp-managed-table-migration` (or paste
   the app's `/mcp` URL).
2. **Save.** Genie Code validates connectivity (an `initialize` + `tools/list` handshake) *before* it
   persists the entry.

> **If it lists but won't persist on Save, it's a CORS preflight failure — not a Genie Code bug.**
> Genie Code validates from the browser at the workspace origin, so the app must return
> `Access-Control-Allow-Origin: https://<workspace-host>` **with the scheme** and reflect the browser's
> requested headers (including `mcp-session-id`). A bare-hostname `DATABRICKS_HOST` (no `https://`) or a
> too-narrow `Access-Control-Allow-Headers` makes the preflight fail and the entry silently vanishes on
> Save. This is handled in `app/server/mcp/register-mcp.ts` (`normalizeOrigin` forces a full origin;
> `setCors` reflects `Access-Control-Request-Headers`) — redeploy the bundle (`databricks bundle deploy
> -t <target>`) if you still see it. Verify with:
>
> ```bash
> curl -sS -i -X OPTIONS "$APP_URL/mcp" \
>   -H "Origin: https://<workspace-host>" \
>   -H "Access-Control-Request-Method: POST" \
>   -H "Access-Control-Request-Headers: content-type,authorization,mcp-protocol-version,mcp-session-id,accept" \
>   | grep -iE '^access-control'
> # expect: access-control-allow-origin: https://<workspace-host>  (with scheme)
> #         access-control-allow-headers reflecting mcp-session-id
> ```

## Alternative: Unity Catalog MCP Service (centralized governance — needs account admin)

Prefer this only when you want the tool surface governed as a **Unity Catalog `MCP_SERVICE` securable**
(central `EXECUTE` grants, audit, one registration shared across users). It is **not** required for
Genie Code to work — the direct path above already runs on each user's identity. DABs cannot manage an
`MCP_SERVICE`, so this is a one-time **UC admin flow** that `bundle destroy` / a fresh clone will
neither remove nor recreate. Treat the commands in
[the runbook (§9)](../backend/docs/integration/genie-code-mcp.md#9-registration-runbook--source-of-truth)
as the source of truth.

The flow, in brief (full commands + the wizard walkthrough in §9):

1. **Register** the app's `/mcp` URL as an external MCP server — recommended via the **AI Gateway →
   Register MCP Server** wizard (it provisions the OAuth app + redirect for you), or headless over
   REST once you have a valid OAuth client.
2. **Grant** the caller `EXECUTE` on the service + `USE CATALOG`/`USE SCHEMA` on its parents, and
   `CAN_USE` on the app; each user logs in once (per-user OAuth).
3. **Verify** with a `tools/list` call against the Unity Gateway service URL.

> **Why this needs an account admin (§9.2):** per-user U2M completes at the fixed redirect
> `https://<workspace-host>/login/oauth/http.html`. The connection's OAuth client **must** have that
> redirect registered — do **not** use the app's own `oauth2_app_client_id` (its redirects are the
> app's own login callback, so authorize fails with `redirect_uri … not registered`). Registering a
> dedicated custom OAuth app (or letting the wizard provision one) is an **account-admin** operation.
> No account admin? Use the direct Genie Code path above instead.

## Finishing a wave

After a wave converts, "what next" walks the same finish steps as the app: `mtm_unfreeze` (restart
streams and writer jobs), `mtm_reshare`, `mtm_enable_compat`, and `mtm_enable_optimization` ("Turn on
liquid clustering" — automatic liquid clustering where eligible). Only the ones the wave needs are
recommended. They scope by the exact wave (`plan_run_id` + `wave_id`). Compatibility Mode still needs
a storage location you supply. Skipping clustering for now (keeping the statements on the runbook but
changing no table), or deferring a re-share or Compat action (with a reason), is something you ask for;
the agent never does it on its own. Details:
[the design doc §5.2d–§5.2f](../backend/docs/integration/genie-code-mcp.md).

## Working on one schema, and team priorities

Name a schema and the agent works on it: "let's migrate `main.finance`" makes it pass that
schema as `target` to `mtm_get_next_step` on every turn until you say otherwise. The answer is
pinned to the wave the plan works on next **for that schema** (a converted wave finishes its
restarts / re-share / optimize before the next wave starts) and carries a `focus` block: where the
schema stands, "wave i of n", work handled elsewhere (streaming, review, blocked), and tables found
after the plan. `mtm_list_schema_steps` lists every schema with its own next step, so "which
schemas can I work on?" has a direct answer. This is the same per-schema recommendation the app's
**Work on one schema** view shows — the conductor computes it once for every interface.

**Team priorities** change the order for everyone: `mtm_set_focus_pins` (v2, `confirm=true`) adds
(with a reason), moves, removes or clears pinned schemas. The recommendation follows the first
pinned schema only after work already in flight, never skips a check, and a pin clears itself when
its schema is done. The agent calls it only when you explicitly ask to prioritize a schema for the
team.

## Safety

The MCP surface adds no new authority. Conversions run as the App service principal, gated exactly as
the app and notebooks are: the fail-closed managed-location gate, the freeze / stream-stop coverage
gates, the convert permission, and dry-run status are all re-checked inside `04_convert`. The MCP layer
only *steers* the agent — `mtm_conversion_preflight` refuses unreasonable requests (e.g. "convert
everything") and points at the correct next stage — and requires `confirm=true` before any mutating
tool triggers. See [Conversion and validation](./08-conversion-and-validation.md) for the gates and
[the design doc §5](../backend/docs/integration/genie-code-mcp.md) for the guardrail model.
