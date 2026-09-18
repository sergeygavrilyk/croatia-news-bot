import pytest
from unittest.mock import patch, MagicMock

# Імпортуємо функції з вашого основного файлу
from news_bot import (
    generate_hashtags,
    is_quality_article,
    prioritize_ukraine_news,
    get_source_name
)

class TestBotLogic:
    
    # --- 1. Тестування генерації хештегів ---
    def test_generate_hashtags_ukraine(self):
        item = {"title": "Zelenski poslao poruku", "description": "", "source": ""}
        tags = generate_hashtags(item)
        assert "#Україна" in tags

    def test_generate_hashtags_zadar(self):
        item = {"title": "Prometna nesreća u centru", "description": "", "source": "057info"}
        tags = generate_hashtags(item)
        assert "#Zadar" in tags

    def test_generate_hashtags_refugees(self):
        item = {"title": "Nova direktiva o zaštiti", "description": "privremena zaštita produljena", "source": ""}
        tags = generate_hashtags(item)
        assert "#ТимчасовийЗахист" in tags

    def test_generate_hashtags_empty(self):
        item = {"title": "Neka obicna vijest", "description": "Samo tekst", "source": "Izvor"}
        assert generate_hashtags(item) == ""

    # --- 2. Тестування фільтру якості новин (Anti-Spam) ---
    def test_is_quality_article_valid(self):
        title = "Ovo je potpuno validan i normalan naslov vijesti"
        description = "Ovdje ide neki duzi tekst koji ima vise od dvadeset znakova i sve je u redu."
        url = "https://example.com/vijesti/clanak-1"
        assert is_quality_article(title, description, url) is True

    def test_is_quality_article_invalid_category(self):
        # Повинен відхиляти спортивні URL
        assert is_quality_article("Dobar naslov", "Dobar opis", "https://example.com/sport/utakmica") is False

    def test_is_quality_article_spam_keyword(self):
        # Повинен відхиляти промо та рекламу
        assert is_quality_article("Ovo je sponzorirani članak", "Neki tekst ovdje", "https://example.com/1") is False

    def test_is_quality_article_caps_lock(self):
        # Повинен відхиляти жовту пресу (великі літери)
        assert is_quality_article("OVO JE NASLOV KOJI VIČE", "Normalan tekst", "https://example.com/1") is False

    def test_is_quality_article_too_short(self):
        # Повинен відхиляти занадто короткі заголовки
        assert is_quality_article("Kratko", "Opis", "https://example.com/1") is False

    # --- 3. Тестування пріоритезації українських новин ---
    def test_prioritize_ukraine_news(self):
        backlog = [
            {"title": "Lokalna vijest o vremenu", "description": "Kisa ce padati sutra.", "url": "1"},
            {"title": "Zelenski stigao u posjet", "description": "Sastanak na vrhu.", "url": "2"},
            {"title": "Rezultati izbora", "description": "Lokalni izbori.", "url": "3"}
        ]
        
        sorted_backlog = prioritize_ukraine_news(backlog)
        
        # Українська новина має бути ПЕРШОЮ у списку
        assert sorted_backlog[0]["url"] == "2"
        # Загальна кількість новин не повинна змінитися
        assert len(sorted_backlog) == 3
        
    # --- 4. Тестування форматування назв джерел ---
    def test_get_source_name(self):
        assert get_source_name("https://www.antenazadar.hr/clanak/123") == "Antenazadar.hr"
        assert get_source_name("https://slobodnadalmacija.hr/vijesti") == "Slobodnadalmacija.hr"
        assert get_source_name("invalid_url") == "Джерело"