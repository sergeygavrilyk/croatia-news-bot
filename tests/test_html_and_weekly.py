"""T-22: виправлення HTML перекладу замість відхилення; I-04: тижневий звіт."""
import calendar
import time

import pytest

from conftest import Recorder

NOW = 1_900_000_000  # 2030-03-17, неділя (UTC)


# --- безпечний HTML ---

@pytest.mark.parametrize("raw, fixed", [
    ("<b>жирний <i>курсив</b> далі", "<b>жирний <i>курсив</i></b> далі"),
    ("Текст <b>незакритий", "Текст <b>незакритий</b>"),
    ("зайвий </i> закривальний", "зайвий  закривальний"),
    ('<a href="x">лінк</a> і <code>код</code>', "лінк і код"),
    ("5 < 7 & 8 > 6, &amp; лишається", "5 &lt; 7 &amp; 8 &gt; 6, &amp; лишається"),
    ('<b class="x">атрибут</b>', "<b>атрибут</b>"),
])
def test_sanitize_translation_html(bot, raw, fixed):
    assert bot.sanitize_translation_html(raw) == fixed
    assert bot.sanitize_translation_html(fixed) == fixed  # повторний виклик нічого не змінює
    assert bot.html_fragment_is_valid(fixed)


def translation(**kw):
    base = {"id": 0, "title": "Новина про Задар", "content": "Текст новини про місто.",
            "hashtags": "#Zadar #Суспільство"}
    return {**base, **kw}


ARTICLE = {"title": "Vijest iz Zadra", "description": "Opis vijesti", "url": "https://a.hr/1"}


def test_broken_html_is_repaired_not_rejected(bot):
    item = translation(content="Мерія <b>Задара оголосила ремонт.")
    assert bot.validate_translation_strict(item, ARTICLE)
    assert item["content"] == "Мерія <b>Задара оголосила ремонт.</b>"


def test_tags_in_title_are_removed(bot):
    item = translation(title="<b>Новина</b> про Задар")
    assert bot.validate_translation_strict(item, ARTICLE)
    assert item["title"] == "Новина про Задар"
    assert not bot.validate_translation_strict(translation(title="<b></b>"), ARTICLE)


# --- тижневий звіт ---

def run_at(seconds_ago, **extra):
    return {"timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(NOW - seconds_ago)),
            "errors_count": 0, "execution_time": 100, **extra}


def test_weekly_report_due_only_first_monday_run(bot, monkeypatch):
    monday = calendar.timegm((2030, 3, 18, 8, 1, 0))  # понеділок, 08:01 UTC
    assert time.gmtime(monday).tm_wday == 0
    assert bot.weekly_report_due([run_at(3600)], monday)
    today_run = {"timestamp": time.strftime("%Y-%m-%d 07:01:00", time.gmtime(monday))}
    assert not bot.weekly_report_due([today_run], monday)
    assert not bot.weekly_report_due([], NOW)  # неділя
    monkeypatch.setattr(bot, "DRY_RUN", True)
    assert not bot.weekly_report_due([], monday)


def test_build_weekly_report(bot):
    runs = [
        run_at(9 * 24 * 3600, published={"total": 50}),  # старше тижня — не враховується
        run_at(2 * 24 * 3600, errors_count=1,
               published={"total": 3, "with_images": 2, "og_images_found": 1, "ukraine_priority": 1,
                          "sources": {"023": 2, "Slobodna dalmacija": 1}},
               funnel={"scraped_total": 40, "already_seen": 10, "already_rejected": 4,
                       "rejected_by_keywords": 5, "rejected_by_rule": {"foto": 3, "sport": 2},
                       "duplicates_removed_by_ai": 12, "rejected_by_reason": {"Побут інших міст": 8, "Геоблок": 4},
                       "scraped_by_source": {"023": 15, "Slobodna dalmacija": 25}},
               model_attempts=[{"task": "filter", "model": "gemini-3-flash-preview", "outcome": "unavailable"},
                               {"task": "filter", "model": "gemini-3.6-flash", "outcome": "ok"}],
               exhausted_models=["gemini-3.8-flash"]),
        run_at(3600, published={"total": 1, "sources": {"023": 1}},
               funnel={"scraped_by_source": {"023": 15}},
               source_errors={"Zadarski list": "timeout"}, backlog={"total_size": 7}),
    ]
    report = bot.build_weekly_report(runs, NOW)
    assert "Запусків: 2 | з помилками: 1" in report
    assert "Опубліковано: 4</b> (з фото 50%, з них зі сторінки статті 1)" in report
    assert "▪️ 023 — 3 (75%)" in report
    assert "з них відхилені фільтром раніше: 4" in report
    assert "Відсіяно кодом: 5 — foto 3 · sport 2" in report
    assert "Побут інших міст 8 (67%) · Геоблок 4 (33%)" in report
    assert "(категорії — за 1 з 2 запусків)" in report
    assert "3-flash-preview — виконала 0% | спроб 1, вдалих 0 (0%) | 503×1" in report
    assert "⛽ Вичерпані квоти: 3.8-flash — у 1 з 7 днів" in report
    assert "🔴 Zadarski list" in report and "🟢 023" in report
    assert "Беклог на останній запуск: 7" in report
    assert bot.html_fragment_is_valid(report)  # лише <b>/<i>, усе закрито


def test_build_weekly_report_empty(bot):
    assert "немає запусків" in bot.build_weekly_report([], NOW)


def test_weekly_report_cli_sends_only_report(bot, monkeypatch, tmp_path):
    recorder = Recorder()
    monkeypatch.setattr(bot.requests, "post", recorder)
    monkeypatch.setattr(bot.sys, "argv", ["news_bot.py", "--weekly-report"])
    monkeypatch.setattr(bot, "scrape_feeds", lambda stats=None: pytest.fail("новини не збираються"))
    with pytest.raises(SystemExit) as exc:
        bot.main()
    assert exc.value.code == 0
    assert len(recorder.sent) == 1 and "на вимогу" in recorder.sent[0]["data"]["text"]
    assert not (tmp_path / bot.ANALYTICS_FILE).exists()
