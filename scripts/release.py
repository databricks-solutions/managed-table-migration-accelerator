#!/usr/bin/env python3
"""Cut a GitHub release from a local checkout (the repository has no GitHub Actions).

    python scripts/release.py check   v0.1.0   # tag == version in every versioned file + changelog
    python scripts/release.py notes   0.1.0    # print that version's CHANGELOG.md section
    python scripts/release.py publish v0.1.0   # check, tag HEAD of main, push the tag, gh release create

Run ``make ci`` before ``publish`` (``make release TAG=vX.Y.Z`` does both). ``publish`` refuses a
dirty tree, a HEAD that is not ``<remote>/main``, and an existing tag that points elsewhere.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TAG_RE = re.compile(r"v(\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?)")


def parse_tag(tag: str) -> str:
    m = TAG_RE.fullmatch(tag)
    if not m:
        raise ValueError(f"tag {tag!r} is not vMAJOR.MINOR.PATCH[-PRERELEASE]")
    return m.group(1)


def is_prerelease(version: str) -> bool:
    return "-" in version


def _grab(text: str, pattern: str) -> str | None:
    m = re.search(pattern, text, re.M)
    return m.group(1) if m else None


def found_versions(root: Path) -> dict[str, str | None]:
    lock = json.loads((root / "app/package-lock.json").read_text(encoding="utf-8"))
    return {
        "backend/src/uc_ext2managed/__init__.py": _grab(
            (root / "backend/src/uc_ext2managed/__init__.py").read_text(encoding="utf-8"),
            r'^__version__ = "([^"]+)"',
        ),
        "app/package.json": json.loads((root / "app/package.json").read_text(encoding="utf-8")).get(
            "version"
        ),
        "app/package-lock.json (top level)": lock.get("version"),
        'app/package-lock.json (packages[""])': lock.get("packages", {}).get("", {}).get("version"),
        "app/server/telemetry/product.ts": _grab(
            (root / "app/server/telemetry/product.ts").read_text(encoding="utf-8"),
            r"^export const VERSION = '([^']+)'",
        ),
    }


def changelog_has_heading(changelog: str, version: str) -> bool:
    return re.search(rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}$", changelog, re.M) is not None


def changelog_notes(changelog: str, version: str) -> str:
    """The body of ``## [version]`` up to the next ``## [`` heading, minus link references."""
    out: list[str] = []
    on = False
    for line in changelog.splitlines():
        if line.startswith(f"## [{version}]"):
            on = True
            continue
        if on and line.startswith("## ["):
            break
        if on and re.match(r"^\[[^\]]+\]: ", line):
            continue
        if on:
            out.append(line)
    return "\n".join(out).strip()


def check(root: Path, tag: str) -> list[str]:
    """Every problem that should stop a release of ``tag``; empty means good to go."""
    try:
        want = parse_tag(tag)
    except ValueError as e:
        return [str(e)]
    problems = [f"{label}: {got!r} != {want!r}" for label, got in found_versions(root).items() if got != want]
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    if not changelog_has_heading(changelog, want):
        problems.append(f"CHANGELOG.md: no '## [{want}] - YYYY-MM-DD' heading")
    elif not changelog_notes(changelog, want):
        problems.append(f"CHANGELOG.md: the {want} section is empty")
    return problems


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


def publish(tag: str, remote: str) -> None:
    problems = check(ROOT, tag)
    if problems:
        sys.exit("refusing to release:\n  " + "\n  ".join(problems))
    if _git("status", "--porcelain"):
        sys.exit("refusing to release: the working tree is not clean")
    _git("fetch", remote, "main", "--tags")
    head, main = _git("rev-parse", "HEAD"), _git("rev-parse", f"{remote}/main")
    if head != main:
        sys.exit(f"refusing to release: HEAD {head[:12]} is not {remote}/main {main[:12]}")
    existing = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout.strip()
    if existing and existing != head:
        sys.exit(f"refusing to release: tag {tag} already points at {existing[:12]}, not HEAD")
    if not existing:
        _git("tag", "-a", tag, "-m", tag)
    subprocess.run(["git", "push", remote, f"refs/tags/{tag}"], cwd=ROOT, check=True)

    version = parse_tag(tag)
    notes = changelog_notes((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), version)
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(notes + "\n")
    flag = "--prerelease" if is_prerelease(version) else "--latest"
    url = _git("remote", "get-url", remote)
    subprocess.run(
        ["gh", "release", "create", tag, "--repo", url, "--verify-tag", "--title", tag,
         "--notes-file", f.name, flag],
        cwd=ROOT, check=True,
    )


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check").add_argument("tag")
    sub.add_parser("notes").add_argument("version")
    pub = sub.add_parser("publish")
    pub.add_argument("tag")
    pub.add_argument("--remote", default="origin")
    a = p.parse_args(argv)

    if a.cmd == "check":
        problems = check(ROOT, a.tag)
        for label, got in found_versions(ROOT).items():
            print(f"  {label}: {got}")
        if problems:
            sys.exit("NOT releasable:\n  " + "\n  ".join(problems))
        print(f"{a.tag} is releasable")
    elif a.cmd == "notes":
        print(changelog_notes((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), a.version))
    else:
        publish(a.tag, a.remote)


if __name__ == "__main__":
    main()
