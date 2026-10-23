"""Owner-notice generation for the convert-permission CUJ (read-only planning tier).

Pure — no network, no I/O. ``build_owner_notice`` drafts the owner-facing message the comms
workstream sends before a convert. It is a CONTRACTED artifact (the ``comms_feed.owner_notice``
column), not ad-hoc prose, so every owner gets a consistent WHAT / WHY / DURATION / RESTORE /
CONTACT message.

Ownership never changes: the convert identity needs ``OWNER`` or ``MANAGE`` plus ``SELECT`` and
``MODIFY`` (``migration.authority``), so the notice says which privileges may be granted and that
the ones the migration adds are revoked after cleanup.
"""

from __future__ import annotations

from typing import Optional

_DEFAULT_CONTACT = "the migration team"

_WHY = (
    "Databricks requires the converting identity to hold MANAGE (or ownership) plus SELECT and "
    "MODIFY on an external table before `SET MANAGED` — this is the platform's rule."
)

_RESTORE = (
    "Ownership does not change, downstream consumers are unaffected (their grants are unchanged), "
    "and any privilege the migration adds is revoked once the table is fully migrated (past the "
    "14-day rollback window)."
)


def build_owner_notice(
    *,
    full_name: str,
    prior_owner: Optional[str],
    planned_window: Optional[str] = None,
    owned_by_migration: bool = False,
    contact: Optional[str] = None,
) -> Optional[str]:
    """Build the owner-facing notice for one table, or ``None`` when the migration identity itself
    owns it (nothing to tell anyone). Pure: no I/O."""
    if owned_by_migration:
        return None
    owner_txt = (prior_owner or "").strip() or "the current owner"
    window = (planned_window or "").strip()
    who = (contact or "").strip() or _DEFAULT_CONTACT
    what = (
        f"`{full_name}` (owned by {owner_txt}) is being converted from an external to a managed "
        f"table. To run the conversion, the migration identity will be granted SELECT/MODIFY on "
        f"your table (and MANAGE where it lacks it); ownership does not change."
    )
    parts = [what, _WHY]
    if window:
        parts.append(f"Planned window: {window}.")
    parts.append(_RESTORE)
    parts.append(f"Questions: contact {who}.")
    return " ".join(parts)
