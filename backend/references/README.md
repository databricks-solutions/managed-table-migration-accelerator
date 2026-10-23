# Reference material

## workspace-profiler.py (tier-2 source-scan port target)

Tier 2 of path-access attribution (`notebooks/01_source_scan.py` +
`src/migration/source_scan.py`) needs to export notebook/job **source** from the workspace
and scan it for storage-path literals. The robust Workspace/Jobs listing + SOURCE-export
logic should be **ported from** an existing internal profiler:

- Source path:
  `~/Library/CloudStorage/GoogleDrive-<you>/My Drive/DSA/DBRUpgrades/workspace-profiler/workspace-profiler.py`

It implements paginated `workspace/list` + `workspace/export?format=SOURCE` and job/task
enumeration with rate-limit handling — exactly the `_collect_sources()` step stubbed in
`notebooks/01_source_scan.py`.

### Status: available locally

`references/workspace-profiler.py` is now present (copied from the Drive path above). To finish
the tier-2 port:

1. Read `references/workspace-profiler.py` for its workspace/job listing + SOURCE-export logic.
2. Port those functions into `_collect_sources()` in `notebooks/01_source_scan.py`, returning
   `{entity_kind, entity_id, entity_name, source_path, lang, text}` per source. The pure scan +
   attribution + write logic downstream of `_collect_sources()` is already implemented.

Note: this reference is a development aid only — it does not need to ship with the deployed
bundle.
