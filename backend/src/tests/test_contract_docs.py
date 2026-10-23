"""The contract docs agree with the code they describe (one-wave M5, design §6). No Spark, no network."""
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import actions, audit, authority, conductor, executor, focus, manifest, optimization, ownership  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
JOBS_MD = (ROOT / "backend/docs/contracts/jobs.md").read_text()
CONTRACT_MD = (ROOT / "backend/docs/contracts/control-tables.md").read_text()
INTEGRATION_MD = (ROOT / "backend/docs/integration/control-tables.md").read_text()
AGENTS_MD = (ROOT / "AGENTS.md").read_text()
RESOURCES = ROOT / "backend/resources"

CODE_COLUMNS = {
    "table_manifest": manifest.MANIFEST_COLUMN_NAMES,
    "migration_actions": actions.ACTION_COLUMN_NAMES,
    "migration_next_actions": conductor.NEXT_ACTIONS_COLUMN_NAMES,
    "migration_next_actions_by_scope": focus.BY_SCOPE_COLUMN_NAMES,
    "migration_focus_pins": focus.PIN_EVENT_COLUMN_NAMES,
    "audit_log": audit.AUDIT_COLUMN_NAMES,
    "freeze_ledger": executor.LEDGER_COLUMN_NAMES,
    "ownership_ledger": ownership.LEDGER_COLUMN_NAMES,
    "convert_authority": authority.AUTHORITY_COLUMN_NAMES,
}


def _registry(obj: str) -> list[str]:
    """The jobs.md "Control-column registry" list for ``obj``."""
    m = re.search(rf"^- `{obj}\[\*\]`:\n\s+`([^`]+)`", JOBS_MD, re.M)
    assert m, f"no {obj}[*] entry in the jobs.md registry"
    return [c.strip() for c in m.group(1).split(",")]


def _contract_rows(obj: str) -> list[str]:
    """The column names control-tables.md documents for ``obj`` (one table row each)."""
    return re.findall(rf"^\| {obj} \| ([a-z0-9_]+) \|", CONTRACT_MD, re.M)


@pytest.mark.parametrize("obj", sorted(CODE_COLUMNS))
def test_jobs_registry_lists_every_code_column(obj):
    assert sorted(_registry(obj)) == sorted(CODE_COLUMNS[obj])


@pytest.mark.parametrize("obj", sorted(CODE_COLUMNS))
def test_control_tables_contract_documents_every_code_column(obj):
    rows = _contract_rows(obj)
    assert len(rows) == len(set(rows)), sorted({c for c in rows if rows.count(c) > 1})
    assert sorted(set(rows)) == sorted(CODE_COLUMNS[obj])


def test_jobs_registry_wave_status_matches_the_contract_rows():
    assert sorted(_registry("wave_status")) == sorted(_contract_rows("wave_status"))


def _projection_names(select_list: str) -> list[str]:
    """Output column names of a SELECT list (top-level commas; ``AS x`` or a bare name)."""
    parts, depth, cur = [], 0, ""
    for ch in select_list:
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    names = []
    for p in (x.strip() for x in parts if x.strip()):
        m = re.search(r"\bAS\s+([a-z0-9_]+)\s*$", p, re.S) or re.match(r"^([a-z0-9_]+)$", p)
        assert m, p
        names.append(m.group(1))
    return names


def test_wave_status_view_columns_match_the_contract_rows():
    from migration import progress  # noqa: PLC0415

    sql = progress.create_wave_status_view_sql("c", "s")
    select_list = sql.split("FROM with_actions")[0].rsplit("\nSELECT\n", 1)[1]
    assert sorted(_projection_names(select_list)) == sorted(_contract_rows("wave_status"))


def test_external_reader_readiness_view_columns_are_documented():
    from migration import compat  # noqa: PLC0415

    sql = compat.create_external_reader_readiness_view_sql("c", "s")
    view_cols = re.findall(r"\bAS ([a-z_]+)\b", sql.split(") r\n")[0].split("SELECT * FROM (")[1])
    documented = []
    for row in re.findall(r"^\| external_reader_readiness \| ([a-z0-9_ /]+) \|", CONTRACT_MD, re.M):
        documented += [c.strip() for c in row.split("/")]
    assert sorted(view_cols) == sorted(documented)


def test_next_step_kind_domain_matches_the_conductor():
    row = re.search(r"^\| migration_next_actions \| next_step_kind \|.*$", CONTRACT_MD, re.M).group(0)
    documented = {v.strip(" `") for v in re.search(r"Values: ([^.]+)\.", row).group(1).split(",")}
    code = {v for k, v in vars(conductor).items() if k.startswith("STEP_") and isinstance(v, str)}
    assert documented == code


def test_integration_audit_action_domain_lists_every_audit_action():
    row = re.search(r"^\| `action` \| STRING \|.*$", INTEGRATION_MD, re.M).group(0)
    documented = set(re.findall(r"`([A-Z_]+)`", row))
    code = {v for k, v in vars(audit).items() if k.startswith("ACTION_") and isinstance(v, str)}
    assert code <= documented, sorted(code - documented)


def test_integration_doc_does_not_count_the_deferred_defaults():
    assert "Every key of `manifest._DEFERRED_DEFAULTS`" in INTEGRATION_MD
    assert not re.search(r"\b\d+\s+keys of\s+`(manifest\.)?_DEFERRED_DEFAULTS`", INTEGRATION_MD, re.S)


def test_agents_md_recompute_tail_list_matches_the_bundle():
    tailed = {re.match(r"job_(\w+?)_", p.name).group(1)
              for p in RESOURCES.glob("job_*.yml") if "task_key: recompute_next_step" in p.read_text()}
    sentence = re.search(r"`run_if: ALL_DONE`\):(.*?)\(and", AGENTS_MD, re.S).group(1)
    assert set(re.findall(r"`(\w+)`", sentence)) == tailed


def test_agents_md_names_the_live_stamp_function():
    assert "optimization.tables_to_stamp" not in AGENTS_MD
    assert "optimization.optimize_stamp_rows" in AGENTS_MD
    assert callable(optimization.optimize_stamp_rows)
