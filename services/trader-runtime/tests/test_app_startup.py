"""启动口径：配置解析阶段的拒绝（525-B）与 lifespan 的故障收尾（525-C3）。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from trader_runtime.main import create_app

TOKEN = "startup-test-token"


def test_an_invalid_mode_is_refused_before_the_database_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """525-B：写错的 auth/data mode 在 resolve_settings 就失败——不再先跑
    build_services 写共享库（对账、恢复），才由 wiring/auth 报错。"""
    database = tmp_path / "mode-refused.sqlite3"
    monkeypatch.setenv("AI_TRADER_AUTH_MODE", "owner-passsword")  # 手滑的配置
    with pytest.raises(ValueError, match="invalid_auth_mode"):
        create_app(database, agent_mode="fake")
    assert not database.exists()
    monkeypatch.setenv("AI_TRADER_AUTH_MODE", "local-token")
    monkeypatch.setenv("AI_TRADER_DATA_MODE", "tqkq")  # 缩写/旧名不在枚举里
    with pytest.raises(ValueError, match="invalid_data_mode"):
        create_app(database, agent_mode="fake")
    assert not database.exists()


def _app(tmp_path: Path, database: str):
    return create_app(tmp_path / database, local_token=TOKEN, agent_mode="fake", data_mode="fake")


async def test_a_failed_startup_step_still_shuts_down_what_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """525-C3：启动中途失败（这里 lifecycle.start 抛错）不再把已启动的组件留在
    进程里——收尾序列照常走完，直到 store.close()。"""
    app = _app(tmp_path, "startup-fail.sqlite3")
    try:
        async def broken_start() -> None:
            raise RuntimeError("startup boom")

        monkeypatch.setattr(app.state.ctx.lifecycle, "start", broken_start)
        with pytest.raises(RuntimeError, match="startup boom"):
            await app.router.lifespan_context(app).__aenter__()
        # 收尾的最后一步是 store.close()：连接已关，再查询即报错。
        with pytest.raises(sqlite3.ProgrammingError):
            app.state.store.connection.execute("SELECT 1")
    finally:
        app.state.store.close()


async def test_a_failing_close_step_does_not_strand_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """525-C3：第一个 close 步骤（guests.close）抛错不能吞掉其余收尾——后面的
    组件照常关闭，直到 store.close()。"""
    app = _app(tmp_path, "close-fail.sqlite3")
    try:
        async def broken_close() -> None:
            raise RuntimeError("close boom")

        monkeypatch.setattr(app.state.guests, "close", broken_close)
        async with app.router.lifespan_context(app):
            pass
        with pytest.raises(sqlite3.ProgrammingError):
            app.state.store.connection.execute("SELECT 1")
    finally:
        app.state.store.close()
