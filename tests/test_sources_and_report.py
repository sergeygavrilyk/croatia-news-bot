"""Фото зі сторінки, стан джерел, звіт адміну."""
import json
import types

from conftest import Recorder

PAGE = ('<meta property="og:image" content="/media/korcula.jpg">'
        '<meta name="twitter:image" content="https://x/t.jpg">')


def test_find_og_image(bot):
    assert bot.find_og_image(PAGE, "https://sd.hr/a/1") == "https://sd.hr/media/korcula.jpg"
    assert bot.find_og_image('<meta content="https://x/i.webp" property="og:image">'
                             '<meta property="og:image:url" content="https://x/i.jpg">', "https://x/") == "https://x/i.jpg"
    assert bot.find_og_image('<meta property="og:image" content="https://x/static/logo.png">', "https://x/") is None


def test_add_og_images_only_for_items_without_photo(bot, monkeypatch):
    class Scraper:
        def get(self, url, timeout=10):
            if url.endswith("err"):
                raise RuntimeError("timeout")
            return types.SimpleNamespace(status_code=200, text=PAGE if url.endswith("/1") else "")

    monkeypatch.setattr(bot.cloudscraper, "create_scraper", lambda **kw: Scraper())
    batch = [{"url": "https://a/1", "image_urls": []}, {"url": "https://a/2", "image_urls": ["https://rss/p.jpg"]},
             {"url": "https://a/err", "image_urls": []}]
    stats = bot._empty_stats()
    bot.add_og_images(batch, stats)
    assert batch[0]["image_urls"] == ["https://a/media/korcula.jpg"]
    assert batch[1]["image_urls"] == ["https://rss/p.jpg"] and batch[2]["image_urls"] == []
    assert stats["published"]["og_images_found"] == 1


def test_empty_feed_is_source_error(bot, monkeypatch, tmp_path):
    class Scraper:
        def get(self, url, headers=None, timeout=15):
            return types.SimpleNamespace(status_code=200, text="<html/>", raise_for_status=lambda: None)

    monkeypatch.setattr(bot.cloudscraper, "create_scraper", lambda **kw: Scraper())
    monkeypatch.setattr(bot.feedparser, "parse", lambda text: types.SimpleNamespace(entries=[]))
    json.dump([{"name": "023", "url": "https://023.hr/feed/"}], open(tmp_path / "feeds.json", "w"))
    stats = bot._empty_stats()
    bot.scrape_feeds(stats)
    assert stats["errors_count"] == 1 and "порожня стрічка" in stats["source_errors"]["023"]


def failed(*names):
    return {"source_errors": {name: "e" for name in names}}


def test_source_health_alerts(bot):
    current = {**failed("023"), "funnel": {"scraped_by_source": {"SD": 15}}}
    assert bot.check_source_health(current, [{}, failed("023")]) == []
    alerts = bot.check_source_health(current, [{}, failed("023"), failed("023")])
    assert len(alerts) == 1 and "3 запуски поспіль: 023" in alerts[0]
    assert bot.check_source_health(current, [failed("023")] * 3) == []
    back = {"funnel": {"scraped_by_source": {"023": 10}}}
    assert "знову працює: 023" in bot.check_source_health(back, [{}] + [failed("023")] * 3)[0]
    assert bot.check_source_health(back, [{"timestamp": "old"}]) == []


def test_admin_report_lines(bot, monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(bot.requests, "post", recorder)
    stats = bot._empty_stats()
    stats["funnel"].update(rejected_by_keywords=3, rejected_by_rule={"foto": 2, "sport": 1}, unclassified_by_ai=1)
    stats["source_errors"] = {"023": "порожня стрічка"}
    stats["published"]["og_images_found"] = 2
    stats["models_by_task"] = {"filter": "gemini-3-flash-preview", "translate": "gemini-3.6-flash"}
    stats["exhausted_models"] = ["gemini-3.8-flash"]
    stats["model_attempts"] = [{"task": "translate", "model": "gemini-3.8-flash", "outcome": "quota"},
                               {"task": "translate", "model": "gemini-3.6-flash", "outcome": "ok"}]
    bot.send_analytics_to_admin(stats)
    text = recorder.sent[-1]["data"]["text"]
    for expected in ("Відсіяно кодом: 3 (foto 2, sport 1)", "Gemini не класифікував (лишились у черзі): 1",
                     "🔴 Недоступні джерела: 023 (порожня стрічка)", "зі сторінки статті: 2",
                     "фільтр — 3-flash-preview | переклад — 3.6-flash", "⛽ Квоту вичерпано: gemini-3.8-flash",
                     "🧾 Спроби: 3.8-flash ⛽ | 3.6-flash ✅"):
        assert expected in text
    assert recorder.sent[-1]["timeout"] == bot.TELEGRAM_TIMEOUT_SECONDS
