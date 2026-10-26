"""The profiler's discovery never picks up a catalog's information_schema (text scan; no Spark)."""

import re
from pathlib import Path

PROFILER = Path(__file__).resolve().parents[2] / "notebooks" / "00_profiler.py"


def test_both_discovery_queries_skip_information_schema():
    src = PROFILER.read_text(encoding="utf-8")
    queries = re.findall(r"FROM [^\n]*information_schema\.tables\s+WHERE table_type = 'EXTERNAL'(.*?)\"\"\"",
                         src, flags=re.S)
    assert len(queries) == 2
    for q in queries:
        assert "table_schema <> 'information_schema'" in q
