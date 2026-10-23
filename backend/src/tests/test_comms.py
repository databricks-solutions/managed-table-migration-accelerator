"""Unit tests for owner-notice generation (comms.py). No Spark required."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import comms as cm  # noqa: E402


def _notice(**kw):
    base = dict(full_name="c.s.orders", prior_owner="alice@x.com", planned_window="off-peak hours")
    base.update(kw)
    return cm.build_owner_notice(**base)


def test_migration_owned_table_gets_no_notice():
    assert _notice(owned_by_migration=True) is None


def test_notice_says_grants_and_that_ownership_does_not_change():
    msg = _notice()
    assert "c.s.orders" in msg                       # WHAT (the table)
    assert "alice@x.com" in msg                      # the current owner
    assert "granted SELECT/MODIFY on your table" in msg
    assert "ownership does not change" in msg.lower()
    assert "SET MANAGED" in msg                      # WHY (platform rule)
    assert "revoked" in msg                          # RESTORE: added privileges are revoked
    assert "off-peak hours" in msg                   # DURATION / when (planned_window)
    for stale in ("lease", "OWNER TO", "transfer"):
        assert stale.lower() not in msg.lower(), stale


def test_notice_without_window_omits_window_phrase():
    assert "Planned window" not in _notice(planned_window=None)


def test_notice_default_and_explicit_contact():
    assert "the migration team" in _notice()
    assert "data-platform" in _notice(contact="data-platform")


def test_notice_unknown_prior_owner_reads_gracefully():
    assert "the current owner" in _notice(prior_owner=None)
