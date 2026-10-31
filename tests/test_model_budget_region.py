"""D-14 (R-16): денний бюджет спільної моделі для перекладу; D-13: 400 «User location is not supported»."""
import json
import time

import pytest

from conftest import FakeAPIError, Recorder, translations

SHARED = "gemini-3.6-flash"


def location_error():
    return FakeAPIError(400, "FAILED_PRECONDITION. {'error': {'code': 400, "
                             "'message': 'User location is not supported for the API use.'}}")


def ts(epoch):
    """Timestamp у форматі analytics.json, що повертається назад через _run_epoch()."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


def run_with(epoch, model, n, task="filter", outcome="ok"):
    return {"timestamp": ts(epoch), "model_attempts": [{"task": task, "model": model, "outcome": outcome}] * n}


# 31.10.2026 12:00 UTC = 05:00 за тихоокеанським літнім часом (UTC-7) → доба квоти почалась о 07:00 UTC.
NOON_UTC = 1_793_448_000
PT_MIDNIGHT = NOON_UTC - 5 * 60 * 60


def test_quota_day_start_is_pacific_midnight(bot):
    assert bot.quota_day_start(NOON_UTC) == PT_MIDNIGHT


def test_model_calls_today_counts_only_current_quota_day(bot):
    runs = [run_with(PT_MIDNIGHT - 60, SHARED, 5),            # учора за PT — не рахується
            run_with(PT_MIDNIGHT + 60, SHARED, 2),
            run_with(PT_MIDNIGHT + 3600, SHARED, 1, task="translate", outcome="unavailable"),
            run_with(PT_MIDNIGHT + 7200, SHARED, 3, outcome="location"),  # регіон — не рахується
            run_with(PT_MIDNIGHT + 7200, "gemini-3.5-flash-lite", 4)]
    assert bot.model_calls_today(runs, SHARED, NOON_UTC) == 3


def test_translate_keeps_shared_model_under_budget(bot):
    runs = [run_with(PT_MIDNIGHT + 60, SHARED, bot.SHARED_MODEL_TRANSLATE_BUDGET - 2)]
    stats = bot._empty_stats()
    stats["model_attempts"] = [{"task": "filter", "model": SHARED, "outcome": "ok"}]
    assert bot.translate_cascade_with_budget(stats, runs, NOON_UTC) == bot.TRANSLATE_MODELS
    assert stats["translate_budget_deferred"] == []


def test_translate_defers_shared_model_when_budget_spent(bot):
    # попередні запуски + фільтр цього запуску = бюджет → переклад відкладає 3.6-flash у кінець
    runs = [run_with(PT_MIDNIGHT + 60, SHARED, bot.SHARED_MODEL_TRANSLATE_BUDGET - 1)]
    stats = bot._empty_stats()
    stats["model_attempts"] = [{"task": "filter", "model": SHARED, "outcome": "ok"}]
    cascade = bot.translate_cascade_with_budget(stats, runs, NOON_UTC)
    assert cascade == ["gemini-3.8-flash", "gemini-3.5-flash-lite", SHARED]
    assert stats["translate_budget_deferred"] == [SHARED]
    # фільтра бюджет не стосується
    assert bot.FILTER_MODELS[1] == SHARED


def test_budget_ignores_model_not_in_filter_cascade(load_bot):
    bot = load_bot(GEMINI_FILTER_MODELS="gemini-3.5-flash-lite")
    runs = [run_with(PT_MIDNIGHT + 60, SHARED, 50, task="translate")]
    assert bot.translate_cascade_with_budget(bot._empty_stats(), runs, NOON_UTC) == bot.TRANSLATE_MODELS


def test_translation_uses_budget_cascade(bot, fake_gemini, monkeypatch, tmp_path):
    json.dump([run_with(PT_MIDNIGHT + 60, SHARED, bot.SHARED_MODEL_TRANSLATE_BUDGET)],
              open(tmp_path / "analytics.json", "w"))
    monkeypatch.setattr(bot.time, "time", lambda: NOON_UTC)
    calls = fake_gemini(bot, {"gemini-3.8-flash": [Exception("503 UNAVAILABLE")] * 2,
                              "gemini-3.5-flash-lite": [translations(0)]})
    stats = bot._empty_stats()
    result = bot.generate_news_texts_json("prompt", {0}, stats)
    assert result[0]["id"] == 0
    assert SHARED not in calls  # lite відповіла раніше, ніж черга дійшла до 3.6-flash
    assert stats["translate_budget_deferred"] == [SHARED]


def test_location_error_stops_cascade(bot, fake_gemini):
    calls = fake_gemini(bot, {"gemini-3-flash-preview": [location_error()]})
    stats = bot._empty_stats()
    with pytest.raises(bot.GeminiRegionUnavailable):
        bot.call_gemini_json("prompt", stats)
    assert calls == ["gemini-3-flash-preview"]  # решта каскаду не викликається
    assert stats["model_attempts"][0]["outcome"] == "location"


def test_location_is_not_a_model_failure_for_memory(bot):
    now = NOON_UTC
    runs = [run_with(now - 1800, "gemini-3-flash-preview", 1, outcome="location"),
            run_with(now - 600, "gemini-3-flash-preview", 1, outcome="location")]
    assert bot.demoted_models(runs, "filter", now) == set()


def test_filter_location_error_propagates_without_alert(bot, fake_gemini, monkeypatch):
    fake_gemini(bot, {"gemini-3-flash-preview": [location_error()]})
    alerts = []
    monkeypatch.setattr(bot, "send_alert_to_admin", alerts.append)
    articles = [{"title": "Novost iz Zadra", "description": "Opis", "url": "https://023.hr/a/", "source": "023"}]
    with pytest.raises(bot.GeminiRegionUnavailable):
        bot.deduplicate_news(articles, [], bot._empty_stats())
    assert alerts == []


def test_translation_location_error_keeps_failure_counter(bot, monkeypatch, tmp_path):
    json.dump([{"name": "023", "url": "https://023.hr/feed/"}], open(tmp_path / "feeds.json", "w"))
    now = 1_900_000_000
    json.dump([{"title": "Novost broj 1 iz Zadra danas", "description": "Opis " * 20,
                "url": "https://www.023.hr/v-1/", "image_urls": ["https://x/p.jpg"], "source": "023",
                "timestamp": now}], open(tmp_path / "news_backlog.json", "w"))
    monkeypatch.setattr(bot.time, "time", lambda: now + 100)
    monkeypatch.setattr(bot, "deduplicate_news", lambda a, h=None, stats=None: ([a[0]], []))

    def region(*args, **kwargs):
        raise bot.GeminiRegionUnavailable("переклад новин: регіон")

    monkeypatch.setattr(bot, "generate_news_texts_json", region)
    stats = bot._empty_stats()
    with pytest.raises(bot.GeminiRegionUnavailable):
        bot.process_backlog(stats)
    backlog = json.load(open(tmp_path / "news_backlog.json"))
    assert [item["url"] for item in backlog] == ["https://www.023.hr/v-1/"]
    assert "translation_failures" not in backlog[0]
    assert stats["errors_count"] == 0


def test_region_blocked_run_stays_green_and_reports(bot, monkeypatch, tmp_path):
    def region(stats=None):
        raise bot.GeminiRegionUnavailable("фільтрація новин: регіон")

    monkeypatch.setattr(bot, "scrape_feeds", lambda stats=None: None)
    monkeypatch.setattr(bot, "process_backlog", region)
    monkeypatch.setattr(bot.genai, "Client", lambda **kw: None)
    recorder = Recorder()
    monkeypatch.setattr(bot.requests, "post", recorder)
    bot.main()  # без SystemExit: запуск зелений
    saved = json.load(open(tmp_path / "analytics.json"))[-1]
    assert saved["gemini_region_blocked"] is True and saved["errors_count"] == 0
    assert any("регіону раннера" in item["data"]["text"] for item in recorder.sent)


def test_weekly_report_lines(bot):
    now = NOON_UTC
    runs = [{"timestamp": ts(now - 3600), "gemini_region_blocked": True, "model_attempts": [
                {"task": "filter", "model": "gemini-3-flash-preview", "outcome": "location"}]},
            {"timestamp": ts(now - 1800), "translate_budget_deferred": [SHARED], "model_attempts": [
                {"task": "translate", "model": "gemini-3.5-flash-lite", "outcome": "ok"}]}]
    text = bot.build_weekly_report(runs, now=now)
    assert "🌍 Gemini недоступний з регіону раннера GitHub: 1 запусків" in text
    assert "💰 Переклад без спільної з фільтром моделі (денний бюджет): у 1 запусках" in text
    assert "🌍×1" in text
