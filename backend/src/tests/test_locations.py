"""Unit tests for locations pure logic (managed-location gate). No Spark required."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import locations as loc  # noqa: E402


# --------------------------------------------------------------------------- #
# has_managed_location (precedence schema > catalog > metastore)
# --------------------------------------------------------------------------- #

def test_has_managed_location_schema_wins():
    ok, scope, path = loc.has_managed_location("s3://schema", "s3://catalog", "s3://meta")
    assert ok is True
    assert scope == "SCHEMA"
    assert path == "s3://schema"


def test_has_managed_location_falls_back_to_catalog():
    ok, scope, path = loc.has_managed_location(None, "s3://catalog", None)
    assert ok is True
    assert scope == "CATALOG"


def test_has_managed_location_falls_back_to_metastore():
    ok, scope, _ = loc.has_managed_location("", "   ", "s3://meta")
    assert ok is True
    assert scope == "METASTORE"


def test_has_managed_location_none_configured():
    ok, scope, path = loc.has_managed_location(None, "", "   ")
    assert ok is False
    assert scope is None
    assert path is None


# --------------------------------------------------------------------------- #
# managed_location_from_desc
# --------------------------------------------------------------------------- #

def test_managed_location_from_desc_storage_root():
    assert loc.managed_location_from_desc({"Storage Root": "s3://x"}) == "s3://x"


def test_managed_location_from_desc_location_alias():
    assert loc.managed_location_from_desc({"Location": "abfss://y"}) == "abfss://y"


def test_managed_location_from_desc_absent():
    assert loc.managed_location_from_desc({"Owner": "me"}) is None


# --------------------------------------------------------------------------- #
# effective_managed_location (CONFIGURED / NOT_CONFIGURED / UNRESOLVED)
# --------------------------------------------------------------------------- #

def _ok(locv=None):
    return {"managed_location": locv, "status": loc.STATUS_OK}


def _err():
    return {"managed_location": None, "status": loc.STATUS_ERROR}


def test_effective_schema_configured_wins():
    eff = loc.effective_managed_location(_ok("s3://schema"), _ok(), _ok())
    assert eff["status"] == loc.ML_CONFIGURED
    assert eff["source_scope"] == "SCHEMA"
    assert eff["managed_location"] == "s3://schema"


def test_effective_catalog_fallback():
    eff = loc.effective_managed_location(_ok(None), _ok("abfss://cat"), _ok())
    assert eff["status"] == loc.ML_CONFIGURED
    assert eff["source_scope"] == "CATALOG"


def test_effective_metastore_fallback():
    eff = loc.effective_managed_location(_ok(None), _ok(None), _ok("gs://meta"))
    assert eff["status"] == loc.ML_CONFIGURED
    assert eff["source_scope"] == "METASTORE"


def test_effective_all_resolved_absent_is_not_configured():
    eff = loc.effective_managed_location(_ok(None), _ok(None), _ok(None))
    assert eff["status"] == loc.ML_NOT_CONFIGURED
    assert eff["managed_location"] is None


def test_effective_lower_scope_error_is_unresolved():
    # Schema resolved-absent, but catalog lookup errored -> cannot prove absence.
    eff = loc.effective_managed_location(_ok(None), _err(), _ok(None))
    assert eff["status"] == loc.ML_UNRESOLVED


def test_effective_none_row_is_unresolved_when_no_location():
    eff = loc.effective_managed_location(_ok(None), _ok(None), None)
    assert eff["status"] == loc.ML_UNRESOLVED


def test_effective_location_found_despite_higher_scope_error():
    # Schema errored (absence unknown) but catalog HAS a location -> still CONFIGURED (safe).
    eff = loc.effective_managed_location(_err(), _ok("s3://cat"), _ok(None))
    assert eff["status"] == loc.ML_CONFIGURED
    assert eff["source_scope"] == "CATALOG"


# --------------------------------------------------------------------------- #
# replace_owned_blockers
# --------------------------------------------------------------------------- #

def test_replace_owned_drops_repaired_and_keeps_foreign():
    # existing has an owned (repaired) blocker + a foreign one; active is now empty.
    out = loc.replace_owned_blockers(
        "no_managed_location, external_writer_present",
        loc.DRY_RUN_OWNED_BLOCKERS, [])
    assert out == "external_writer_present"


def test_replace_owned_swaps_owned_blocker():
    out = loc.replace_owned_blockers(
        "no_managed_location, external_writer_present",
        loc.DRY_RUN_OWNED_BLOCKERS, ["managed_location_unresolved"])
    assert "no_managed_location" not in out
    assert "external_writer_present" in out
    assert "managed_location_unresolved" in out


def test_replace_owned_none_when_empty():
    assert loc.replace_owned_blockers("no_managed_location", loc.DRY_RUN_OWNED_BLOCKERS, []) is None


def test_replace_owned_dedupes_active():
    out = loc.replace_owned_blockers(None, loc.DRY_RUN_OWNED_BLOCKERS,
                                     ["corrupted_or_inaccessible", "corrupted_or_inaccessible"])
    assert out == "corrupted_or_inaccessible"
