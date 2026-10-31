"""Відсів кодом і редакторський фільтр Gemini."""
import json

import pytest


@pytest.mark.parametrize("title, url, expected", [
    ("Košarkaši Zadra slavili u derbiju protiv Splita", "https://a.hr/x", "košarkaši"),
    ("Nogometaši Zadra slavili u derbiju protiv Splita", "https://a.hr/x", "nogometaši"),
    ("Novi most u Zadru otvoren za promet od danas", "https://a.hr/sport/x", "URL /sport/"),
    ("UŽAS U ZADRU", "https://a.hr/x", "короткий текст"),
    ("Novi vrtić u Zadru otvara vrata za 120 djece", "https://a.hr/x", None),
])
def test_quality_rejection_reason(bot, title, url, expected):
    description = title + " Opis vijesti iz Zadra s dovoljno teksta."
    assert bot.quality_rejection_reason(title, description, url) == expected
    assert bot.is_quality_article(title, description, url) == (expected is None)


def test_rule_breakdown(bot):
    stats = bot._empty_stats()
    for rule in ("foto", "foto", "sport"):
        bot._count_code_rejection(stats, rule)
    assert stats["funnel"]["rejected_by_keywords"] == 3
    assert bot._rule_breakdown(stats["funnel"]) == " (foto 2, sport 1)"


class FilterResult:
    def __init__(self, keep, rejected):
        self.keep, self.rejected = keep, rejected


def run_filter(bot, monkeypatch, keep, rejected_ids):
    captured = {}
    rejected = [bot.RejectedItem(id=i, entities_found=["x"], reason="Спорт") for i in rejected_ids]

    def fake_call(prompt, stats=None):
        captured["prompt"] = prompt
        return FilterResult(keep, rejected)

    monkeypatch.setattr(bot, "call_gemini_json", fake_call)
    articles = [{"title": f"Vijest {i}", "description": "Riječ " * 1000, "url": f"https://a.hr/{i}"} for i in range(3)]
    stats = bot._empty_stats()
    result = bot.deduplicate_news(articles, [], stats)
    return result, stats, captured, articles


def test_filter_gets_short_description(bot, monkeypatch):
    _, _, captured, articles = run_filter(bot, monkeypatch, [0], [1, 2])
    data = json.loads(captured["prompt"].split("CANDIDATES:\n", 1)[1].split("\n\nHISTORY:", 1)[0])
    assert all(len(item["description"]) <= 602 for item in data)
    assert len(articles[0]["description"]) == 6000


def test_unclassified_candidates_do_not_stop_the_run(bot, monkeypatch):
    (kept, rejected), stats, _, _ = run_filter(bot, monkeypatch, [0], [1])
    assert len(kept) == 1 and len(rejected) == 1
    assert stats["funnel"]["unclassified_by_ai"] == 1


@pytest.mark.parametrize("keep, rejected_ids", [([], []), ([0, 0], []), ([0], [0]), ([7], [])])
def test_invalid_filter_answer_fails_closed(bot, monkeypatch, keep, rejected_ids):
    monkeypatch.setattr(bot, "send_alert_to_admin", lambda message: None)
    with pytest.raises(RuntimeError):
        run_filter(bot, monkeypatch, keep, rejected_ids)
