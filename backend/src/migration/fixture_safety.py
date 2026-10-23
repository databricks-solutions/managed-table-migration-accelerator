"""Fail-closed safety guard for the synthetic E2E fixture estate."""

DEV_FIXTURE_CONTROL_PLANES = frozenset({
    ("mtm_e2e_dev", "migration"),
})


def parse_control_plane(value: str | None) -> tuple[str, str] | None:
    """``catalog.schema`` -> ``(catalog, schema)``; blank -> ``None``.

    Anything that is not exactly two non-blank dot-separated parts raises, so a typo in the
    deploy-time allowlist entry can never silently widen (or vanish from) the allowlist.
    """
    text = (value or "").strip()
    if not text:
        return None
    parts = [p.strip() for p in text.split(".")]
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"fixture control plane must be 'catalog.schema', got {value!r}")
    return parts[0], parts[1]


def allowed_control_planes(extra: str | None = None) -> frozenset:
    """The built-in allowlist plus the one optional deploy-time ``catalog.schema`` entry."""
    pair = parse_control_plane(extra)
    return DEV_FIXTURE_CONTROL_PLANES | ({pair} if pair else set())


def require_fixture_dev_control_plane(catalog: str, schema: str, extra: str | None = None) -> None:
    """Raise unless the resolved control plane is explicitly allowed for fixtures."""
    allowed = allowed_control_planes(extra)
    resolved_control_plane = (catalog, schema)
    if resolved_control_plane in allowed:
        return

    allowed_text = ", ".join(
        f"{allowed_catalog}.{allowed_schema}"
        for allowed_catalog, allowed_schema in sorted(allowed)
    )
    raise RuntimeError(
        "refusing to seed synthetic fixture estate: resolved control plane "
        f"{catalog}.{schema} is not in the dev allowlist [{allowed_text}]; "
        f"resolved catalog={catalog}"
    )
