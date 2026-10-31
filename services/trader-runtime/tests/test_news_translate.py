"""Batch translation of English flash titles: recorded doubles only, no network."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from trader_runtime.news_translate import TITLE_CHARS, NewsTranslator, TranslationBatch, is_english_title

NOW = datetime(2026, 10, 11, 6, 0, tzinfo=UTC)  # 14:00 Beijing


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Model:
    """The adapter surface the translator touches: answers every title with a fixed Chinese line."""

    agent_authenticated = True
    model = "test-model"

    def __init__(self, broken: bool = False, echo: bool = False) -> None:
        self.broken, self.echo, self.calls = broken, echo, 0
        self.called = asyncio.Event()

    async def complete_structured(self, *, instructions: str, input_text: str, schema: type,
                                  timeout_seconds: float = 60, model: str | None = None,
                                  usage_user: str | None = None, **_: Any) -> SimpleNamespace:
        assert schema is TranslationBatch and usage_user == "platform:news-translate"
        self.calls += 1
        self.called.set()
        if self.broken:
            raise TimeoutError("model timed out")
        payload = json.loads(input_text)
        if self.echo:  # 译不动：按提示词原样返回
            translations = [{"key": item["key"], "title": item["title"]} for item in payload["titles"]]
        else:
            translations = [{"key": item["key"], "title": f"〔中文〕{item['title']}"} for item in payload["titles"]]
        return SimpleNamespace(value=TranslationBatch.model_validate({"translations": translations}), tokens=60)


def translator(tmp_path, model: _Model | None = None, **kwargs: Any) -> tuple[NewsTranslator, sqlite3.Connection]:
    connection = sqlite3.connect(tmp_path / "translate.sqlite3", check_same_thread=False)
    with connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS news_flash (id TEXT PRIMARY KEY, source TEXT NOT NULL,"
            " sources_json TEXT NOT NULL, published_at TEXT NOT NULL, title TEXT NOT NULL,"
            " text TEXT NOT NULL, link TEXT, products_json TEXT NOT NULL, tags_json TEXT NOT NULL)")
        if "orig_title" not in {row[1] for row in connection.execute("PRAGMA table_info(news_flash)")}:
            connection.execute("ALTER TABLE news_flash ADD COLUMN orig_title TEXT")
    service = NewsTranslator(connection=connection, lock=threading.RLock(), generator=(lambda: model),
                             clock=_Clock(), now=lambda: NOW, **kwargs)
    return service, connection


def store_english_item(connection: sqlite3.Connection, item_id: str, title: str) -> None:
    with connection:
        connection.execute(
            "INSERT INTO news_flash (id, source, sources_json, published_at, title, text, link, products_json,"
            " tags_json, orig_title) VALUES (?, 'FinancialJuice', '[\"FinancialJuice\"]',"
            " '2026-10-11T13:00:00+08:00', ?, ?, NULL, '[\"cu\"]', '[\"macro\"]', NULL)",
            (item_id, title, title))


def test_the_queue_only_holds_english_work(tmp_path):
    assert is_english_title("Copper rises as dollar weakens after Fed comments")
    assert not is_english_title("铜价上涨，因美元走弱")
    assert not is_english_title("LME铜上涨")  # 中文标题里的英文代码不算英文标题
    service, _ = translator(tmp_path)
    added = service.offer([
        {"id": "en1", "title": "Copper rises in London trade"},
        {"id": "zh1", "title": "铜价上涨"},
        {"id": "", "title": "Copper rises"},
    ])
    assert added == 1 and list(service._pending) == ["en1"]


def test_a_round_translates_titles_and_keeps_the_originals(tmp_path):
    model = _Model()
    service, connection = translator(tmp_path, model)
    store_english_item(connection, "en1", "Copper rises as Fed signals patience")
    assert service.offer([{"id": "en1", "title": "Copper rises as Fed signals patience"}]) == 1
    assert asyncio.run(service.run_round()) == 1
    row = connection.execute("SELECT title, orig_title FROM news_flash WHERE id = 'en1'").fetchone()
    assert row == ("〔中文〕Copper rises as Fed signals patience", "Copper rises as Fed signals patience")
    kept = connection.execute("SELECT title_zh FROM news_translation WHERE item_id = 'en1'").fetchone()
    assert kept == ("〔中文〕Copper rises as Fed signals patience",)
    # A restart (fresh queue) does not translate it again.
    assert service.offer([{"id": "en1", "title": "Copper rises as Fed signals patience"}]) == 0
    assert asyncio.run(service.run_round()) == 0 and model.calls == 1


def test_a_failing_model_keeps_the_original_title_and_backs_off(tmp_path):
    model = _Model(broken=True)
    service, connection = translator(tmp_path, model)
    store_english_item(connection, "en1", "Copper rises as Fed signals patience")
    service.offer([{"id": "en1", "title": "Copper rises as Fed signals patience"}])
    assert asyncio.run(service.run_round()) == 0
    title = connection.execute("SELECT title FROM news_flash WHERE id = 'en1'").fetchone()[0]
    assert title == "Copper rises as Fed signals patience"  # 原文照常显示
    usable, reason = service.usable()
    assert not usable and reason == "news_translate_model_failing"
    service.clock.now += 6 * 60  # back-off 过后可以再试
    assert service.usable() == (True, None)


def test_over_the_daily_cap_the_original_stands(tmp_path):
    model = _Model()
    service, connection = translator(tmp_path, model, daily_calls=1)
    items = [{"id": f"en{i}", "title": f"Copper inventory note number {i}"} for i in range(25)]
    for item in items:
        store_english_item(connection, item["id"], item["title"])
    service.offer(items)
    assert asyncio.run(service.run_round()) == 24  # 一批 24 条，当天唯一一次调用
    usable, reason = service.usable()
    assert not usable and reason == "news_translate_budget_exhausted"
    assert service.usage()["calls"] == 1
    assert asyncio.run(service.run_round()) == 0  # 超额的条目不翻译、不报错
    untranslated = connection.execute(
        "SELECT COUNT(*) FROM news_flash WHERE orig_title IS NULL").fetchone()[0]
    assert untranslated == 1  # 第 25 条保持英文原文显示
    assert connection.execute("SELECT title FROM news_flash WHERE id = 'en24'").fetchone()[0] \
        == "Copper inventory note number 24"


def test_without_an_authenticated_adapter_nothing_runs(tmp_path):
    service, connection = translator(tmp_path, None)
    store_english_item(connection, "en1", "Copper rises")
    service.offer([{"id": "en1", "title": "Copper rises"}])
    usable, reason = service.usable()
    assert not usable and reason == "news_translate_model_unavailable"
    assert asyncio.run(service.run_round()) == 0
    assert connection.execute("SELECT title, orig_title FROM news_flash WHERE id = 'en1'").fetchone() \
        == ("Copper rises", None)


def test_the_background_loop_translates_offered_work_without_waiting(tmp_path):
    """_run 自己（不是 burst）：有待处理且模型可用时立即翻译，不等下一次 60 秒唤醒。"""
    async def scenario() -> None:
        model = _Model()
        service, connection = translator(tmp_path, model)
        service.start()
        await asyncio.sleep(0.05)  # 空转着：没有排队的工作就不调模型
        assert model.calls == 0
        store_english_item(connection, "en1", "Copper rises as Fed signals patience")
        service.offer([{"id": "en1", "title": "Copper rises as Fed signals patience"}])
        await asyncio.wait_for(model.called.wait(), 2)  # 循环被 offer 唤醒并当场开翻
        for _ in range(200):  # 模型答完写库（≤2 s 的 10 ms 轮询，不真实等待）
            await asyncio.sleep(0.01)
            if connection.execute("SELECT orig_title FROM news_flash WHERE id = 'en1'").fetchone()[0]:
                break
        assert connection.execute("SELECT title, orig_title FROM news_flash WHERE id = 'en1'").fetchone() \
            == ("〔中文〕Copper rises as Fed signals patience", "Copper rises as Fed signals patience")
        assert service.usage()["pending"] == 0
        await service.close()
    asyncio.run(scenario())


def test_prune_clears_translations_of_pruned_items(tmp_path):
    """news_translation 跟着 news_flash 的保留期清理：条目被清掉的译文一起删，留下的保留。"""
    service, connection = translator(tmp_path)
    store_english_item(connection, "keep", "Copper rises")
    store_english_item(connection, "gone", "Zinc falls")
    with connection:
        connection.execute("DELETE FROM news_flash WHERE id = 'gone'")  # 保留期把它清掉了
        connection.executemany(
            "INSERT INTO news_translation (item_id, title_zh, model, translated_at) VALUES (?, ?, 'm',"
            " '2026-10-11T13:00:00+08:00')", [("keep", "铜价上涨"), ("gone", "锌价下跌")])
    service.prune()
    assert connection.execute("SELECT item_id FROM news_translation").fetchall() == [("keep",)]


def test_a_queued_item_pruned_before_its_round_costs_no_call(tmp_path):
    """#516：排队时还在、轮到时已被保留期删掉的条目不再花模型调用和日额度。"""
    model = _Model()
    service, connection = translator(tmp_path, model)
    store_english_item(connection, "gone", "Zinc falls on weak demand")
    assert service.offer([{"id": "gone", "title": "Zinc falls on weak demand"}]) == 1
    with connection:
        connection.execute("DELETE FROM news_flash WHERE id = 'gone'")
    assert asyncio.run(service.run_round()) == 0 and model.calls == 0 and not service._pending


def test_an_unchanged_answer_is_recorded_and_never_requeued(tmp_path):
    """模型按提示词把译不动的标题原样返回：记为已处理（不再重复排队烧调用），条目照显原文；
    整批原样返回也不是空轮，不触发 5 分钟 backoff。"""
    model = _Model(echo=True)
    service, connection = translator(tmp_path, model)
    store_english_item(connection, "en1", "Copper rises as Fed signals patience")
    service.offer([{"id": "en1", "title": "Copper rises as Fed signals patience"}])
    assert asyncio.run(service.run_round()) == 1  # 处理了一条（虽未改写）
    assert service.usable() == (True, None)  # 不是失败：没有 backoff
    assert connection.execute("SELECT title, orig_title FROM news_flash WHERE id = 'en1'").fetchone() \
        == ("Copper rises as Fed signals patience", None)  # 原文照常显示
    assert connection.execute("SELECT title_zh FROM news_translation WHERE item_id = 'en1'").fetchone() \
        == ("Copper rises as Fed signals patience",)
    assert service.offer([{"id": "en1", "title": "Copper rises as Fed signals patience"}]) == 0
    assert asyncio.run(service.run_round()) == 0 and model.calls == 1  # 不会再为它调模型


def test_the_previous_versions_tables_survive_a_blue_green_overlap(tmp_path):
    """蓝绿切换时旧版本还在同一个库上跑：新版本不用 news_translate_lease / news_translate_day，但不删，
    旧版本的认领、计数与 close() 照常；旧计数并入共享预算一次。"""
    connection = sqlite3.connect(tmp_path / "translate.sqlite3", check_same_thread=False)
    with connection:  # 旧版本建的两张表
        connection.execute("CREATE TABLE news_translate_lease (id INTEGER PRIMARY KEY CHECK (id = 1),"
                           " holder TEXT NOT NULL, expires REAL NOT NULL)")
        connection.execute("CREATE TABLE news_translate_day (day TEXT PRIMARY KEY, calls INTEGER NOT NULL)")
        connection.execute("INSERT INTO news_translate_day (day, calls) VALUES ('2026-10-11', 7)")
    service, connection = translator(tmp_path)
    assert service._used() == 7
    with connection:  # 旧版本 _claim()/_count_call()/close() 用到的语句
        connection.execute("INSERT OR IGNORE INTO news_translate_lease (id, holder, expires) VALUES (1, 'old', 1.0)")
        connection.execute("UPDATE news_translate_day SET calls = calls + 1 WHERE day = '2026-10-11'")
        connection.execute("DELETE FROM news_translate_lease WHERE id = 1 AND holder = 'old'")
    translator(tmp_path)  # 再起一次（回滚后再发布）：已并入的计数不重复加
    assert service._used() == 7


def test_a_standby_drops_queue_items_the_holder_already_translated(tmp_path):
    """只有实例租约持有者跑翻译循环（test_instance_lease 覆盖），但每个进程都排队：standby 在持有者
    翻译前就排了同一条，接手租约后复查 news_translation，已译条目直接丢出批次、不再为它调模型。"""
    holder, standby = _Model(), _Model()
    service_a, connection = translator(tmp_path, holder)
    service_b, _ = translator(tmp_path, standby)
    store_english_item(connection, "en1", "Copper rises as Fed signals patience")
    # 两个进程的轮询各排了同一条（B 排在 A 翻译之前）。
    service_a.offer([{"id": "en1", "title": "Copper rises as Fed signals patience"}])
    service_b.offer([{"id": "en1", "title": "Copper rises as Fed signals patience"}])
    assert asyncio.run(service_a.run_round()) == 1 and holder.calls == 1
    asyncio.run(service_a.close())
    assert asyncio.run(service_b.run_round()) == 0 and standby.calls == 0  # 复查后批次清空
    assert service_b.usage()["pending"] == 0


def test_the_daily_call_count_survives_a_restart(tmp_path):
    """当日调用数记在共享库里：重启（或蓝绿换进程）不重拿当日额度。"""
    model = _Model()
    service_a, connection = translator(tmp_path, model, daily_calls=1)
    store_english_item(connection, "en1", "Copper rises as Fed signals patience")
    service_a.offer([{"id": "en1", "title": "Copper rises as Fed signals patience"}])
    assert asyncio.run(service_a.run_round()) == 1
    service_b, _ = translator(tmp_path, _Model(), daily_calls=1)  # 同一个 sqlite：换了个进程
    usable, reason = service_b.usable()
    assert not usable and reason == "news_translate_budget_exhausted"
    assert service_b.usage()["calls"] == 1


def test_an_unchanged_echo_of_a_long_title_is_matched_before_truncation(tmp_path):
    """超过 120 字的标题被模型原样返回时，原样判断用未截断的全文比较：不误当成译文
    覆盖标题，记为已处理、条目照显原文。"""
    model = _Model(echo=True)
    service, connection = translator(tmp_path, model)
    long_title = ("Copper rises as the dollar weakens " + "and inventories keep falling " * 5).strip()
    assert len(long_title) > 120
    store_english_item(connection, "en1", long_title)
    service.offer([{"id": "en1", "title": long_title}])
    assert asyncio.run(service.run_round()) == 1
    assert connection.execute("SELECT title, orig_title FROM news_flash WHERE id = 'en1'").fetchone() \
        == (long_title, None)  # 截断前的比较认出原样返回：标题没被“截断版译文”覆盖
    assert connection.execute("SELECT title_zh FROM news_translation WHERE item_id = 'en1'").fetchone() \
        == (long_title[:TITLE_CHARS],)  # 记录列本身仍按 TITLE_CHARS 截（只 bound 译文列）


def test_an_unchanged_echo_of_an_over_200_char_title_is_matched_on_what_was_sent(tmp_path):
    """超过 200 字符的标题：模型收到的（也原样返回的）是截到 200 的前缀，原样判断按发出的值
    比较——不误当译文把标题换成 120 字符的英文前缀、把全文挪进 orig_title。"""
    model = _Model(echo=True)
    service, connection = translator(tmp_path, model)
    long_title = ("Copper rises as the dollar weakens and inventories keep falling "
                  + "across every major exchange warehouse " * 4).strip()
    assert len(long_title) > 200
    store_english_item(connection, "en1", long_title)
    service.offer([{"id": "en1", "title": long_title}])
    assert asyncio.run(service.run_round()) == 1
    assert connection.execute("SELECT title, orig_title FROM news_flash WHERE id = 'en1'").fetchone() \
        == (long_title, None)  # 认出原样返回：完整标题没被“截断版译文”覆盖
    assert connection.execute("SELECT title_zh FROM news_translation WHERE item_id = 'en1'").fetchone() \
        == (long_title[:TITLE_CHARS],)


def test_a_translation_whose_queued_title_was_merged_away_does_not_land(tmp_path):
    """排队后标题被跨源合并改写：写回带标题守卫，译文不覆盖合并结果、也不记完成。"""
    model = _Model()
    service, connection = translator(tmp_path, model)
    store_english_item(connection, "en1", "Copper rises as Fed signals patience")
    service.offer([{"id": "en1", "title": "Copper rises as Fed signals patience"}])
    with connection:  # 合并把这一行的标题换成了另一家的版本
        connection.execute("UPDATE news_flash SET title = '铜价上涨，因美联储暗示保持耐心'"
                           " WHERE id = 'en1'")
    assert asyncio.run(service.run_round()) == 0
    assert connection.execute("SELECT title, orig_title FROM news_flash WHERE id = 'en1'").fetchone() \
        == ("铜价上涨，因美联储暗示保持耐心", None)
    assert connection.execute("SELECT COUNT(*) FROM news_translation").fetchone()[0] == 0
