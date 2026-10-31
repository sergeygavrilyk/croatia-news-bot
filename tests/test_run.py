"""Повний цикл process_backlog / main без мережі: стан, код виходу, тестовий режим."""
import hashlib
import json
import os
import types

import pytest

from conftest import Recorder


def write_state(path, count=4):
    json.dump([{"name": "023", "url": "https://023.hr/feed/"}], open(path / "feeds.json", "w"))
    now = 1_900_000_000
    items = [{"title": f"Novost broj {i} iz Zadra danas", "description": "Opis " * 20,
              "url": f"https://www.023.hr/v-{i}/", "image_urls": ["https://x/p.jpg"], "source": "023",
              "timestamp": now - i} for i in range(count)]
    json.dump(items, open(path / "news_backlog.json", "w"))


@pytest.fixture
def offline_cycle(bot, monkeypatch, tmp_path):
    """Фільтр залишає 0 і 1, відхиляє 2, «забуває» 3; переклад валідний; Telegram приймає все."""
    write_state(tmp_path)
    monkeypatch.setattr(bot.time, "time", lambda: 1_900_000_100)
    monkeypatch.setattr(bot, "scrape_feeds", lambda stats=None: None)

    def fake_filter(articles, history=None, stats=None):
        if stats is not None:
            stats["funnel"]["unclassified_by_ai"] = 1
        return [articles[0], articles[1]], [{"id": 2, "entities_found": [], "reason": "Спорт"}]

    monkeypatch.setattr(bot, "deduplicate_news", fake_filter)
    monkeypatch.setattr(bot, "generate_news_texts_json", lambda p, ids, stats=None, id_aliases=None: [
        {"id": i, "title": f"Новина {i}", "content": "Текст.", "hashtags": "#Zadar #Суспільство"} for i in sorted(ids)])
    sent = []
    monkeypatch.setattr(bot, "send_to_telegram", lambda text, url, source, images=None: sent.append(url) or (True, "image"))
    monkeypatch.setattr(bot.genai, "Client", lambda **kw: types.SimpleNamespace(models=None))
    monkeypatch.setattr(bot.requests, "post", Recorder())
    return sent


def test_process_backlog_state(bot, offline_cycle, tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(bot.time, "sleep", lambda s: sleeps.append(s))
    stats = bot._empty_stats()
    bot.process_backlog(stats)
    left = [item["url"] for item in json.load(open(tmp_path / "news_backlog.json"))]
    assert left == ["https://www.023.hr/v-3/"]  # «забута» фільтром лишилась у черзі
    assert open(tmp_path / "posted_news.txt").read().split() == ["https://www.023.hr/v-0/", "https://www.023.hr/v-1/"]
    assert stats["published"]["total"] == 2 and stats["funnel"]["duplicates_removed_by_ai"] == 1
    assert sleeps.count(bot.POST_PUBLICATION_DELAY_SECONDS) == 1  # без паузи після останнього поста


def test_partial_error_keeps_run_green(bot, offline_cycle, monkeypatch):
    real = bot.validate_translation_strict
    monkeypatch.setattr(bot, "validate_translation_strict",
                        lambda t, item: False if t["id"] == 1 else real(t, item))
    bot.main()  # не SystemExit: одна невдала новина — не критична помилка
    assert len(offline_cycle) == 1


def test_critical_error_exit_code(bot, offline_cycle, monkeypatch):
    def boom(stats=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(bot, "process_backlog", boom)
    with pytest.raises(SystemExit) as exc:
        bot.main()
    assert exc.value.code == 1


def test_dry_run_changes_no_state_and_posts_to_admin(load_bot, monkeypatch, tmp_path):
    bot = load_bot(DRY_RUN="true")
    write_state(tmp_path, count=3)
    digest = {n: hashlib.sha256(open(tmp_path / n, "rb").read()).hexdigest() for n in os.listdir(tmp_path)}
    monkeypatch.setattr(bot.time, "time", lambda: 1_900_000_100)
    monkeypatch.setattr(bot, "scrape_feeds", lambda stats=None: None)
    monkeypatch.setattr(bot, "deduplicate_news", lambda a, h=None, stats=None: (a, []))
    monkeypatch.setattr(bot, "generate_news_texts_json", lambda p, ids, stats=None, id_aliases=None: [
        {"id": i, "title": f"Новина {i}", "content": "Текст.", "hashtags": "#Zadar #Суспільство"} for i in sorted(ids)])
    payloads = []
    monkeypatch.setattr(bot, "execute_telegram_request", lambda api, payload: payloads.append(payload) or {})
    monkeypatch.setattr(bot.genai, "Client", lambda **kw: types.SimpleNamespace(models=None))
    recorder = Recorder()
    monkeypatch.setattr(bot.requests, "post", recorder)
    cwd = os.getcwd()
    try:
        bot.main()
    finally:
        os.chdir(cwd)
    assert len(payloads) == 3 and all(p["chat_id"] == "admin-chat" for p in payloads)
    assert all(p["caption"].startswith(bot.DRY_RUN_HEADER) for p in payloads)
    after = {n: hashlib.sha256(open(tmp_path / n, "rb").read()).hexdigest() for n in os.listdir(tmp_path)}
    assert after == digest
    assert any("ТЕСТОВИЙ ЗАПУСК" in (s["data"] or {}).get("text", "") for s in recorder.sent)


def test_update_prefix_added_by_code(bot, offline_cycle, monkeypatch, tmp_path):
    texts = []
    monkeypatch.setattr(bot, "send_to_telegram",
                        lambda text, url, source, images=None: texts.append(text) or (True, "image"))

    def fake_filter(articles, history=None, stats=None):
        articles[0]["is_update"] = True
        return [articles[0], articles[1]], []

    monkeypatch.setattr(bot, "deduplicate_news", fake_filter)
    bot.process_backlog(bot._empty_stats())
    assert texts[0].startswith("<b>🔄 [ОНОВЛЕНО]: Новина 0</b>")
    assert texts[1].startswith("<b>Новина 1</b>")


def test_rejected_news_are_remembered(bot, offline_cycle, tmp_path):
    bot.process_backlog(bot._empty_stats())
    memory = json.load(open(tmp_path / bot.REJECTED_FILE, encoding="utf-8"))
    assert [item["url"] for item in memory] == ["https://www.023.hr/v-2/"]  # відхилена «Спорт»
