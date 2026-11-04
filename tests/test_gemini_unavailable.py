"""R-22 (04.11): сувора перевірка перекладу — лише заголовок і лід; «Gemini недоступний» не валить запуск."""
import json
import time

import pytest

from conftest import Recorder, deadline_error, quota_error, unavailable_error

LEAD = "Župan Bilaver predstavio je Zadarsku županiju na sajmu WTM u Londonu. " * 5  # > 300 символів


def make(title="Жупан Біла­вер у Лондоні", content="Жупан представив Задарську жупанію на ярмарку.",
         hashtags="#Zadar #Туризм"):
    return {"id": 0, "title": title, "content": content, "hashtags": hashtags}


def original(description):
    return {"title": "Župan Bilaver iz Londona", "description": description, "image_urls": []}


def test_sport_deep_in_source_text_passes_validation(bot):
    """04.11: «…sastanak s ministrom turizma i sporta…» далеко в тексті — новина 3 рази не проходила."""
    source = original(LEAD + "Održao je sastanak s ministrom turizma i sporta Tončijem Glavinom.")
    assert len(LEAD) > bot.STOP_WORDS_DESCRIPTION_CHARS
    assert bot.validate_translation_strict(make(), source)


def test_sport_in_title_lead_or_hashtags_still_blocked(bot):
    assert not bot.validate_translation_strict(make(), original("Utakmica KK Zadra večeras. " + LEAD))
    assert not bot.validate_translation_strict(make(title="Спорт: Задар переміг"), original(LEAD))
    assert not bot.validate_translation_strict(make(content="Матч завершився перемогою. " + LEAD), original(LEAD))
    assert not bot.validate_translation_strict(make(hashtags="#Zadar #Спорт"), original(LEAD))
    assert not bot.validate_translation_strict(make(), original("Plaćeni oglas: " + LEAD))


@pytest.mark.parametrize("errors", [
    {"gemini-3.8-flash": [unavailable_error()], "gemini-3.6-flash": [deadline_error()],
     "gemini-3.5-flash-lite": [quota_error()]},
])
def test_all_models_unavailable_raises_gemini_unavailable(bot, fake_gemini, errors):
    fake_gemini(bot, errors)
    with pytest.raises(bot.GeminiUnavailable):
        bot.generate_news_texts_json("p", {0}, bot._empty_stats())


def test_bad_answer_is_still_a_real_error(bot, fake_gemini):
    fake_gemini(bot, {"gemini-3.8-flash": [unavailable_error()], "gemini-3.6-flash": ["not json"],
                      "gemini-3.5-flash-lite": [unavailable_error()]})
    with pytest.raises(RuntimeError) as exc:
        bot.generate_news_texts_json("p", {0}, bot._empty_stats())
    assert not isinstance(exc.value, bot.GeminiUnavailable)


def test_all_quota_exhausted_before_call_is_unavailable(bot, fake_gemini):
    fake_gemini(bot, {})
    bot.EXHAUSTED_MODELS.update(bot.TRANSLATE_MODELS)
    with pytest.raises(bot.GeminiUnavailable):
        bot.generate_news_texts_json("p", {0}, bot._empty_stats())


def test_filter_unavailable_sends_no_alert(bot, fake_gemini, monkeypatch):
    fake_gemini(bot, {m: [unavailable_error()] for m in bot.FILTER_MODELS})
    alerts = []
    monkeypatch.setattr(bot, "send_alert_to_admin", alerts.append)
    with pytest.raises(bot.GeminiUnavailable):
        bot.deduplicate_news([{"title": "Novost iz Zadra", "description": "Opis", "url": "https://a/1"}],
                             [], bot._empty_stats())
    assert alerts == []


def test_translation_unavailable_keeps_failure_counter(bot, monkeypatch, tmp_path):
    json.dump([{"name": "023", "url": "https://023.hr/feed/"}], open(tmp_path / "feeds.json", "w"))
    now = 1_900_000_000
    json.dump([{"title": "Novost broj 1 iz Zadra danas", "description": "Opis " * 20,
                "url": "https://www.023.hr/v-1/", "image_urls": ["https://x/p.jpg"], "source": "023",
                "timestamp": now}], open(tmp_path / "news_backlog.json", "w"))
    monkeypatch.setattr(bot.time, "time", lambda: now + 100)
    monkeypatch.setattr(bot, "deduplicate_news", lambda a, h=None, stats=None: ([a[0]], []))

    def down(*args, **kwargs):
        raise bot.GeminiUnavailable("переклад новин: Gemini недоступний")

    monkeypatch.setattr(bot, "generate_news_texts_json", down)
    stats = bot._empty_stats()
    with pytest.raises(bot.GeminiUnavailable):
        bot.process_backlog(stats)
    backlog = json.load(open(tmp_path / "news_backlog.json"))
    assert [item["url"] for item in backlog] == ["https://www.023.hr/v-1/"]
    assert "translation_failures" not in backlog[0] and stats["errors_count"] == 0


def _main_with_outage(bot, monkeypatch, tmp_path, previous_runs):
    if previous_runs:
        json.dump(previous_runs, open(tmp_path / "analytics.json", "w"))

    def down(stats=None):
        raise bot.GeminiUnavailable("фільтрація новин: Gemini недоступний")

    monkeypatch.setattr(bot, "scrape_feeds", lambda stats=None: None)
    monkeypatch.setattr(bot, "process_backlog", down)
    monkeypatch.setattr(bot.genai, "Client", lambda **kw: None)
    recorder = Recorder()
    monkeypatch.setattr(bot.requests, "post", recorder)
    alerts = []
    monkeypatch.setattr(bot, "send_alert_to_admin", alerts.append)
    bot.main()  # без SystemExit: запуск зелений
    return recorder, alerts


def test_outage_run_is_green_reported_and_not_alerted(bot, monkeypatch, tmp_path):
    recorder, alerts = _main_with_outage(bot, monkeypatch, tmp_path, [])
    saved = json.load(open(tmp_path / "analytics.json"))[-1]
    assert saved["gemini_unavailable"] is True and saved["errors_count"] == 0
    assert any("Gemini недоступний" in item["data"]["text"] for item in recorder.sent)
    assert alerts == []


def test_outage_alert_on_third_run_in_a_row(bot, monkeypatch, tmp_path):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    previous = [{"timestamp": ts, "gemini_unavailable": True}, {"timestamp": ts, "gemini_unavailable": True}]
    _, alerts = _main_with_outage(bot, monkeypatch, tmp_path, previous)
    assert len(alerts) == 1 and "3 запуски поспіль" in alerts[0]


def test_streak_counts_only_latest_runs(bot):
    runs = [{"gemini_unavailable": True}, {}, {"gemini_unavailable": True}, {"gemini_unavailable": True}]
    assert bot.gemini_unavailable_streak(runs) == 2
    assert bot.gemini_unavailable_streak([]) == 0


def test_weekly_report_counts_outages(bot):
    now = time.time()
    runs = [{"timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 3600)),
             "gemini_unavailable": True, "model_attempts": [
                 {"task": "filter", "model": "gemini-3.5-flash-lite", "outcome": "unavailable"}]}]
    assert "☁️ Gemini недоступний (усі моделі 503 / 504 / квота): 1 запусків" in bot.build_weekly_report(runs, now=now)
