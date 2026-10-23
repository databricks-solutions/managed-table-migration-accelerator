"""Metastore-wide system-table reads are scoped in Spark before they reach the driver (text scan)."""

from pathlib import Path

NOTEBOOKS = Path(__file__).resolve().parents[2] / "notebooks"


def test_planner_scopes_lineage_to_the_candidates_before_collecting():
    src = (NOTEBOOKS / "02_wave_planner.py").read_text(encoding="utf-8")
    i_read = src.index('spark.table("system.access.table_lineage")')
    i_collect = src.index(".collect()", i_read)
    scoped = src[i_read:i_collect]
    assert 'F.col("source_table_catalog").isin(cand_catalogs)' in scoped
    assert '"left_semi"' in scoped
    assert "if not cand_names:\n        return events" in src[:i_read]
