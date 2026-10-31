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
    def __init__(self, keep, rejected, updates=None):
        self.keep, self.rejected, self.updates = keep, rejected, updates or []


def run_filter(bot, monkeypatch, keep, rejected_ids, updates=None, articles=None):
    captured = {}
    rejected = [bot.RejectedItem(id=i, entities_found=["x"], reason="Спорт") for i in rejected_ids]

    def fake_call(prompt, stats=None):
        captured["prompt"] = prompt
        return FilterResult(keep, rejected, updates)

    monkeypatch.setattr(bot, "call_gemini_json", fake_call)
    if articles is None:
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


# --- R-12: фіксовані категорії відхилення і позначка «оновлено» від фільтра ---

def test_rejection_reason_is_a_fixed_category(bot):
    assert len(bot.REJECTION_CATEGORIES) == 8
    item = bot.RejectedItem(id=0, entities_found=[], reason="Побут інших міст", detail="Дубровник")
    assert item.detail == "Дубровник"
    assert bot.RejectedItem(id=0, entities_found=[], reason="Геоблок").detail == ""
    with pytest.raises(Exception):
        bot.RejectedItem(id=0, entities_found=[], reason="Побут чужого міста (Госпич)")


def test_filter_response_updates_default_empty(bot):
    assert bot.NewsFilterResponse(keep=[0], rejected=[]).updates == []


def test_filter_prompt_lists_categories_and_updates(bot, monkeypatch):
    _, _, captured, _ = run_filter(bot, monkeypatch, [0], [1, 2])
    prompt = captured["prompt"]
    assert '"updates"' in prompt and 'додавай також у масив "updates"' in prompt
    for category in bot.REJECTION_CATEGORIES:
        assert f'"{category}"' in prompt


def test_updates_mark_only_kept_articles(bot, monkeypatch):
    articles = [{"title": f"Vijest {i}", "description": "Opis", "url": f"https://a.hr/{i}"} for i in range(3)]
    articles[1]["is_update"] = True  # застаріла позначка з попереднього запуску
    (kept, _), stats, _, _ = run_filter(bot, monkeypatch, [0, 1], [2], updates=[0, 2], articles=articles)
    assert kept[0].get("is_update") is True
    assert "is_update" not in kept[1]       # фільтр цього разу не позначив — позначку знято
    assert "is_update" not in articles[2]   # ID поза keep ігнорується
    assert stats["funnel"]["updates_marked"] == 1


def test_rejection_stats_by_category_and_detail(bot, monkeypatch):
    def fake_call(prompt, stats=None):
        return FilterResult([0], [
            bot.RejectedItem(id=1, entities_found=["Split"], reason="Побут інших міст", detail="Спліт"),
            bot.RejectedItem(id=2, entities_found=[], reason="Побут інших міст"),
        ])

    monkeypatch.setattr(bot, "call_gemini_json", fake_call)
    articles = [{"title": f"Vijest {i}", "description": "Opis", "url": f"https://a.hr/{i}"} for i in range(3)]
    stats = bot._empty_stats()
    bot.deduplicate_news(articles, [], stats)
    assert stats["funnel"]["rejected_by_reason"] == {"Побут інших міст": 2}
    assert "→ Побут інших міст (Спліт)" in stats["funnel"]["rejection_reasons_list"][0]
    assert bot._rule_breakdown(stats["funnel"], "rejected_by_reason") == " (Побут інших міст 2)"


@pytest.mark.parametrize("text, expected", [
    ("Migranti uhićeni na granici kod Bajakova", False),
    ("Humanitarna akcija za beskućnike u Zadru", False),
    ("Izbjeglice iz Ukrajine dobivaju novu potporu", True),
    ("Privremena zaštita produljena do 2027.", True),
])
def test_ukraine_priority_keywords(bot, text, expected):
    assert bot.is_ukraine_priority({"title": text, "description": ""}) is expected


def test_filter_prompt_owner_edits_31_10(bot, monkeypatch):
    _, _, captured, _ = run_filter(bot, monkeypatch, [0], [1, 2])
    prompt = captured["prompt"]
    assert "Кожен ID з CANDIDATES має бути рівно в одному з масивів" in prompt
    assert "Межа: великий кримінал, політичні рішення, ціни й закони для всієї країни" in prompt
