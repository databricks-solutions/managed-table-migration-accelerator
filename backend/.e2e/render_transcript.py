"""Render install front-door output to a PNG for the docs, with identifiers replaced.

Two inputs:

* ``--text FILE``: a CLI or MCP transcript (plain text) drawn as a terminal window.
* ``--notebook-html FILE``: a notebook run exported with ``databricks jobs export-run`` (the
  ``views[0].content`` HTML, or the export JSON itself), rendered as the workspace shows it.

``--redact FILE`` is a JSON ``{find: replace}`` map kept OUTSIDE the repo (emails, principal ids,
hosts, catalog names). Text input is replaced before drawing; a rendered notebook is replaced in
the page after it loads. Review every PNG for leftover identifiers before filing it.

Usage:
  python .e2e/render_transcript.py --text /tmp/cli_check.txt --title "databricks bundle run install" \\
      --redact /tmp/redact.json --out docs/images/install/cli/check.png
  python .e2e/render_transcript.py --notebook-html /tmp/export.json --redact /tmp/redact.json \\
      --from-text "Can you install" --to-text "Grant plan" --out docs/images/install/notebook/rights.png
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

_TERMINAL = """<!doctype html><html><head><meta charset="utf-8"><style>
body {{ margin: 0; background: #f4f4f5; font-family: -apple-system, system-ui, sans-serif; }}
.frame {{ display: inline-block; padding: 24px; }}
.win {{ width: {width}px; border-radius: 10px; overflow: hidden; box-shadow: 0 8px 28px rgba(0,0,0,.18); }}
.bar {{ background: #2b2d31; color: #c9cbd1; font-size: 12px; padding: 8px 12px; display: flex; gap: 8px;
        align-items: center; }}
.dot {{ width: 11px; height: 11px; border-radius: 50%; display: inline-block; }}
pre {{ margin: 0; background: #1b1c1f; color: #e6e6e6; padding: 16px 18px; font: 12.5px/1.5 ui-monospace,
       SFMono-Regular, Menlo, monospace; white-space: pre-wrap; word-break: break-word; }}
.cmd {{ color: #8ab4f8; }} .ok {{ color: #7ee2a8; }} .warn {{ color: #f6c177; }} .bad {{ color: #ff8b8b; }}
</style></head><body><div class="frame"><div class="win"><div class="bar"><span class="dot" style="background:#ff5f57"></span>
<span class="dot" style="background:#febc2e"></span><span class="dot" style="background:#28c840"></span>
<span style="margin-left:8px">{title}</span></div><pre>{body}</pre></div></div></body></html>"""

_REDACT_JS = """(pairs) => {
  const swap = (t) => pairs.reduce((acc, [a, b]) => acc.split(a).join(b), t);
  const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = walk.nextNode(); n; n = walk.nextNode()) n.nodeValue = swap(n.nodeValue);
}"""

# Notebook cell outputs sit in fixed-height scroll boxes; a doc image shows the whole output.
_UNCAP_JS = """() => {
  for (const o of document.querySelectorAll('.ansiout')) {
    for (let n = o.parentElement, i = 0; n && i < 8; n = n.parentElement, i++) {
      if (parseFloat(getComputedStyle(n).height) < o.scrollHeight) {
        n.style.height = 'auto'; n.style.maxHeight = 'none'; n.style.overflow = 'visible';
      }
    }
  }
}"""

_COLUMN = re.compile(r"(?<=  )\S")


def load_redactions(path: str | None) -> list[tuple[str, str]]:
    if not path:
        return []
    pairs = json.loads(Path(path).read_text()).items()
    return sorted(pairs, key=lambda kv: -len(kv[0]))


def _columns(lines: list[str]) -> dict[int, int]:
    """How many lines start a field (text after 2+ spaces) at each column."""
    seen: dict[int, int] = {}
    for ln in lines:
        for m in _COLUMN.finditer(ln):
            seen[m.start()] = seen.get(m.start(), 0) + 1
    return seen


def _realign(line: str, at: int, shift: int, columns: dict[int, int]) -> str:
    """Put the field after position ``at`` back on its block's column: the right-most column two
    other lines use, at or left of where the field stood before the value changed length."""
    m = re.compile(r"\S( +)(?=\S)").search(line, max(0, at - 1))
    if not m:
        return line
    gap, field = m.start(1), m.end(1)
    passed = [c for c, n in columns.items() if n >= 2 and gap + 2 <= c <= field + shift]
    if not passed:
        return line
    return line[:gap] + " " * (max(passed) - gap) + line[field:]


def redact(text: str, pairs: list[tuple[str, str]]) -> str:
    """Replace every identifier. On a column-formatted line (fields split by 2+ spaces) a value that
    changed length would shift the next field, so that field is realigned to its block."""
    lines = text.split("\n")
    columns = _columns(lines)
    out = []
    for orig in lines:
        ln, moved = orig, None
        for a, b in pairs:
            if a in ln:
                i = ln.index(a)
                ln = ln.replace(a, b)
                if len(b) != len(a):
                    moved = i + len(b) if moved is None else min(moved, i + len(b))
        if moved is not None and re.search(r"\S  +\S", orig):
            ln = _realign(ln, moved, len(orig) - len(ln), columns)
        out.append(ln)
    return "\n".join(out)


def _tone(line: str) -> str:
    esc = html.escape(line)
    if line.startswith("$ "):
        return f'<span class="cmd">{esc}</span>'
    for word, cls in (("NO-GO", "bad"), ("FAIL", "bad"), ("WARN", "warn"), ("GO ", "ok"), ("[present]", "ok"),
                      ("[missing]", "bad"), ("granted", "ok"), ("revoked", "ok")):
        if word in line:
            return f'<span class="{cls}">{esc}</span>'
    return esc


def terminal_html(text: str, title: str, width: int) -> str:
    body = "\n".join(_tone(ln) for ln in text.rstrip("\n").splitlines())
    return _TERMINAL.format(width=width, title=html.escape(title), body=body)


def notebook_html(path: Path) -> str:
    raw = path.read_text()
    if raw.lstrip().startswith("{"):
        return json.loads(raw)["views"][0]["content"]
    return raw


def render(page_html: str, out: Path | None, *, pairs, width: int, notebook: bool, from_text: str | None,
           to_text: str | None, print_text: bool = False) -> None:
    from playwright.sync_api import sync_playwright

    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        # The exported notebook scrolls inside its own container, so a tall viewport lays out every cell.
        page = browser.new_page(viewport={"width": width + 48, "height": 40000 if notebook else 900},
                                device_scale_factor=2)
        page.set_content(page_html, wait_until="networkidle" if notebook else "load")
        if notebook:
            page.wait_for_timeout(2500)
            page.evaluate(_UNCAP_JS)
            if pairs:
                # Cell output is plain monospace text: redact it line by line, keeping its columns.
                texts = page.evaluate("() => [...document.querySelectorAll('.ansiout')].map(e => e.textContent)")
                page.evaluate("(ts) => document.querySelectorAll('.ansiout').forEach((e, i) => { e.textContent = ts[i]; })",
                              [redact(t, pairs) for t in texts])
                page.evaluate(_REDACT_JS, [list(p) for p in pairs])
            page.wait_for_timeout(300)
        if print_text:
            print(page.inner_text("body"))
        if out and from_text:
            page.screenshot(path=str(out), clip=_clip(page, from_text, to_text, width + 48))
        elif out and notebook:
            page.screenshot(path=str(out), clip={"x": 0, "y": 0, "width": width + 48, "height": 900})
        elif out:
            page.locator(".frame").screenshot(path=str(out))
        browser.close()


def _locate(page, text: str):
    """The first element showing ``text``; a ``re:`` prefix makes it a regular expression. In a
    notebook only cell OUTPUT (``.ansiout``) is searched, since the source shows the same strings."""
    pattern = re.compile(text[3:], re.M) if text.startswith("re:") else text
    outputs = page.locator(".ansiout")
    if outputs.count():
        return outputs.filter(has_text=pattern).first
    return page.get_by_text(pattern).first


def _clip(page, from_text: str, to_text: str | None, width: int) -> dict:
    """From the ``from_text`` element to the end of the ``to_text`` one (or of itself)."""
    top = _locate(page, from_text).bounding_box()
    end = _locate(page, to_text).bounding_box() if to_text else top
    x0, y0 = max(0, top["x"] - 16), max(0, top["y"] - 16)
    return {"x": x0, "y": y0, "width": min(width - x0, top["width"] + 32),
            "height": max(40, end["y"] + end["height"] + 16 - y0)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--text", help="plain-text transcript (CLI or MCP)")
    src.add_argument("--notebook-html", help="export-run HTML (or its JSON)")
    ap.add_argument("--out", help="PNG to write (omit with --print-text to only review the text)")
    ap.add_argument("--print-text", action="store_true", help="print the page text after redaction")
    ap.add_argument("--title", default="Terminal")
    ap.add_argument("--redact", help="JSON {find: replace} file outside the repo")
    ap.add_argument("--width", type=int, default=980)
    ap.add_argument("--from-text", help="clip from the first element with this text")
    ap.add_argument("--to-text", help="clip to the first element with this text")
    a = ap.parse_args()
    pairs = load_redactions(a.redact)
    if a.text:
        page_html = terminal_html(redact(Path(a.text).read_text(), pairs), redact(a.title, pairs), a.width)
    else:
        page_html = notebook_html(Path(a.notebook_html))
    if not a.out and not a.print_text:
        ap.error("give --out, --print-text, or both")
    render(page_html, Path(a.out) if a.out else None, pairs=pairs, width=a.width,
           notebook=bool(a.notebook_html), from_text=a.from_text, to_text=a.to_text, print_text=a.print_text)
    if a.out:
        print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
