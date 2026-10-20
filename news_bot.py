"""Croatia News Bot — hardened version.

Required environment variables:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, GEMINI_API_KEY
Optional:
  ADMIN_CHAT_ID

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
import time
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import cloudscraper
import feedparser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field, ValidationError
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
RETRY_COOLDOWN_SECONDS = 60 * 60

GEMINI_MODELS = ["gemini-3.6-flash", "gemini-3.5-flash-lite"]
client = None  # Created inside main(), after environment validation.


# ---------------------------------------------------------------------------
# Structured model responses
# ---------------------------------------------------------------------------
class RejectedItem(BaseModel):
    id: int
    entities_found: list[str] = Field(
        description="3-5 ключових фігурантів, компаній, цифр або локацій"
    )
    reason: str = Field(
        description="СУВОРО 1-3 слова. Лише категорія (наприклад: Спорт, Реклама, Побут інших міст, Світовий шум)"
    )


class NewsFilterResponse(BaseModel):
    keep: list[int]
    rejected: list[RejectedItem]


class TranslatedNews(BaseModel):
    id: int = Field(description="Числовий ID статті з масиву кандидатів")
    title: str = Field(description="Перекладений заголовок без HTML-тегів")
    content: str = Field(description="Основний текст новини з дозволеним HTML-форматуванням")
    hashtags: str = Field(description="2-3 хештеги одним рядком")


class TranslationResponse(BaseModel):
    translations: list[TranslatedNews]


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
            data={"chat_id": ADMIN_CHAT_ID, "text": f"⚠️ ALARM: {message}"[:4000]},
            timeout=10,
        )
        if resp.status_code != 200:
            logging.error("Помилка відправки алерта в Telegram: %s", resp.text[:1000])
    except Exception:
        logging.exception("Збій з'єднання під час надсилання алерта адміністратору")


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
        f"⏱ Час виконання: {stats['execution_time']} сек\n"
        f"⚠️ Помилок: {stats['errors_count']} | Повторних спроб Gemini: {stats['gemini_retries']}\n\n"
        "🔍 <b>Воронка:</b>\n"
        f"▪️ Переглянуто RSS-записів: {funnel['scraped_total']}\n"
        f"▪️ За джерелами: {html.escape(scraped_src)}\n"
        f"▪️ Уже відомі/застарілі: {funnel['already_seen']}\n"
        f"▪️ Відсіяно кодом: {funnel['rejected_by_keywords']}\n"
        f"▪️ Відхилено Gemini: {funnel['duplicates_removed_by_ai']}\n\n"
        "❌ <b>Причини відхилення:</b>\n"
        f"{reasons[:2500]}\n\n"
        f"📥 <b>Беклог: {backlog_stats['total_size']}</b>\n"
        f"▪️ {html.escape(backlog_src)}\n\n"
        f"✅ <b>Опубліковано: {published['total']}</b>\n"
        f"▪️ Із фото: {published['with_images']} | Текстом: {published['text_only']}\n"
        f"▪️ Український пріоритет: {published['ukraine_priority']}\n"
        f"▪️ Джерела: {html.escape(pub_src)}\n\n"
        "🤖 <b>Успішно використані моделі:</b> "
        f"Lite: {stats['models_used'].get('gemini-3.5-flash-lite', 0)} | "
        f"Pro: {stats['models_used'].get('gemini-3.6-flash', 0)}"
    )
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            api_url,
            data={"chat_id": ADMIN_CHAT_ID, "text": message[:4000], "parse_mode": "HTML"},
            timeout=10,
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
        elif "zadarskilist.hr" in host:
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


SPORTS_PATTERN = re.compile(
    r"\b(?:sport\w*|šport\w*|nogomet\w*|fudbal\w*|football\w*|"
    r"košark\w*|tenis\w*|utakmic\w*|meč\w*|turnir\w*|derbi\w*|"
    r"igrač\w*|igrači|футбол\w*|баскетбол\w*|спорт\w*|теніс\w*|"
    r"матч\w*|турнір\w*|спортсмен\w*)\b",
    re.IGNORECASE | re.UNICODE,
)
AD_PATTERN = re.compile(
    r"\b(?:oglas\w*|sponzor\w*|promo\w*|horoskop\w*|native\s+oglas\w*|"
    r"plaćeni\s+oglas\w*|nagradna\s+igra)\b",
    re.IGNORECASE | re.UNICODE,
)

# Keep every explicit exclusion from the original bot. These complement, rather than
# replace, the more general sport/ad patterns above.
LEGACY_UNWANTED_KEYWORDS = [
    "oglasi", "sponzorirani", "promo", "redikul", "horoskop", "plaćeni oglas",
    "nagradna igra", "native", "pr vijesti", "foto", "video", "sport", "nogomet",
    "košarka", "tenis", "utakmica", "hnl", "liga prvaka", "derbi", "trener",
    "igrači", "kk zadar", "nk zadar", "prvenstvo", "turnir", "bodovi", "polufinale",
]

def plain_text(value: str) -> str:
    return BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True)


def is_quality_article(title: str, description: str, url: str) -> bool:
    lowered_url = url.lower()
    unwanted_url_categories = ("/sport/", "/lifestyle/", "/magazin/")
    if any(category in lowered_url for category in unwanted_url_categories):
        return False

    text_to_check = f"{title} {plain_text(description)} {url}"
    if SPORTS_PATTERN.search(text_to_check) or AD_PATTERN.search(text_to_check):
        return False
    normalized_text = text_to_check.casefold()
    for keyword in LEGACY_UNWANTED_KEYWORDS:
        if re.search(rf"\b{re.escape(keyword.casefold())}\b", normalized_text, re.IGNORECASE | re.UNICODE):
            return False
    if len(title.strip()) < 15 or len(text_to_check.strip()) < 20:
        return False
    if "!!!" in title or "???" in title:
        return False

    letters = [char for char in title if char.isalpha()]
    if letters and sum(char.isupper() for char in letters) / len(letters) > 0.4:
        return False
    return True


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


def _generate_structured_response(prompt: str, schema, stats: dict | None, operation: str,
                                  expected_ids: set[int] | None = None, 
                                  temperature: float = 0.0):
    if client is None:
        raise RuntimeError("Gemini-клієнт не ініціалізований")

    last_error = None
    for model_name in GEMINI_MODELS:
        # Retry a transient failure once per model; otherwise move to fallback model.
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
                    if len(ids) != len(set(ids)):
                        raise ValueError("Gemini повернув повторювані ID перекладів")
                    if set(ids) != expected_ids:
                        raise ValueError(
                            f"Невідповідність ID перекладів: очікували {sorted(expected_ids)}, "
                            f"отримали {sorted(ids)}"
                        )

                if stats is not None:
                    stats["models_used"][model_name] = stats["models_used"].get(model_name, 0) + 1
                logging.info("Gemini %s: успішна структурована відповідь", model_name)
                return parsed

            except Exception as exc:
                last_error = exc
                should_retry_same_model = attempt == 0 and is_transient_gemini_error(exc)
                if stats is not None:
                    stats["gemini_retries"] += 1
                logging.warning(
                    "%s: модель %s, спроба %s не вдалася: %s",
                    operation, model_name, attempt + 1, exc,
                )
                if should_retry_same_model:
                    time.sleep(3)
                    continue
                break
        logging.warning("%s: переходимо до резервної моделі після помилки %s", operation, model_name)

    raise RuntimeError(f"Усі моделі Gemini не змогли виконати операцію {operation}") from last_error


def call_gemini_json(prompt: str, stats: dict | None = None) -> NewsFilterResponse:
    return _generate_structured_response(prompt, NewsFilterResponse, stats, "фільтрація новин", temperature=0.0)

HOMOGLYPHS = {
    "ά": "а", "α": "а", "ό": "о", "ο": "о",
    "έ": "е", "ε": "е", "ί": "і", "ι": "і",
    "ή": "и", "η": "и", "ν": "н", "Ν": "Н",
    "ב": "б", "ب": "б", "є́": "є", "і́": "і",
}

def clean_generated_field(value: str) -> str:
    """Restore the original Unicode/ellipsis cleanup, applied to fields, not raw JSON."""
    cleaned = str(value or "").replace("\u0301", "")
    cleaned = cleaned.replace("[...]", ".").replace("...", ".")
    for bad_char, good_char in HOMOGLYPHS.items():
        cleaned = cleaned.replace(bad_char, good_char)
    return cleaned

def generate_news_texts_json(prompt: str, expected_ids: set[int], stats: dict | None = None) -> list[dict]:
    # The original translation operation used temperature=0.1 and Pro-first model fallback.
    result = _generate_structured_response(
        prompt, TranslationResponse, stats, "переклад новин",
        expected_ids=expected_ids, temperature=0.1,
    )
    translations = [item.model_dump() for item in result.translations]
    for item in translations:
        for key in ("title", "content", "hashtags"):
            item[key] = clean_generated_field(item.get(key, ""))
    return translations


# ---------------------------------------------------------------------------
# Editorial filter: fail closed and validate the entire response partition
# ---------------------------------------------------------------------------
def deduplicate_news(articles: list[dict], history_titles: list[str] | None = None,
                     stats: dict | None = None) -> tuple[list[dict], list[dict]]:
    if not articles:
        return [], []

    recent_history = history_titles[-75:] if history_titles else []
    prompt_data = [
        {"id": idx, "title": article["title"], "description": plain_text(article.get("description", ""))}
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

    КРОК 4. СТОП-ТЕМИ (АБСОЛЮТНА ЗАБОРОНА): 
    УВАГА: Цей крок перекриває всі інші! Навіть якщо в новині є слово "Задар" або "Україна", але новина стосується наступних тем — ВІДХИЛЯЙ ЇЇ.
1. СПОРТ: Будь-які матчі (зокрема баскетбольний клуб Задар), турніри, результати, дербі, спортсмени, тайми.
2. РЕКЛАМА: Спонсорські матеріали, промо-акції, розіграші.
3. ПОБУТ ЧУЖИХ МІСТ: Ремонти доріг, відключення води, локальні затори, страйки комунальників, дрібні ДТП у Загребі, Спліті, Рієці чи БУДЬ-ЯКОМУ іншому місті, ОКРІМ Задара, виняток — концерти, культурні події та фестивалі;
4. ІНШІ КРАЇНИ (ГЕОБЛОК): БУДЬ-ЯКІ події за межами Хорватії та України (навіть масові вбивства, теракти, вибори, катастрофи чи великий кримінал у Польщі, США, Франції тощо), ЯКЩО вони прямо не стосуються українських біженців або українців.
5. СВІТОВИЙ ШУМ: Абстрактна наука, поради дієтологів, рецепти.

    КРОК 5. ТЕМАТИКА (ЩО ОБОВ'ЯЗКОВО ЗАЛИШИТИ): 
1. ЗАДАР (Пріоритет №1): Побут, перекриття вулиць, тарифи, ремонти, відключення світла/води, погода, місцевий кримінал. 
2. ХОРВАТІЯ (Національний рівень): Важливі події всієї країни — великий кримінал, політика, економіка.
3. УКРАЇНА ТА  УКРАЇНСЬКІ БІЖЕНЦІ: Головні події в Україні (війна, енергетика, економіка, життя, політика, технології, оборона), рішення ЄС (Директива 2001/55), виплати українським біженцям.

    КРОК 6. ФІНАЛЬНИЙ ФІЛЬТР:
    Якщо новина не підпадає під ЖОДНУ з категорій КРОКУ 5 — ти ЗОБОВ'ЯЗАНИЙ її відхилити. Ніяких винятків для "цікавих світових новин".

CANDIDATES:
{json.dumps(prompt_data, ensure_ascii=False, indent=2)}

HISTORY:
{json.dumps(recent_history, ensure_ascii=False, indent=2)}

Поверни результат СУВОРО у форматі валідного JSON-об'єкта з двома ключами:
    1. "keep": масив ID новин, які треба ЗАЛИШИТИ (наприклад, [0, 2]).
    2. "rejected": масив об'єктів для відхилених новин, де кожен містить:
       - "id": номер новини,
       - "entities_found": масив з 3-5 ключових сутностей (фігуранти, компанії, локації),
       - "reason": СУВОРО 1-3 слова, лише категорія відхилення (наприклад: "Спорт", "Реклама", "Побут інших міст", "Дубль в історії", "Світовий шум", "Геоблок"). КАТЕГОРИЧНО ЗАБОРОНЕНО писати довгі речення чи пояснення!
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
        if set(keep_ids).union(rejected_ids) != candidate_ids:
            raise ValueError("Gemini не класифікував усі кандидати; беклог не змінюємо")

        filtered = [articles[idx] for idx in keep_ids]
        rejected = [item.model_dump() for item in result.rejected]
        rejection_log = []
        for rejected_item in result.rejected:
            title = articles[rejected_item.id]["title"]
            entities = ", ".join(rejected_item.entities_found)
            rejection_log.append(
                f"• {title[:70]} → {rejected_item.reason[:100]} | Сутності: {entities[:160]}"
            )
        if stats is not None:
            stats["funnel"]["rejection_reasons_list"] = rejection_log
        logging.info("Gemini залишив %s з %s кандидатів", len(filtered), len(articles))
        for rejected_item in result.rejected:
            title = articles[rejected_item.id]["title"]
            entities = ", ".join(rejected_item.entities_found)
            reason = rejected_item.reason
            logging.info(
                "Відхилено Gemini | ID=%s | причина=%s | сутності=%s | заголовок=%s",
                rejected_item.id, reason, entities, title,
            )
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

    # Enforce editorial limits on the visible translation, before adding source attribution.
    image_urls = original_article.get("image_urls") or original_article.get("image_url")
    max_visible_chars = 900 if image_urls else 2500
    visible_length = len(title) + 2 + len(content_plain) + 2 + len(hashtags)
    if visible_length > max_visible_chars:
        logging.warning(
            "Переклад %s задовгий: %s символів, ліміт %s",
            translation.get("id"), visible_length, max_visible_chars,
        )
        return False
    if "..." in title or "..." in content:
        logging.warning("Переклад %s містить заборонені трикрапки", translation.get("id"))
        return False
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

    resp = requests.post(api_url, data=plain_payload, timeout=15)
    return _response_to_telegram_result(resp)


@retry(
    stop=stop_after_attempt(4),
    wait=_telegram_wait,
    reraise=True,
    retry=retry_if_exception(_telegram_retry_predicate),
)
def execute_telegram_request(api_url: str, payload: dict):
    resp = requests.post(api_url, data=payload, timeout=15)
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

def send_to_telegram(text: str, url: str, source_name: str, image_urls=None, tags: str = "") -> tuple[bool, str]:
    image_urls = [] if not image_urls else ([image_urls] if isinstance(image_urls, str) else list(image_urls))
    image_urls = image_urls[:10]

    parsed_url = urlparse(url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError(f"Небезпечне або некоректне посилання на джерело: {url!r}")

    body, hashtag_suffix = _extract_hashtag_suffix(text)
    extra_tags = str(tags or "").strip()
    suffix_parts = [part for part in (hashtag_suffix, extra_tags) if part]
    suffix = "\n\n".join(suffix_parts)
    safe_url = html.escape(url, quote=True)
    safe_source = html.escape(source_name or "Джерело", quote=True)
    source_line = f'\n\n🔗 Джерело: <a href="{safe_url}">{safe_source}</a>'
    crop_note = "\n\n👉 <i>Повні деталі читайте за посиланням нижче</i> 👇"
    api_url_text = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    def compose(candidate_body: str) -> str:
        pieces = [candidate_body.strip()]
        if crop_note.strip() in candidate_body:
            pass
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
                "chat_id": TELEGRAM_CHANNEL_ID,
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
            "chat_id": TELEGRAM_CHANNEL_ID,
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
                    "chat_id": TELEGRAM_CHANNEL_ID,
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
            "chat_id": TELEGRAM_CHANNEL_ID,
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
                "chat_id": TELEGRAM_CHANNEL_ID,
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
    r"\bodesa", r"\bizbjeglic", r"\bhumanitar", r"\bmigrant",
    r"\bprivremen\w*\s+zaštit", r"\bdirektiv\w*\s*2001",
]


def is_ukraine_priority(article: dict) -> bool:
    text = (article.get("title", "") + " " + plain_text(article.get("description", ""))).lower()
    return any(re.search(pattern, text) for pattern in UKRAINE_KEYWORDS)


def prioritize_ukraine_news(backlog_items: list[dict]) -> list[dict]:
    ukraine = [item for item in backlog_items if is_ukraine_priority(item)]
    other = [item for item in backlog_items if not is_ukraine_priority(item)]
    return ukraine + other


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
            if not feed.entries:
                logging.warning("RSS не містить записів: %s", feed_url)
                continue
        except Exception:
            logging.exception("Помилка доступу/парсингу RSS %s", feed_url)
            if stats is not None:
                stats["errors_count"] += 1
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
            if any(tag in blacklist_categories for tag in entry_tags):
                if stats is not None:
                    stats["funnel"]["rejected_by_keywords"] += 1
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

            if not is_quality_article(title, description_plain, article_url):
                if stats is not None:
                    stats["funnel"]["rejected_by_keywords"] += 1
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
2. ДЕТАЛІЗАЦІЯ ДОКУМЕНТІВ (ДИРЕКТИВА 2001/55/ЄС): Якщо новина стосується тимчасового захисту, прав біженців, виплат чи документів, ПЕРЕКЛАДАЙ ЇЇ МАКСИМАЛЬНО ДЕТАЛЬНО. Не скорочуй жодної цифри, дати чи юридичної назви. Додай на початок заголовка емодзі 📄 або 🇪🇺.
3. ПРІОРИТЕТ "УКРАЇНА": Якщо в оригінальній новині згадується Україна, українці, війна або біженці — обов'язково постав емодзі 🇺🇦 на самий початок заголовка та винеси цю інформацію у перший абзац, щоб привернути увагу.
4. ПРИРОДНІСТЬ МОВИ ТА СЛОВНИК: Перекладай живою та природною українською мовою. УНИКАЙ ДОСЛІВНОГО ПЕРЕКЛАДУ (кальок) — передавай суть фрази, а не перекладай слово за словом. КАТЕГОРИЧНО ЗАБОРОНЕНО вигадувати неіснуючі слова (наприклад, замість кальки "бізшрний" чи "бізарний" використовуй існуючі аналоги: "дивний", "курйозний", "химерний"). Усі слова у твоєму тексті мають існувати в словнику української мови.«УВАЖНО ПЕРЕВІРЯЙ ОРФОГРАФІЮ: Звертай особливу увагу на використання апострофа (наприклад: "м'ясо", "комп'ютер"). Ніколи не пропускай літери.»
5. ТОПОНІМИ ТА ІМЕНА: Назви міст (наприклад, Задар, Спліт, Загреб) транслітеруй українською. Хорватські імена, прізвища, назви вулиць, компаній і продуктів (наприклад, Apple, Facebook) залишай оригінальною латиницею без змін. 
6. ЖУРНАЛІСТСЬКИЙ СТИЛЬ (АНТИ-КЛІКБЕЙТ): Пиши у стриманому стилі якісних медіа (як BBC). КАТЕГОРИЧНО ЗАБОРОНЕНО використовувати жовтий клікбейт та штучні епітети (наприклад, "зіркова", "шокуюча", "скандальна", "кривава"). Роби текст легким для читання. Абзаци мають бути короткими (не більше 3-4 речень). Уникай сухого поліцейського сленгу та дослівного перекладу юридичних термінів. Наприклад, "prekršajni odjel" адаптуй як "відділ з адміністративних правопорушень" (а не "проміжний відділ"). 
7. НІЯКОГО ШІ-КОНТЕКСТУ: Видавай СУВОРО лише готовий текст новини. Жодних вступних фраз, привітань, коментарів чи висновків від себе. Зберігай абсолютну журналістську об'єктивність.
8. АКЦЕНТИ: Заголовок — без HTML. У content дозволені тільки HTML-теги <b>, <i>, <u>, <s>; теги мають бути коректно закриті. Не використовуй Markdown. Виділяй жирним ключові суми штрафів, дати, імена посадовців і назви законів.
9. ХЕШТЕГИ: В самому кінці тексту обов'язково додай 2-3 хештеги з нового рядка. 
    ПРАВИЛО ГЕОГРАФІЇ: Перший хештег — це ГЕОГРАФІЯ, і він має бути ТІЛЬКИ ОДИН. Якщо ти знаєш місто — пиши ТІЛЬКИ його латиницею (наприклад, #Zadar, #Split, #SeverinNaKupi). Якщо точне місто невідоме — пиши ТІЛЬКИ назву країни (наприклад, #Hrvatska або #Moldova). КАТЕГОРИЧНО ЗАБОРОНЕНО ставити хештег країни (наприклад, #Хорватія чи #Hrvatska), якщо ти вже поставив хештег конкретного міста!
    ТЕМАТИЧНІ ХЕШТЕГИ: Другі хештеги мають СУВОРО відповідати змісту статті (наприклад, #Економіка, #Суспільство, #Закон, #Політика, #Кримінал). Категорично ЗАБОРОНЕНО ставити хештеги #ТимчасовийЗахист або #Україна, якщо в самому тексті новини про це не йдеться.
10. ОНОВЛЕННЯ ПОДІЙ: Якщо ти бачиш, що новина є продовженням чи оновленням попередніх подій (наприклад, завершення слідства, нові деталі аварії), додай на самий початок заголовка префікс 🔄 [ОНОВЛЕНО]:
11. ДОВЖИНА: якщо є фото, видимий текст перекладу (заголовок, тіло й хештеги) має бути не довшим за 900 символів. Без фото — не довшим за 2500 символів. Якщо текст довший, стисни головну суть без втрати важливих фактів, цифр або дат. Не обрізай речення на півслові; якщо вихідний текст обірваний або закінчується на три крапки / [...], прибери незавершене речення і не вигадуй продовження. Для розширених текстів використовуй марковані списки
(із символом • або -), щоб інформацію було зручно читати.
12. ID: У полі id поверни саме ціле числове ID з блока новини. Не повертай URL як ID.

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
    len_before = len(candidate_batch)
    history_titles = load_history(TITLES_FILE)

    # If filtering fails or its output is inconsistent, this raises before the backlog is altered.
    approved_articles, rejected_items = deduplicate_news(candidate_batch, history_titles, stats)
    approved_urls = {item["url"] for item in approved_articles}
    rejected_urls = evaluated_urls - approved_urls
    if stats is not None:
        stats["funnel"]["duplicates_removed_by_ai"] += len(rejected_urls)

    batch = approved_articles[:BATCH_SIZE]
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
        translations = generate_news_texts_json(prompt, expected_ids, stats)
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

        formatted_text = (
            f"<b>{translation['title']}</b>\n\n"
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
        time.sleep(POST_PUBLICATION_DELAY_SECONDS)

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
            "duplicates_removed_by_ai": 0,
            "rejection_reasons_list": [],
        },
        "backlog": {"total_size": 0, "sources": {}},
        "published": {
            "total": 0,
            "ukraine_priority": 0,
            "with_images": 0,
            "text_only": 0,
            "sources": {},
        },
        "models_used": {model: 0 for model in GEMINI_MODELS},
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


def main() -> None:
    global client
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, GEMINI_API_KEY]):
        logging.critical("Не налаштовані TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID або GEMINI_API_KEY")
        sys.exit(1)

    client = genai.Client(api_key=GEMINI_API_KEY)
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
        try:
            save_analytics(stats)
        except Exception:
            critical_error = True
            logging.exception("Не вдалося зберегти аналітику")
        if stats["published"]["total"] > 0 or stats["errors_count"] > 0:
            send_analytics_to_admin(stats)

    # Non-zero process status ensures the scheduled runner records partial/critical failures.
    if critical_error or stats["errors_count"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
