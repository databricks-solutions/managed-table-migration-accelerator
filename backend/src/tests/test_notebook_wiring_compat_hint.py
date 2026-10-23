"""Text guard: the conductor's selector carries open_compat, so build_exact_command can tell one
Compat table (a single compat_location) from several (one path per table, D7)."""
from pathlib import Path

NOTEBOOK = Path(__file__).resolve().parents[2] / "notebooks" / "08_conductor.py"


def test_conductor_selector_carries_open_compat():
    src = NOTEBOOK.read_text()
    start = src.index("selector = {")
    end = src.index("exact_command = cd.build_exact_command(selector)")
    assert '"open_compat"' in src[start:end]
