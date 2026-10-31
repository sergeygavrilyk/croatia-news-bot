"""R-11 (31.10.2026): стоп-слова без хибних спрацювань — і далі відсіюють справжній спорт і рекламу."""
import pytest

from conftest import Recorder

DESCRIPTION = " Opis vijesti iz Zadra s dovoljno teksta za provjeru."


@pytest.mark.parametrize("title", [
    "Gradonačelnik Zadra oglasio se nakon nesreće na Poluotoku",
    "FOTO Veliki požar kod Zadra, vatrogasci na terenu",
    "VIDEO Snimka nevremena u Zadru: ulice pod vodom",
    "Novi zakon: kazneni bodovi za vozače od 1. siječnja",
    "Promocija knjige zadarskog autora u Gradskoj knjižnici",
    "U Zadru otvoreno novo sportsko igralište za djecu",
    "Počela obnova sportske dvorane na Višnjiku",
])
def test_no_longer_rejected(bot, title):
    assert bot.quality_rejection_reason(title, title + DESCRIPTION, "https://a.hr/vijesti/1") is None


@pytest.mark.parametrize("title, rule", [
    ("Zadarski sportaš osvojio zlato na prvenstvu Europe", "sportaš"),
    ("Hajduk i Dinamo igraju derbi u subotu navečer", "derbi"),
    ("Plaćeni oglas: najbolje ponude u trgovini", "plaćeni oglas"),
    ("PROMO Velika akcija u trgovačkom centru Zadar", "promo"),
    ("Mali oglasi: prodaja stanova u Zadru ovaj tjedan", "mali oglasi"),
    ("Sponzorirani članak o novoj ponudi banke", "sponzorirani"),
    ("Tjedni horoskop za sve znakove zodijaka", "horoskop"),
    ("Što je novo u svijetu sporta ovog vikenda", "sporta"),
])
def test_still_rejected(bot, title, rule):
    assert bot.quality_rejection_reason(title, title + DESCRIPTION, "https://a.hr/vijesti/1") == rule


def test_translation_sports_check_keeps_adjectives(bot):
    item = {"id": 0, "title": "У Задарі відкрили новий спортивний майданчик",
            "content": "Дитячий спортивний майданчик відкрили в районі Білі Бриг.", "hashtags": "#Zadar #Суспільство"}
    assert bot.validate_translation_strict(item, {"title": "Novo igralište", "description": "Opis", "image_urls": []})
    item = {"id": 0, "title": "Новини спорту", "content": "Результати матчу.", "hashtags": "#Zadar #Суспільство"}
    assert not bot.validate_translation_strict(item, {"title": "N", "description": "O", "image_urls": []})


def test_code_rejections_are_logged_with_titles(bot, monkeypatch):
    stats = bot._empty_stats()
    for i in range(25):
        bot._count_code_rejection(stats, "derbi", f"Naslov {i}")
    examples = stats["funnel"]["rejected_by_code_examples"]
    assert len(examples) == bot.CODE_REJECTION_EXAMPLES_LIMIT and examples[0] == "[derbi] Naslov 0"
    recorder = Recorder()
    monkeypatch.setattr(bot.requests, "post", recorder)
    bot.DRY_RUN = True
    bot.send_dry_run_filter_report([], [], [], stats)
    text = recorder.sent[-1]["data"]["text"]
    assert "Відсіяно кодом (до Gemini)" in text and "[derbi] Naslov 0" in text
