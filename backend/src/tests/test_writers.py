from migration import writers as wr


def test_classify_scheduled_vs_event_job():
    assert wr.classify_writer("JOB", trigger_type="CRON") == wr.WRITER_SCHEDULED_JOB
    assert wr.classify_writer("JOB", trigger_type="FILE_ARRIVAL") == wr.WRITER_EVENT_JOB
    assert wr.classify_writer("JOB", trigger_type="CONTINUOUS") == wr.WRITER_EVENT_JOB
    # No trigger info -> default SCHEDULED_JOB (predictability is decided later, flagged low-confidence).
    assert wr.classify_writer("JOB") == wr.WRITER_SCHEDULED_JOB
    # A continuous pipeline writer is unpredictable -> EVENT_JOB.
    assert wr.classify_writer("LAKEFLOW_PIPELINE") == wr.WRITER_EVENT_JOB


def test_classify_adhoc_and_external():
    assert wr.classify_writer("NOTEBOOK") == wr.WRITER_NOTEBOOK
    assert wr.classify_writer("DBSQL_QUERY") == wr.WRITER_USER
    assert wr.classify_writer("TRINO") == wr.WRITER_EXTERNAL
    assert wr.classify_writer("EXTERNAL") == wr.WRITER_EXTERNAL
    # Honest: an entity type we can't place is None, never silently "external".
    assert wr.classify_writer("") is None


def test_writer_profile_degrades_on_empty():
    prof = wr.writer_profile([])
    assert prof["writer_classes"] is None
    assert prof["writer_job_ids"] == []
    assert prof["has_adhoc_writer"] is None
    assert wr.NOTE_WRITER_ATTRIBUTION_UNAVAILABLE in (prof["notes"] or "")


def test_writer_profile_has_adhoc_is_three_valued():
    # positive ad-hoc evidence is definite True even if a row was unclassifiable
    prof = wr.writer_profile([{"entity_type": "NOTEBOOK", "entity_id": "nb"}, {"entity_type": ""}])
    assert prof["has_adhoc_writer"] is True
    # all rows classified, none ad-hoc -> definite False, no note
    prof2 = wr.writer_profile([{"entity_type": "JOB", "entity_id": "1", "trigger_type": "CRON"}], trigger_known=True)
    assert prof2["has_adhoc_writer"] is False
    assert prof2["notes"] is None
    # some rows unclassifiable, none of the classified are ad-hoc -> None (cannot rule out)
    prof3 = wr.writer_profile([{"entity_type": "JOB", "entity_id": "1", "trigger_type": "CRON"}, {"entity_type": ""}], trigger_known=True)
    assert prof3["has_adhoc_writer"] is None
    assert wr.NOTE_WRITER_ATTRIBUTION_UNAVAILABLE in (prof3["notes"] or "")


def test_writer_profile_flags_adhoc_and_collects_job_ids():
    rows = [
        {"entity_type": "JOB", "entity_id": 111, "trigger_type": "CRON"},
        {"entity_type": "NOTEBOOK", "entity_id": "nb-1"},
    ]
    prof = wr.writer_profile(rows, trigger_known=True)
    assert wr.WRITER_SCHEDULED_JOB in prof["writer_classes"]
    assert prof["has_adhoc_writer"] is True
    assert prof["writer_job_ids"] == ["111"]  # entity_id coerced to str; only JOB/EVENT ids collected
    assert prof["notes"] is None  # every row classified -> no degrade note


def test_writer_source_rows_cover_all_columns():
    classified = wr.classify_writers(
        [{"entity_type": "JOB", "entity_id": "111", "trigger_type": "CRON"}], trigger_known=True)
    rows = wr.writer_source_rows("c.s.t", "c", "s", classified, scanned_at=None)
    assert len(rows) == 1
    assert set(rows[0].keys()) == set(wr.WRITER_SOURCES_COLUMN_NAMES)
    assert rows[0]["full_name"] == "c.s.t"
    assert rows[0]["writer_class"] == wr.WRITER_SCHEDULED_JOB
    assert rows[0]["confidence"] == "high"


def test_writer_sources_column_names_match_tuple():
    assert wr.WRITER_SOURCES_COLUMN_NAMES == tuple(c for c, _ in wr.WRITER_SOURCES_COLUMNS)


def test_writer_sources_has_next_scheduled_run_at_column():
    names = wr.WRITER_SOURCES_COLUMN_NAMES
    assert "next_scheduled_run_at" in names
    typ = dict(wr.WRITER_SOURCES_COLUMNS)["next_scheduled_run_at"]
    assert typ == "TIMESTAMP"


def test_build_manifest_writer_update_serializes_classes():
    prof = wr.writer_profile(
        [{"entity_type": "JOB", "entity_id": "9", "trigger_type": "CRON"},
         {"entity_type": "NOTEBOOK", "entity_id": "nb"}], trigger_known=True)
    upd = wr.build_manifest_writer_update("c.s.t", prof, scanned_at=None)
    assert upd["full_name"] == "c.s.t"
    assert upd["has_adhoc_writer"] is True
    # CSV, sorted, deterministic.
    assert upd["writer_classes"] == "NOTEBOOK, SCHEDULED_JOB"
    assert set(upd.keys()) == {"full_name", *wr.MANIFEST_WRITER_COLUMNS}


def test_build_manifest_writer_update_passes_none_through():
    # A degraded/None has_adhoc_writer must survive as None (SR7), not collapse to False.
    upd = wr.build_manifest_writer_update(
        "c.s.t", {"writer_classes": set(), "has_adhoc_writer": None, "notes": None}, scanned_at=None)
    assert upd["has_adhoc_writer"] is None


def test_compaction_job_actor_ids_extracts_job_actors():
    rows = [
        {"actor_kind": "JOB", "actor_id": "111"},
        {"actor_kind": "PREDICTIVE_OPTIMIZATION", "actor_id": None},
        {"actor_kind": "NOTEBOOK", "actor_id": "n1"},
        {"actor_kind": "job", "actor_id": 222},  # lowercase kind + int id -> normalized
    ]
    assert wr.compaction_job_actor_ids(rows) == {"111", "222"}


def test_compaction_job_actor_ids_empty_is_empty_set():
    assert wr.compaction_job_actor_ids(None) == set()
    assert wr.compaction_job_actor_ids([]) == set()


def test_exclude_compaction_actor_writers_drops_matching_job_only():
    writers = [
        {"entity_type": "JOB", "entity_id": "111"},        # OPTIMIZE maintenance (compaction actor)
        {"entity_type": "JOB", "entity_id": "999"},        # genuine data-writer job -> kept
        {"entity_type": "NOTEBOOK", "entity_id": "111"},   # notebook, same id -> kept (not a JOB)
    ]
    kept = wr.exclude_compaction_actor_writers(writers, {"111"})
    assert kept == [
        {"entity_type": "JOB", "entity_id": "999"},
        {"entity_type": "NOTEBOOK", "entity_id": "111"},
    ]


def test_exclude_compaction_actor_writers_empty_ids_is_passthrough():
    writers = [{"entity_type": "JOB", "entity_id": "111"}]
    assert wr.exclude_compaction_actor_writers(writers, set()) == writers
    assert wr.exclude_compaction_actor_writers(writers, None) == writers


def test_exclude_compaction_actor_writers_preserves_classified_row_shape():
    # 03b passes ALREADY-CLASSIFIED rows (extra keys); a SURVIVING row must keep them verbatim,
    # and an excluded one must still be dropped. WORKFLOW is a JOB entity type too.
    writers = [
        {"entity_type": "JOB", "entity_id": "111", "writer_class": "SCHEDULED_JOB", "confidence": "low"},
        {"entity_type": "WORKFLOW", "entity_id": "999", "writer_class": "SCHEDULED_JOB", "confidence": "high"},
    ]
    kept = wr.exclude_compaction_actor_writers(writers, {"111"})
    assert kept == [
        {"entity_type": "WORKFLOW", "entity_id": "999", "writer_class": "SCHEDULED_JOB", "confidence": "high"},
    ]


def test_profiler_excludes_compaction_actor_writers():
    # Source guard for the 05b cross-bracket fix: 00_profiler must filter compaction JOB actors
    # OUT of writer attribution before classifying/persisting writer_sources — otherwise a
    # scheduled OPTIMIZE/VACUUM maintenance job is double-bracketed (compaction retire vs writer
    # forward-restart) and 05b fails "expected PAUSED (retired=True) but schedule is 'UNPAUSED'".
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[2] / "notebooks" / "00_profiler.py").read_text()
    assert "exclude_compaction_actor_writers" in src, (
        "00_profiler must call wr.exclude_compaction_actor_writers before classifying writers"
    )
    assert "compaction_job_actor_ids" in src, (
        "00_profiler must derive compaction JOB actor ids to feed the exclusion"
    )
    # Order guard: the exclusion must run BEFORE classification/rollup (which feed the persisted
    # writer_sources) — otherwise a reorder that filters AFTER persist silently reintroduces the bug.
    assert src.index("exclude_compaction_actor_writers") < src.index("wr.classify_writers("), (
        "00_profiler must exclude compaction actors BEFORE wr.classify_writers(...)"
    )
    assert src.index("exclude_compaction_actor_writers") < src.index("wr.writer_profile("), (
        "00_profiler must exclude compaction actors BEFORE wr.writer_profile(...)"
    )


def test_03b_excludes_compaction_actor_writers():
    # Source guard: 03b_compaction_scan reads writer_sources and REGENERATES actions (its
    # obsolete-detection also deletes any not re-emitted). It re-scans compaction fresh, so it must
    # re-apply the compaction-actor exclusion to writer_by_table before generate_actions — otherwise
    # a maintenance job re-enters the writer-stop bracket even after the profiler filtered it out.
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[2] / "notebooks"
           / "03b_compaction_scan.py").read_text()
    assert "exclude_compaction_actor_writers" in src, (
        "03b must call wr.exclude_compaction_actor_writers on writer_by_table before generate_actions"
    )
    # Must be applied BEFORE the generate_actions call (order guard).
    assert src.index("exclude_compaction_actor_writers") < src.index("ax.generate_actions("), (
        "the exclusion must run before ax.generate_actions(...) in 03b"
    )


def test_compaction_job_actor_ids_include_vacuum_jobs():
    rows = [{"actor_kind": "JOB", "actor_id": "vac-9", "operation_class": "VACUUM"}]
    assert wr.compaction_job_actor_ids(rows) == {"vac-9"}
