"""scripts/release.py: the local release gate (releases are published from a checkout, not from CI)."""

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("release_script", REPO / "scripts" / "release.py")
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)

CHANGELOG = """# Changelog

## [Unreleased]

## [0.2.0] - 2026-11-01

### Changed

- Something.

## [0.1.0] - 2026-10-22

### Added

- First.

[Unreleased]: https://example.com/compare/v0.2.0...HEAD
[0.2.0]: https://example.com/releases/tag/v0.2.0
"""


def _estate(root: Path, version: str, changelog: str = CHANGELOG, lock_inner: str | None = None) -> Path:
    (root / "backend/src/uc_ext2managed").mkdir(parents=True)
    (root / "app/server/telemetry").mkdir(parents=True)
    (root / "backend/src/uc_ext2managed/__init__.py").write_text(f'PRODUCT = "x"\n__version__ = "{version}"\n')
    (root / "app/package.json").write_text(json.dumps({"name": "x", "version": version}))
    (root / "app/package-lock.json").write_text(
        json.dumps({"version": version, "packages": {"": {"version": lock_inner or version}}})
    )
    (root / "app/server/telemetry/product.ts").write_text(f"export const VERSION = '{version}' as const;\n")
    (root / "CHANGELOG.md").write_text(changelog)
    return root


@pytest.mark.parametrize("tag,version", [("v0.1.0", "0.1.0"), ("v1.2.3-rc.1", "1.2.3-rc.1")])
def test_parse_tag_accepts_semver(tag, version):
    assert release.parse_tag(tag) == version


@pytest.mark.parametrize("tag", ["0.1.0", "v0.1", "v0.1.0.1", "v0.1.0-", "release-0.1.0"])
def test_parse_tag_rejects_non_semver(tag):
    with pytest.raises(ValueError):
        release.parse_tag(tag)


def test_prerelease_is_any_suffix():
    assert release.is_prerelease("1.0.0-rc.1") and not release.is_prerelease("1.0.0")


def test_notes_stop_at_the_next_heading_and_drop_link_references():
    assert release.changelog_notes(CHANGELOG, "0.2.0") == "### Changed\n\n- Something."
    assert release.changelog_notes(CHANGELOG, "0.1.0") == "### Added\n\n- First."
    assert release.changelog_notes(CHANGELOG, "9.9.9") == ""


def test_check_passes_when_every_file_matches(tmp_path):
    assert release.check(_estate(tmp_path, "0.2.0"), "v0.2.0") == []


def test_check_names_every_mismatched_file(tmp_path):
    problems = release.check(_estate(tmp_path, "0.2.0", lock_inner="0.1.0"), "v0.2.0")
    assert problems == ["app/package-lock.json (packages[\"\"]): '0.1.0' != '0.2.0'"]


def test_check_requires_a_dated_changelog_heading(tmp_path):
    problems = release.check(_estate(tmp_path, "0.3.0"), "v0.3.0")
    assert problems == ["CHANGELOG.md: no '## [0.3.0] - YYYY-MM-DD' heading"]


def test_check_rejects_an_empty_changelog_section(tmp_path):
    empty = "## [0.3.0] - 2026-12-01\n\n## [0.2.0] - 2026-11-01\n\n- x\n"
    assert release.check(_estate(tmp_path, "0.3.0", changelog=empty), "v0.3.0") == [
        "CHANGELOG.md: the 0.3.0 section is empty"
    ]


def test_check_rejects_a_bad_tag_before_reading_files(tmp_path):
    assert release.check(tmp_path, "v0.3") == ["tag 'v0.3' is not vMAJOR.MINOR.PATCH[-PRERELEASE]"]


def test_the_checked_in_tree_is_consistent_with_itself():
    versions = set(release.found_versions(REPO).values())
    assert len(versions) == 1, versions
    (version,) = versions
    assert release.check(REPO, f"v{version}") == []
