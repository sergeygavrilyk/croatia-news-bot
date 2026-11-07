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
    assert bot.FILTER_MODELS == ["gemini-3-flash-preview", "gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
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


def test_503_and_504_go_to_next_model_without_retry(bot, fake_gemini):
    """R-19: повтор після 503 допомагав 7 разів з 53 і списував денну квоту — тепер одразу наступна модель."""
    calls = fake_gemini(bot, {"gemini-3.8-flash": [unavailable_error()], "gemini-3.6-flash": [translations(0)]})
    bot.generate_news_texts_json("p", {0}, bot._empty_stats())
    assert calls == ["gemini-3.8-flash", "gemini-3.6-flash"]
    calls = fake_gemini(bot, {"gemini-3.8-flash": [deadline_error()], "gemini-3.6-flash": [translations(0)]})
    bot.generate_news_texts_json("p", {0}, bot._empty_stats())
    assert calls == ["gemini-3.8-flash", "gemini-3.6-flash"]


def test_connection_reset_and_wrong_ids_still_retry_same_model(bot, fake_gemini):
    calls = fake_gemini(bot, {"gemini-3.8-flash": [ConnectionError("Connection reset by peer"), translations(0)]})
    bot.generate_news_texts_json("p", {0}, bot._empty_stats())
    assert calls == ["gemini-3.8-flash", "gemini-3.8-flash"]
    calls = fake_gemini(bot, {"gemini-3.8-flash": [translations(5, 6), translations(0)]})
    bot.generate_news_texts_json("p", {0}, bot._empty_stats())
    assert calls == ["gemini-3.8-flash", "gemini-3.8-flash"]


def test_all_models_failed_raises(bot, fake_gemini):
    fake_gemini(bot, {"gemini-3.8-flash": [quota_error()], "gemini-3.6-flash": [quota_error()],
                      "gemini-3.5-flash-lite": [quota_error()]})
    with pytest.raises(RuntimeError):
        bot.generate_news_texts_json("p", {0}, bot._empty_stats())


def test_translation_prompt_has_no_update_rule(bot):
    """R-12: «оновлено» визначає фільтр, правило 10 з промпту перекладу прибрано, 11–12 → 10–11."""
    prompt = bot._build_translation_prompt([{"title": "T", "description": "D", "url": "https://a.hr/1"}])
    assert "ОНОВЛЕНО" not in prompt and "ОНОВЛЕННЯ ПОДІЙ" not in prompt
    assert "\n10. ДОВЖИНА:" in prompt and "\n11. ID:" in prompt and "\n12." not in prompt


def test_translation_prompt_owner_edits_31_10(bot):
    """Правки власника 31.10 (CODE_REVIEW §5): порядок значків, 800 символів з фото, без лапок у правилі 4."""
    prompt = bot._build_translation_prompt([{"title": "T", "description": "D", "url": "https://a.hr/1"}])
    assert "спершу 🇺🇦, потім 📄" in prompt
    assert "не довшим за 800 символів" in prompt and "900" not in prompt
    assert "мови. УВАЖНО ПЕРЕВІРЯЙ ОРФОГРАФІЮ" in prompt and "літери.»" not in prompt


def test_translation_prompt_owner_edits_05_11(bot):
    """R-23 (05.11): правило 3 — 🇺🇦 лише коли Україна головна тема; правило 9 — хештег країни події."""
    prompt = bot._build_translation_prompt([{"title": "T", "description": "D", "url": "https://a.hr/1"}])
    assert ("— ГОЛОВНА ТЕМА новини, обов'язково постав емодзі 🇺🇦 на самий початок заголовка та винеси цю "
            "інформацію у перший абзац. Якщо Україну лише згадано побіжно (у переліку країн, одним реченням) — "
            "НЕ став 🇺🇦 і НЕ змінюй заголовок чи головну тему: головна подія оригіналу лишається в заголовку й "
            "першому абзаці, а згадка про Україну — на своєму місці.") in prompt
    assert "щоб привернути увагу" not in prompt and "Якщо в оригінальній новині згадується Україна" not in prompt
    assert ("(наприклад, #Hrvatska). Якщо подія в іншій країні — ця країна латиницею (наприклад, #CrnaGora, "
            "#Ukrajina, #Srbija), а не #Hrvatska.") in prompt


@pytest.mark.parametrize("hashtags", ["#CrnaGora #Політика", "#Ukrajina #Політика", "#Srbija #Суспільство"])
def test_foreign_country_first_hashtag_passes_validation(bot, hashtags):
    assert bot.validate_translation_strict(make(hashtags), ORIGINAL)


# --- R-24 (05.11): слова чужим алфавітом у перекладі ---

@pytest.mark.parametrize("content", [
    "Програма призначена для родин із چهارма та більше дітьми.",   # 4.11, lite: перське «чотири»
    "Міністр сказав שלום журналістам.",
    "Новина про 東京 і Загреб.",
])
def test_foreign_script_translation_rejected(bot, content):
    item = make("#Zadar #Суспільство", content=content)
    assert not bot.validate_translation_strict(item, ORIGINAL)
    assert item["rejection_reason"].startswith("чужий алфавіт:")


@pytest.mark.parametrize("content", [
    "Міністр Ivan Šipić сказав, що м'ясо подорожчає на 5 % 🇺🇦 📄.",
    "Компанія Apple і бренд Mercedes-Benz — в Đakovo та Čakovec.",
    "Апостроф-модифікатор: обʼєкт, памʼять; мікрограм — 5 µg.",
])
def test_latin_cyrillic_emoji_and_apostrophes_pass(bot, content):
    assert bot.validate_translation_strict(make("#Zadar #Суспільство", content=content), ORIGINAL)


def test_foreign_script_reason_in_backlog_and_report(bot, monkeypatch, tmp_path):
    import json
    json.dump([{"name": "023", "url": "https://023.hr/feed/"}], open(tmp_path / "feeds.json", "w"))
    now = 1_900_000_000
    json.dump([{"title": "Odobrene prve novčane potpore za velike obitelji", "description": "Opis " * 20,
                "url": "https://www.novilist.hr/v-1/", "image_urls": [], "source": "Zadarski list",
                "timestamp": now}], open(tmp_path / "news_backlog.json", "w"))
    monkeypatch.setattr(bot.time, "time", lambda: now + 100)
    monkeypatch.setattr(bot, "deduplicate_news", lambda a, h=None, stats=None: ([a[0]], []))
    monkeypatch.setattr(bot, "add_og_images", lambda batch, stats=None: None)
    monkeypatch.setattr(bot, "generate_news_texts_json", lambda p, ids, stats=None, id_aliases=None: [
        {"id": 0, "title": "Допомога багатодітним", "content": "Для родин із چهارма дітьми.",
         "hashtags": "#Hrvatska #Економіка"}])
    sent = []
    monkeypatch.setattr(bot, "send_to_telegram", lambda *a, **k: sent.append(a) or (True, "text"))
    stats = bot._empty_stats()
    bot.process_backlog(stats)
    backlog = json.load(open(tmp_path / "news_backlog.json"))
    assert sent == [] and backlog[0]["translation_failures"] == 1
    assert "чужий алфавіт: چهارма" in backlog[0]["last_translation_error"]
    assert stats["translation_rejected"] == ["чужий алфавіт: چهارма — Odobrene prve novčane potpore za velike obitelji"]
