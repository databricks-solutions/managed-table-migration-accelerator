"""Guard test: the knowledge library on disk matches the registry. No Spark/SDK."""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from migration import genie_space as gs  # noqa: E402

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def test_registry_is_nonempty_and_excludes_setup():
    assert gs.KNOWLEDGE_DOCS, "registry empty"
    assert "00_setup" not in gs.KNOWLEDGE_DOCS
    assert len(gs.KNOWLEDGE_DOCS) == len(set(gs.KNOWLEDGE_DOCS))


def test_volume_fqn_bound():
    assert gs.volume_fqn("c", "s") == "c.s.genie_knowledge"


def test_every_registered_doc_has_md_with_header():
    for path in gs.knowledge_doc_md_paths(_REPO_ROOT):
        assert os.path.exists(path), f"missing knowledge doc: {path}"
        with open(path, encoding="utf-8") as fh:
            head = fh.read(600)
        assert head.lstrip().startswith("# "), f"{path} missing '# Title'"
        # one-paragraph description under the title
        assert len(head.strip().splitlines()) >= 3, f"{path} missing description"


def test_setup_doc_exists_but_is_not_uploaded():
    setup = os.path.join(_REPO_ROOT, "docs", "genie", "00_setup.md")
    assert os.path.exists(setup)


def _docs() -> dict:
    out = {}
    for name, path in zip(gs.KNOWLEDGE_DOCS, gs.knowledge_doc_md_paths(_REPO_ROOT)):
        with open(path, encoding="utf-8") as fh:
            out[name] = fh.read()
    return out


_LINK = re.compile(r"\]\(([^)#\s]+\.md)(?:#[^)]*)?\)")


def test_every_doc_link_resolves_to_a_registered_doc():
    for name, text in _docs().items():
        for target in _LINK.findall(text):
            assert target[:-3] in gs.KNOWLEDGE_DOCS, f"{name}.md links to unknown doc {target}"


def _retired_references(text: str) -> list:
    """Retired doc names written as doc references: ``name.md`` / ``name.pdf`` anywhere, or a
    backticked multi-word name (``post_convert_optimization``). Single words such as ``streaming``
    are also bucket names and ordinary vocabulary, so a bare backtick does not count for them."""
    names = "|".join(sorted(gs.RETIRED_KNOWLEDGE_DOCS, key=len, reverse=True))
    files = re.findall(rf"(?<![\w/])(?:{names})\.(?:md|pdf)\b", text)
    multi = "|".join(n for n in gs.RETIRED_KNOWLEDGE_DOCS if "_" in n)
    ticked = re.findall(rf"`(?:{multi})`", text)
    return files + ticked


def test_retired_reference_detector():
    assert _retired_references("see `post_convert_optimization` and glossary.md") == [
        "glossary.md", "`post_convert_optimization`"]
    assert _retired_references("the `streaming` bucket; an overview of waves") == []


def test_no_doc_names_a_retired_doc():
    for name, text in _docs().items():
        assert _retired_references(text) == [], f"{name}.md references retired docs"


# --- one home per fact ----------------------------------------------------------------------
# Detail-only tokens: each is the detail of ONE topic and may appear only in that topic's home doc
# (plus a one-line definition in the glossary and a symptom heading in troubleshooting). Never a job
# parameter name: those legitimately appear in the command reference and next-step answers.
HOME_TOKENS = {
    "finish_steps": ("zOrderBy", "REPLACE PARTITIONED BY", "hints_rejected", "WITH HISTORY",
                     "enabledFormats", "ALTER SHARE"),
    "rollback_and_cleanup": ("from_state", "UNSET MANAGED"),
    "stages_and_gates": ("name_based_access", "describe_detail", "history_retained",
                         "streaming_restart", "delta_sharing_reshare"),
    "freeze_streams_and_writers": ("PAUSE_JOB", "STOP_PIPELINE", "STOP_STREAM_JOB",
                                   "PAUSE_WRITER_JOB", "recent_vacuum_activity"),
    "planning_and_eligibility": ("path_redirect_requires_name_based_or_opt_in", "exceeds_wave_byte_cap",
                                 "path_based_access_blocks_managed", "external_writer_blocks_managed"),
    "platform_reference": ("DELTA_TRUNCATED_TRANSACTION_LOG", "inCommitTimestamp"),
}
_ALSO_ALLOWED = ("overview_and_glossary", "troubleshooting")

# Broad topics: a non-home doc may mention them at most twice (outside its Related documents list).
BROAD_TOPICS = {
    r"\b(?:14 days|14-day)\b": "rollback_and_cleanup",
    r"freeze bracket": "freeze_streams_and_writers",
}
_BROAD_CAP = 2


def test_home_token_table_is_well_formed():
    assert set(HOME_TOKENS) <= set(gs.KNOWLEDGE_DOCS)
    assert set(BROAD_TOPICS.values()) <= set(gs.KNOWLEDGE_DOCS)
    tokens = [t for ts in HOME_TOKENS.values() for t in ts]
    assert len(tokens) == len(set(tokens))


def test_every_home_token_is_actually_in_its_home():
    docs = _docs()
    for home, tokens in HOME_TOKENS.items():
        for token in tokens:
            assert token in docs[home], f"{token!r} is not in its home {home}.md (stale table)"


def test_detail_tokens_live_only_in_their_home():
    docs = _docs()
    for home, tokens in HOME_TOKENS.items():
        for token in tokens:
            strays = [n for n, t in docs.items()
                      if n != home and n not in _ALSO_ALLOWED and token in t]
            assert not strays, f"{token!r} belongs in {home}.md but also appears in {strays}"


def test_broad_topics_are_explained_in_one_place():
    docs = _docs()
    for pattern, home in BROAD_TOPICS.items():
        assert re.search(pattern, docs[home], re.I), f"{pattern} missing from its home {home}.md"
        for name, text in docs.items():
            if name == home:
                continue
            body = text.split("\n## Related documents")[0]
            count = len(re.findall(pattern, body, re.I))
            assert count <= _BROAD_CAP, f"{name}.md mentions {pattern} {count} times; link to {home}.md"


def test_setup_doc_is_not_uploaded():
    assert "00_setup" not in gs.KNOWLEDGE_DOCS
    assert not any("00_setup" in target for _, target in gs.knowledge_upload_plan("/r", "c", "s"))


def test_no_rendered_pdfs_remain():
    pdf_dir = os.path.join(_REPO_ROOT, "docs", "genie", "pdf")
    assert not os.path.exists(pdf_dir) or not os.listdir(pdf_dir), "the library ships Markdown"


# --- job_99_setup publishing edge ---------------------------------------------------------

def _setup_notebook() -> str:
    with open(os.path.join(_REPO_ROOT, "notebooks", "99_setup.py"), encoding="utf-8") as fh:
        return fh.read()


def test_setup_publishes_docs_before_the_space_update():
    nb = _setup_notebook()
    assert nb.index("gs.knowledge_upload_plan(") < nb.index("gs.update_space_request(")


def test_setup_prunes_only_after_every_upload_and_only_through_the_plan():
    nb = _setup_notebook()
    gate = nb.index("KNOWLEDGE_PUBLISHED = uploaded == len(plan)")
    prune = nb.index("gs.knowledge_prune_plan(")
    assert gate < prune
    assert nb.count("w.files.delete(") == 1
    assert "for path in _remove:" in nb[prune:nb.index("w.files.delete(")]


def test_setup_attaches_the_volume_and_retries_only_on_a_400():
    nb = _setup_notebook()
    assert "include_knowledge_volume=True" in nb
    assert "gs.should_retry_without_volume(ex)" in nb
