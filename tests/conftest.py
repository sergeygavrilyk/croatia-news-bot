"""Спільні фікстури: бот завантажується «начисто» для кожного тесту, мережа заблокована.

Тести НЕ публікують і не звертаються до Gemini/Telegram/сайтів: усі зовнішні виклики підмінено.
Запуск: pip install -r requirements-dev.txt && pytest
"""
import importlib.util
import json
import pathlib
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _no_network(*args, **kwargs):
    raise AssertionError("тест намагався звернутися до мережі")


@pytest.fixture
def load_bot(monkeypatch, tmp_path):
    """Повертає функцію, що завантажує свіжу копію news_bot.py з потрібними змінними середовища."""
    monkeypatch.chdir(tmp_path)
    # Локальний .env власника не повинен впливати на тести (DRY_RUN, моделі тощо).
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)

    def _load(**env):
        base = dict(TELEGRAM_BOT_TOKEN="dummy", TELEGRAM_CHANNEL_ID="channel-id",
                    GEMINI_API_KEY="dummy", ADMIN_CHAT_ID="admin-chat")
        for key in ("DRY_RUN", "GEMINI_FILTER_MODELS", "GEMINI_TRANSLATE_MODELS"):
            monkeypatch.delenv(key, raising=False)
        for key, value in {**base, **env}.items():
            monkeypatch.setenv(key, value)
        spec = importlib.util.spec_from_file_location("news_bot_under_test", ROOT / "news_bot.py")
        bot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bot)
        monkeypatch.setattr(bot.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(bot.requests, "post", _no_network)
        monkeypatch.setattr(bot.requests, "get", _no_network)
        monkeypatch.setattr(bot.cloudscraper, "create_scraper", _no_network)
        return bot

    return _load


@pytest.fixture
def bot(load_bot):
    return load_bot()


class FakeResponse:
    def __init__(self, text):
        self.text = text


class FakeAPIError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code} {message}")
        self.code = code


@pytest.fixture
def fake_gemini():
    """fake_gemini(bot, {model: [відповідь | виняток, ...]}) -> список викликаних моделей."""
    def _install(bot, script):
        calls = []

        def generate_content(model, contents, config):
            calls.append(model)
            queue = script.get(model) or []
            if not queue:
                raise AssertionError(f"неочікуваний виклик моделі {model}")
            action = queue.pop(0)
            if isinstance(action, Exception):
                raise action
            return FakeResponse(action if isinstance(action, str) else json.dumps(action))

        bot.client = types.SimpleNamespace(models=types.SimpleNamespace(generate_content=generate_content))
        return calls

    return _install


def translations(*ids):
    return {"translations": [{"id": i, "title": f"Новина {i}", "content": "Текст новини.",
                              "hashtags": "#Zadar #Суспільство"} for i in ids]}


def quota_error():
    return FakeAPIError(429, "RESOURCE_EXHAUSTED. Quota exceeded for metric: generate_content_free_tier_requests")


def unavailable_error():
    return FakeAPIError(503, "UNAVAILABLE. This model is currently experiencing high demand.")


def deadline_error():
    return FakeAPIError(504, "DEADLINE_EXCEEDED. Deadline expired before operation could complete.")


class Recorder:
    """Підміна requests.post для звітів/алертів: запам'ятовує, що надіслано."""

    def __init__(self):
        self.sent = []

    def __call__(self, url, data=None, timeout=None):
        self.sent.append({"url": url, "data": data, "timeout": timeout})
        return types.SimpleNamespace(status_code=200, text="{}", json=lambda: {"ok": True, "result": {}})
