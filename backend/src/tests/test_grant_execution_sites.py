"""Only a short, known list of files may execute a GRANT or REVOKE.

Building a statement is pure and allowed anywhere; running one is not. The executors are the
setup job (99), Authorize (03d), the day-14 revoke (07), and the install engine's thin edge
(``provision_io``), which only the install notebook and the install CLI may import.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCAN_DIRS = [ROOT / "backend" / "notebooks", ROOT / "backend" / "src" / "migration",
             ROOT / "backend" / "scripts"]

EXECUTORS = {
    "backend/notebooks/99_setup.py",
    "backend/notebooks/03d_lease_ownership.py",
    "backend/notebooks/07_post_convert_cleanup.py",
    "backend/notebooks/install.py",
    "backend/scripts/install.py",
    "backend/src/migration/provision_io.py",
}
PROVISION_IO_IMPORTERS = {"backend/notebooks/install.py", "backend/scripts/install.py"}

_EXEC = re.compile(r"\b(?:spark\.sql|run_sql|execute_statement)\((.*)")
_GRANTISH = re.compile(r"(?i)(?<!show_)(?:grant|revoke)(?!ee)")
_IMPORTS_IO = re.compile(r"^\s*(?:from\s+migration\s+import\s+[^#\n]*\bprovision_io\b"
                         r"|import\s+migration\.provision_io\b|from\s+migration\.provision_io\s+import)",
                         re.M)


def _files():
    for d in SCAN_DIRS:
        if d.exists():
            yield from sorted(d.rglob("*.py"))


def _rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def _executing_files():
    out = set()
    for p in _files():
        for line in p.read_text().splitlines():
            m = _EXEC.search(line)
            if m and _GRANTISH.search(m.group(1)):
                out.add(_rel(p))
                break
    return out


def test_only_known_files_execute_grants():
    rogue = _executing_files() - EXECUTORS
    assert not rogue, f"these files execute GRANT/REVOKE outside the allowed list: {sorted(rogue)}"


def test_scan_sees_a_known_executor():
    assert "backend/notebooks/99_setup.py" in _executing_files()


def test_only_install_doors_import_provision_io():
    importers = {_rel(p) for p in _files() if _IMPORTS_IO.search(p.read_text())}
    assert importers <= PROVISION_IO_IMPORTERS, sorted(importers - PROVISION_IO_IMPORTERS)


def test_both_install_doors_use_provision_io():
    importers = {_rel(p) for p in _files() if _IMPORTS_IO.search(p.read_text())}
    assert importers == PROVISION_IO_IMPORTERS
