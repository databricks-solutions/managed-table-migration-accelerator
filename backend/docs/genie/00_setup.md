# Admin Setup — The Knowledge Volume and the Genie Agent

This is a one-time administrator runbook for letting the Genie Agent answer deeper "what
does this mean" and "what do I do next" questions from the curated knowledge library, not
just the control tables. It is **not** part of the knowledge library itself — it is repo-only
guidance for the person who publishes the Agent, and it is deliberately excluded from the
files uploaded to the volume. The structured layer (tables, views, instructions, example SQL)
works **fully without any of these steps**; the volume only adds the file-grounded reasoning
layer on top.

## What `job_99_setup` does for you

The install-time setup job (`job_99_setup`) handles the volume end to end:

1. It creates the volume `<manifest_catalog>.<manifest_schema>.genie_knowledge` idempotently
   (via `bootstrap.ensure_control_plane`).
2. It uploads the 11 Markdown knowledge docs (`gs.KNOWLEDGE_DOCS`) into the volume root.
3. Only after **every** upload succeeds, it removes the files the library no longer ships: the
   retired doc names from the earlier 30-doc library, old PDF copies, and a probe file. It
   removes those exact names only; it never touches a directory or any other file.
4. It sets the volume description (`COMMENT ON VOLUME`), which the Agent reads to pick files.
5. It creates or updates the Genie space with the volume attached (`data_sources.volumes`). A
   setup run therefore keeps the attachment instead of dropping it. If the workspace rejects
   the volume (the preview is off), setup retries once without it and prints a note.

Substitute your deployment's real catalog and schema for `<manifest_catalog>` /
`<manifest_schema>` throughout.

## This is a Beta, region-gated feature

Reading files in a volume from a Genie Agent is a Beta capability and may be gated by region
or account entitlement. If it is unavailable in your workspace, stop here: the Agent still
answers every structured question from the tables and views, and the knowledge documents
remain a browsable read-only reference in the repo. The migration is unaffected either way.

## Steps (manual, once per workspace)

1. **Enable the preview.** In the workspace admin previews, turn on **"Analyze Files in
   Volumes with Genie Agents."** Then re-run `job_99_setup` so it attaches the volume (a run
   before the preview was on attached nothing and printed a note).
2. **Check that content search is on.** Databricks turns content search on automatically when a
   volume is attached, and the Agent needs it to read the docs. The Markdown docs are only
   searchable with it. If the space's **Sources** tab shows content search off for the volume,
   turn it on.
3. **Confirm entitlements and grants.** Every operator who will use the file-grounded answers
   needs the **Workspace access** and **Databricks SQL access** entitlements, plus **`READ
   VOLUME`** on `<manifest_catalog>.<manifest_schema>.genie_knowledge`. The creator has read
   access by ownership; broaden the `READ VOLUME` grant to additional principals as needed.
4. **Ask in Agent mode.** File-grounded questions are answered only in the Agent's **Agent
   mode** (in the UI, or the Agent-mode API `POST /api/2.0/genie/agents/{space_id}/responses`).
   The classic conversation API answers from the tables only.

## Keeping the volume current

Whenever the knowledge documents change, redeploy (`databricks bundle deploy`) and re-run
`job_99_setup`. It uploads the docs again, prunes anything retired, and keeps the volume
attached. Content search re-indexes changed files automatically, usually within minutes.

To check answer quality after a change, run the question set:
`python backend/.e2e/e2e_genie_knowledge.py run --profile <profile> --label after`, then
`compare` it against an earlier `scores.json`.
