"""The docs describe convert permission, not the retired ownership lease. No Spark, no network.

Ownership never changes: the convert identity is authorized (USE CATALOG / USE SCHEMA, ownership or
MANAGE, SELECT and MODIFY) and Authorize grants only what is missing. A doc that still tells a reader
to lease or take ownership, or to run ``OWNER TO``, misdirects them. The only exception is a note about
leases taken before convert authority, which cleanup still restores (the legacy drain).
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
# Live operator docs. Plans, specs, the northstar mockups and the design-deltas record are
# historical and keep the wording they were written in.
_HISTORICAL = ("superpowers", "plans", "northstar")
SWEPT = sorted(
    p for p in (
        *(ROOT / "backend/docs").rglob("*.md"),
        *(ROOT / "docs").rglob("*.md"),
        *(ROOT / "app/docs").rglob("*.md"),
        ROOT / "AGENTS.md",
        ROOT / "README.md",
    )
    if not set(p.relative_to(ROOT).parts) & set(_HISTORICAL) and p.name != "design-deltas.md"
    # The docs site project (build output, node_modules) holds no source pages.
    and not p.is_relative_to(ROOT / "docs" / "site")
)

FORBIDDEN = [
    re.compile(r"\b(ownership lease|lease ownership|take ownership|temporary ownership|"
               r"borrows table ownership|leased ownership)\b", re.I),
    re.compile(r"\bOWNER TO\b"),
]
LEGACY_DRAIN = re.compile(r"older ownership model|before convert authority|legacy|superseded|"
                          r"kept from the ownership lease|replaces the ownership lease", re.I)


def _violations(text: str) -> list[str]:
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        if LEGACY_DRAIN.search(line):
            continue
        out.extend(f"{n}: {m.group(0)!r}" for p in FORBIDDEN for m in p.finditer(line))
    return out


def test_sweep_reads_the_docs():
    assert (ROOT / "databricks.yml").is_file()
    names = {p.relative_to(ROOT).as_posix() for p in SWEPT}
    assert {
        "backend/docs/genie/stages_and_gates.md", "backend/docs/genie/overview_and_glossary.md",
        "docs/07b-convert-permissions.md", "backend/docs/contracts/jobs.md",
        "app/docs/app-sp-grants.md", "AGENTS.md", "README.md",
    } <= names
    assert not any("/plans/" in n or n.endswith("design-deltas.md") for n in names)


@pytest.mark.parametrize("path", SWEPT, ids=lambda p: p.name)
def test_doc_has_no_ownership_lease_wording(path):
    assert _violations(path.read_text()) == []


@pytest.mark.parametrize("line", [
    "Run the ownership lease stage first.",
    "Lease ownership before converting.",
    "It will take ownership of the table.",
    "ALTER TABLE t OWNER TO `g`",
    "Cleanup restores leased ownership.",
])
def test_sweep_flags_lease_wording(line):
    assert _violations(line)


@pytest.mark.parametrize("line", [
    "Ownership never changes, so there is no owner to restore.",
    "Tables leased under the older ownership model are still restored.",
    "Run job_03d_lease_ownership (Authorize); it closes LEASE_OWNERSHIP.",
    "The gate name is kept from the ownership lease it replaced.",
])
def test_sweep_allows_convert_permission_and_legacy_notes(line):
    assert _violations(line) == []
