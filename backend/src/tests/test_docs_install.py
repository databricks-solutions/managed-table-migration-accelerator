"""The install instructions work as written: the commands they show, the order they give, and the
bundle settings they rely on."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
README = ROOT / "README.md"
GUIDE = ROOT / "docs" / "00-installation.md"
DOCS = [README, *sorted((ROOT / "docs").glob("*.md"))]
FENCE = re.compile(r"```(?:bash|sh|shell)?\n(.*?)```", re.S)


def _bundle():
    return yaml.safe_load((ROOT / "databricks.yml").read_text())


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    level = heading.split(" ", 1)[0]
    nxt = re.search(rf"(?m)^{level} ", text[start + len(heading):])
    return text[start:start + len(heading) + nxt.start()] if nxt else text[start:]


def test_install_examples_never_pass_deploy_settings_the_run_would_lose():
    # `bundle run` resolves the variables again, so a --var given only to deploy is lost on the run.
    offenders = []
    for doc in DOCS:
        for block in FENCE.findall(doc.read_text()):
            if "bundle run install" not in block:
                continue
            for line in block.splitlines():
                if "bundle deploy" in line and "--var" in line:
                    offenders.append(f"{doc.relative_to(ROOT)}: {line.strip()}")
    assert not offenders, offenders


def test_readme_install_gives_the_workspace_path_then_the_cli_then_the_app():
    install = _section(README.read_text(), "## Install\n")
    order = [install.index(h) for h in ("### Install from the workspace", "### Install with the CLI",
                                        "### Open the app")]
    assert order == sorted(order)
    assert "cannot do the first install" in install


def test_the_guide_gives_both_install_paths_before_the_reference():
    text = GUIDE.read_text()
    order = [text.index(h) for h in ("## Before you start", "## Install from the workspace",
                                     "## Install with the CLI", "## Check that it worked", "## Upgrade",
                                     "## Reference", "## Troubleshooting")]
    assert order == sorted(order)


def test_dev_target_takes_the_workspace_from_the_profile_or_git_folder():
    dev = _bundle()["targets"]["dev"]
    assert "host" not in (dev.get("workspace") or {})
    assert "warehouse_id" in dev["variables"]


def test_dashboard_dataset_follows_the_control_schema():
    variables = _bundle()["variables"]
    assert variables["dataset_catalog"]["default"] == "${var.manifest_catalog}"
    assert variables["dataset_schema"]["default"] == "${var.manifest_schema}"


def test_personal_targets_are_included_and_git_ignored():
    assert "databricks.local*.yml" in _bundle()["include"]
    assert "databricks.local*.yml" in (ROOT / ".gitignore").read_text().splitlines()
