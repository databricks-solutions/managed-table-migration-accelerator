"""The GitHub workflow and Dependabot files parse as YAML and keep their hardening. No network.

GitHub ignores an unparseable workflow, so a typo would silently stop CI or the docs deploy:
an unquoted ``:all: `` in a ``run:`` line did exactly that once.
"""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
DEPENDABOT = ROOT / ".github" / "dependabot.yml"
_SHA_PIN = re.compile(r"^[\w.-]+/[\w.-]+(/[\w./-]+)?@[0-9a-f]{40}$")


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _steps(workflow: dict):
    for job in workflow["jobs"].values():
        yield from job.get("steps", [])


def test_there_are_workflows():
    assert [p.name for p in WORKFLOWS] == ["ci.yml", "deploy-docs.yml"]


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_parses_with_jobs(path):
    workflow = _load(path)
    assert isinstance(workflow, dict) and workflow.get("jobs"), path.name


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_is_read_only_at_the_top(path):
    assert _load(path)["permissions"] == {"contents": "read"}


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_commit_sha(path):
    uses = [s["uses"] for s in _steps(_load(path)) if "uses" in s]
    assert uses, path.name
    assert [u for u in uses if not _SHA_PIN.match(u)] == []


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_checkout_never_persists_credentials(path):
    checkouts = [s for s in _steps(_load(path)) if s.get("uses", "").startswith("actions/checkout@")]
    assert checkouts and all(s.get("with", {}).get("persist-credentials") is False for s in checkouts)


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_pull_request_target(path):
    # PyYAML reads the bare key `on` as the boolean True.
    triggers = _load(path).get(True) or _load(path).get("on")
    assert "pull_request_target" not in triggers


def test_installs_skip_scripts_and_hash_check_python():
    runs = [s["run"] for p in WORKFLOWS for s in _steps(_load(p)) if "run" in s]
    npm = [r for r in runs if "npm ci" in r]
    pip = [r for r in runs if "pip install" in r]
    assert npm and all("--ignore-scripts" in r for r in npm)
    assert pip and all("--require-hashes" in r and "--only-binary :all:" in r for r in pip)


def test_dependabot_parses_and_github_actions_uses_only_default_days():
    config = _load(DEPENDABOT)
    assert config["version"] == 2
    for update in config["updates"]:
        cooldown = update.get("cooldown", {})
        if update["package-ecosystem"] == "github-actions":
            # GitHub rejects the semver-*-days keys for this ecosystem and ignores the whole file.
            assert set(cooldown) <= {"default-days"}, cooldown
