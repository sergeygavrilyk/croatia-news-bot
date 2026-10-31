"""Croatia News Bot — hardened version.

Required environment variables:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, GEMINI_API_KEY
Optional:
  ADMIN_CHAT_ID
  GEMINI_FILTER_MODELS, GEMINI_TRANSLATE_MODELS — model cascades, names separated by commas
    in priority order (GitHub -> Settings -> Secrets and variables -> Actions -> Variables);
    if not set, the defaults below are used.
  DRY_RUN=1 — test run: the full cycle runs (RSS, Gemini filter and translation are real),
    but posts go to ADMIN_CHAT_ID marked "ТЕСТ" and the state files are not changed.
    Requires ADMIN_CHAT_ID; TELEGRAM_CHANNEL_ID is not needed.

Keep feeds.json and the state files in the working directory. The script intentionally
fails closed on invalid editorial-filter responses and preserves unprocessed backlog items.
"""

import calendar
import html
import json
import logging
import os
import random
import re
import shutil
import sys
import tempfile
import time
import unicodedata
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import cloudscraper
import feedparser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai
from google.genai import types
from typing import Literal, get_args

from pydantic import BaseModel, Field
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
feedparser.USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")

# T-05: тестовий запуск — увесь цикл справжній (RSS, фільтр і переклад Gemini), але пости
# йдуть в адмін-чат з позначкою «ТЕСТ», а файли стану в репозиторії не змінюються.
DRY_RUN = os.getenv("DRY_RUN", "").strip().lower() in ("1", "true", "yes")
DRY_RUN_HEADER = "🧪 <b>ТЕСТ — не опубліковано в каналі</b>\n\n"
DRY_RUN_POST_DELAY_SECONDS = 10

HISTORY_FILE = "posted_news.txt"
TITLES_FILE = "posted_titles.txt"
BACKLOG_FILE = "news_backlog.json"
FEEDS_FILE = "feeds.json"
ANALYTICS_FILE = "analytics.json"

MAX_HISTORY_SIZE = 1000
MAX_AGE_SECONDS = 12 * 60 * 60
BATCH_SIZE = 3
PRE_TRANSLATION_DELAY_SECONDS = 15
POST_PUBLICATION_DELAY_SECONDS = 45
MAX_CANDIDATES_PER_FILTER = 25
MAX_TRANSLATION_FAILURES = 5
# L5 (CODE_REVIEW): фільтру достатньо заголовка й початку опису; переклад отримує повний текст
FILTER_DESCRIPTION_CHARS = 600
RETRY_COOLDOWN_SECONDS = 60 * 60

# --- Моделі Gemini (T-04): окремий каскад для фільтра й перекладу, як у молдовському боті ---
# На безкоштовному тарифі в кожної моделі своя денна квота (flash-моделі ~20 запитів/добу,
# lite-моделі ~500), тому моделі використовуються по черзі: перша, що відповіла, — і далі.
# Змінити без правки коду: GitHub -> Settings -> Secrets and variables -> Actions -> Variables
# (GEMINI_FILTER_MODELS / GEMINI_TRANSLATE_MODELS, назви через кому в порядку пріоритету).
# Значення за замовчуванням — як у молдовському боті (перевірено там реальними запитами 30.10.2026):
#   фільтр — gemini-3-flash-preview (окрема flash-модель, не перетинається з моделями перекладу),
#            lite-моделі як резерв; переклад — flash-моделі, lite лише останній резерв
#            (lite помітно гірше перекладає).
# НЕ використовувати gemini-3.5-flash і gemini-3.7-flash: зняті з обігу 08.10.2026 і переадресовуються
# на 3.6-flash / 3.8-flash (фактично витрачають їхню квоту).
DEFAULT_FILTER_MODELS = "gemini-3-flash-preview,gemini-3.5-flash-lite,gemini-3.1-flash-lite"
DEFAULT_TRANSLATE_MODELS = "gemini-3.8-flash,gemini-3.6-flash,gemini-3.5-flash-lite"
# Таймаут одного запиту: модель, що «зависла», не блокує запуск — каскад перейде до наступної.
# 90 с: звичайна відповідь — 5–40 с; 30.10 увечері перевантажені моделі мовчали по 3 хв (504).
GEMINI_TIMEOUT_MS = 90_000
# Таймаут запитів до Telegram API: sendPhoto за URL — Telegram сам завантажує картинку з сайту,
# це буває довше 15 с (30.10 усі запити по 10–15 с падали за таймаутом). Як у молдовському боті.
TELEGRAM_TIMEOUT_SECONDS = 60
# Джерело, яке не віддає записів (помилка або порожня стрічка) стільки запусків поспіль, —
# алерт адміну; коли знову запрацює — повідомлення «знову працює» (як D-01 у молдовському боті).
SOURCE_ALERT_AFTER_RUNS = 3


def parse_models(env_name: str, default: str) -> list[str]:
    raw = os.getenv(env_name) or default
    return [model.strip() for model in raw.split(",") if model.strip()]


FILTER_MODELS = parse_models("GEMINI_FILTER_MODELS", DEFAULT_FILTER_MODELS)
TRANSLATE_MODELS = parse_models("GEMINI_TRANSLATE_MODELS", DEFAULT_TRANSLATE_MODELS)
# Моделі, чия квота (429) вичерпана в цьому запуску, — до кінця запуску більше не викликаються.
EXHAUSTED_MODELS: set[str] = set()
client = None  # Created inside main(), after environment validation.


# ---------------------------------------------------------------------------
# Structured model responses
# ---------------------------------------------------------------------------
# R-12 (owner, 31.10.2026): a fixed list of rejection categories instead of free text.
RejectionCategory = Literal[
    "Дубль", "Вже публікувалось", "Спорт", "Реклама",
    "Побут інших міст", "Геоблок", "Світовий шум", "Не за тематикою",
]
REJECTION_CATEGORIES = get_args(RejectionCategory)
UPDATE_PREFIX = "🔄 [ОНОВЛЕНО]: "


class RejectedItem(BaseModel):
    id: int
    entities_found: list[str] = Field(
        description="3-5 ключових фігурантів, компаній, цифр або локацій"
    )
    reason: RejectionCategory = Field(
        description="Рівно одна категорія з фіксованого переліку"
    )
    detail: str = Field(
        default="",
        description="Коротке пояснення до 10 слів (місто чи подія) або порожній рядок",
    )


class NewsFilterResponse(BaseModel):
    keep: list[int]
    rejected: list[RejectedItem]
    updates: list[int] = Field(
        default_factory=list,
        description="ID з keep, які є суттєвим оновленням події з HISTORY",
    )


class TranslatedNews(BaseModel):
    id: int = Field(description="Числовий ID статті з масиву кандидатів")
    title: str = Field(description="Перекладений заголовок без HTML-тегів")
    content: str = Field(description="Основний текст новини з дозволеним HTML-форматуванням")
    hashtags: str = Field(description="2-3 хештеги одним рядком")


class TranslationResponse(BaseModel):
    translations: list[TranslatedNews]


class TranslationIdMismatch(ValueError):
    """Gemini returned translation IDs that do not match the batch and cannot be mapped back."""


class TelegramAPIError(RuntimeError):
    def __init__(self, error_code: int, description: str, retry_after: int | None = None):
        super().__init__(f"Telegram API Error {error_code}: {description}")
        self.error_code = error_code
        self.retry_after = retry_after


# ---------------------------------------------------------------------------
# General utilities
# ---------------------------------------------------------------------------
def normalize_url(url: str) -> str:
    """Normalize article URLs while preserving parameters that may identify an article."""
    try:
        parsed = urlparse(str(url).strip())
        tracking_names = {
            "fbclid", "gclid", "dclid", "msclkid", "igshid", "mc_cid", "mc_eid"
        }
        filtered_params = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            key_lower = key.lower()
            if key_lower.startswith("utm_") or key_lower in tracking_names:
                continue
            filtered_params.append((key, value))

        # Fragments do not identify a different server-side article.
        return urlunparse((
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path,
            parsed.params,
            urlencode(filtered_params, doseq=True),
            "",
        ))
    except Exception:
        return str(url)


def atomic_write_text(filename: str, content: str) -> None:
    temp_file = f"{filename}.tmp"
    try:
        with open(temp_file, "w", encoding="utf-8", newline="") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_file, filename)
    except Exception:
        try:
            if os.path.exists(temp_file):
                os.remove(temp_file)
        except OSError:
            pass
        raise


def trim_history(filename: str, max_lines: int = MAX_HISTORY_SIZE) -> None:
    try:
        if os.path.exists(filename):
            with open(filename, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > max_lines:
                atomic_write_text(filename, "".join(lines[-max_lines:]))
    except Exception:
        logging.exception("Помилка обрізки файлу історії %s", filename)


def normalize_backlog_item(item: dict) -> dict:
    if not isinstance(item, dict):
        raise ValueError("Елемент беклогу має бути JSON-об'єктом")
    required = ("url", "title", "description")
    if any(not isinstance(item.get(key), str) or not item[key].strip() for key in required):
        raise ValueError("Елемент беклогу не містить валідних url/title/description")
    item["url"] = normalize_url(item["url"])
    item.setdefault("image_urls", [])
    item.setdefault("source", "Джерело")
    item.setdefault("timestamp", time.time())
    if not isinstance(item["image_urls"], list):
        item["image_urls"] = [item["image_urls"]] if item["image_urls"] else []
    return item


def load_history(filename: str, is_urls: bool = False) -> list[str]:
    if not os.path.exists(filename):
        return []
    with open(filename, "r", encoding="utf-8") as f:
        items = [line.strip() for line in f if line.strip()]
    if is_urls:
        # Also normalize older history entries, not only new RSS URLs.
        return list(dict.fromkeys(normalize_url(item) for item in items))
    return items


def save_to_history(history_list: list[str], new_item: str, filename: str) -> list[str]:
    if filename == HISTORY_FILE:
        new_item = normalize_url(new_item)
    updated = (history_list + [str(new_item)])[-MAX_HISTORY_SIZE:]
    atomic_write_text(filename, "".join(f"{item}\n" for item in updated))
    return updated


def _read_backlog_file(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{path} має містити JSON-масив")
    return [normalize_backlog_item(item) for item in data]


def load_backlog() -> list[dict]:
    if not os.path.exists(BACKLOG_FILE):
        return []
    try:
        return _read_backlog_file(BACKLOG_FILE)
    except Exception as primary_error:
        logging.error("Не вдалося прочитати беклог: %s", primary_error)
        bak_file = f"{BACKLOG_FILE}.bak"
        if os.path.exists(bak_file):
            try:
                recovered = _read_backlog_file(bak_file)
                logging.warning("Беклог відновлено з резервної копії %s", bak_file)
                return recovered
            except Exception as backup_error:
                logging.exception("Резервна копія беклогу також пошкоджена: %s", backup_error)
        # Do not silently turn a corrupt queue into an empty queue.
        raise RuntimeError("Беклог пошкоджений, резервну копію прочитати не вдалося") from primary_error


def save_backlog(backlog: list[dict]) -> None:
    normalized = [normalize_backlog_item(dict(item)) for item in backlog]
    temp_file = f"{BACKLOG_FILE}.tmp"
    bak_file = f"{BACKLOG_FILE}.bak"

    # Do not overwrite a good backup with a malformed current file.
    if os.path.exists(BACKLOG_FILE):
        try:
            _read_backlog_file(BACKLOG_FILE)
            shutil.copy2(BACKLOG_FILE, bak_file)
        except Exception as backup_error:
            logging.warning("Не оновлюємо резервну копію з некоректного беклогу: %s", backup_error)

    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(normalized, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_file, BACKLOG_FILE)
    except Exception:
        try:
            if os.path.exists(temp_file):
                os.remove(temp_file)
        except OSError:
            pass
        logging.exception("Помилка збереження беклогу")
        raise


def load_analytics() -> list:
    if not os.path.exists(ANALYTICS_FILE):
        return []
    try:
        with open(ANALYTICS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError, TypeError):
        logging.exception("Не вдалося прочитати аналітику; буде створено новий файл")
        return []


def save_analytics(stats: dict) -> None:
    history = load_analytics()
    history.append(stats)
    temp_file = f"{ANALYTICS_FILE}.tmp"
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(history[-100:], f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_file, ANALYTICS_FILE)
    except Exception:
        try:
            if os.path.exists(temp_file):
                os.remove(temp_file)
        except OSError:
            pass
        logging.exception("Помилка збереження аналітики")
        raise


def send_alert_to_admin(message: str) -> None:
    if not ADMIN_CHAT_ID or not TELEGRAM_BOT_TOKEN:
        return
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            api_url,
            data={"chat_id": ADMIN_CHAT_ID,
                  "text": f"{'🧪 DRY_RUN — ' if DRY_RUN else ''}⚠️ ALARM: {message}"[:4000]},
            timeout=TELEGRAM_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            logging.error("Помилка відправки алерта в Telegram: %s", resp.text[:1000])
    except Exception:
        logging.exception("Збій з'єднання під час надсилання алерта адміністратору")


def _rule_breakdown(funnel: dict, key: str = "rejected_by_rule") -> str:
    """« (foto 5, sport 3, КАПС у заголовку 1)» — which code rule (or, with
    key="rejected_by_reason", which filter category) rejected how many articles."""
    by_rule = funnel.get(key) or {}
    if not by_rule:
        return ""
    parts = sorted(by_rule.items(), key=lambda kv: (-kv[1], kv[0]))
    return " (" + ", ".join(f"{rule} {count}" for rule, count in parts) + ")"


def _source_errors_line(stats: dict) -> str:
    return "; ".join(f"{name} ({error[:80]})" for name, error in (stats.get("source_errors") or {}).items())


def check_source_health(stats: dict, history: list[dict]) -> list[str]:
    """Alerts for sources that have failed SOURCE_ALERT_AFTER_RUNS runs in a row (sent once, on the
    N-th failed run) and for sources that work again after such a streak. `history` — earlier runs
    from analytics.json (oldest first). Returns the alert texts."""
    def failed(run: dict) -> set[str]:
        return set((run.get("source_errors") or {}).keys())

    failed_now = failed(stats)
    alerts = []
    for name in sorted(failed_now):
        streak = 1
        for run in reversed(history):
            if name not in failed(run):
                break
            streak += 1
        if streak == SOURCE_ALERT_AFTER_RUNS:
            alerts.append(f"🔴 Джерело не працює {streak} запуски поспіль: {name}. "
                          f"Остання помилка: {stats['source_errors'][name][:200]}")
    if history:
        working_now = set((stats.get("funnel", {}).get("scraped_by_source") or {}).keys())
        for name in sorted(failed(history[-1]) & working_now):
            streak = 0
            for run in reversed(history):
                if name not in failed(run):
                    break
                streak += 1
            if streak >= SOURCE_ALERT_AFTER_RUNS:
                alerts.append(f"✅ Джерело знову працює: {name} (після {streak} невдалих запусків)")
    return alerts


def _short_model(name: str | None) -> str:
    return (name or "—").replace("gemini-", "")


def _models_line(stats: dict) -> str:
    """«фільтр — 3-flash-preview | переклад — 3.8-flash»: which model actually answered."""
    by_task = stats.get("models_by_task") or {}
    return (f"фільтр — {_short_model(by_task.get('filter'))} | "
            f"переклад — {_short_model(by_task.get('translate'))}")


def _model_attempts_summary(stats: dict) -> str:
    """«3-flash-preview ✅ | 3.8-flash 503×2 | 3.6-flash ✅» — every model call of the run."""
    marks = {"ok": "✅", "quota": "⛽", "unavailable": "503", "timeout": "⏱",
             "bad_response": "⚠️", "error": "❌"}
    counts: dict[tuple[str, str], int] = {}
    for attempt in stats.get("model_attempts", []):
        key = (_short_model(attempt.get("model")), attempt.get("outcome", "error"))
        counts[key] = counts.get(key, 0) + 1
    return " | ".join(
        f"{model} {marks.get(outcome, outcome)}" + (f"×{n}" if n > 1 else "")
        for (model, outcome), n in counts.items()
    )


def send_analytics_to_admin(stats: dict) -> None:
    if not ADMIN_CHAT_ID or not TELEGRAM_BOT_TOKEN:
        logging.warning("ADMIN_CHAT_ID або TELEGRAM_BOT_TOKEN не налаштовано")
        return

    funnel = stats["funnel"]
    backlog_stats = stats["backlog"]
    published = stats["published"]
    scraped_src = ", ".join(f"{k}: {v}" for k, v in funnel.get("scraped_by_source", {}).items()) or "Немає"
    backlog_src = ", ".join(f"{k}: {v}" for k, v in backlog_stats["sources"].items()) or "Порожньо"
    pub_src = ", ".join(f"{k}: {v}" for k, v in published["sources"].items()) or "Немає"
    reasons = "\n".join(funnel.get("rejection_reasons_list", [])) or "Немає деталей"
    # Екрануємо можливі HTML-символи з причин відхилення
    reasons = html.escape(reasons)

    # Escape model/source-derived values before using Telegram HTML parse mode.
    message = (
        "📊 <b>Звіт Croatia News Bot</b>\n\n"
        + ("🧪 <b>ТЕСТОВИЙ ЗАПУСК (DRY_RUN)</b> — у канал нічого не опубліковано, "
           "файли стану не змінено\n\n" if DRY_RUN else "") +
        f"⏱ Час виконання: {stats['execution_time']} сек\n"
        f"⚠️ Помилок: {stats['errors_count']} | Повторних спроб Gemini: {stats['gemini_retries']}\n\n"
        "🔍 <b>Воронка:</b>\n"
        f"▪️ Переглянуто RSS-записів: {funnel['scraped_total']}\n"
        f"▪️ За джерелами: {html.escape(scraped_src)}\n"
        + (f"🔴 Недоступні джерела: {html.escape(_source_errors_line(stats))}\n"
           if stats.get("source_errors") else "") +
        f"▪️ Уже відомі/застарілі: {funnel['already_seen']}\n"
        f"▪️ Відсіяно кодом: {funnel['rejected_by_keywords']}{html.escape(_rule_breakdown(funnel))}\n"
        f"▪️ Відхилено Gemini: {funnel['duplicates_removed_by_ai']}{html.escape(_rule_breakdown(funnel, 'rejected_by_reason'))}\n"
        + (f"▪️ Позначено як оновлення: {funnel['updates_marked']}\n" if funnel.get("updates_marked") else "")
        + (f"▪️ Gemini не класифікував (лишились у черзі): {funnel['unclassified_by_ai']}\n"
           if funnel.get("unclassified_by_ai") else "")
        + "\n"
        "❌ <b>Причини відхилення:</b>\n"
        f"{reasons[:2500]}\n\n"
        f"📥 <b>Беклог: {backlog_stats['total_size']}</b>\n"
        f"▪️ {html.escape(backlog_src)}\n\n"
        f"✅ <b>{'Підготовлено (тест, не опубліковано)' if DRY_RUN else 'Опубліковано'}: {published['total']}</b>\n"
        f"▪️ Із фото: {published['with_images']} (з них зі сторінки статті: {published.get('og_images_found', 0)}) | Текстом: {published['text_only']}\n"
        f"▪️ Український пріоритет: {published['ukraine_priority']}\n"
        f"▪️ Джерела: {html.escape(pub_src)}\n\n"
        f"🤖 <b>Моделі:</b> {html.escape(_models_line(stats))}"
        + (f"\n⛽ Квоту вичерпано: {html.escape(', '.join(stats['exhausted_models']))}"
           if stats.get("exhausted_models") else "")
        + (f"\n🧾 Спроби: {html.escape(_model_attempts_summary(stats))}"
           if stats.get("model_attempts") else "")
    )
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            api_url,
            data={"chat_id": ADMIN_CHAT_ID, "text": message[:4000], "parse_mode": "HTML"},
            timeout=TELEGRAM_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            logging.error("Не вдалося надіслати аналітику: %s", resp.text[:1000])
    except Exception:
        logging.exception("Збій під час надсилання аналітики")


def get_rss_feeds() -> list[dict]:
    if not os.path.exists(FEEDS_FILE):
        raise FileNotFoundError(f"Не знайдено {FEEDS_FILE}")
    with open(FEEDS_FILE, "r", encoding="utf-8") as f:
        feeds = json.load(f)
    if not isinstance(feeds, list):
        raise ValueError(f"{FEEDS_FILE} має містити JSON-масив")
    return feeds


def get_source_name(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return host.capitalize() or "Джерело"
    except Exception:
        return "Джерело"


# --- Фото зі сторінки статті (og:image), якщо в RSS картинки немає (як у молдовському боті) ---
_OG_META_RE = re.compile(
    r"<meta\b[^>]*?\b(?:property|name)\s*=\s*[\"'](?:og:image|og:image:url|twitter:image)[\"'][^>]*>", re.I)
_CONTENT_RE = re.compile(r"\bcontent\s*=\s*[\"']([^\"']+)[\"']", re.I)
UNSUPPORTED_IMAGE_FORMATS = (".webp", ".avif", ".svg")  # Telegram їх не приймає як фото


def find_og_image(page_html: str, page_url: str) -> str | None:
    """Main article image from meta og:image / twitter:image; skips unsupported formats and logos."""
    for tag in _OG_META_RE.findall((page_html or "")[:300000]):
        match = _CONTENT_RE.search(tag)
        if not match:
            continue
        url = urljoin(page_url, html.unescape(match.group(1).strip()))
        if not url.lower().startswith(("http://", "https://")):
            continue
        path = urlparse(url).path.lower()
        if path.endswith(UNSUPPORTED_IMAGE_FORMATS) or "logo" in path:
            continue
        return url
    return None


def fetch_og_image(scraper, page_url: str, timeout: int = 10) -> str | None:
    try:
        response = scraper.get(page_url, timeout=timeout)
        if response.status_code != 200:
            return None
        return find_og_image(response.text, page_url)
    except Exception as exc:
        logging.warning("og:image не отримано для %s: %s", page_url, exc)
        return None


def add_og_images(batch: list[dict], stats: dict | None) -> None:
    """Before translation (the photo changes the length limit): a photo from the article page
    for batch articles without a picture in RSS. Saved in the backlog item, so it is fetched once."""
    scraper = None
    for item in batch:
        if item.get("image_urls"):
            continue
        try:
            scraper = scraper or cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "mobile": False})
        except Exception:
            logging.exception("og:image: не вдалося створити scraper — публікуємо без фото зі сторінки")
            return
        og_image = fetch_og_image(scraper, item["url"])
        if not og_image:
            continue
        item["image_urls"] = [og_image]
        if stats is not None:
            stats["published"]["og_images_found"] = stats["published"].get("og_images_found", 0) + 1
        logging.info("Фото зі сторінки статті: %s -> %s", item["url"], og_image)


def extract_image_urls(entry) -> list[str]:
    urls = []
    unsupported = {".webp", ".avif", ".svg"}

    for media in entry.get("media_content", []) or []:
        url = media.get("url")
        if url and url not in urls and urlparse(url).path.lower().endswith(tuple(unsupported)) is False:
            urls.append(url)

    for enclosure in entry.get("enclosures", []) or []:
        if (enclosure.get("type", "").lower().startswith("image/")):
            url = enclosure.get("href") or enclosure.get("url")
            if url and url not in urls and urlparse(url).path.lower().endswith(tuple(unsupported)) is False:
                urls.append(url)

    return urls[:10]


# ---------------------------------------------------------------------------
# Article extraction and deterministic quality checks
# ---------------------------------------------------------------------------
def fetch_full_article_text(url: str, scraper) -> str | None:
    """Extract article paragraphs without deleting every div in the article body."""
    try:
        resp = scraper.get(url, timeout=10)
        if resp.status_code != 200:
            logging.warning("HTTP %s під час завантаження статті %s", resp.status_code, url)
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        article_body = None
        host = urlparse(url).netloc.lower()

        if "slobodnadalmacija.hr" in host:
            article_body = soup.select_one(".itemFullText, .article-content")
        elif "zadarskilist.hr" in host or "novilist.hr" in host:  # статті Zadarski list — на novilist.hr
            article_body = soup.select_one(".article-body, article .content")
        elif "023.hr" in host:
            article_body = soup.select_one(".td-post-content")

        if article_body is None:
            article_body = soup.find("article") or soup.find("main")
        if article_body is None:
            article_body = soup.body
        if article_body is None:
            return None

        for unwanted in article_body.select(
            "script, style, nav, footer, aside, "
            ".related-news, .related, .recommendations, .ads, .ad, "
            ".comments, .social-share, .breadcrumb, .newsletter, .author-box"
        ):
            unwanted.decompose()

        paragraphs = []
        seen_paragraphs = set()
        for paragraph in article_body.find_all("p"):
            text = paragraph.get_text(" ", strip=True)
            if len(text) > 30 and text not in seen_paragraphs:
                paragraphs.append(text)
                seen_paragraphs.add(text)

        full_text = " ".join(paragraphs).strip()
        return full_text[:3500] if full_text else None
    except Exception:
        logging.exception("Не вдалося завантажити повний текст %s", url)
        return None


# R-11 (власник, 31.10.2026): «sport»/«спорт» — лише іменники (sport, sporta, sportaš…); прикметники
# sportska/sportsko («sportska dvorana», «sportsko igralište», «спортивний майданчик») більше не відсіюються
# кодом — спортивні новини все одно відкидає фільтр Gemini (КРОК 4.1).
SPORTS_PATTERN = re.compile(
    r"\b(?:sport(?:a|u|om|ovi|ova|ovima|aš\w*)?|šport(?:a|u|om|aš\w*)?|nogomet\w*|fudbal\w*|football\w*|"
    r"košark\w*|tenis\w*|utakmic\w*|meč\w*|turnir\w*|derbi\w*|"
    r"igrač\w*|igrači|футбол\w*|баскетбол\w*|спорт(?:у|ом|і)?|теніс\w*|"
    r"матч\w*|турнір\w*|спортсмен\w*)\b",
    re.IGNORECASE | re.UNICODE,
)
# R-11: лише явні рекламні позначки. Раніше oglas\w* ловив «oglasio se» (= «висловився»),
# promo\w* — «promocija knjige»; рекламу без позначок відкидає фільтр Gemini (КРОК 4.2).
AD_PATTERN = re.compile(
    r"\b(?:oglas|oglasi|mali\s+oglasi|sponzor\w*|promo|pr\s+članak|horoskop\w*|native\s+oglas\w*|"
    r"plaćeni\s+oglas\w*|nagradna\s+igra)\b",
    re.IGNORECASE | re.UNICODE,
)

# Keep every explicit exclusion from the original bot. These complement, rather than
# replace, the more general sport/ad patterns above.
LEGACY_UNWANTED_KEYWORDS = [
    "oglasi", "sponzorirani", "promo", "redikul", "horoskop", "plaćeni oglas",
    "nagradna igra", "native", "pr vijesti", "sport", "nogomet",
    "košarka", "tenis", "utakmica", "hnl", "liga prvaka", "derbi", "trener",
    "igrači", "kk zadar", "nk zadar", "prvenstvo", "turnir", "polufinale",
]  # R-11: прибрано "foto", "video" (позначки медіа в заголовках) і "bodovi" (штрафні бали водіїв)

def plain_text(value: str) -> str:
    return BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True)


def quality_rejection_reason(title: str, description: str, url: str) -> str | None:
    """Why the code rejects an article (None = keep). Same rules and order as before;
    the reason only feeds the admin report («Відсіяно кодом: … (foto 5, sport 3)»)."""
    lowered_url = url.lower()
    unwanted_url_categories = ("/sport/", "/lifestyle/", "/magazin/")
    for category in unwanted_url_categories:
        if category in lowered_url:
            return f"URL {category}"

    text_to_check = f"{title} {plain_text(description)} {url}"
    for pattern in (SPORTS_PATTERN, AD_PATTERN):
        match = pattern.search(text_to_check)
        if match:
            return match.group(0).casefold()
    normalized_text = text_to_check.casefold()
    for keyword in LEGACY_UNWANTED_KEYWORDS:
        if re.search(rf"\b{re.escape(keyword.casefold())}\b", normalized_text, re.IGNORECASE | re.UNICODE):
            return keyword.casefold()
    if len(title.strip()) < 15 or len(text_to_check.strip()) < 20:
        return "короткий текст"
    if "!!!" in title or "???" in title:
        return "!!!/???"

    letters = [char for char in title if char.isalpha()]
    if letters and sum(char.isupper() for char in letters) / len(letters) > 0.4:
        return "КАПС у заголовку"
    return None


def is_quality_article(title: str, description: str, url: str) -> bool:
    return quality_rejection_reason(title, description, url) is None


CODE_REJECTION_EXAMPLES_LIMIT = 20  # скільки відсіяних кодом заголовків зберігати в analytics.json за запуск


def _count_code_rejection(stats: dict | None, reason: str, title: str = "") -> None:
    logging.info("Відсіяно кодом [%s]: %s", reason, title)
    if stats is not None:
        stats["funnel"]["rejected_by_keywords"] += 1
        by_rule = stats["funnel"].setdefault("rejected_by_rule", {})
        by_rule[reason] = by_rule.get(reason, 0) + 1
        examples = stats["funnel"].setdefault("rejected_by_code_examples", [])
        if len(examples) < CODE_REJECTION_EXAMPLES_LIMIT:
            examples.append(f"[{reason}] {title[:100]}")


# ---------------------------------------------------------------------------
# Gemini requests with structured response validation and controlled retries
# ---------------------------------------------------------------------------
def _exception_status_code(exc: Exception) -> int | None:
    for attr in ("status_code", "code"):
        value = getattr(exc, attr, None)
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            pass
    return None


def is_slow_gemini_failure(exc: Exception) -> bool:
    """Timeout or 504 DEADLINE_EXCEEDED: the model was silent for a long time — no retry on it."""
    if _exception_status_code(exc) == 504:
        return True
    text = str(exc).lower()
    return ("deadline" in text or "timed out" in text or "timeout" in text
            or "timeout" in type(exc).__name__.lower())


def is_transient_gemini_error(exc: Exception) -> bool:
    code = _exception_status_code(exc)
    if code == 429 or (code is not None and code >= 500):
        return True
    msg = str(exc).lower()
    return any(term in msg for term in (
        "timeout", "timed out", "temporarily unavailable", "connection reset",
        "connection error", "resource exhausted", "rate limit", "internal error",
        "service unavailable", "bad gateway", "deadline exceeded",
    ))


def is_quota_error(exc: Exception) -> bool:
    return _exception_status_code(exc) == 429 or "RESOURCE_EXHAUSTED" in str(exc)


def classify_model_error(exc: Exception) -> str:
    """Type of a failed model call for the admin report: quota | unavailable | timeout | bad_response | error."""
    code = _exception_status_code(exc)
    text = str(exc).lower()
    if is_quota_error(exc):
        return "quota"
    if (code is not None and code >= 500) or "unavailable" in text or "high demand" in text:
        return "unavailable"
    if "timeout" in type(exc).__name__.lower() or "timed out" in text or "timeout" in text \
            or "deadline exceeded" in text:
        return "timeout"
    if isinstance(exc, ValueError):  # empty answer, invalid JSON / schema, ID mismatch
        return "bad_response"
    return "error"


def _record_model_attempt(stats: dict | None, task: str, model: str, outcome: str) -> None:
    if stats is not None:
        stats.setdefault("model_attempts", []).append({"task": task, "model": model, "outcome": outcome})


def _remap_translation_ids(ids: list[int], expected_ids: set[int],
                           id_aliases: dict[int, int] | None) -> list[int] | None:
    """Map wrong translation IDs back to batch IDs when it is unambiguous; otherwise None.

    Seen in production: the model returned the article number from the source URL
    (e.g. 1587601) instead of the batch ID 0.
    """
    if len(ids) != len(expected_ids):
        return None
    if len(expected_ids) == 1:
        # One article in the batch and one translation back: it can only be this article.
        return [next(iter(expected_ids))]
    aliases = id_aliases or {}
    mapped = [item_id if item_id in expected_ids else aliases.get(item_id) for item_id in ids]
    if None in mapped or len(set(mapped)) != len(mapped) or set(mapped) != expected_ids:
        return None
    return mapped


def _url_number_aliases(batch: list[dict]) -> dict[int, int]:
    """Numbers (>= 100) from article URL paths/queries that belong to exactly one batch article."""
    owners: dict[int, set[int]] = {}
    for idx, item in enumerate(batch):
        parsed = urlparse(str(item.get("url", "")))
        for number in {int(n) for n in re.findall(r"\d+", f"{parsed.path}?{parsed.query}")}:
            if number >= 100:
                owners.setdefault(number, set()).add(idx)
    return {number: next(iter(idxs)) for number, idxs in owners.items() if len(idxs) == 1}


def _generate_structured_response(prompt: str, schema, stats: dict | None, operation: str,
                                  expected_ids: set[int] | None = None, 
                                  temperature: float = 0.0,
                                  id_aliases: dict[int, int] | None = None,
                                  models: list[str] | None = None,
                                  task: str = "filter"):
    if client is None:
        raise RuntimeError("Gemini-клієнт не ініціалізований")

    cascade = models if models is not None else FILTER_MODELS
    available = [model for model in cascade if model not in EXHAUSTED_MODELS]
    if not available:
        raise RuntimeError(
            f"{operation}: квоту вичерпано для всіх моделей каскаду ({', '.join(cascade)})"
        )

    last_error = None
    for model_name in available:
        # Retry a transient failure once per model; otherwise move to fallback model.
        # Exhausted quota (429) is not retried: the model is skipped until the end of the run.
        for attempt in range(2):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=temperature,  # Повернено жорстку температуру для зменшення галюцинацій
                        response_mime_type="application/json",
                        response_schema=schema,
                        safety_settings=[
                            types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
                            types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
                            types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
                            types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
                        ],
                    ),
                )
                if not response or not getattr(response, "text", None):
                    raise ValueError(f"Gemini повернув порожню відповідь ({model_name})")

                parsed = schema.model_validate_json(response.text)
                if expected_ids is not None:
                    ids = [item.id for item in parsed.translations]
                    if len(ids) != len(set(ids)) or set(ids) != expected_ids:
                        remapped = _remap_translation_ids(ids, expected_ids, id_aliases)
                        if remapped is None:
                            if len(ids) != len(set(ids)):
                                raise TranslationIdMismatch("Gemini повернув повторювані ID перекладів")
                            raise TranslationIdMismatch(
                                f"Невідповідність ID перекладів: очікували {sorted(expected_ids)}, "
                                f"отримали {sorted(ids)}"
                            )
                        for item, new_id in zip(parsed.translations, remapped):
                            item.id = new_id
                        logging.warning(
                            "%s: модель %s повернула ID %s замість %s; зіставлено однозначно: %s",
                            operation, model_name, ids, sorted(expected_ids), remapped,
                        )

                if stats is not None:
                    stats["models_used"][model_name] = stats["models_used"].get(model_name, 0) + 1
                    stats.setdefault("models_by_task", {})[task] = model_name
                _record_model_attempt(stats, task, model_name, "ok")
                logging.info("Gemini %s: успішна структурована відповідь", model_name)
                return parsed

            except Exception as exc:
                last_error = exc
                _record_model_attempt(stats, task, model_name, classify_model_error(exc))
                if stats is not None:
                    stats["gemini_retries"] += 1
                logging.warning(
                    "%s: модель %s, спроба %s не вдалася: %s",
                    operation, model_name, attempt + 1, exc,
                )
                if is_quota_error(exc):
                    EXHAUSTED_MODELS.add(model_name)
                    logging.warning("%s: квоту моделі %s вичерпано — до кінця запуску не використовуємо",
                                    operation, model_name)
                    break
                # A wrong-ID answer or a quick 5xx (e.g. 503) is worth one more try on the same model
                # before the fallback. A timeout / 504 is not: the model already kept us waiting for
                # up to GEMINI_TIMEOUT_MS, so we go to the next model at once.
                should_retry_same_model = attempt == 0 and not is_slow_gemini_failure(exc) and (
                    is_transient_gemini_error(exc) or isinstance(exc, TranslationIdMismatch)
                )
                if should_retry_same_model:
                    time.sleep(3)
                    continue
                break
        logging.warning("%s: переходимо до резервної моделі після помилки %s", operation, model_name)

    raise RuntimeError(f"Усі моделі Gemini не змогли виконати операцію {operation}") from last_error


def call_gemini_json(prompt: str, stats: dict | None = None) -> NewsFilterResponse:
    return _generate_structured_response(prompt, NewsFilterResponse, stats, "фільтрація новин", temperature=0.0,
                                         models=FILTER_MODELS, task="filter")

HOMOGLYPHS = {
    "ά": "а", "α": "а", "ό": "о", "ο": "о",
    "έ": "е", "ε": "е", "ί": "і", "ι": "і",
    "ή": "и", "η": "и", "ν": "н", "Ν": "Н",
    "ב": "б", "ب": "б", "є́": "є", "і́": "і",
}

def clean_generated_field(value: str) -> str:
    """Restore the original Unicode/ellipsis cleanup, applied to fields, not raw JSON."""
    cleaned = str(value or "").replace("\u0301", "")
    # L7: the single-character ellipsis «…» is cleaned the same way as «...»
    cleaned = cleaned.replace("[...]", ".").replace("[…]", ".").replace("...", ".").replace("…", ".")
    for bad_char, good_char in HOMOGLYPHS.items():
        cleaned = cleaned.replace(bad_char, good_char)
    return cleaned

def generate_news_texts_json(prompt: str, expected_ids: set[int], stats: dict | None = None,
                             id_aliases: dict[int, int] | None = None) -> list[dict]:
    # The original translation operation used temperature=0.1; models — TRANSLATE_MODELS cascade.
    result = _generate_structured_response(
        prompt, TranslationResponse, stats, "переклад новин",
        expected_ids=expected_ids, temperature=0.1, id_aliases=id_aliases,
        models=TRANSLATE_MODELS, task="translate",
    )
    translations = [item.model_dump() for item in result.translations]
    for item in translations:
        for key in ("title", "content", "hashtags"):
            item[key] = clean_generated_field(item.get(key, ""))
    return translations


# ---------------------------------------------------------------------------
# Editorial filter: fail closed and validate the entire response partition
# ---------------------------------------------------------------------------
def _shorten_for_filter(text: str, limit: int = FILTER_DESCRIPTION_CHARS) -> str:
    """L5: only the beginning of the description goes to the filter (word boundary)."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit * 0.6 else cut).rstrip(" ,;:") + " …"


def deduplicate_news(articles: list[dict], history_titles: list[str] | None = None,
                     stats: dict | None = None) -> tuple[list[dict], list[dict]]:
    if not articles:
        return [], []

    recent_history = history_titles[-75:] if history_titles else []
    prompt_data = [
        {"id": idx, "title": article["title"],
         "description": _shorten_for_filter(plain_text(article.get("description", "")))}
        for idx, article in enumerate(articles)
    ]
    candidates_log = "\n".join(f"[{item['id']}] {item['title']}" for item in prompt_data)
    logging.info("Кандидати на редакційну фільтрацію (%s):\n%s", len(prompt_data), candidates_log)

    prompt = f"""Ти — професійний та прагматичний головний редактор Telegram-каналу для української діаспори, яка проживає в Хорватії (з особливим фокусом на місто Задар). Твоя мета — дати читачам максимально повну та корисну картину дня, відсікаючи інформаційне сміття.

    АЛГОРИТМ ВІДБОРУ (Оцінюй кожну новину суворо за цими кроками у заданому порядку):

    КРОК 1. ІДЕНТИФІКАЦІЯ "КОРЕНЕВОЇ ПОДІЇ":
    Визначай "корінь" кожної новини (головну фізичну подію: ХТО, ЩО і ДЕ). 

    КРОК 2. КЛАСТЕРИЗАЦІЯ ДУБЛІВ: 
    Якщо кілька новин стосуються ОДНІЄЇ кореневої події (навіть якщо це нові деталі чи реакції), збери їх у кластер. ЗАЛИШ ТІЛЬКИ ОДИН ID (найбільш загальну і повну статтю). Інші дублікати — відхиляй.

    КРОК 3. ФІЛЬТР ІСТОРІЇ ТА ВАЖЛИВІ ОНОВЛЕННЯ: 
    Порівняй кандидатів зі списком <HISTORY>. 
- Якщо новина є просто переказом вчорашньої події або містить дрібні, несуттєві коментарі — ВІДХИЛЯЙ.
- РЕАКЦІЇ ТА КОМЕНТАРІ (АНТИ-ДУБЛЬ): Якщо в історії вже є базова новина (наприклад, інцидент чи ДТП), а новий кандидат — це просто коментар політика, реакція партії, дискусія на сесії чи думка експерта щодо цього інциденту — БЕЗЖАЛЬНО ВІДХИЛЯЙ. Це інформаційний шум.
- АЛЕ якщо це СУТТЄВЕ ОНОВЛЕННЯ вже знайомої події (наприклад: кардинально зросла кількість жертв, знайдено злочинця, ухвалено закон, який вчора лише обговорювався, або є офіційна реакція влади на кризу) — ОБОВ'ЯЗКОВО ЗАЛИШАЙ.
- Такі новини-оновлення, крім "keep", додавай також у масив "updates".

    КРОК 4. СТОП-ТЕМИ (АБСОЛЮТНА ЗАБОРОНА): 
    УВАГА: Цей крок перекриває всі інші! Навіть якщо в новині є слово "Задар" або "Україна", але новина стосується наступних тем — ВІДХИЛЯЙ ЇЇ.
1. СПОРТ: Будь-які матчі (зокрема баскетбольний клуб Задар), турніри, результати, дербі, спортсмени, тайми.
2. РЕКЛАМА: Спонсорські матеріали, промо-акції, розіграші.
3. ПОБУТ ЧУЖИХ МІСТ: Ремонти доріг, відключення води, локальні затори, страйки комунальників, дрібні ДТП у Загребі, Спліті, Рієці чи БУДЬ-ЯКОМУ іншому місті, ОКРІМ Задара, виняток — концерти, культурні події та фестивалі;
4. ІНШІ КРАЇНИ (ГЕОБЛОК): БУДЬ-ЯКІ події за межами Хорватії та України (навіть масові вбивства, теракти, вибори, катастрофи чи великий кримінал у Польщі, США, Франції тощо), ЯКЩО вони прямо не стосуються українських біженців або українців.
5. СВІТОВИЙ ШУМ: Абстрактна наука, поради дієтологів, рецепти.

    КРОК 5. ТЕМАТИКА (ЩО ОБОВ'ЯЗКОВО ЗАЛИШИТИ): 
1. ЗАДАР (Пріоритет №1): Побут, перекриття вулиць, тарифи, ремонти, відключення світла/води, погода, місцевий кримінал. 
2. ХОРВАТІЯ (Національний рівень): Важливі події всієї країни — великий кримінал, політика, економіка. Межа: великий кримінал, політичні рішення, ціни й закони для всієї країни — залишай, навіть якщо подія в Спліті чи Загребі; ремонт школи, відключення води чи місцевий конфлікт в іншому місті — це КРОК 4.3, відхиляй.
3. УКРАЇНА ТА  УКРАЇНСЬКІ БІЖЕНЦІ: Головні події в Україні (війна, енергетика, економіка, життя, політика, технології, оборона), рішення ЄС (Директива 2001/55), виплати українським біженцям.

    КРОК 6. ФІНАЛЬНИЙ ФІЛЬТР:
    Якщо новина не підпадає під ЖОДНУ з категорій КРОКУ 5 — ти ЗОБОВ'ЯЗАНИЙ її відхилити. Ніяких винятків для "цікавих світових новин".

CANDIDATES:
{json.dumps(prompt_data, ensure_ascii=False, indent=2)}

HISTORY:
{json.dumps(recent_history, ensure_ascii=False, indent=2)}

Поверни результат СУВОРО у форматі валідного JSON-об'єкта з трьома ключами:
    1. "keep": масив ID новин, які треба ЗАЛИШИТИ (наприклад, [0, 2]).
    2. "rejected": масив об'єктів для відхилених новин, де кожен містить:
       - "id": номер новини,
       - "entities_found": масив з 3-5 ключових сутностей (фігуранти, компанії, локації),
       - "reason": РІВНО одна категорія з переліку, інших значень не буває:
         "Дубль" — дублікат іншого кандидата (КРОК 2);
         "Вже публікувалось" — подія вже є в HISTORY або це реакція/коментар до неї (КРОК 3);
         "Спорт", "Реклама", "Побут інших міст", "Геоблок", "Світовий шум" — стоп-теми (КРОК 4);
         "Не за тематикою" — не підпадає під жодну тему КРОКУ 5 (КРОК 6),
       - "detail": коротке пояснення до 10 слів (наприклад, місто чи подія) або порожній рядок.
    3. "updates": масив ID з "keep", які є суттєвим оновленням події з HISTORY (КРОК 3). Якщо таких немає — [].
Кожен ID з CANDIDATES має бути рівно в одному з масивів "keep" або "rejected" — жодного пропущеного чи повтореного ID.
"""

    try:
        result = call_gemini_json(prompt, stats)
        candidate_ids = set(range(len(articles)))
        keep_ids = result.keep
        rejected_ids = [item.id for item in result.rejected]

        if len(keep_ids) != len(set(keep_ids)):
            raise ValueError("Gemini повернув повторювані ID у keep")
        if len(rejected_ids) != len(set(rejected_ids)):
            raise ValueError("Gemini повернув повторювані ID у rejected")
        if not set(keep_ids).issubset(candidate_ids) or not set(rejected_ids).issubset(candidate_ids):
            raise ValueError("Gemini повернув ID поза діапазоном кандидатів")
        if set(keep_ids).intersection(rejected_ids):
            raise ValueError("Один ID одночасно присутній у keep та rejected")
        if not keep_ids and not rejected_ids:
            raise ValueError("Gemini не класифікував жодного кандидата; беклог не змінюємо")
        unclassified_ids = candidate_ids - set(keep_ids) - set(rejected_ids)
        if unclassified_ids:
            # L4: not a reason to stop the whole run — these articles simply stay in the backlog
            # (neither kept nor rejected) and go to the filter again next run.
            logging.warning(
                "Gemini не класифікував %s з %s кандидатів — вони лишаються в черзі: %s",
                len(unclassified_ids), len(articles),
                "; ".join(articles[idx]["title"][:70] for idx in sorted(unclassified_ids)),
            )

        # L6 (R-12): the filter sees HISTORY, so it decides what is an update; code adds the prefix.
        update_ids = set(result.updates or [])
        ignored_updates = update_ids - set(keep_ids)
        if ignored_updates:
            logging.warning("Gemini позначив як оновлення ID поза keep — ігноруємо: %s",
                            sorted(ignored_updates))
        update_ids -= ignored_updates
        for idx in keep_ids:
            if idx in update_ids:
                articles[idx]["is_update"] = True
            else:
                articles[idx].pop("is_update", None)

        filtered = [articles[idx] for idx in keep_ids]
        rejected = [item.model_dump() for item in result.rejected]
        rejection_log = []
        for rejected_item in result.rejected:
            title = articles[rejected_item.id]["title"]
            entities = ", ".join(rejected_item.entities_found)
            detail = f" ({rejected_item.detail[:80]})" if rejected_item.detail else ""
            rejection_log.append(
                f"• {title[:70]} → {rejected_item.reason}{detail} | Сутності: {entities[:160]}"
            )
        if stats is not None:
            stats["funnel"]["rejection_reasons_list"] = rejection_log
            stats["funnel"]["unclassified_by_ai"] = len(unclassified_ids)
            by_reason = stats["funnel"].setdefault("rejected_by_reason", {})
            for rejected_item in result.rejected:
                by_reason[rejected_item.reason] = by_reason.get(rejected_item.reason, 0) + 1
            stats["funnel"]["updates_marked"] = len(update_ids)
        logging.info("Gemini залишив %s з %s кандидатів", len(filtered), len(articles))
        for rejected_item in result.rejected:
            title = articles[rejected_item.id]["title"]
            entities = ", ".join(rejected_item.entities_found)
            reason = rejected_item.reason
            logging.info(
                "Відхилено Gemini | ID=%s | причина=%s | пояснення=%s | сутності=%s | заголовок=%s",
                rejected_item.id, reason, rejected_item.detail, entities, title,
            )
        for idx in sorted(update_ids):
            logging.info("Позначено як оновлення: %s", articles[idx]["title"])
        return filtered, rejected

    except Exception as exc:
        logging.exception("Критична помилка редакційної фільтрації; публікацію зупинено")
        send_alert_to_admin(f"Gemini/валідація фільтрації не спрацювала. Беклог збережено. Помилка: {exc}")
        raise RuntimeError("News filtering failed; backlog left unchanged") from exc


# ---------------------------------------------------------------------------
# Translation validation and Telegram delivery
# ---------------------------------------------------------------------------
class AllowedHTMLValidator(HTMLParser):
    ALLOWED = {"b", "i", "u", "s"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.errors = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.ALLOWED:
            self.errors.append(f"Заборонений тег <{tag}>")
            return

        if attrs:
            self.errors.append(f"Тег <{tag}> має атрибути")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag not in self.ALLOWED:
            self.errors.append(f"Заборонений закривальний тег </{tag}>")
        elif not self.stack or self.stack[-1] != tag:
            self.errors.append(f"Неправильна вкладеність </{tag}>")
        else:
            self.stack.pop()

    def handle_startendtag(self, tag, attrs):
        self.errors.append(f"Непідтримуваний самозакривальний тег <{tag}/>")

    def error(self, message):
        self.errors.append(message)


def normalize_translation_html(content: str) -> str:
    """Normalize common Gemini HTML variants while preserving the allow-list."""
    content = str(content or "")
    content = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", content)
    content = re.sub(r"(?i)<\s*/?\s*(?:p|div)\b[^>]*>", "\n\n", content)
    content = re.sub(r"(?i)<\s*strong\b[^>]*>", "<b>", content)
    content = re.sub(r"(?i)</\s*strong\s*>", "</b>", content)
    content = re.sub(r"(?i)<\s*em\b[^>]*>", "<i>", content)
    content = re.sub(r"(?i)</\s*em\s*>", "</i>", content)
    content = re.sub(r"(?i)<\s*/?\s*(?:ul|ol)\b[^>]*>", "\n", content)
    content = re.sub(r"(?i)<\s*li\b[^>]*>", "• ", content)
    content = re.sub(r"(?i)</\s*li\s*>", "\n", content)
    content = re.sub(r"(?i)</?\s*span\b[^>]*>", "", content)
    content = re.sub(
        r"(?i)<\s*(b|i|u|s)\b[^>]*>",
        lambda m: f"<{m.group(1).lower()}>", content,
    )
    content = re.sub(
        r"(?i)</\s*(b|i|u|s)\s*>",
        lambda m: f"</{m.group(1).lower()}>", content,
    )
    return re.sub(r"\n{3,}", "\n\n", content).strip()


def html_fragment_is_valid(fragment: str) -> bool:
    parser = AllowedHTMLValidator()
    try:
        parser.feed(fragment or "")
        parser.close()
        if parser.stack:
            parser.errors.append(f"Незакриті теги: {', '.join(parser.stack)}")
        if parser.errors:
            logging.warning("Деталі помилки HTML: %s", "; ".join(parser.errors[:10]))
            return False
        return True
    except Exception as exc:
        logging.warning("HTML-парсер завершився помилкою: %s", exc)
        return False


def record_translation_failure(article: dict, reason: str) -> None:
    failures = int(article.get("translation_failures", 0)) + 1
    article["translation_failures"] = failures
    article["retry_after"] = time.time() + min(RETRY_COOLDOWN_SECONDS * failures, 6 * 60 * 60)
    article["last_translation_error"] = reason[:300]
    logging.warning("Статтю відкладено до повторної спроби (%s/%s): %s", failures,
                    MAX_TRANSLATION_FAILURES, article.get("title", "Без заголовка"))
    if failures >= MAX_TRANSLATION_FAILURES:
        send_alert_to_admin(
            f"Стаття повторно не проходить переклад/валідацію ({failures} спроб): "
            f"{article.get('title', 'Без заголовка')}. Залишена в беклозі."
        )


def _latinize_first_hashtag(hashtags: str) -> str:
    """L1: «#Korčula #Суспільство» -> «#Korcula #Суспільство». Only a Latin first hashtag with
    diacritics is changed; Cyrillic or other tags are left as they are (and fail validation as before)."""
    match = re.search(r"(?<!\w)#[\w]+", hashtags or "", flags=re.UNICODE)
    if not match:
        return hashtags
    tag = match.group(0)
    plain = unicodedata.normalize("NFKD", tag.replace("đ", "d").replace("Đ", "D"))
    plain = "".join(char for char in plain if not unicodedata.combining(char))
    if plain == tag or not re.fullmatch(r"#[A-Za-z][A-Za-z0-9_]*", plain):
        return hashtags
    logging.info("Перший хештег %s -> %s (без діакритики)", tag, plain)
    return hashtags[:match.start()] + plain + hashtags[match.end():]


def validate_translation_strict(translation: dict, original_article: dict) -> bool:
    title = str(translation.get("title", "")).strip()
    content = normalize_translation_html(translation.get("content", ""))
    translation["content"] = content
    hashtags = str(translation.get("hashtags", "")).strip()

    if not title or not content or not hashtags:
        logging.warning("Переклад %s має порожнє поле", translation.get("id"))
        return False
    if re.search(r"</?\w+[^>]*>", title):
        logging.warning("Заголовок %s містить HTML-теги", translation.get("id"))
        return False
    if not html_fragment_is_valid(content):
        logging.warning("Переклад %s містить некоректний або заборонений HTML", translation.get("id"))
        return False

    content_plain = plain_text(content)
    title_and_body = f"{title}\n{content_plain}"
    source_text = f"{original_article.get('title', '')} {plain_text(original_article.get('description', ''))}"
    sports_text = f"{source_text} {title_and_body} {hashtags}"
    if SPORTS_PATTERN.search(sports_text):
        logging.warning("Статтю %s заблоковано через спортивну тематику", translation.get("id"))
        return False

    if AD_PATTERN.search(source_text):
        logging.warning("Статтю %s заблоковано як імовірну рекламу", translation.get("id"))
        return False

    hashtags = _latinize_first_hashtag(hashtags)
    translation["hashtags"] = hashtags
    hashtag_list = re.findall(r"(?<!\w)#[\w]+", hashtags, flags=re.UNICODE)
    if not 2 <= len(hashtag_list) <= 3:
        logging.warning("Переклад %s має містити 2-3 хештеги", translation.get("id"))
        return False
    # First hashtag is the geographic one and must use Latin script.
    if not re.fullmatch(r"#[A-Za-z][A-Za-z0-9_]*", hashtag_list[0]):
        logging.warning("Перший хештег перекладу %s має бути географічним і латиницею", translation.get("id"))
        return False
    if re.search(r"#(?:sport|спорт|football|nogomet|košarka|футбол|баскетбол)\w*\b", hashtags, re.IGNORECASE):
        logging.warning("Переклад %s має спортивний хештег", translation.get("id"))
        return False

    # L3: a long translation is no longer rejected — send_to_telegram() trims it safely to the
    # Telegram limit (1024 with a photo / 4096), keeps the hashtags and adds «Повні деталі… 👇».
    image_urls = original_article.get("image_urls") or original_article.get("image_url")
    max_visible_chars = 800 if image_urls else 2500
    visible_length = len(title) + 2 + len(content_plain) + 2 + len(hashtags)
    if visible_length > max_visible_chars:
        logging.info(
            "Переклад %s довший за рекомендований (%s символів, ліміт %s) — буде обрізано при відправці",
            translation.get("id"), visible_length, max_visible_chars,
        )
    return True



def _telegram_retry_predicate(exc: Exception) -> bool:
    if isinstance(exc, requests.exceptions.RequestException):
        return True
    if isinstance(exc, TelegramAPIError):
        return exc.error_code == 429 or exc.error_code >= 500
    return False


def _telegram_wait(retry_state) -> float:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, TelegramAPIError) and exc.retry_after:
        return min(max(float(exc.retry_after), 1.0), 120.0)
    return wait_exponential(multiplier=2, min=3, max=30)(retry_state)


def _response_to_telegram_result(resp):
    try:
        data = resp.json()
    except ValueError as exc:
        raise TelegramAPIError(resp.status_code, f"Некоректна JSON-відповідь: {resp.text[:500]}") from exc

    if resp.status_code != 200 or not data.get("ok"):
        code = data.get("error_code") or resp.status_code
        description = data.get("description", resp.text[:500])
        retry_after = (data.get("parameters") or {}).get("retry_after")
        raise TelegramAPIError(int(code), str(description), retry_after=retry_after)
    return data.get("result")


def handle_telegram_html_error(api_url: str, payload: dict):
    """Retry a malformed HTML message as plain text; verify Telegram's JSON `ok`."""
    logging.warning("Помилка HTML-розмітки Telegram; повторюємо без HTML")

    def strip_html(text: str) -> str:
        # HTML-unescape after stripping tags, so &amp; is rendered as & in plain text.
        return html.unescape(re.sub(r"<[^>]*>", "", str(text)))

    plain_payload = dict(payload)
    if "text" in plain_payload:
        plain_payload["text"] = strip_html(plain_payload["text"])
    if "caption" in plain_payload:
        plain_payload["caption"] = strip_html(plain_payload["caption"])
    if "media" in plain_payload:
        media_objects = json.loads(plain_payload["media"])
        for obj in media_objects:
            if "caption" in obj:
                obj["caption"] = strip_html(obj["caption"])
            obj.pop("parse_mode", None)
        plain_payload["media"] = json.dumps(media_objects, ensure_ascii=False)
    plain_payload.pop("parse_mode", None)

    resp = requests.post(api_url, data=plain_payload, timeout=TELEGRAM_TIMEOUT_SECONDS)
    return _response_to_telegram_result(resp)


@retry(
    stop=stop_after_attempt(4),
    wait=_telegram_wait,
    reraise=True,
    retry=retry_if_exception(_telegram_retry_predicate),
)
def execute_telegram_request(api_url: str, payload: dict):
    resp = requests.post(api_url, data=payload, timeout=TELEGRAM_TIMEOUT_SECONDS)
    try:
        return _response_to_telegram_result(resp)
    except TelegramAPIError as exc:
        if exc.error_code == 400 and "parse entities" in str(exc).lower():
            return handle_telegram_html_error(api_url, payload)
        raise

def _extract_hashtag_suffix(text: str) -> tuple[str, str]:
    """Keep a final hashtag block separate so truncation never removes it."""
    parts = (text or "").strip().split("\n\n")
    if parts:
        last = parts[-1].strip()
        hashtags = re.findall(r"(?<!\w)#[\w]+", last, flags=re.UNICODE)
        if 2 <= len(hashtags) <= 3 and re.fullmatch(r"\s*(?:#[\w]+)(?:\s+#[\w]+){1,2}\s*", last, re.UNICODE):
            return "\n\n".join(parts[:-1]).strip(), last
    return (text or "").strip(), ""


def _truncate_html_visible(fragment: str, max_visible_chars: int) -> str:
    """Truncate a validated small HTML fragment without splitting tags/entities."""
    if max_visible_chars <= 0:
        return ""
    token_pattern = re.compile(r"</?(?:b|i|u|s)\s*>|&(?:#[0-9]+|#x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]+);|[^<]+|<[^>]*>", re.I)
    output = []
    stack = []
    visible = 0
    truncated = False

    for match in token_pattern.finditer(fragment or ""):
        token = match.group(0)
        tag_match = re.fullmatch(r"<(/?)(b|i|u|s)\s*>", token, re.I)
        if tag_match:
            closing, tag = tag_match.group(1), tag_match.group(2).lower()
            if closing:
                output.append(f"</{tag}>")
                if stack and stack[-1] == tag:
                    stack.pop()
            else:
                output.append(f"<{tag}>")
                stack.append(tag)
            continue

        if token.startswith("<") and token.endswith(">"):
            # Caller validates HTML first; do not copy an unknown tag on truncation.
            continue

        decoded = html.unescape(token)
        remaining = max_visible_chars - visible
        if len(decoded) <= remaining:
            output.append(html.escape(decoded, quote=False))
            visible += len(decoded)
        else:
            clipped = decoded[:remaining]
            # Prefer a word boundary to splitting a word.
            last_space = max(clipped.rfind(" "), clipped.rfind("\n"), clipped.rfind("\t"))
            if last_space > max(0, int(len(clipped) * 0.6)):
                clipped = clipped[:last_space]
            output.append(html.escape(clipped, quote=False))
            visible += len(clipped)
            truncated = True
            break

    if truncated:
        while stack:
            output.append(f"</{stack.pop()}>")
    return "".join(output).rstrip()

def _target_chat_id() -> str | None:
    """DRY_RUN: posts go to the admin chat instead of the channel."""
    return ADMIN_CHAT_ID if DRY_RUN else TELEGRAM_CHANNEL_ID


def send_to_telegram(text: str, url: str, source_name: str, image_urls=None) -> tuple[bool, str]:
    image_urls = [] if not image_urls else ([image_urls] if isinstance(image_urls, str) else list(image_urls))
    image_urls = image_urls[:10]

    parsed_url = urlparse(url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError(f"Небезпечне або некоректне посилання на джерело: {url!r}")

    body, hashtag_suffix = _extract_hashtag_suffix(text)
    if DRY_RUN:
        # The header is part of the body, so the Telegram length limits account for it.
        body = DRY_RUN_HEADER + body
    suffix = hashtag_suffix
    safe_url = html.escape(url, quote=True)
    safe_source = html.escape(source_name or "Джерело", quote=True)
    source_line = f'\n\n🔗 Джерело: <a href="{safe_url}">{safe_source}</a>'
    crop_note = "\n\n👉 <i>Повні деталі читайте за посиланням нижче</i> 👇"
    api_url_text = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    def compose(candidate_body: str) -> str:
        pieces = [candidate_body.strip()]
        if suffix:
            pieces.append(suffix)
        return "\n\n".join(p for p in pieces if p) + source_line

    max_visible = 1024 if image_urls else 4096
    full_message = compose(body)
    if len(plain_text(full_message)) > max_visible:
        logging.warning("Публікація завелика; безпечно скорочуємо тіло та зберігаємо хештеги")
        tail_visible = len(plain_text((suffix + source_line).strip()))
        crop_visible = len(plain_text(crop_note))
        # Keep room for a divider and the original end-of-message attribution.
        body_budget = max_visible - tail_visible - crop_visible - 4
        if body_budget <= 0:
            raise ValueError("Хештеги й посилання не залишають місця для тексту новини")
        short_body = _truncate_html_visible(body, body_budget)
        body = short_body + crop_note
        full_message = compose(body)
        # Account for small differences introduced by HTML decoding/entities.
        while len(plain_text(full_message)) > max_visible and body_budget > 20:
            body_budget -= len(plain_text(full_message)) - max_visible + 2
            body = _truncate_html_visible(body, body_budget) + crop_note
            full_message = compose(body)
        if len(plain_text(full_message)) > max_visible:
            raise ValueError("Не вдалося скоротити повідомлення до ліміту Telegram")

    if not image_urls:
        try:
            execute_telegram_request(api_url_text, {
                "chat_id": _target_chat_id(),
                "text": full_message,
                "parse_mode": "HTML",
            })
            return True, "text"
        except Exception:
            logging.exception("Не вдалося надіслати текстове повідомлення")
            return False, "error"

    if len(image_urls) == 1:
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        payload = {
            "chat_id": _target_chat_id(),
            "photo": image_urls[0],
            "caption": full_message,
            "parse_mode": "HTML",
        }
        try:
            execute_telegram_request(api_url, payload)
            return True, "image"
        except Exception as exc:
            if not isinstance(exc, TelegramAPIError) or exc.error_code == 429 or exc.error_code >= 500:
                logging.exception("Невизначений результат відправки фото; не робимо текстовий fallback")
                return False, "error"
            logging.warning("Telegram відхилив фото, пробуємо надіслати текстом: %s", exc)
            try:
                execute_telegram_request(api_url_text, {
                    "chat_id": _target_chat_id(),
                    "text": full_message,
                    "parse_mode": "HTML",
                })
                return True, "text"
            except Exception:
                logging.exception("Не вдалося надіслати резервну текстову публікацію")
                return False, "error"

    media = []
    for index, image_url in enumerate(image_urls):
        obj = {"type": "photo", "media": image_url}
        if index == 0:
            obj["caption"] = full_message
            obj["parse_mode"] = "HTML"
        media.append(obj)

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMediaGroup"
    try:
        execute_telegram_request(api_url, {
            "chat_id": _target_chat_id(),
            "media": json.dumps(media, ensure_ascii=False),
        })
        return True, "image"
    except Exception as exc:
        if not isinstance(exc, TelegramAPIError) or exc.error_code == 429 or exc.error_code >= 500:
            logging.exception("Невизначений результат відправки альбому; не робимо текстовий fallback")
            return False, "error"
        logging.warning("Telegram відхилив альбом, пробуємо текстовий fallback: %s", exc)
        try:
            execute_telegram_request(api_url_text, {
                "chat_id": _target_chat_id(),
                "text": full_message,
                "parse_mode": "HTML",
            })
            return True, "text"
        except Exception:
            logging.exception("Не вдалося надіслати резервну текстову публікацію")
            return False, "error"


# ---------------------------------------------------------------------------
# News selection and backlog processing
# ---------------------------------------------------------------------------
UKRAINE_KEYWORDS = [
    r"\bukrajin", r"\bdijaspor", r"\bzelensk", r"\bzelenšk", r"\bkijev",
    r"\bodesa", r"\bizbjeglic",
    r"\bprivremen\w*\s+zaštit", r"\bdirektiv\w*\s*2001",
]


def is_ukraine_priority(article: dict) -> bool:
    text = (article.get("title", "") + " " + plain_text(article.get("description", ""))).lower()
    return any(re.search(pattern, text) for pattern in UKRAINE_KEYWORDS)


def scrape_feeds(stats: dict | None = None) -> None:
    history_urls = set(load_history(HISTORY_FILE, is_urls=True))
    history_titles = set(load_history(TITLES_FILE))
    backlog = load_backlog()
    current_time = time.time()

    backlog = [item for item in backlog if current_time - float(item.get("timestamp", current_time)) <= MAX_AGE_SECONDS]
    backlog_urls = {item["url"] for item in backlog}
    scraper = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "mobile": False})
    feeds = get_rss_feeds()
    if not feeds:
        raise RuntimeError("feeds.json порожній: немає RSS-джерел для перевірки")

    for feed_item in feeds:
        feed_url = feed_item.get("url")
        if not feed_url:
            logging.error("Пропущено RSS-конфігурацію без url: %s", feed_item)
            if stats is not None:
                stats["errors_count"] += 1
            continue
        source_name = feed_item.get("name") or get_source_name(feed_url)
        source_count = 0

        try:
            response = scraper.get(
                feed_url,
                headers={"Accept": "application/rss+xml, application/xml, text/xml, */*"},
                timeout=15,
            )
            response.raise_for_status()
            feed = feedparser.parse(response.text)
            # Порожня стрічка (або HTML-сторінка замість RSS) — це збій джерела, а не «нічого нового».
            if not feed.entries:
                raise RuntimeError("порожня стрічка: 0 записів (можливо, сайт віддав сторінку замість RSS)")
        except Exception as exc:
            logging.exception("Помилка доступу/парсингу RSS %s", feed_url)
            if stats is not None:
                stats["errors_count"] += 1
                stats.setdefault("source_errors", {})[source_name] = str(exc)[:200]
            continue

        for entry in feed.entries[:15]:
            if stats is not None:
                stats["funnel"]["scraped_total"] += 1
            source_count += 1
            article_url = normalize_url(entry.get("link", ""))
            title = plain_text(entry.get("title", ""))
            if not article_url or not title:
                continue

            if article_url in history_urls or title in history_titles or article_url in backlog_urls:
                if stats is not None:
                    stats["funnel"]["already_seen"] += 1
                continue

            article_time = time.time()
            published_parsed = entry.get("published_parsed")
            if published_parsed:
                article_time = calendar.timegm(published_parsed)
                if current_time - article_time > MAX_AGE_SECONDS:
                    continue

            entry_tags = [str(tag.get("term", "")).lower() for tag in entry.get("tags", [])]
            blacklist_categories = {"sport", "košarka", "nogomet", "šport", "lifestyle", "zabava", "magazin"}
            blocked_tag = next((tag for tag in entry_tags if tag in blacklist_categories), None)
            if blocked_tag:
                _count_code_rejection(stats, f"тег RSS {blocked_tag}", title)
                continue

            description = entry.get("summary", title)
            contents = entry.get("content", [])
            if contents:
                description = contents[0].get("value", description)
            description = str(description or title)

            if "..." in description[-15:] or "[...]" in description[-15:] or len(plain_text(description)) < 200:
                full_text = fetch_full_article_text(article_url, scraper)
                if full_text:
                    description = full_text

            description_plain = plain_text(description)
            if description_plain.endswith(("...", "[…]", "[...]")):
                match = re.search(r"(.*[.!?])", description_plain)
                if match:
                    description_plain = match.group(1).strip()

            rejection_reason = quality_rejection_reason(title, description_plain, article_url)
            if rejection_reason:
                _count_code_rejection(stats, rejection_reason, title)
                continue

            article = {
                "title": title,
                "description": description_plain,
                "url": article_url,
                "image_urls": extract_image_urls(entry),
                "source": source_name,
                "timestamp": article_time,
            }
            backlog.append(article)
            backlog_urls.add(article_url)

        if stats is not None:
            stats["funnel"]["scraped_by_source"][source_name] = source_count

    save_backlog(backlog)
    logging.info("Після збору RSS беклог містить %s новин", len(backlog))


def _save_postprocessing_backlog(backlog: list[dict], evaluated_urls: set[str],
                                 approved_urls: set[str], sent_urls: set[str]) -> None:
    """Remove only AI-rejected and successfully sent items; preserve all other backlog items."""
    remaining = [
        item for item in backlog
        if item["url"] not in sent_urls
        and (item["url"] not in evaluated_urls or item["url"] in approved_urls)
    ]
    remaining.sort(key=lambda item: item.get("timestamp", 0), reverse=True)
    # Do not truncate to 30: that silently discarded valid pending news.
    save_backlog(remaining)


def _build_translation_prompt(batch: list[dict]) -> str:
    rules = """Ти — досвідчений редактор українського Telegram-каналу для українців у Хорватії.
Переклади кожну статтю українською природно, точно та без доданих фактів.

СУВОРІ ПРАВИЛА ПЕРЕКЛАДУ ТА ФОРМАТУВАННЯ:
1. ГЕОГРАФІЯ: Зберігай оригінальну країну події. Якщо подія сталася в Хорватії — пиши про Хорватію, якщо в іншій країні (світові новини) — так і залишай. "Хорвати" — це місцеві жителі. НІКОЛИ не міняй місцями слова "хорвати"/"Хорватія" та "українці"/"Україна", якщо цього немає в оригінальному тексті.
2. ДЕТАЛІЗАЦІЯ ДОКУМЕНТІВ (ДИРЕКТИВА 2001/55/ЄС): Якщо новина стосується тимчасового захисту, прав українських біженців, виплат чи документів, ПЕРЕКЛАДАЙ ЇЇ МАКСИМАЛЬНО ДЕТАЛЬНО. Не скорочуй жодної цифри, дати чи юридичної назви. Додай на початок заголовка емодзі 📄 або 🇪🇺.
3. ПРІОРИТЕТ "УКРАЇНА": Якщо в оригінальній новині згадується Україна, українці, війна в Україні або українські біженці — обов'язково постав емодзі 🇺🇦 на самий початок заголовка та винеси цю інформацію у перший абзац, щоб привернути увагу. Якщо підходять і правило 2, і правило 3 — спершу 🇺🇦, потім 📄 (наприклад: «🇺🇦📄 …»).
4. ПРИРОДНІСТЬ МОВИ ТА СЛОВНИК: Перекладай живою та природною українською мовою. УНИКАЙ ДОСЛІВНОГО ПЕРЕКЛАДУ (кальок) — передавай суть фрази, а не перекладай слово за словом. КАТЕГОРИЧНО ЗАБОРОНЕНО вигадувати неіснуючі слова (наприклад, замість кальки "бізшрний" чи "бізарний" використовуй існуючі аналоги: "дивний", "курйозний", "химерний"). Усі слова у твоєму тексті мають існувати в словнику української мови. УВАЖНО ПЕРЕВІРЯЙ ОРФОГРАФІЮ: Звертай особливу увагу на використання апострофа (наприклад: "м'ясо", "комп'ютер"). Ніколи не пропускай літери.
5. ТОПОНІМИ ТА ІМЕНА: Назви міст (наприклад, Задар, Спліт, Загреб) транслітеруй українською. Хорватські імена, прізвища, назви вулиць, компаній і продуктів (наприклад, Apple, Facebook) залишай оригінальною латиницею без змін. 
6. ЖУРНАЛІСТСЬКИЙ СТИЛЬ (АНТИ-КЛІКБЕЙТ): Пиши у стриманому стилі якісних медіа (як BBC). КАТЕГОРИЧНО ЗАБОРОНЕНО використовувати жовтий клікбейт та штучні епітети (наприклад, "зіркова", "шокуюча", "скандальна", "кривава"). Роби текст легким для читання. Абзаци мають бути короткими (не більше 3-4 речень). Уникай сухого поліцейського сленгу та дослівного перекладу юридичних термінів. Наприклад, "prekršajni odjel" адаптуй як "відділ з адміністративних правопорушень" (а не "проміжний відділ"). 
7. НІЯКОГО ШІ-КОНТЕКСТУ: Видавай СУВОРО лише готовий текст новини. Жодних вступних фраз, привітань, коментарів чи висновків від себе. Зберігай абсолютну журналістську об'єктивність.
8. АКЦЕНТИ: Заголовок — без HTML. У content дозволені тільки HTML-теги <b>, <i>, <u>, <s>; теги мають бути коректно закриті. Не використовуй Markdown. Виділяй жирним ключові суми штрафів, дати, імена посадовців і назви законів.
9. ХЕШТЕГИ: Повертай їх ТІЛЬКИ в окремому полі "hashtags". КАТЕГОРИЧНО ЗАБОРОНЕНО писати хештеги всередині текстового поля "content"!
    ПРАВИЛО ГЕОГРАФІЇ: Перший хештег — це ГЕОГРАФІЯ, і він має бути ТІЛЬКИ ОДИН. Якщо ти знаєш місто — пиши ТІЛЬКИ його латиницею (наприклад, #Zadar, #Split, #SeverinNaKupi). Якщо точне місто невідоме — пиши ТІЛЬКИ назву країни (наприклад, #Hrvatska). КАТЕГОРИЧНО ЗАБОРОНЕНО ставити хештег країни (наприклад, #Хорватія чи #Hrvatska), якщо ти вже поставив хештег конкретного міста!
    ТЕМАТИЧНІ ХЕШТЕГИ: Другі хештеги мають СУВОРО відповідати змісту статті (наприклад, #Економіка, #Суспільство, #Закон, #Політика, #Кримінал). Категорично ЗАБОРОНЕНО ставити хештеги #ТимчасовийЗахист або #Україна, якщо в самому тексті новини про це не йдеться.
10. ДОВЖИНА: якщо є фото, видимий текст перекладу (заголовок, тіло й хештеги) має бути не довшим за 800 символів. Без фото — не довшим за 2500 символів. Якщо текст довший, стисни головну суть без втрати важливих фактів, цифр або дат. Не обрізай речення на півслові; якщо вихідний текст обірваний або закінчується на три крапки / [...], прибери незавершене речення і не вигадуй продовження. Для розширених текстів використовуй марковані списки
(із символом • або -), щоб інформацію було зручно читати.
11. ID: У полі id поверни саме ціле числове ID з блока новини. Не повертай URL як ID.

Поверни тільки JSON, який відповідає схемі TranslationResponse.
"""
    chunks = [rules, "\nНОВИНИ ДЛЯ ПЕРЕКЛАДУ:\n"]
    for idx, item in enumerate(batch):
        images = item.get("image_urls", []) or ([item["image_url"]] if item.get("image_url") else [])
        chunks.append(
            f"\n=== НОВИНА ID {idx} ===\n"
            f"Має картинку: {'Так' if images else 'Ні'}\n"
            f"Джерело: {item.get('source', 'Джерело')}\n"
            f"URL (лише контекст, не ID): {item['url']}\n"
            f"Заголовок оригіналу: {item['title']}\n"
            f"Текст оригіналу: {item['description']}\n"
        )
    return "".join(chunks)


def send_dry_run_filter_report(candidates: list[dict], approved: list[dict],
                               rejected_items: list[dict], stats: dict | None) -> None:
    """DRY_RUN: what the filter kept, rejected (with reasons) and did not classify — to judge
    prompt or model changes. Sent to the admin chat only in a test run."""
    if not ADMIN_CHAT_ID or not TELEGRAM_BOT_TOKEN:
        return
    def esc(value) -> str:
        return html.escape(str(value or ""), quote=False)

    model = ((stats or {}).get("models_by_task") or {}).get("filter")
    lines = [f"🧪 <b>ТЕСТ: фільтр Gemini</b> ({esc(_short_model(model))})", "",
             f"✅ <b>Залишено {len(approved)}</b> (у пости підуть перші {BATCH_SIZE}):"]
    for number, item in enumerate(approved, 1):
        mark = "🔄 " if item.get("is_update") else ""
        lines.append(f"{number}. {mark}{esc(item['title'][:110])} <i>({esc(item.get('source'))})</i>")
    lines += ["", "✖ <b>Відкинуто:</b>"]
    rejected_idx = set()
    for rejected in rejected_items:
        rejected_idx.add(rejected["id"])
        detail = f" — {esc(rejected.get('detail'))}" if rejected.get("detail") else ""
        lines.append(f"• [{esc(rejected.get('reason'))}] {esc(candidates[rejected['id']]['title'][:100])}{detail}")
    approved_urls = {item["url"] for item in approved}
    unclassified = [item for idx, item in enumerate(candidates)
                    if idx not in rejected_idx and item["url"] not in approved_urls]
    code_rejected = ((stats or {}).get("funnel") or {}).get("rejected_by_code_examples") or []
    if code_rejected:
        lines += ["", "🚫 <b>Відсіяно кодом (до Gemini):</b>"]
        lines += [f"• {esc(example)}" for example in code_rejected]
    if unclassified:
        lines += ["", "❔ <b>Не класифіковано (лишились у черзі):</b>"]
        lines += [f"• {esc(item['title'][:100])}" for item in unclassified]
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": ADMIN_CHAT_ID, "text": "\n".join(lines)[:4000], "parse_mode": "HTML"},
            timeout=TELEGRAM_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            logging.error("Не вдалося надіслати звіт фільтра: %s", resp.text[:500])
    except Exception:
        logging.exception("Збій під час надсилання звіту фільтра")


def process_backlog(stats: dict | None = None) -> None:
    backlog = load_backlog()
    # A successful Telegram post may already be in history even if a later state write
    # failed. Remove such entries before selecting candidates to avoid reposting them.
    posted_urls = set(load_history(HISTORY_FILE, is_urls=True))
    unposted_backlog = [item for item in backlog if item["url"] not in posted_urls]
    if len(unposted_backlog) != len(backlog):
        backlog = unposted_backlog
        save_backlog(backlog)
    if not backlog:
        return

    now = time.time()
    ready_backlog = [item for item in backlog if float(item.get("retry_after", 0)) <= now]
    if not ready_backlog:
        logging.info("Усі новини беклогу очікують часу повторної спроби")
        return

    ready_backlog.sort(key=lambda item: item.get("timestamp", 0), reverse=True)
    all_feeds = get_rss_feeds()
    all_sources = list(dict.fromkeys(
        feed.get("name") or get_source_name(feed.get("url", "")) for feed in all_feeds
    ))
    backlog_sources = list(dict.fromkeys(item.get("source", "Джерело") for item in ready_backlog))
    all_sources = sorted(set(all_sources).union(backlog_sources))
    if not all_sources:
        raise RuntimeError("Немає жодного джерела для обробки беклогу")

    ordered_sources = all_sources.copy()
    random.shuffle(ordered_sources)
    candidate_batch = []
    used_urls = set()

    # Ukraine-priority stories are first, but the candidate window remains bounded.
    for item in ready_backlog:
        if is_ukraine_priority(item) and item["url"] not in used_urls:
            candidate_batch.append(item)
            used_urls.add(item["url"])
            if len(candidate_batch) >= MAX_CANDIDATES_PER_FILTER:
                break

    # Fill remaining slots with round-robin source selection.
    while len(candidate_batch) < MAX_CANDIDATES_PER_FILTER:
        added_this_round = False
        for source in ordered_sources:
            if len(candidate_batch) >= MAX_CANDIDATES_PER_FILTER:
                break
            next_item = next((
                item for item in ready_backlog
                if item.get("source", "Джерело") == source and item["url"] not in used_urls
            ), None)
            if next_item is not None:
                candidate_batch.append(next_item)
                used_urls.add(next_item["url"])
                added_this_round = True
        if not added_this_round:
            break

    if not candidate_batch:
        return

    evaluated_urls = {item["url"] for item in candidate_batch}
    history_titles = load_history(TITLES_FILE)

    # If filtering fails or its output is inconsistent, this raises before the backlog is altered.
    approved_articles, rejected_items = deduplicate_news(candidate_batch, history_titles, stats)
    approved_urls = {item["url"] for item in approved_articles}
    rejected_urls = {candidate_batch[item["id"]]["url"] for item in rejected_items}
    # L4: articles the filter did not classify are not "evaluated" — they stay in the backlog.
    evaluated_urls = approved_urls | rejected_urls
    if stats is not None:
        stats["funnel"]["duplicates_removed_by_ai"] += len(rejected_urls)
    if DRY_RUN:
        send_dry_run_filter_report(candidate_batch, approved_articles, rejected_items, stats)

    batch = approved_articles[:BATCH_SIZE]
    add_og_images(batch, stats)
    sent_urls = set()  # Must exist before any successful Telegram send.

    if not batch:
        _save_postprocessing_backlog(backlog, evaluated_urls, approved_urls, sent_urls)
        return

    logging.info(
        "Для перекладу вибрано %s новин; джерела: %s",
        len(batch), ", ".join(dict.fromkeys(item.get("source", "Джерело") for item in batch)),
    )
    prompt = _build_translation_prompt(batch)
    expected_ids = set(range(len(batch)))

    try:
        # Restore the original pacing before the translation request.
        time.sleep(PRE_TRANSLATION_DELAY_SECONDS)
        translations = generate_news_texts_json(
            prompt, expected_ids, stats, id_aliases=_url_number_aliases(batch)
        )
    except Exception as exc:
        for item in batch:
            record_translation_failure(item, str(exc))
        _save_postprocessing_backlog(backlog, evaluated_urls, approved_urls, sent_urls)
        send_alert_to_admin(f"Переклад Gemini не вдався; погоджені новини залишені в беклозі: {exc}")
        if stats is not None:
            stats["errors_count"] += 1
        raise RuntimeError("Translation failed; approved news preserved for retry") from exc

    translations_by_id = {translation["id"]: translation for translation in translations}
    history_urls = load_history(HISTORY_FILE, is_urls=True)
    history_titles = load_history(TITLES_FILE)

    for idx, item in enumerate(batch):
        translation = translations_by_id.get(idx)
        if translation is None:
            record_translation_failure(item, f"Відсутній переклад із ID {idx}")
            if stats is not None:
                stats["errors_count"] += 1
            continue

        if not validate_translation_strict(translation, item):
            record_translation_failure(item, "Не пройдено сувору перевірку перекладу")
            if stats is not None:
                stats["errors_count"] += 1
            continue

        title_prefix = UPDATE_PREFIX if item.get("is_update") else ""
        formatted_text = (
            f"<b>{title_prefix}{translation['title']}</b>\n\n"
            f"{translation['content']}\n\n"
            f"{translation['hashtags']}"
        )
        images = item.get("image_urls", []) or ([item["image_url"]] if item.get("image_url") else [])
        success, pub_type = send_to_telegram(
            formatted_text, item["url"], item.get("source", "Джерело"), images
        )

        if not success:
            record_translation_failure(item, "Telegram не підтвердив публікацію")
            if stats is not None:
                stats["errors_count"] += 1
            continue

        # Telegram confirmed a post. Mark it as sent before state writes so cleanup can
        # avoid leaving it in backlog if a subsequent history write fails.
        sent_urls.add(item["url"])
        try:
            history_urls = save_to_history(history_urls, item["url"], HISTORY_FILE)
            history_titles = save_to_history(history_titles, item["title"], TITLES_FILE)
        except Exception as exc:
            _save_postprocessing_backlog(backlog, evaluated_urls, approved_urls, sent_urls)
            send_alert_to_admin(
                f"Повідомлення опубліковано, але історію не вдалося повністю зберегти. "
                f"Щоб не дублювати пост, URL вилучено з беклогу. Помилка: {exc}"
            )
            raise

        item.pop("translation_failures", None)
        item.pop("retry_after", None)
        item.pop("last_translation_error", None)
        if stats is not None:
            stats["published"]["total"] += 1
            if is_ukraine_priority(item):
                stats["published"]["ukraine_priority"] += 1
            if pub_type == "image":
                stats["published"]["with_images"] += 1
            else:
                stats["published"]["text_only"] += 1
            source = item.get("source", "Джерело")
            stats["published"]["sources"][source] = stats["published"]["sources"].get(source, 0) + 1
        # Pause between posts (Telegram limits); nothing to wait for after the last one (L8).
        if idx < len(batch) - 1:
            time.sleep(DRY_RUN_POST_DELAY_SECONDS if DRY_RUN else POST_PUBLICATION_DELAY_SECONDS)

    _save_postprocessing_backlog(backlog, evaluated_urls, approved_urls, sent_urls)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _empty_stats() -> dict:
    return {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "execution_time": 0,
        "funnel": {
            "scraped_total": 0,
            "scraped_by_source": {},
            "already_seen": 0,
            "rejected_by_keywords": 0,
            "rejected_by_rule": {},
            "unclassified_by_ai": 0,
            "duplicates_removed_by_ai": 0,
            "rejection_reasons_list": [],
            "rejected_by_reason": {},
            "updates_marked": 0,
        },
        "backlog": {"total_size": 0, "sources": {}},
        "published": {
            "total": 0,
            "ukraine_priority": 0,
            "with_images": 0,
            "text_only": 0,
            "og_images_found": 0,
            "sources": {},
        },
        "models_used": {},
        "models_by_task": {"filter": None, "translate": None},
        "model_attempts": [],
        "exhausted_models": [],
        "errors_count": 0,
        "gemini_retries": 0,
    }


def refresh_backlog_stats(stats: dict) -> None:
    current = load_backlog()
    stats["backlog"]["total_size"] = len(current)
    source_counts = {}
    for item in current:
        source = item.get("source", "Невідомо")
        source_counts[source] = source_counts.get(source, 0) + 1
    stats["backlog"]["sources"] = source_counts


def _enter_dry_run_sandbox() -> str:
    """DRY_RUN: work on copies of feeds.json and the state files in a temporary folder.

    All the usual code paths (backlog, history, analytics writes) stay the same, but they
    write to the copies, so the files in the repository are never changed.
    """
    sandbox = tempfile.mkdtemp(prefix="croatia-news-dry-run-")
    for name in (FEEDS_FILE, HISTORY_FILE, TITLES_FILE, BACKLOG_FILE,
                 f"{BACKLOG_FILE}.bak", ANALYTICS_FILE):
        if os.path.exists(name):
            shutil.copy2(name, os.path.join(sandbox, name))
    os.chdir(sandbox)
    return sandbox


def main() -> None:
    global client
    if DRY_RUN:
        if not all([TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, ADMIN_CHAT_ID]):
            logging.critical("DRY_RUN: потрібні TELEGRAM_BOT_TOKEN, GEMINI_API_KEY і ADMIN_CHAT_ID")
            sys.exit(1)
        sandbox = _enter_dry_run_sandbox()
        logging.warning(
            "🧪 DRY_RUN: тестовий запуск — пости йдуть в адмін-чат, а не в канал; "
            "файли стану — копії в %s (у репозиторії не змінюються)", sandbox,
        )
    elif not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, GEMINI_API_KEY]):
        logging.critical("Не налаштовані TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID або GEMINI_API_KEY")
        sys.exit(1)

    client = genai.Client(api_key=GEMINI_API_KEY,
                          http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_MS))
    logging.info("Моделі фільтра: %s", ", ".join(FILTER_MODELS))
    logging.info("Моделі перекладу: %s", ", ".join(TRANSLATE_MODELS))
    start_time = time.time()
    stats = _empty_stats()
    critical_error = False

    try:
        scrape_feeds(stats)
        refresh_backlog_stats(stats)
        process_backlog(stats)
    except Exception as exc:
        critical_error = True
        stats["errors_count"] += 1
        logging.exception("Критична помилка в основному циклі бота")
        send_alert_to_admin(f"Скрипт зупинився з помилкою. Беклог має бути збережений: {exc}")
    finally:
        trim_history(HISTORY_FILE, MAX_HISTORY_SIZE)
        trim_history(TITLES_FILE, MAX_HISTORY_SIZE)
        stats["execution_time"] = round(time.time() - start_time, 1)
        try:
            refresh_backlog_stats(stats)
        except Exception:
            critical_error = True
            logging.exception("Не вдалося оновити статистику беклогу")
        stats["exhausted_models"] = sorted(EXHAUSTED_MODELS)
        if not DRY_RUN:  # у тестовому запуску історія не оновлюється, тож і алертів немає
            try:
                for alert in check_source_health(stats, load_analytics()):
                    logging.warning(alert)
                    send_alert_to_admin(alert)
            except Exception:
                logging.exception("Не вдалося перевірити стан джерел")
        try:
            save_analytics(stats)
        except Exception:
            critical_error = True
            logging.exception("Не вдалося зберегти аналітику")
        if DRY_RUN or stats["published"]["total"] > 0 or stats["errors_count"] > 0:
            send_analytics_to_admin(stats)

    # Non-zero status only for critical failures (the main loop stopped, or state/analytics
    # could not be saved). Partial errors (one article failed validation, one RSS feed or
    # one Telegram post failed) are reported to the admin, but the run stays green.
    if critical_error:
        sys.exit(1)


if __name__ == "__main__":
    main()
