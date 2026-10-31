"""本站模拟引擎的增量落盘（2026-10-12）：每个事务只写当前状态行 + 新增成交行，
整本快照降为检查点；恢复、迁移、回滚与读快照的合同。"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime

import pytest

from test_paper_book import (
    ACCOUNT,
    TRADING_DATES,
    as_legacy_db,
    au_quote,
    build_engine,
    feed,
    submit_request,
)
from tqkq_readonly_sidecar.paper import (
    PaperEngine,
    PaperMigrationError,
    VirtualClock,
    check_db,
    checkpoint_db,
    checkpoint_lag,
)
from tqkq_readonly_sidecar.paper_book import PaperBook

START = "2026-11-02T09:00:00+08:00"


def _clock(moment: str = START) -> VirtualClock:
    return VirtualClock(datetime.fromisoformat(moment))


def _round_trips(engine: PaperEngine, count: int, prefix: str, last: float = 820.0) -> None:
    """count 次对价开平今（行情最新价 last），每次 2 笔成交。"""
    for index in range(count):
        opened = engine.submit(ACCOUNT, submit_request(f"{prefix}-o{index}", limitPrice=round(last + 0.02, 2)))
        closed = engine.submit(ACCOUNT, submit_request(
            f"{prefix}-c{index}", direction="SELL", offset="CLOSETODAY", limitPrice=round(last - 0.02, 2)))
        assert opened["order"]["filledVolume"] == closed["order"]["filledVolume"] == 1


def _rows(db, sql: str, *params):
    connection = sqlite3.connect(db)
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def _export(engine: PaperEngine) -> str:
    return json.dumps(engine._accounts[ACCOUNT].book.export_state(), sort_keys=True)


def _ledger(engine: PaperEngine) -> tuple:
    activity = engine.activity(ACCOUNT)
    # 重启后第一笔行情到之前行情源状态是 unavailable，不属于账本。
    account = {key: value for key, value in engine.account(ACCOUNT).items()
               if key not in ("capturedAt", "dataStatus")}
    return (account, engine.positions(ACCOUNT), activity["orders"], activity["trades"])


def _crash(engine: PaperEngine) -> None:
    """模拟进程被杀：actor 停下，不走 close（不合并检查点），直接放掉连接与文件锁。"""
    engine._stop.set()
    if engine._thread is not None:
        engine._thread.join(timeout=5)
    assert engine._db is not None and engine._lock_fd is not None
    engine._db.close()
    os.close(engine._lock_fd)
    engine._db, engine._lock_fd = None, None


def test_each_transaction_writes_only_the_increment(tmp_path):
    db = tmp_path / "paper.sqlite3"
    engine = build_engine(db, clock=_clock())
    try:
        feed(engine, au_quote("09:31:00.000000", 820.0))
        _round_trips(engine, 1, "early")
        early = len(_rows(db, "SELECT head_json FROM paper_account_heads")[0][0])
        _round_trips(engine, 5, "late")
        late = len(_rows(db, "SELECT head_json FROM paper_account_heads")[0][0])
        # 当前状态行不随当日成交笔数增长（成交不在里面）；成交逐行追加、序号连续。
        assert abs(late - early) < 200
        seqs = [row[0] for row in _rows(db, "SELECT seq FROM paper_trades ORDER BY seq")]
        assert seqs == list(range(12))
        assert len(engine._accounts[ACCOUNT].book._sim._trades) == 12
        # 盘中命令不重写整本快照：检查点仍是开户时的版本 0。
        assert _rows(db, "SELECT revision, checkpoint_of FROM paper_accounts") == [(0, 0)]
        assert engine.account_info(ACCOUNT)["revision"] == 12
        assert _rows(db, "SELECT revision FROM paper_account_heads") == [(12,)]
    finally:
        engine.close()


def test_crash_after_checkpoint_replays_the_increments(tmp_path):
    """日结写检查点，之后的成交只在当前状态行与成交行里；进程被杀（不合并检查点）
    后重启，账本 = 当前状态行 + 检查点之后的成交行，与被杀前逐项一致。"""
    db = tmp_path / "paper.sqlite3"
    clock = _clock()
    engine = build_engine(db, clock=clock)
    feed(engine, au_quote("09:31:00.000000", 820.0))
    _round_trips(engine, 3, "day1")
    engine.submit(ACCOUNT, submit_request("day1-hold", volume=2))
    feed(engine, au_quote("15:10:00.000000", 821.0, settlement=821.0))
    feed(engine, au_quote("2026-11-03 09:31:00.000000", 822.0, pre_settlement=821.0))
    assert engine.settlement_count(ACCOUNT) == 1
    checkpoint = _rows(db, "SELECT revision, checkpoint_of, state_json FROM paper_accounts")[0]
    assert checkpoint[0] == checkpoint[1] == engine._accounts[ACCOUNT].revision
    assert json.loads(checkpoint[2])["_trades"] == []  # 日结后当日成交清零，检查点最小
    # 前一日的成交行随日结删除（已进 paper_trade_log）。
    assert _rows(db, "SELECT count(*) FROM paper_trades") == [(0,)]
    _round_trips(engine, 4, "day2", last=822.0)
    engine.submit(ACCOUNT, submit_request("day2-rest", limitPrice=815.0))
    before, ledger = _export(engine), _ledger(engine)
    head_revision = engine._accounts[ACCOUNT].revision
    _crash(engine)
    assert _rows(db, "SELECT revision FROM paper_accounts") == [(checkpoint[0],)]
    assert head_revision > checkpoint[0]
    restarted = PaperEngine(db_path=str(db), clock=clock, trading_dates=TRADING_DATES)
    restarted.open()
    restarted.start()
    try:
        assert _export(restarted) == before
        assert _ledger(restarted) == ledger
        assert restarted.account_info(ACCOUNT)["revision"] == head_revision
        assert restarted.query_order(ACCOUNT, "day2-rest")["order"]["status"] == "ALIVE"
        # 重放同一命令仍幂等：不再撮合。
        again = restarted.submit(ACCOUNT, submit_request("day2-o0", limitPrice=822.02))
        assert again["order"]["filledVolume"] == 1
        assert _ledger(restarted) == ledger
    finally:
        restarted.close()
    # 正常停机把检查点合并到最新版本，旧版本按整本快照恢复即同一本账。
    (revision, checkpoint_of, state_json), = _rows(
        db, "SELECT revision, checkpoint_of, state_json FROM paper_accounts")
    assert revision == checkpoint_of == head_revision
    legacy = PaperBook(account_id=ACCOUNT, init_balance=1_000_000.0, clock=clock,
                       trading_dates=TRADING_DATES)
    legacy.restore_state(json.loads(state_json))
    assert json.dumps(legacy.export_state(), sort_keys=True) == before


def test_legacy_snapshot_migrates_once_and_keeps_the_snapshot_column(tmp_path):
    db = tmp_path / "paper.sqlite3"
    clock = _clock()
    engine = build_engine(db, clock=clock)
    feed(engine, au_quote("09:31:00.000000", 820.0))
    _round_trips(engine, 5, "legacy")
    engine.submit(ACCOUNT, submit_request("legacy-rest", limitPrice=815.0))
    before, ledger = _export(engine), _ledger(engine)
    engine.close()
    as_legacy_db(db)
    snapshot = _rows(db, "SELECT revision, state_json FROM paper_accounts")
    migrated = PaperEngine(db_path=str(db), clock=clock, trading_dates=TRADING_DATES)
    migrated.open()
    migrated.start()
    try:
        assert _export(migrated) == before
        assert _ledger(migrated) == ledger
        assert _rows(db, "SELECT count(*) FROM paper_trades") == [(10,)]
        assert _rows(db, "SELECT revision, trades_from_seq FROM paper_account_heads") == [
            (snapshot[0][0], 0)]
    finally:
        migrated.close()
    # 迁移只登记 checkpoint_of，不改写快照列；再启动没有变化（幂等）。
    assert _rows(db, "SELECT revision, state_json FROM paper_accounts") == snapshot
    heads = _rows(db, "SELECT * FROM paper_account_heads")
    again = PaperEngine(db_path=str(db), clock=clock, trading_dates=TRADING_DATES)
    again.open()
    try:
        assert _export(again) == before
    finally:
        again.close()
    assert _rows(db, "SELECT * FROM paper_account_heads") == heads
    assert _rows(db, "SELECT revision, state_json FROM paper_accounts") == snapshot


def test_migration_failure_fails_closed_and_leaves_the_snapshot(tmp_path):
    db = tmp_path / "paper.sqlite3"
    clock = _clock()
    engine = build_engine(db, clock=clock)
    feed(engine, au_quote("09:31:00.000000", 820.0))
    _round_trips(engine, 2, "bad")
    engine.close()
    as_legacy_db(db)
    with sqlite3.connect(db) as connection:
        good = connection.execute("SELECT state_json FROM paper_accounts").fetchone()[0]
        broken = json.loads(good)
        del broken["_trades"]
        connection.execute("UPDATE paper_accounts SET state_json=?", (json.dumps(broken),))
    snapshot = _rows(db, "SELECT revision, state_json FROM paper_accounts")
    failing = PaperEngine(db_path=str(db), clock=clock, trading_dates=TRADING_DATES)
    with pytest.raises(PaperMigrationError, match=f"account {ACCOUNT}.*_trades"):
        failing.open()
    # 整体回滚：没有当前状态行、没有成交行，快照列原样；文件锁已释放。
    assert _rows(db, "SELECT count(*) FROM paper_account_heads") == [(0,)]
    assert _rows(db, "SELECT count(*) FROM paper_trades") == [(0,)]
    assert _rows(db, "SELECT revision, state_json FROM paper_accounts") == snapshot
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE paper_accounts SET state_json=?", (good,))
    fixed = PaperEngine(db_path=str(db), clock=clock, trading_dates=TRADING_DATES)
    fixed.open()
    try:
        assert len(fixed._accounts[ACCOUNT].book._sim._trades) == 4
    finally:
        fixed.close()


def test_old_version_writes_after_rollback_win_on_roll_forward(tmp_path):
    """回滚后旧版本又写过这个账户（revision+1、不认识 checkpoint_of）：再升级时以
    它写的整本快照为准，不拿停机前的当前状态行覆盖。"""
    db = tmp_path / "paper.sqlite3"
    clock = _clock()
    engine = build_engine(db, clock=clock)
    feed(engine, au_quote("09:31:00.000000", 820.0))
    engine.submit(ACCOUNT, submit_request("rollback-open", volume=2))
    engine.close()
    with sqlite3.connect(db) as connection:
        state = json.loads(connection.execute("SELECT state_json FROM paper_accounts").fetchone()[0])
        # 旧版本在回滚期间多开了 1 手：它的落盘只改 state_json 与 revision。
        old = PaperBook(account_id=ACCOUNT, init_balance=1_000_000.0, clock=clock,
                        trading_dates=TRADING_DATES)
        old.restore_state(state)
        old.inject_quote(au_quote("09:41:00.000000", 820.0))
        old.submit(submit_request("rollback-old-version", limitPrice=820.02))
        connection.execute(
            "UPDATE paper_accounts SET state_json=?, revision=revision+1",
            (json.dumps(old.export_state()),))
    expected = json.dumps(old.export_state(), sort_keys=True)
    forward = PaperEngine(db_path=str(db), clock=clock, trading_dates=TRADING_DATES)
    forward.open()
    try:
        assert _export(forward) == expected
        assert forward.positions(ACCOUNT)[0]["volume"] == 3
    finally:
        forward.close()


def test_periodic_checkpoint_catches_up_without_a_settlement(tmp_path):
    db = tmp_path / "paper.sqlite3"
    engine = build_engine(db, clock=_clock())
    try:
        feed(engine, au_quote("09:31:00.000000", 820.0))
        _round_trips(engine, 2, "periodic")
        assert _rows(db, "SELECT revision FROM paper_accounts") == [(0,)]
        engine.checkpoint_interval_seconds = 0.0
        engine._checkpoint_scan_at = 0.0
        engine._maybe_checkpoint()
        (revision, checkpoint_of, state_json), = _rows(
            db, "SELECT revision, checkpoint_of, state_json FROM paper_accounts")
        assert revision == checkpoint_of == engine._accounts[ACCOUNT].revision == 4
        assert len(json.loads(state_json)["_trades"]) == 4
    finally:
        engine.close()


def test_activity_rows_are_read_outside_the_matching_lock_from_one_snapshot(tmp_path):
    """activity 锁内只定读快照；锁外读行期间撮合照常提交，读到的仍是那一刻的一致切面。"""
    db = tmp_path / "paper.sqlite3"
    engine = build_engine(db, clock=_clock())
    try:
        feed(engine, au_quote("09:31:00.000000", 820.0))
        engine.submit(ACCOUNT, submit_request("snap-open"))
        cut = engine._activity_cut(ACCOUNT)
        # 读事务开着：新命令照常撮合并提交（不被读挡住）。
        closed = engine.submit(ACCOUNT, submit_request(
            "snap-close", direction="SELL", offset="CLOSETODAY", limitPrice=819.98))
        assert closed["order"]["filledVolume"] == 1
        frozen = engine._activity_rows(cut)
        assert [row["clientOrderId"] for row in frozen["orders"]] == ["snap-open"]
        assert len(frozen["trades"]) == 1
        fresh = engine.activity(ACCOUNT)
        assert {row["clientOrderId"] for row in fresh["orders"]} == {"snap-open", "snap-close"}
        assert len(fresh["trades"]) == 2
        combined = engine.combined(ACCOUNT)
        assert combined["positions"] == [] and len(combined["activity"]["trades"]) == 2
    finally:
        engine.close()


def test_check_db_rehearses_the_migration_on_a_copy(tmp_path):
    db = tmp_path / "paper.sqlite3"
    engine = build_engine(db, clock=_clock())
    feed(engine, au_quote("09:31:00.000000", 820.0))
    _round_trips(engine, 3, "rehearse")
    engine.submit(ACCOUNT, submit_request("rehearse-rest", limitPrice=815.0))
    engine.close()
    as_legacy_db(db)
    report = check_db(str(db))
    assert report == {"status": "ok", "accounts": 1, "migratedFromSnapshot": 1, "tradeRows": 6,
                      "checkpointsCurrent": 1, "expirySettlementsOpen": 0, "mismatches": []}
    assert check_db(str(db))["migratedFromSnapshot"] == 0


def _lag(db) -> list[str]:
    connection = sqlite3.connect(db)
    try:
        return checkpoint_lag(connection)
    finally:
        connection.close()


def test_close_reports_whether_the_checkpoints_caught_up(tmp_path, monkeypatch):
    """#527/#510：正常停机合并检查点并如实报结果——合并失败、actor 没在时限内退出都
    不能当成已追平（旧版本回滚会读到落后快照）；serve() 据此非 0，回滚前的
    --checkpoint-db 把它补齐并逐户核对。"""
    import threading

    db = tmp_path / "paper.sqlite3"
    engine = build_engine(db, clock=_clock())
    feed(engine, au_quote("09:31:00.000000", 820.0))
    engine.submit(ACCOUNT, submit_request("close-ok"))
    assert _lag(db) == [ACCOUNT]          # 增量落盘：当前状态行领先检查点
    assert engine.close() is True
    assert _lag(db) == []

    engine = build_engine(db, clock=_clock("2026-11-02T09:40:00+08:00"))
    feed(engine, au_quote("09:41:00.000000", 821.0))
    engine.submit(ACCOUNT, submit_request("close-broken"))

    def broken(account_id):
        raise sqlite3.OperationalError("probe_disk_full")

    monkeypatch.setattr(engine, "_write_checkpoint", broken)
    assert engine.close() is False
    assert _lag(db) == [ACCOUNT]
    # 回滚前的门：新版本合并检查点并逐户核对。
    assert checkpoint_db(str(db)) == {"status": "ok", "accounts": 1, "lagging": []}
    assert _lag(db) == []

    engine = build_engine(db, clock=_clock("2026-11-02T09:50:00+08:00"))
    release = threading.Event()
    stuck = threading.Event()

    def hang() -> None:
        stuck.set()
        release.wait(5)

    monkeypatch.setattr(engine, "_maybe_checkpoint", hang)
    assert stuck.wait(5)
    try:
        assert engine.close(join_seconds=0.05) is False   # actor 卡住：没合并，报没追平
    finally:
        release.set()
    assert engine.close() is True


def test_checkpoint_db_refuses_when_the_checkpoints_stay_behind(tmp_path, monkeypatch):
    db = tmp_path / "paper.sqlite3"
    engine = build_engine(db, clock=_clock())
    feed(engine, au_quote("09:31:00.000000", 820.0))
    engine.submit(ACCOUNT, submit_request("gate-open"))
    _crash(engine)                         # 非正常退出：检查点落后
    monkeypatch.setattr(PaperEngine, "_checkpoint_all", lambda self: True)
    assert checkpoint_db(str(db)) == {"status": "not_caught_up", "accounts": 1, "lagging": [ACCOUNT]}


def test_check_db_flags_a_clean_close_that_left_checkpoints_behind(tmp_path, monkeypatch):
    """预演里正常停机后检查点仍落后 = 停机路径坏了：判不一致，不再只报个数照样 ok。"""
    db = tmp_path / "paper.sqlite3"
    engine = build_engine(db, clock=_clock())
    feed(engine, au_quote("09:31:00.000000", 820.0))
    engine.submit(ACCOUNT, submit_request("rehearse-lag"))
    _crash(engine)
    assert check_db(str(db))["status"] == "ok"   # 正常停机能把落后的检查点补齐
    engine = build_engine(db, clock=_clock("2026-11-02T09:40:00+08:00"))
    feed(engine, au_quote("09:41:00.000000", 821.0))
    engine.submit(ACCOUNT, submit_request("rehearse-lag-2"))
    _crash(engine)
    monkeypatch.setattr(PaperEngine, "_checkpoint_all", lambda self: True)
    report = check_db(str(db))
    assert report["status"] == "mismatch" and report["checkpointsCurrent"] == 0
    assert report["mismatches"] == [f"{ACCOUNT}: checkpoint not caught up after a clean close"]
