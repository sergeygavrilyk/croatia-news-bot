"""T-21 / D-25: пам'ять відхилених новин і «пам'ять» про недоступні моделі."""
import json
import time
import types

NOW = 1_900_000_000


def ts(seconds_ago):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(NOW - seconds_ago))


def run(seconds_ago, *attempts):
    return {"timestamp": ts(seconds_ago),
            "model_attempts": [{"task": t, "model": m, "outcome": o} for t, m, o in attempts]}


# --- пам'ять відхилених новин ---

def test_remember_and_load_rejected(bot, tmp_path):
    assert bot.load_rejected(NOW) == []
    (tmp_path / bot.REJECTED_FILE).write_text("")  # workflow робить touch
    assert bot.load_rejected(NOW) == []
    bot.remember_rejected([{"url": "https://a/1", "title": "Old"}], now=NOW - 25 * 3600)
    bot.remember_rejected([{"url": "https://a/2", "title": "Two"}, {"url": "https://a/2", "title": "Two"}], now=NOW)
    memory = bot.load_rejected(NOW)
    assert [item["url"] for item in memory] == ["https://a/2"]  # старше 24 год — забуто, дубль не пишеться
    stored = json.load(open(tmp_path / bot.REJECTED_FILE, encoding="utf-8"))
    assert stored == [{"url": "https://a/2", "title": "Two", "timestamp": NOW}]


def test_scraper_skips_rejected_url_and_title(bot, monkeypatch, tmp_path):
    class Scraper:
        def get(self, url, headers=None, timeout=15):
            return types.SimpleNamespace(status_code=200, text="<rss/>", raise_for_status=lambda: None)

    published = time.gmtime(NOW - 600)
    entries = [
        {"link": "https://023.hr/a/1", "title": "Tučnjava četvorice muškaraca u Zadru", "published_parsed": published},
        {"link": "https://023.hr/a/2", "title": "Isti naslov drugi link", "published_parsed": published},
    ]
    monkeypatch.setattr(bot.cloudscraper, "create_scraper", lambda **kw: Scraper())
    monkeypatch.setattr(bot.feedparser, "parse", lambda text: types.SimpleNamespace(entries=entries, bozo=0))
    monkeypatch.setattr(bot.time, "time", lambda: NOW)
    json.dump([{"name": "023", "url": "https://023.hr/feed/"}], open(tmp_path / "feeds.json", "w"))
    bot.remember_rejected([{"url": "https://023.hr/a/1", "title": "x"},
                           {"url": "https://other/9", "title": "Isti naslov drugi link"}], now=NOW)
    stats = bot._empty_stats()
    bot.scrape_feeds(stats)
    assert stats["funnel"]["already_rejected"] == 2
    assert bot.load_backlog() == []


# --- «пам'ять» про недоступні моделі ---

def test_demoted_after_two_failed_runs_in_window(bot):
    runs = [run(7000, ("filter", "p", "unavailable"), ("filter", "lite", "ok")),
            run(3600, ("filter", "p", "unavailable"), ("filter", "p", "unavailable"), ("filter", "lite", "ok"))]
    assert bot.demoted_models(runs, "filter", NOW) == {"p"}
    assert bot.demoted_models(runs, "translate", NOW) == set()


def test_not_demoted_when_answered_or_old_or_single_run(bot):
    answered = [run(7000, ("filter", "p", "unavailable")),
                run(3600, ("filter", "p", "unavailable"), ("filter", "p", "ok"))]
    old = [run(4 * 3600, ("filter", "p", "unavailable")), run(3600, ("filter", "p", "timeout"))]
    single = [run(3600, ("filter", "p", "quota"))]
    bad_answer = [run(7000, ("filter", "p", "bad_response")), run(3600, ("filter", "p", "bad_response"))]
    for runs in (answered, old, single, bad_answer):
        assert bot.demoted_models(runs, "filter", NOW) == set()


def test_model_promoted_again_after_window(bot):
    runs = [run(7000, ("filter", "p", "unavailable")), run(3600, ("filter", "p", "unavailable"))]
    assert bot.demoted_models(runs, "filter", NOW) == {"p"}
    assert bot.demoted_models(runs, "filter", NOW + 3 * 3600) == set()


def test_reorder_cascade_keeps_all_models(bot):
    assert bot.reorder_cascade(["p", "f", "lite"], {"p"}) == ["f", "lite", "p"]
    assert bot.reorder_cascade(["p", "f", "lite"], set()) == ["p", "f", "lite"]


def test_apply_model_memory(bot):
    first = bot.FILTER_MODELS[0]
    runs = [run(7000, ("filter", first, "unavailable")), run(3600, ("filter", first, "unavailable"))]
    stats = bot._empty_stats()
    bot.apply_model_memory(stats, runs, NOW)
    assert bot.FILTER_MODELS[-1] == first and len(bot.FILTER_MODELS) == 4
    assert stats["demoted_models"] == [f"filter: {first}"]


def test_default_filter_cascade(bot):
    assert bot.FILTER_MODELS[:3] == ["gemini-3-flash-preview", "gemini-3.6-flash", "gemini-3.5-flash-lite"]


def test_filter_prompt_other_source_same_event(bot, monkeypatch):
    captured = {}

    def fake_call(prompt, stats=None):
        captured["prompt"] = prompt
        return bot.NewsFilterResponse(keep=[0], rejected=[])

    monkeypatch.setattr(bot, "call_gemini_json", fake_call)
    bot.deduplicate_news([{"title": "T", "description": "D", "url": "https://a/1"}], [], bot._empty_stats())
    assert "- ІНШЕ ДЖЕРЕЛО — ТА САМА ПОДІЯ: якщо в HISTORY вже є новина" in captured["prompt"]
