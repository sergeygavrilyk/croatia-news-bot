"""Перевірка перекладу: хештеги, довжина, трикрапки, «чужі» ID, каскад моделей."""
import pytest

from conftest import deadline_error, quota_error, translations, unavailable_error

ORIGINAL = {"title": "Naslov", "description": "Opis", "image_urls": []}


def make(hashtags, content="Текст новини про подію в місті."):
    return {"id": 0, "title": "Заголовок новини", "content": content, "hashtags": hashtags}


@pytest.mark.parametrize("source, expected", [
    ("#Korčula #Суспільство", "#Korcula #Суспільство"),
    ("#Šibenik #Кримінал", "#Sibenik #Кримінал"),
    ("#Đakovo #Економіка", "#Dakovo #Економіка"),
    ("#Zadar #Суспільство", "#Zadar #Суспільство"),
])
def test_first_hashtag_diacritics_removed(bot, source, expected):
    item = make(source)
    assert bot.validate_translation_strict(item, ORIGINAL)
    assert item["hashtags"] == expected


@pytest.mark.parametrize("hashtags", ["#Задар #Суспільство", "#Korčula #Спорт", "#Zadar", "#Zadar #A #B #C"])
def test_bad_hashtags_still_rejected(bot, hashtags):
    assert not bot.validate_translation_strict(make(hashtags), ORIGINAL)


def test_long_translation_accepted_and_trimmed_for_photo(bot, monkeypatch):
    body = ("Перше речення новини з важливою інформацією. " * 60).strip()
    item = make("#Zadar #Суспільство", content=body)
    assert bot.validate_translation_strict(item, {"title": "N", "description": "O", "image_urls": ["https://x/a.jpg"]})
    payloads = []
    monkeypatch.setattr(bot, "execute_telegram_request", lambda api, payload: payloads.append(payload) or {})
    ok, kind = bot.send_to_telegram(f"<b>{item['title']}</b>\n\n{item['content']}\n\n{item['hashtags']}",
                                    "https://www.023.hr/vijest/", "023", ["https://x/a.jpg"])
    caption = payloads[-1]["caption"]
    assert ok and kind == "image"
    assert len(bot.plain_text(caption)) <= 1024
    assert "#Zadar #Суспільство" in caption and "Повні деталі" in caption and "Джерело" in caption


def test_ellipsis_cleanup(bot):
    assert bot.clean_generated_field("Вулиця… закрита [...] і [ще…]") == "Вулиця. закрита . і [ще.]"


def test_remap_translation_ids(bot):
    remap = bot._remap_translation_ids
    assert remap([1587601], {0}, {}) == [0]
    assert remap([0, 1587601, 2], {0, 1, 2}, {1587601: 1}) == [0, 1, 2]
    assert remap([0, 999, 2], {0, 1, 2}, {}) is None
    assert remap([0, 0, 2], {0, 1, 2}, {}) is None
    assert remap([0, 1], {0, 1, 2}, {}) is None


def test_url_number_aliases(bot):
    batch = [{"url": "https://www.023.hr/2026/10/30/zff/"},
             {"url": "https://slobodnadalmacija.hr/zadar/komunalci-1587601"},
             {"url": "https://www.novilist.hr/2026/10/30/unija/?p=48213"}]
    aliases = bot._url_number_aliases(batch)
    assert aliases.get(1587601) == 1 and aliases.get(48213) == 2
    assert 2026 not in aliases and all(number >= 100 for number in aliases)


def test_default_cascades(bot):
    assert bot.FILTER_MODELS == ["gemini-3-flash-preview", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
    assert bot.TRANSLATE_MODELS == ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"]
    assert bot.GEMINI_TIMEOUT_MS == 90_000


def test_cascades_from_github_variables(load_bot):
    bot = load_bot(GEMINI_FILTER_MODELS=" gemini-a , gemini-b ,", GEMINI_TRANSLATE_MODELS="")
    assert bot.FILTER_MODELS == ["gemini-a", "gemini-b"]
    assert bot.TRANSLATE_MODELS[0] == "gemini-3.8-flash"


def test_url_number_as_id_accepted_without_fallback(bot, fake_gemini):
    calls = fake_gemini(bot, {"gemini-3.8-flash": [translations(1587601)]})
    result = bot.generate_news_texts_json("p", {0}, bot._empty_stats())
    assert [t["id"] for t in result] == [0] and calls == ["gemini-3.8-flash"]


def test_quota_skips_model_for_the_rest_of_run(bot, fake_gemini):
    stats = bot._empty_stats()
    calls = fake_gemini(bot, {"gemini-3.8-flash": [quota_error()],
                              "gemini-3.6-flash": [translations(0), translations(0)]})
    bot.generate_news_texts_json("p", {0}, stats)
    bot.generate_news_texts_json("p", {0}, stats)
    assert calls == ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.6-flash"]


def test_503_retries_same_model_504_does_not(bot, fake_gemini):
    calls = fake_gemini(bot, {"gemini-3.8-flash": [unavailable_error(), translations(0)]})
    bot.generate_news_texts_json("p", {0}, bot._empty_stats())
    assert calls == ["gemini-3.8-flash", "gemini-3.8-flash"]
    calls = fake_gemini(bot, {"gemini-3.8-flash": [deadline_error()], "gemini-3.6-flash": [translations(0)]})
    bot.generate_news_texts_json("p", {0}, bot._empty_stats())
    assert calls == ["gemini-3.8-flash", "gemini-3.6-flash"]


def test_all_models_failed_raises(bot, fake_gemini):
    fake_gemini(bot, {"gemini-3.8-flash": [quota_error()], "gemini-3.6-flash": [quota_error()],
                      "gemini-3.5-flash-lite": [quota_error()]})
    with pytest.raises(RuntimeError):
        bot.generate_news_texts_json("p", {0}, bot._empty_stats())
