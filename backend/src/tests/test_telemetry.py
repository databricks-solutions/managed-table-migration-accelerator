"""Usage telemetry: closed vocabularies, no leaks, bounded and never-raising (no Spark, no SDK).

The guarantees under test:
  * NO LEAK — names, paths, identities, messages and free text can never reach a key or value,
    whatever a caller passes in.
  * CLOSED — every emitted pair is ``e2m.``-namespaced, SDK-User-Agent-safe, and drawn from a
    vocabulary owned by another module (or a band).
  * BEST-EFFORT — the send is bounded, sends at most once per run, and never raises.
"""

import re
import sys
import types

import pytest

from migration import actions as ac
from migration import audit as au
from migration import convert as cv
from migration import eligibility as el
from migration import external_writes as xw
from migration import gate_eval as ge
from migration import locations as loc
from migration import path_access as pa
from migration import reconcile as rc
from migration import telemetry as tm

UA_RE = re.compile(r"^[a-zA-Z0-9_.+-]+$")  # databricks.sdk.useragent.alphanum_pattern

ADVERSARIAL = [
    "main.sales.customers",
    "abfss://container@acct.dfs.core.windows.net/raw/customers",
    "s3://bucket/path/to/table",
    "jane.doe@example.com",
    "line1\nline2",
    "a/b",
    "café",
    "DROP TABLE x; --",
    "A" * 81,
    "[DELTA_ALTER_TABLE_SET_MANAGED_FAILED] table main.sales.customers failed",
]


def _all_strings(pairs):
    for k, v in pairs:
        yield k
        yield v


def _assert_clean(pairs):
    for k, v in pairs:
        assert k.startswith(tm.KEY_PREFIX), k
        assert UA_RE.match(k) and UA_RE.match(v), (k, v)
    blob = " ".join(_all_strings(pairs))
    for bad in ("main.sales", "customers", "abfss", "s3:", "@", "\n", "DROP", "café"):
        assert bad not in blob, bad


# ---------------------------------------------------------------------------------------------
# Bands
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("n,expected", [
    (0, "0"), (1, "1-9"), (9, "1-9"), (10, "10-99"), (99, "10-99"), (100, "100-999"),
    (999, "100-999"), (1000, "1k-9k"), (9999, "1k-9k"), (10_000, "10k+"), (10**9, "10k+"),
    (5.0, "1-9"),
])
def test_band_edges(n, expected):
    assert tm.band(n) == expected


@pytest.mark.parametrize("n", [None, -1, True, False, 3.5, "7", "x", object()])
def test_band_rejects_non_counts(n):
    assert tm.band(n) is None


TIB = 1024 ** 4


@pytest.mark.parametrize("b,expected", [
    (0, "lt1tb"), (TIB - 1, "lt1tb"), (TIB, "1-10tb"), (10 * TIB, "10-100tb"),
    (100 * TIB, "100tb-1pb"), (1024 * TIB, "1pb+"),
])
def test_tb_band_edges(b, expected):
    assert tm.tb_band(b) == expected


@pytest.mark.parametrize("b", [None, -1, True, float("nan"), "big"])
def test_tb_band_rejects(b):
    assert tm.tb_band(b) is None


# ---------------------------------------------------------------------------------------------
# Tokens, error classes, switch
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ADVERSARIAL)
def test_safe_token_rejects_adversarial_except_slash(bad):
    tok = tm.safe_token(bad)
    if bad == "a/b":
        assert tok == "a+b"
    else:
        assert tok is None or tok == bad and UA_RE.match(bad)
    assert tm.safe_token("main.sales.customers") == "main.sales.customers"  # shape-valid...
    # ...which is exactly why token-bearing keys also need a vocabulary (tested below).


def test_error_class_precedence_and_never_parses_message():
    class E(Exception):
        def getCondition(self):
            return "DELTA_STATE_RECOVER_ERRORS"

        def getErrorClass(self):
            return "OTHER_CLASS"

    assert tm.error_class(E("DELTA_TRUNCATED_TRANSACTION_LOG in main.s.t")) == "DELTA_STATE_RECOVER_ERRORS"

    class F(Exception):
        def getErrorClass(self):
            return "DELTA_ALTER_TABLE_SET_MANAGED_FAILED.FILE_VALIDATION_FAILED"

    assert tm.error_class(F()) == "DELTA_ALTER_TABLE_SET_MANAGED_FAILED.FILE_VALIDATION_FAILED"

    class G(Exception):
        error_code = "PERMISSION_DENIED"

    assert tm.error_class(G()) == "PERMISSION_DENIED"
    # Message text is never mined, even when it contains a perfect error class.
    assert tm.error_class(RuntimeError("[DELTA_STATE_RECOVER_ERRORS] boom")) is None


@pytest.mark.parametrize("raw", ["main.sales.t", "lower_case", "A" * 90, "X\nY", "", None, 42])
def test_error_class_token_rejects(raw):
    assert tm.error_class_token(raw) is None


def test_error_class_token_maps_combined_class():
    assert tm.error_class_token("DELTA_TRUNCATED_TRANSACTION_LOG/DELTA_TXN_LOG_FAILED_INTEGRITY") \
        == "DELTA_TRUNCATED_TRANSACTION_LOG+DELTA_TXN_LOG_FAILED_INTEGRITY"


def test_broken_error_accessor_is_no_class():
    class B(Exception):
        def getCondition(self):
            raise RuntimeError("nope")

    assert tm.error_class(B()) is None


def test_exception_type_is_identifier_only():
    assert tm.exception_type(RuntimeError()) == "RuntimeError"
    assert tm.exception_type(None) is None


@pytest.mark.parametrize("raw,expected", [
    (None, True), ("", True), ("true", True), ("TRUE", True), ("yes", True),
    ("false", False), (" False ", False), ("0", False), ("no", False), ("off", False),
])
def test_parse_enabled(raw, expected):
    assert tm.parse_enabled(raw) is expected


def test_nonce_shape():
    assert re.fullmatch(r"[0-9a-f]{8}", tm.new_nonce())


# ---------------------------------------------------------------------------------------------
# Vocabularies
# ---------------------------------------------------------------------------------------------


def test_blocker_registry_covers_every_owning_constant():
    owned = set(loc.DRY_RUN_OWNED_BLOCKERS)
    for module in (el, xw, rc, pa):
        owned |= {v for n, v in vars(module).items() if n.startswith("BLOCKER_") and isinstance(v, str)}
    owned.add(cv.BLOCKER_UNEXPECTED_TABLE_TYPE)
    assert owned <= tm.BLOCKER_TOKENS


def test_convert_error_classes_match_classifier():
    """Every class classify_convert_error can return is a registered blocker token."""
    import inspect

    src = inspect.getsource(cv.classify_convert_error)
    returned = set(re.findall(r'"error_class":\s*"([^"]+)"', src))
    assert returned and returned <= tm.CONVERT_ERROR_CLASSES <= tm.BLOCKER_TOKENS


def test_vocabularies_are_imported_not_copied():
    assert tm.ACTION_KINDS == frozenset(ac.ALL_KINDS)
    assert tm.GATE_KINDS == frozenset(ge.ALL_GATE_KINDS) | tm.LOCAL_GATES
    assert not (tm.LOCAL_GATES & frozenset(ge.ALL_GATE_KINDS))  # local names never shadow a kind
    assert au.STATE_CONVERTED in tm.STATES and au.STATE_BLOCKED in tm.STATES


def test_every_vocabulary_token_is_user_agent_safe():
    vocab = (tm.BLOCKER_TOKENS | tm.STATES | tm.AUTHORITY_STATUSES | tm.READER_STATES
             | tm.SCAN_STATUSES | tm.SCHEDULE_VERDICTS | tm.BUCKETS | tm.OP_KINDS | tm.GATE_KINDS
             | tm.ACTION_KINDS | frozenset(tm.STAGES) | frozenset(tm.COUNT_METRICS))
    for tok in vocab:
        assert tm.safe_token(tok), tok


# ---------------------------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------------------------


def test_stage_signals_happy_path():
    pairs = tm.stage_signals(
        mode="execute", outcome="failed", selector="wave",
        counts={"converted": 12, "failed": 1, "targets": 13},
        tokens={
            "err": {"DELTA_ALTER_TABLE_SET_MANAGED_FAILED.FILE_VALIDATION_FAILED": 1},
            "blk": {"recent_vacuum_activity": 2, "no_managed_location": 0},
            "chk": {"12.FAIL": 3, "14.WARN": 1},
        },
        extra={"bucket": "pilot", "streaming": False},
    )
    d = dict(pairs)
    assert d["e2m.mode"] == "execute" and d["e2m.outcome"] == "failed" and d["e2m.sel"] == "wave"
    assert d["e2m.n.converted"] == "10-99" and d["e2m.n.failed"] == "1-9"
    assert d["e2m.err.DELTA_ALTER_TABLE_SET_MANAGED_FAILED.FILE_VALIDATION_FAILED"] == "1-9"
    assert d["e2m.blk.recent_vacuum_activity"] == "1-9"
    assert "e2m.blk.no_managed_location" not in d  # zero counts are not sent for tokens
    assert d["e2m.chk.12.FAIL"] == "1-9" and d["e2m.chk.14.WARN"] == "1-9"
    assert d["e2m.bucket"] == "pilot" and d["e2m.f.streaming"] == "false"
    _assert_clean(pairs)


@pytest.mark.parametrize("bad", ADVERSARIAL)
def test_stage_signals_never_leak(bad):
    pairs = tm.stage_signals(
        mode=bad, outcome=bad, selector=bad,
        counts={bad: 5, "converted": bad},
        tokens={"blk": {bad: 3}, "err": {bad: 1}, "st": {bad: 2}, "chk": {bad: 1}, bad: {bad: 1}},
        extra={bad: bad, "bucket": bad, "streaming": bad},
    )
    _assert_clean(pairs)
    # Unknown tokens collapse into "other" for their category.
    keys = dict(pairs)
    assert keys.get("e2m.blk.other") == "1-9"
    assert keys.get("e2m.st.other") == "1-9"


def test_estate_signals_shape():
    pairs = tm.estate_signals(
        state_counts={"ELIGIBLE": 120, "BLOCKED": 30, "main.s.t": 1},
        total_bytes=12 * TIB,
        blocker_counts=tm.blocker_histogram([
            ("no_managed_location, recent_vacuum_activity", 20),
            ("no_managed_location", 5),
            ("some table main.sales.customers is odd", 1),
        ]),
        action_groups=[("FREEZE_COMPACTION", True, 4), ("FREEZE_COMPACTION", False, 2),
                       ("NOT_A_KIND", False, 1)],
        authority_counts={"NEEDS_GRANT": 7, "jane@example.com": 1},
        bucket_counts={"pilot": 15},
    )
    d = dict(pairs)
    assert d["e2m.est.tables"] == "100-999" and d["e2m.est.tb"] == "10-100tb"
    assert d["e2m.st.ELIGIBLE"] == "100-999" and d["e2m.st.other"] == "1-9"
    assert d["e2m.blk.no_managed_location"] == "10-99"
    assert d["e2m.blk.recent_vacuum_activity"] == "10-99"
    assert d["e2m.blk.other"] == "1-9"
    assert d["e2m.act.FREEZE_COMPACTION.auto"] == "1-9"
    assert d["e2m.act.FREEZE_COMPACTION.manual"] == "1-9"
    assert d["e2m.act.other.manual"] == "1-9"
    assert d["e2m.auth.NEEDS_GRANT"] == "1-9" and d["e2m.auth.other"] == "1-9"
    assert d["e2m.bkt.pilot"] == "10-99"
    _assert_clean(pairs)
    assert tm.vetted_pairs(pairs) == pairs  # everything the builder makes survives vetting


def test_blocker_histogram_counts_each_token_once_per_row():
    assert tm.blocker_histogram([("a, a, b", 3), (None, 9), ("", 2)]) == {"a": 3, "b": 3}


@pytest.mark.parametrize("bad", ADVERSARIAL)
def test_vetted_pairs_drop_foreign_shapes(bad):
    assert tm.vetted_pairs([
        ("e2m.est.tables", bad), (bad, "1-9"), ("e2m.blk." + bad, "1-9"), ("e2m.exc", bad),
        ("e2m.act.FREEZE_COMPACTION.auto", bad), ("e2m.err.X", "1-9"), ("e2m.n.converted", "1-9"),
        "not-a-pair", None,
    ]) == []


# ---------------------------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------------------------


def test_chunk_signals_bounds_and_repeats_header():
    header = tm.header_pairs("04_convert", "0a1b2c3d")
    body = [(f"e2m.blk.{t}", "1-9") for t in sorted(tm.BLOCKER_TOKENS)] * 3
    chunks = tm.chunk_signals(header, body, max_len=300, max_chunks=4)
    assert 1 < len(chunks) <= 4
    n = len(chunks)
    for i, chunk in enumerate(chunks, start=1):
        assert chunk[:2] == header
        assert (f"e2m.chunk", f"{i}of{n}") in chunk
        assert tm.ua_length(chunk) <= 300


def test_chunk_signals_requires_valid_header():
    assert tm.header_pairs("not_a_stage", "0a1b2c3d") == []
    assert tm.header_pairs("04_convert", "main.s.t") == []
    assert tm.chunk_signals([], [("e2m.mode", "execute")]) == []


def test_chunk_signals_header_only_run_still_sends_one():
    header = tm.header_pairs("05_validate", "deadbeef")
    assert tm.chunk_signals(header, []) == [header + [("e2m.chunk", "1of1")]]


# ---------------------------------------------------------------------------------------------
# Send / StageRun / start (fake SDK)
# ---------------------------------------------------------------------------------------------


class FakeConfig:
    def __init__(self, extras=None):
        self.extras = list(extras or [])
        self.retry_timeout_seconds = 300
        self.http_timeout_seconds = 60

    def copy(self):
        return FakeConfig(self.extras)

    def with_user_agent_extra(self, k, v):
        self.extras.append((k, v))
        return self


class FakeWS:
    def __init__(self, config=None):
        self.config = config or FakeConfig()


def _recorder():
    sent = []

    def ping(client):
        sent.append(client.config)

    return sent, ping


def test_send_stamps_pairs_and_tight_timeouts():
    sent, ping = _recorder()
    chunks = tm.chunk_signals(tm.header_pairs("00_profiler", "00000001"), [("e2m.mode", "execute")])
    assert tm.send(chunks, ws_factory=FakeWS, ping=ping, logger=lambda m: None) is True
    assert len(sent) == 1
    cfg = sent[0]
    assert cfg.retry_timeout_seconds == tm.PING_TIMEOUT_SECONDS
    assert cfg.http_timeout_seconds == tm.PING_TIMEOUT_SECONDS
    assert ("e2m.stage", "00_profiler") in cfg.extras and ("e2m.mode", "execute") in cfg.extras


def test_send_skips_unsafe_pairs_even_if_handed_in():
    sent, ping = _recorder()
    tm.send([[("e2m.stage", "00_profiler"), ("e2m.x", "a/b"), ("e2m.y", "line\nbreak")]],
            ws_factory=FakeWS, ping=ping, logger=lambda m: None)
    assert ("e2m.x", "a/b") not in sent[0].extras
    assert all("\n" not in v for _, v in sent[0].extras)


@pytest.mark.parametrize("where", ["factory", "copy", "ping"])
def test_send_never_raises(where):
    logs = []

    def factory():
        if where == "factory":
            raise RuntimeError("no creds")
        ws = FakeWS()
        if where == "copy":
            ws.config.copy = lambda: (_ for _ in ()).throw(RuntimeError("x"))
        return ws

    def ping(_):
        if where == "ping":
            raise TimeoutError("brownout")

    chunks = [tm.header_pairs("00_profiler", "00000001")]
    assert tm.send(chunks, ws_factory=factory, ping=ping, logger=logs.append) is False
    assert logs and "not sent" in logs[0]


def test_send_survives_a_raising_logger():
    def boom(_):
        raise RuntimeError("log broke")

    assert tm.send([[("e2m.stage", "00_profiler")]], ws_factory=lambda: 1 / 0, logger=boom) is False


def test_send_stops_on_first_failure_and_respects_deadline():
    calls = []

    def ping(_):
        calls.append(1)
        raise TimeoutError()

    chunks = [[("e2m.stage", "00_profiler")]] * 5
    tm.send(chunks, ws_factory=FakeWS, ping=ping, logger=lambda m: None)
    assert len(calls) == 1

    ticks = iter([0.0, 0.0, 11.0, 11.0])
    sent, ping2 = _recorder()
    assert tm.send(chunks, ws_factory=FakeWS, ping=ping2, logger=lambda m: None,
                   clock=lambda: next(ticks)) is False
    assert len(sent) == 1


def test_send_nothing_to_send():
    assert tm.send([], ws_factory=FakeWS) is False


def test_stage_run_finish_is_latched_and_disabled_sends_nothing():
    sent, ping = _recorder()
    run = tm.StageRun("05_validate", enabled=True, run="cafebabe", ws_factory=FakeWS, ping=ping,
                      logger=lambda m: None)
    assert run.finish("ok", counts={"validated": 3}) is None  # never echoes in a notebook cell
    assert run.delivered is True
    run.finish("ok")
    assert run.on_error(RuntimeError()) is False
    assert len(sent) == 1

    sent2, ping2 = _recorder()
    off = tm.StageRun("05_validate", enabled=False, ws_factory=FakeWS, ping=ping2)
    off.finish("ok")
    assert off.delivered is False and sent2 == []

    bogus = tm.StageRun("not_a_stage", enabled=True, ws_factory=FakeWS, ping=ping2)
    bogus.finish("ok")
    assert bogus.delivered is False and sent2 == []


def test_stage_run_on_error_reports_type_and_class_not_message():
    sent, ping = _recorder()
    run = tm.StageRun("04_convert", enabled=True, run="cafebabe", ws_factory=FakeWS, ping=ping,
                      logger=lambda m: None)
    run.trip(ge.GATE_COMPACTION_COVERAGE)
    run.trip("main.sales.customers")  # not a gate kind: ignored

    class Err(Exception):
        error_code = "PERMISSION_DENIED"

    assert run.on_error(Err("cannot read main.sales.customers")) is True
    extras = dict(sent[0].extras)
    assert extras["e2m.outcome"] == "error"
    assert extras["e2m.exc"] == "Err"
    assert extras["e2m.err.PERMISSION_DENIED"] == "1-9"
    assert extras[f"e2m.gate.{ge.GATE_COMPACTION_COVERAGE}"] == "1-9"
    _assert_clean([p for p in sent[0].extras if p[0].startswith("e2m.")])


class NotebookExit(Exception):
    """Stands in for the type dbutils.notebook.exit() raises inside post_run_cell (e2e probe)."""


@pytest.mark.parametrize("err,sends", [
    (ZeroDivisionError("division by zero"), True),   # uncaught stage failure -> outcome=error
    (NotebookExit("{}"), False),                      # dbutils.notebook.exit -> a normal end
    (SystemExit(0), False),
    (None, False),                                    # a cell that succeeded
])
def test_cell_hook_reports_errors_but_not_notebook_exit(err, sends):
    sent, ping = _recorder()
    run = tm.StageRun("05_validate", enabled=True, run="cafebabe", ws_factory=FakeWS, ping=ping,
                      logger=lambda m: None)
    assert tm.on_cell_result(run, types.SimpleNamespace(error_in_exec=err)) is sends
    assert len(sent) == (1 if sends else 0)
    if sends:
        assert dict(sent[0].extras)["e2m.exc"] == "ZeroDivisionError"


def test_cell_hook_never_raises_on_odd_results():
    run = tm.StageRun("05_validate", enabled=True, run="cafebabe", ws_factory=lambda: 1 / 0,
                      logger=lambda m: None)
    assert tm.on_cell_result(run, object()) is False
    assert tm.on_cell_result(run, types.SimpleNamespace(error_in_exec=RuntimeError())) is False


def test_finish_vets_ready_made_pairs():
    sent, ping = _recorder()
    run = tm.StageRun("00_profiler", enabled=True, run="cafebabe", ws_factory=FakeWS, ping=ping,
                      logger=lambda m: None)
    run.finish("ok", estate=[("e2m.est.tables", "10-99"), ("e2m.leak", "main.sales.customers")])
    keys = dict(sent[0].extras)
    assert keys["e2m.est.tables"] == "10-99" and "e2m.leak" not in keys


class FakeWidgets:
    def __init__(self, value=None, get_raises=False, text_raises=False):
        self.value, self.get_raises, self.text_raises = value, get_raises, text_raises
        self.declared = []

    def text(self, name, default, label):
        if self.text_raises:
            raise RuntimeError("declared")
        self.declared.append((name, default))
        if self.value is None:
            self.value = default

    def get(self, name):
        if self.get_raises:
            raise RuntimeError("no widget")
        return self.value


def _dbutils(**kw):
    return types.SimpleNamespace(widgets=FakeWidgets(**kw))


@pytest.mark.parametrize("dbu,expected", [
    (_dbutils(), True),                               # default declared "true"
    (_dbutils(value="false"), False),
    (_dbutils(value="off"), False),
    (_dbutils(value="true", text_raises=True), True),
    (_dbutils(get_raises=True), False),               # unreadable -> send nothing
    (None, False),
])
def test_read_enabled(dbu, expected, monkeypatch):
    monkeypatch.delenv(tm.ENV_SWITCH, raising=False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    assert tm.read_enabled(dbu) is expected


def test_env_switch_overrides_widget(monkeypatch):
    monkeypatch.setenv(tm.ENV_SWITCH, "false")
    assert tm.read_enabled(_dbutils(value="true")) is False


@pytest.mark.parametrize("dnt,expected", [("1", False), ("true", False), ("0", True), ("", True)])
def test_do_not_track_disables(dnt, expected, monkeypatch):
    monkeypatch.delenv(tm.ENV_SWITCH, raising=False)
    monkeypatch.setenv("DO_NOT_TRACK", dnt)
    assert tm.read_enabled(_dbutils(value="true")) is expected


def test_start_never_raises_and_activate_uses_sdk_useragent(monkeypatch):
    monkeypatch.delenv(tm.ENV_SWITCH, raising=False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    calls = []
    ua = types.ModuleType("databricks.sdk.useragent")
    ua.with_extra = lambda k, v: calls.append((k, v))
    sdk = types.ModuleType("databricks.sdk")
    sdk.useragent = ua
    monkeypatch.setitem(sys.modules, "databricks", types.ModuleType("databricks"))
    monkeypatch.setitem(sys.modules, "databricks.sdk", sdk)
    monkeypatch.setitem(sys.modules, "databricks.sdk.useragent", ua)
    monkeypatch.setattr(tm, "_ACTIVATED", False)

    run = tm.start("03_dry_run", _dbutils(), register_hook=False)
    assert run.enabled and calls == [(tm.PRODUCT, tm.VERSION)]

    calls.clear()
    off = tm.start("03_dry_run", _dbutils(value="false"), register_hook=False)
    assert not off.enabled and calls == []

    class Exploding:
        @property
        def widgets(self):
            raise RuntimeError("boom")

    assert tm.start("03_dry_run", Exploding(), register_hook=False).enabled is False


def test_activate_without_sdk_is_a_quiet_noop(monkeypatch):
    monkeypatch.setitem(sys.modules, "databricks.sdk", None)
    assert tm.activate(True) is False
    assert tm.activate(False) is False


def test_tally_and_size_never_raise():
    class Row:
        def __init__(self, **kw):
            self._kw = kw

        def __getitem__(self, k):
            return self._kw[k]

    rows = [{"state": "A"}, {"state": "A"}, {"state": None}, Row(state="B"), Row(other=1), 7,
            {"state": ["unhashable"]}]
    assert tm.tally(rows, "state") == {"A": 2, "B": 1}
    assert tm.tally(rows, fn=lambda r: r["state"] + "!") == {"A!": 2, "B!": 1}
    assert tm.tally(None, "x") == {} and tm.tally(42, "x") == {}

    def exploding():
        yield {"k": "a"}
        raise RuntimeError("iterator broke")

    assert tm.tally(exploding(), "k") == {"a": 1}
    assert tm.size([1, 2]) == 2 and tm.size(None) is None and tm.size(iter([])) is None
    assert tm.blocker_histogram(exploding()) == {}  # malformed rows / iterator never raise


def test_estate_method_reads_nothing_when_disabled_and_never_raises():
    class Spark:
        def __init__(self):
            self.calls = 0

        def sql(self, _q):
            self.calls += 1
            raise RuntimeError("TABLE_OR_VIEW_NOT_FOUND")

    off = tm.StageRun("00_profiler", enabled=False)
    sp = Spark()
    assert off.estate(sp, "main", "migration") == [] and sp.calls == 0

    on = tm.StageRun("00_profiler", enabled=True, logger=lambda m: None)
    assert on.estate(sp, "main", "migration") == [] and sp.calls == 4


def test_product_marker_matches_module():
    import uc_ext2managed

    assert tm.PRODUCT == uc_ext2managed.PRODUCT == "uc-ext2managed"
    assert UA_RE.match(uc_ext2managed.__version__)
