"""Text guards: 05b and 08c name the retired job's maintenance class (notebooks need Spark)."""
import os

_NB = os.path.join(os.path.dirname(__file__), "..", "..", "notebooks")


def _read(name):
    with open(os.path.join(_NB, name), encoding="utf-8") as f:
        return f.read()


def _retire_block(src):
    i = src.index("retire_rows = []")
    return src[i:src.index("if retire_rows:", i)]


def _assert_wired(name):
    src = _read(name)
    block = _retire_block(src)
    assert "from migration import compaction as cp" in src or "compaction as cp" in src
    assert "ax.maintenance_classes_by_job(" in block
    assert "cp.compaction_sources_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)" in block
    assert "operation_classes=" in block


def test_05b_passes_maintenance_classes_to_the_retire_row():
    _assert_wired("05b_unfreeze_verify.py")


def test_08c_passes_maintenance_classes_to_the_retire_row():
    _assert_wired("08c_enable_optimization.py")
