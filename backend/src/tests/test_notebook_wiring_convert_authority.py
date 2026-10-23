"""convert_authority writers are wired into the read-only stages (text scan; no Spark)."""

import inspect
from pathlib import Path

from migration import authority as authz

NOTEBOOKS = Path(__file__).resolve().parents[2] / "notebooks"


def _read(name: str) -> str:
    return (NOTEBOOKS / name).read_text(encoding="utf-8")


def test_refresh_reads_then_persists_and_never_raises_across_the_write():
    src = inspect.getsource(authz.refresh_authority)
    i_read = src.index("read_authority_facts(")
    i_try = src.index("    try:\n", i_read)
    i_merge = src.index("merge_authority_rows(", i_try)
    i_except = src.index("    except Exception", i_merge)
    assert i_read < i_try < i_merge < i_except
    assert "raise" not in src[i_except:]


def test_profiler_records_authority_behind_a_default_on_toggle_before_the_action_backlog():
    src = _read("00_profiler.py")
    assert 'dbutils.widgets.dropdown("scan_convert_authority", "true", ["true", "false"]' in src
    i_toggle = src.index("if not SCAN_CONVERT_AUTHORITY:")
    i_call = src.index("authz.refresh_authority(", i_toggle)
    assert "source=authz.SOURCE_PROFILER" in src[i_call:i_call + 300]
    assert "list(cand_by_name.keys())" in src[i_call:i_call + 300]
    assert i_call < src.index("## Generate the migration action backlog")
    assert "from migration import authority as authz" in src
    # `au` is the audit alias in this notebook; authority must not shadow it.
    assert "import authority as au\n" not in src


def test_dry_run_check_4_is_advisory_and_read_once_before_the_per_table_loop():
    src = _read("03_dry_run.py")
    i_refresh = src.index("authz.refresh_authority(")
    i_loop = src.index("for r in candidates:")
    assert i_refresh < i_loop
    assert "source=authz.SOURCE_DRY_RUN" in src[i_refresh:i_refresh + 300]
    check = src[src.index("# Check 4"):src.index("# Check 5")]
    assert "authz.dry_run_check(_authz_by_fn.get(full_name))" in check
    assert "CHECK_FAIL" not in check and "blocker=" not in check


def test_conductor_refreshes_authority_before_deciding_the_next_step():
    src = _read("08_conductor.py")
    i_refresh = src.index("authz.refresh_waves(")
    assert i_refresh < src.index("cd.resolve_next_step(")
    assert "source=authz.SOURCE_CONDUCTOR" in src[i_refresh:i_refresh + 400]
    assert "convert_identity=CONVERT_RUN_AS or None" in src[i_refresh:i_refresh + 400]


def test_cockpit_refreshes_authority_before_the_batch_rollup():
    src = _read("11_bulk_cockpit.py")
    assert src.index("LEASE_NOTE = _apply_authority(JOINED_ROWS)") < src.index("ROLLUPS = pl.batch_readiness(")


def test_every_read_only_writer_uses_its_own_source_label():
    labels = {"00_profiler.py": "SOURCE_PROFILER", "03_dry_run.py": "SOURCE_DRY_RUN",
              "08_conductor.py": "SOURCE_CONDUCTOR", "11_bulk_cockpit.py": "SOURCE_BULK_COCKPIT"}
    for nb, label in labels.items():
        assert f"source=authz.{label}" in _read(nb), nb
        assert getattr(authz, label) in authz.ALL_SOURCES


def test_convert_gate_rereads_live_for_the_remaining_targets_before_any_mutation():
    src = _read("04_convert.py")
    i_targets = src.index("targets = (")
    i_gate = src.index("if REQUIRE_OWNERSHIP_LEASED and targets:")
    i_refresh = src.index("authz.refresh_authority(", i_gate)
    i_row = src.index("authz.build_gate_row(", i_refresh)
    i_write = src.index("ge.try_append_gate_eval_rows(", i_row)
    i_raise = src.index("raise RuntimeError(", i_write)
    i_mutation = src.index("def _convert_one(")
    assert i_targets < i_gate < i_refresh < i_row < i_write < i_raise < i_mutation
    gate = src[i_gate:i_raise]
    assert '[t["full_name"] for t in targets]' in gate and "source=authz.SOURCE_CONVERT" in gate
    assert "read_ownership_inputs" not in src and "resolve_executor_owned" not in src
    assert "OWNER TO" not in src


def test_convert_failure_is_translated_through_explain_failure():
    src = _read("04_convert.py")
    body = src[src.index("def _convert_one("):src.index("results = []")]
    assert "authz.explain_failure(str(ex))" in body
    assert '"guidance": _why or info["guidance"]' in body


def test_convert_accepts_the_deprecated_owner_group_as_a_no_op():
    src = _read("04_convert.py")
    assert 'dbutils.widgets.text("table_owner_group", ""' in src
    assert "is deprecated and ignored" in src


def test_a_refused_describe_detail_is_classified_where_it_is_read():
    profiler = _read("00_profiler.py")
    assert "describe_denied = elig.describe_detail_denied(str(err))" in profiler
    assert "describe_detail_denied=describe_denied," in profiler
    assert "results.append(elig.describe_detail_check(*_describe_detail(full_name_bt)))" in _read("03_dry_run.py")


def test_authorize_by_schema_narrows_to_the_plan_and_unreadable_tables():
    src = _read("03d_lease_ownership.py")
    i_scope = src.index("members, _excluded = authz.schema_scope_rows(_schema_rows, _planned)")
    assert '.select("full_name", "state", "blockers")' in src[:i_scope]
    assert src.index("names = authz.to_convert_names(members)") > i_scope
