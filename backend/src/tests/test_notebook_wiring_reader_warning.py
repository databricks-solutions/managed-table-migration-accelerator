"""Text guards: every job surface renders the pre-convert external-reader warning through the shared
compat helpers, never gates on it, and 04 re-checks Iceberg reads live before the convert command."""
from pathlib import Path

NOTEBOOKS = Path(__file__).resolve().parents[2] / "notebooks"


def _src(name: str) -> str:
    return (NOTEBOOKS / name).read_text()


def test_dry_run_check_15_reads_the_view_by_full_name_and_falls_back_to_the_manifest():
    src = _src("03_dry_run.py")
    assert "cm.external_reader_readiness_view_fqn(" in src
    view_read = src[src.index("_reader_fqn = "):src.index("# COMMAND ----------", src.index("_reader_fqn = "))]
    assert "scp.full_name_in_predicate(" in view_read
    assert "catalog" not in view_read.replace("MANIFEST_CATALOG", "")
    check = src[src.index("# Check 15"):src.index("# Finalize state for this table")]
    assert "cm.reader_check(_reader_state, from_view=_READER_VIEW_OK)" in check
    assert "cm.reader_readiness(d.get(\"needs_compatibility_mode\")" in check
    assert "cm.READER_CHECK_NUMBER, cm.READER_CHECK_NAME" in check
    assert "blocker=" not in check
    assert "cm.preconvert_reader_warning(" in src


def test_convert_prints_the_warning_and_rechecks_iceberg_reads_live():
    src = _src("04_convert.py")
    block = src[src.index("# External readers (ADVISORY ONLY"):src.index("# Optional bounded LIVE re-read")]
    assert "cm.preconvert_reader_warning(" in block
    assert "raise" not in block
    assert "sc.full_name_in_predicate(" in block
    loop = src[src.index("for r in targets:\n    d = r.asDict()"):]
    live = loop.index("cv.resolve_uniform(")
    assert "cv.read_live_properties(spark, full_name_bt)" in loop[live:live + 200]
    assert live < loop.index("cv.reassert_gating(") < loop.index("cv.build_convert_command(full_name_bt, uniform)")
    assert 'uniform = bool(d.get("uniform_iceberg_enabled"))' not in loop


def test_cockpits_print_the_shared_text_before_convert():
    single = _src("09_cockpit.py")
    assert "cm.preconvert_reader_warning(" in single
    advance = single[single.index('elif ACTION == "advance":'):]
    assert advance.index("_reader_warning_for_next()") < advance.index('_launch("04_convert"')
    bulk = _src("11_bulk_cockpit.py")
    assert "pl.reader_step_note(_r)" in bulk
    convert = bulk[bulk.index("elif step == pl.STEP_BULK_CONVERT:"):]
    assert convert.index("pl.reader_step_note(_r)") < convert.index('_launch("11_bulk_convert"')
