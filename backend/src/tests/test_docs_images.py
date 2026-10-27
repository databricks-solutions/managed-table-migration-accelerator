"""Every image a doc links to exists and has alt text, and nothing in docs/images/ is orphaned."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DOC_ROOTS = [ROOT / "README.md", ROOT / "docs", ROOT / "backend" / "docs", ROOT / "app" / "docs"]
IMAGES = ROOT / "docs" / "images"
INSTALL_IMAGES = IMAGES / "install"
DOORS = ("notebook", "cli", "app", "mcp")
IGNORED = ("superpowers", "plans")
# The docs site project: build output and node_modules, no source pages.
SITE = ROOT / "docs" / "site"
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")

_MD_IMG = re.compile(r"!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_HTML_IMG = re.compile(r"<img\b[^>]*>", re.I)
_ATTR = re.compile(r"\b(src|alt)\s*=\s*[\"']([^\"']*)[\"']", re.I)


def _docs():
    for r in DOC_ROOTS:
        if r.is_file():
            yield r
        elif r.exists():
            for p in sorted(r.rglob("*.md")):
                if p.is_relative_to(SITE):
                    continue
                if not any(part in IGNORED for part in p.relative_to(ROOT).parts):
                    yield p


def _images():
    """``(doc, alt, resolved_target)`` for every local image reference."""
    for doc in _docs():
        text = doc.read_text()
        refs = [(m.group(1), m.group(2)) for m in _MD_IMG.finditer(text)]
        for tag in _HTML_IMG.findall(text):
            attrs = {k.lower(): v for k, v in _ATTR.findall(tag)}
            refs.append((attrs.get("alt", ""), attrs.get("src", "")))
        for alt, target in refs:
            target = target.split("#")[0]
            if target and not re.match(r"^[a-z]+:", target):
                yield doc, alt, (doc.parent / target).resolve()


def test_every_linked_image_exists():
    missing = [f"{d.relative_to(ROOT)} -> {t}" for d, _, t in _images() if not t.exists()]
    assert not missing, missing


def test_every_image_has_alt_text():
    bare = [f"{d.relative_to(ROOT)} -> {t.name}" for d, alt, t in _images() if not alt.strip()]
    assert not bare, bare


def test_no_orphan_files_in_docs_images():
    linked = {t for _, _, t in _images()}
    files = [p.resolve() for p in IMAGES.rglob("*") if p.suffix.lower() in IMAGE_SUFFIXES]
    orphans = sorted(str(p.relative_to(ROOT)) for p in files if p not in linked)
    assert not orphans, orphans


def test_each_front_door_has_install_screenshots():
    for door in DOORS:
        assert list((INSTALL_IMAGES / door).glob("*.png")), door
