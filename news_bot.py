import feedparser
import requests
import os
import time
import calendar
import logging
import json
import re
import random
import shutil
import sys
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urlunparse
from dotenv import load_dotenv
from google import genai
from google.genai import types  
from pydantic import BaseModel, Field
import cloudscraper  
from tenacity import retry, stop_after_attempt, wait_exponential

# Додаємо маскування під звичайний браузер
feedparser.USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

# ================= 1. НАЛАШТУВАННЯ =================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHANNEL_ID = os.getenv('TELEGRAM_CHANNEL_ID')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
ADMIN_CHAT_ID = os.getenv('ADMIN_CHAT_ID') 

HISTORY_FILE = 'posted_news.txt'
TITLES_FILE = 'posted_titles.txt'
BACKLOG_FILE = 'news_backlog.json'
FEEDS_FILE = 'feeds.json'
ANALYTICS_FILE = 'analytics.json'

MAX_HISTORY_SIZE = 1000 
MAX_AGE_SECONDS = 43200 # 12 годин
BATCH_SIZE = 3 # 3 новини за один запуск

client = genai.Client(api_key=GEMINI_API_KEY)
# ===================================================

# --- PYDANTIC СХЕМИ ---
class RejectedItem(BaseModel):
    id: int
    entities_found: list[str] = Field(description="3-5 ключових фігурантів, компаній, цифр або локацій")
    reason: str

class NewsFilterResponse(BaseModel):
    keep: list[int]
    rejected: list[RejectedItem]

class TranslatedNews(BaseModel):
    """Схема для валідації перекладеного тексту (Пункт 1)[cite: 6]"""
    id: int = Field(description="ID статті з масиву кандидатів")
    title: str = Field(description="Перекладений заголовок (без HTML тегів <b>)")
    content: str = Field(description="Основний текст новини з HTML форматуванням")
    hashtags: str = Field(description="Усі хештеги одним рядком (наприклад, #Zadar #Суспільство)")

class TranslationResponse(BaseModel):
    translations: list[TranslatedNews]

# --- ДОПОМІЖНІ ФУНКЦІЇ ---

def normalize_url(url):
    """Очищає URL від рекламних UTM-міток та параметрів (Пункт 6)[cite: 6]"""
    try:
        parsed = urlparse(url)
        # Залишаємо схему, домен і шлях. Відкидаємо параметри, запити та фрагменти.
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', ''))
    except Exception:
        return url

def trim_history(filename, max_lines=1000):
    try:
        if os.path.exists(filename):
            with open(filename, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            if len(lines) > max_lines:
                with open(filename, 'w', encoding='utf-8') as f:
                    f.writelines(lines[-max_lines:])
    except Exception as e:
        logging.error(f"Помилка обрізки файлу {filename}: {e}")

def fetch_full_article_text(url, scraper):
    try:
        resp = scraper.get(url, timeout=10) 
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
            paragraphs = soup.find_all('p')
            full_text = ' '.join([p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 30])
            if full_text:
                return full_text[:3500] 
    except Exception as e:
        logging.error(f"⚠️ Не вдалося завантажити повний текст {url}: {e}")
    return None

def send_alert_to_admin(message):
    if not ADMIN_CHAT_ID: 
        return
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        response = requests.post(api_url, data={'chat_id': ADMIN_CHAT_ID, 'text': f"⚠️ ALARM: {message}"}, timeout=10) 
        if response.status_code != 200:
            logging.error(f"Помилка відправки алерта в Telegram: {response.text}")
    except Exception as e:
        logging.error(f"Збій з'єднання при відправці алерта в Telegram: {e}")

def load_history(filename):
    if not os.path.exists(filename): return []
    with open(filename, 'r', encoding='utf-8') as f: return [line.strip() for line in f if line.strip()]

def save_to_history(history_list, new_item, filename):
    history_list.append(new_item)
    history_list = history_list[-MAX_HISTORY_SIZE:]
    with open(filename, 'w', encoding='utf-8') as f:
        for item in history_list: f.write(item + '\n')
    return history_list

def load_backlog():
    if not os.path.exists(BACKLOG_FILE): return []
    try:
        with open(BACKLOG_FILE, 'r', encoding='utf-8') as f: 
            return json.load(f)
    except json.JSONDecodeError as e:
        logging.error(f"❌ Помилка читання беклогу: {e}. Спроба відновити з резервної копії.")
        bak_file = f"{BACKLOG_FILE}.bak"
        if os.path.exists(bak_file):
            try:
                with open(bak_file, 'r', encoding='utf-8') as f:
                    logging.info("Беклог успішно відновлено з резервної копії.")
                    return json.load(f)
            except Exception as bak_e:
                logging.error(f"❌ Резервна копія також пошкоджена: {bak_e}.")
        return []

def save_backlog(backlog):
    temp_file = f"{BACKLOG_FILE}.tmp"
    bak_file = f"{BACKLOG_FILE}.bak"
    
    if os.path.exists(BACKLOG_FILE):
        shutil.copy2(BACKLOG_FILE, bak_file) 
        
    try:
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(backlog, f, ensure_ascii=False, indent=4)
        os.replace(temp_file, BACKLOG_FILE) 
    except Exception as e:
        logging.error(f"Помилка при збереженні беклогу: {e}")
        if os.path.exists(temp_file):
            os.remove(temp_file)

def load_analytics():
    if not os.path.exists(ANALYTICS_FILE): return []
    try:
        with open(ANALYTICS_FILE, 'r', encoding='utf-8') as f: return json.load(f)
    except: return []

def save_analytics(stats):
    history = load_analytics()
    history.append(stats)
    temp_file = f"{ANALYTICS_FILE}.tmp"
    
    try:
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(history[-100:], f, ensure_ascii=False, indent=4)
        os.replace(temp_file, ANALYTICS_FILE) 
    except Exception as e:
        logging.error(f"Помилка при збереженні аналітики: {e}")
        if os.path.exists(temp_file):
            os.remove(temp_file)

def send_analytics_to_admin(stats):
    if not ADMIN_CHAT_ID: 
        logging.warning("ADMIN_CHAT_ID не налаштовано")
        return
        
    scraped_src_str = ", ".join([f"{k}: {v}" for k, v in stats['funnel'].get('scraped_by_source', {}).items()]) if stats['funnel'].get('scraped_by_source') else "Немає"
    backlog_src_str = ", ".join([f"{k}: {v}" for k, v in stats['backlog']['sources'].items()]) if stats['backlog']['sources'] else "Порожньо"
    pub_src_str = ", ".join([f"{k}: {v}" for k, v in stats['published']['sources'].items()]) if stats['published']['sources'] else "Немає"

    reasons_list = stats['funnel'].get('rejection_reasons_list', [])
    reasons_str = "\n".join(reasons_list) if reasons_list else "Нічого не відсіяно або немає деталей"

    message = (
        f"📊 <b>Розширений звіт бота (Хорватія)</b>\n\n"
        f"⏱ Час виконання: {stats['execution_time']} сек\n"
        f"⚠️ Помилок: {stats['errors_count']} | Ретраїв Gemini: {stats['gemini_retries']}\n\n"
        f"🔍 <b>Воронка фільтрації:</b>\n"
        f"▪️ Знайдено в RSS: {stats['funnel']['scraped_total']}\n"
        f"▪️ По джерелах: {scraped_src_str}\n"
        f"▪️ Пропущено (старі/вже в історії): {stats['funnel']['already_seen']}\n"
        f"▪️ Відсіяно фільтрами (код): {stats['funnel']['rejected_by_keywords']}\n"
        f"▪️ Відсіяно Gemini (дублі/історія): {stats['funnel']['duplicates_removed_by_ai']}\n\n"
        f"❌ <b>Чому ШІ відхилив новини:</b>\n"
        f"{reasons_str}\n\n"
        f"📥 <b>Беклог (очікують ШІ-валідації: {stats['backlog']['total_size']}):</b>\n"
        f"▪️ {backlog_src_str}\n\n"
        f"✅ <b>Опубліковано: {stats['published']['total']}</b>\n"
        f"▪️ Формат: З фото ({stats['published']['with_images']}) | Текст ({stats['published']['text_only']})\n"
        f"▪️ Пріоритет 🇺🇦: {stats['published']['ukraine_priority']}\n"
        f"▪️ Джерела: {pub_src_str}\n\n"
        f"🤖 <b>ШІ моделі:</b> Lite: {stats['models_used'].get('gemini-3.5-flash-lite', 0)} | Pro: {stats['models_used'].get('gemini-3.6-flash', 0)}\n\n"
    )
    
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(api_url, data={'chat_id': ADMIN_CHAT_ID, 'text': message, 'parse_mode': 'HTML'}, timeout=10)
    except Exception as e: 
        logging.error(f"Збій з'єднання з Telegram API: {e}")

def get_rss_feeds():
    if not os.path.exists(FEEDS_FILE): return []
    try:
        with open(FEEDS_FILE, 'r', encoding='utf-8') as f: return json.load(f)
    except Exception as e:
        logging.error(f"Помилка читання feeds.json: {e}")
        return []

def extract_image_urls(entry):
    unsupported_formats = ('.webp', '.avif', '.svg')
    urls = []
    if 'media_content' in entry:
        for media in entry.media_content:
            url = media.get('url')
            if url and not url.lower().endswith(unsupported_formats) and url not in urls: 
                urls.append(url)
    if 'enclosures' in entry:
        for enc in entry.enclosures:
            if enc.get('type', '').startswith('image/'):
                url = enc.get('href')
                if url and not url.lower().endswith(unsupported_formats) and url not in urls: 
                    urls.append(url)
    return urls[:10]

def get_source_name(url):
    try:
        parsed_uri = urlparse(url)
        domain = parsed_uri.netloc
        if domain.startswith('www.'): domain = domain[4:]
        return domain.capitalize()
    except: return "Джерело"

def is_quality_article(title, description, url):
    unwanted_url_categories = ['/sport/', '/scena/', '/lifestyle/', '/magazin/']
    url_lower = url.lower()
    if any(category in url_lower for category in unwanted_url_categories):
        return False

    unwanted_roots = [
        'oglas', 'sponzor', 'promo', 'horoskop', 'native', 
        'sport', 'nogomet', 'košark', 'tenis', 'utakmic', 'turnir', 'derbi'
    ]
    
    text_to_check = f"{title} {description} {url}".lower()
    
    if any(root in text_to_check for root in unwanted_roots):
        return False
            
    if len(title.strip()) < 15 or len(text_to_check.strip()) < 20: return False
    if "!!!" in title or "???" in title: return False
    
    letters = [c for c in title if c.isalpha()]
    if letters and (sum(1 for c in letters if c.isupper()) / len(letters)) > 0.4: return False
    
    return True

# --- ІНТЕГРАЦІЯ TENACITY ТА GEMINI ---

@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=2, min=5, max=60))
def call_gemini_json(client, prompt, stats=None):
    models_to_try = ['gemini-3.6-flash', 'gemini-3.5-flash-lite']
    last_error = None
    
    for current_model in models_to_try:
        try:
            response = client.models.generate_content(
                model=current_model, 
                contents=prompt, 
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=NewsFilterResponse,
                    temperature=0.0,
                    safety_settings=[
                        types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
                    ]
                )
            )
            
            if not response or not getattr(response, 'text', None):
                raise ValueError(f"Порожня відповідь (None) з моделі {current_model}")
            
            if stats is not None:
                stats["models_used"][current_model] = stats["models_used"].get(current_model, 0) + 1
                
            return response
            
        except Exception as e:
            logging.warning(f"⚠️ Дедуплікація: Модель {current_model} недоступна ({e}). Перемикаємось...")
            last_error = e
            time.sleep(5) 
            
    if stats is not None: stats["gemini_retries"] += 1
    raise last_error

@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=2, min=15, max=60))
def generate_news_texts_json(client, prompt, expected_count, stats=None):
    """ОНОВЛЕНО: Генерація тексту у форматі JSON (Пункт 1)[cite: 6]"""
    models_to_try = ['gemini-3.6-flash', 'gemini-3.5-flash-lite']
    last_error = None
    
    for current_model in models_to_try:
        try:
            response = client.models.generate_content(
                model=current_model, 
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=TranslationResponse,
                    temperature=0.1,
                    safety_settings=[
                        types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
                    ]
                )
            )
            
            if not response or not getattr(response, 'text', None):
                raise ValueError(f"Порожня відповідь від {current_model}")
                
            logging.info(f"Текст від Gemini JSON ({current_model}):\n{response.text}")
                
            # Очищення галюцинацій у JSON тексті
            HOMOGLYPHS = {
                'ά': 'а', 'α': 'а', 'ό': 'о', 'ο': 'о', 
                'έ': 'е', 'ε': 'е', 'ί': 'і', 'ι': 'і', 
                'ή': 'и', 'η': 'и', 'ν': 'н', 'Ν': 'Н',
                'ב': 'б', 'ب': 'б',
                'є́': 'є', 'і́': 'і'
            }
            clean_text = response.text.replace('\u0301', '')
            clean_text = clean_text.replace('...', '.').replace('[...]', '.')
            
            for bad_char, good_char in HOMOGLYPHS.items():
                clean_text = clean_text.replace(bad_char, good_char)
                
            data = json.loads(clean_text)
            translations = data.get("translations", [])
            
            if len(translations) != expected_count: 
                raise ValueError(f"Невірна кількість текстів від {current_model}: очікували {expected_count}, отримали {len(translations)}")
                
            if stats is not None:
                stats["models_used"][current_model] = stats["models_used"].get(current_model, 0) + 1
                
            return response, translations
            
        except Exception as e:
            logging.warning(f"⚠️ Переклад: Модель {current_model} недоступна ({e}). Перемикаємось...")
            last_error = e
            time.sleep(5)
            
    if stats is not None: stats["gemini_retries"] += 1
    raise last_error

def handle_telegram_html_error(api_url, payload):
    logging.warning("⚠️ Помилка HTML-розмітки. Відправляємо без HTML (очищений текст)...[cite: 6]")
    
    def strip_html(text):
         return re.sub('<[^<]+>', '', text)
         
    if 'text' in payload:
        payload['text'] = strip_html(payload['text'])
    elif 'caption' in payload:
        payload['caption'] = strip_html(payload['caption'])

    if 'media' in payload:
        try:
             media_objs = json.loads(payload['media'])
             for obj in media_objs:
                 if 'caption' in obj:
                     obj['caption'] = strip_html(obj['caption'])
                 if 'parse_mode' in obj:
                     del obj['parse_mode']
             payload['media'] = json.dumps(media_objs)
        except json.JSONDecodeError:
             pass

    if 'parse_mode' in payload: 
        del payload['parse_mode']
        
    resp = requests.post(api_url, data=payload, timeout=10)
    if resp.status_code != 200:
        raise Exception(f"Telegram Error after HTML strip {resp.status_code}: {resp.text}")
    return True

@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=2, min=3, max=30), reraise=True)
def execute_telegram_request(api_url, payload):
    try:
        resp = requests.post(api_url, data=payload, timeout=15) 
        
        try:
            resp_json = resp.json()
            if not resp_json.get('ok'):
                error_code = resp_json.get('error_code')
                description = resp_json.get('description', '')
                
                if error_code == 400 and "parse entities" in description.lower():
                     return handle_telegram_html_error(api_url, payload)
                
                raise Exception(f"Telegram API Error {error_code}: {description}[cite: 6]")
                
        except json.JSONDecodeError:
             if resp.status_code == 200: return True
             raise Exception(f"Telegram Error {resp.status_code}: {resp.text}")
             
    except requests.exceptions.RequestException as e:
        logging.error(f"Мережева помилка під час запиту до Telegram: {e}[cite: 6]")
        raise 

    return True

def validate_translation_simple(translation):
    """Програмна перевірка тексту перед публікацією (Пункт 1)[cite: 6]."""
    content = translation.get("content", "")
    hashtags = translation.get("hashtags", "")
    
    if len(content) > 4000:
         logging.warning(f"⚠️ Текст {translation.get('id')} занадто довгий для публікації.[cite: 6]")
         return False
    
    if "#Sport" in hashtags or "#Спорт" in hashtags:
         logging.warning(f"⚠️ Текст {translation.get('id')} містить заборонений хештег #Sport.[cite: 6]")
         return False
         
    if re.search(r'[A-Za-z]', content):
         logging.warning(f"⚠️ Текст {translation.get('id')} можливо містить латиницю. Перевірте.[cite: 6]")
         
    return True

# --- ОСНОВНА ЛОГІКА ---

def deduplicate_news(client, articles, history_titles=None, stats=None):
    if not articles: return articles, [] 
        
    logging.info("Аналіз на дублікати (включно з історією) через Gemini...")
    recent_history = history_titles[-75:] if history_titles else []
    prompt_data = [{"id": i, "title": a["title"], "description": a.get("description", "")} for i, a in enumerate(articles)]
    
    titles_log = "\n".join([f"[{i}] {a['title']}" for i, a in enumerate(articles)])
    logging.info(f"📋 Кандидати на фільтрацію:\n{titles_log}")

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
    3. ПОБУТ ЧУЖИХ МІСТ: Ремонти доріг, відключення води, локальні затори, страйки комунальників, дрібні ДТП у Загребі, Спліті, Рієці чи БУДЬ-ЯКОМУ іншому місті, ОКРІМ Задара.
    4. ІНШІ КРАЇНИ (ГЕОБЛОК): БУДЬ-ЯКІ події за межами Хорватії та України (навіть масові вбивства, теракти, вибори, катастрофи чи великий кримінал у Польщі, США, Франції тощо), ЯКЩО вони прямо не стосуються українських біженців або українців.
    5. СВІТОВИЙ ШУМ: Абстрактна наука, поради дієтологів, рецепти.


    КРОК 5. ТЕМАТИКА (ЩО ОБОВ'ЯЗКОВО ЗАЛИШИТИ): 
    1. ЗАДАР (Пріоритет №1): Побут, перекриття вулиць, тарифи, ремонти, відключення світла/води, погода, місцевий кримінал. 
    2. ХОРВАТІЯ (Національний рівень): Важливі події всієї країни — великий кримінал, політика, економіка.
    3. УКРАЇНА ТА  УКРАЇНСЬКІ БІЖЕНЦІ: Головні події в Україні (війна, енергетика, економіка, життя, політика, технології, оборона), рішення ЄС (Директива 2001/55), виплати українським біженцям.

    КРОК 6. ФІНАЛЬНИЙ ФІЛЬТР:
    Якщо новина не підпадає під ЖОДНУ з категорій КРОКУ 5 — ти ЗОБОВ'ЯЗАНИЙ її відхилити. Ніяких винятків для "цікавих світових новин".

    <CANDIDATES>
    {json.dumps(prompt_data, ensure_ascii=False, indent=2)}
    </CANDIDATES>

    <HISTORY>
    {json.dumps(recent_history, ensure_ascii=False, indent=2)}
    </HISTORY>
    
    Поверни результат СУВОРО у форматі валідного JSON-об'єкта з двома ключами:
    1. "keep": масив ID новин, які треба ЗАЛИШИТИ (наприклад, [0, 2]).
    2. "rejected": масив об'єктів для відхилених новин, де кожен містить:
       - "id": номер новини,
       - "entities_found": масив з 3-5 ключових сутностей (фігуранти, компанії, локації),
       - "reason": коротка причина відхилення (наприклад: "Спорт", "Побут Спліта").
    """

    try:  
        response = call_gemini_json(client, prompt, stats)
        text_response = response.text.strip()
        
        if "```json" in text_response:
            text_response = text_response.split("```json")[1].split("```")[0].strip()
        elif "```" in text_response:
            text_response = text_response.split("```")[1].split("```")[0].strip()

        data = json.loads(text_response)  
        
        keep_ids = [int(item) for item in data.get("keep", []) if str(item).isdigit()]
        rejected_items = data.get("rejected", [])
        
        filtered_articles = [articles[i] for i in keep_ids if i < len(articles)]  
        
        rejection_log = []
        for item in rejected_items:
            if isinstance(item, dict) and 'id' in item:
                idx = int(item['id'])
                reason = item.get('reason', 'Не вказано')
                if idx < len(articles):
                    title = articles[idx]['title']
                    rejection_log.append(f"• {title[:40]}... ➡️ <b>{reason}</b>")
        
        if stats is not None:
            stats["funnel"]["rejection_reasons_list"] = rejection_log

        removed_count = len(articles) - len(filtered_articles)
        if removed_count > 0: 
            logging.info(f"🗑️ Відсіяно ШІ (дублі/спам/історія): {removed_count}")
            for log_entry in rejection_log:
                logging.info(f"   {log_entry.replace('<b>', '').replace('</b>', '')}")
                
        return filtered_articles, rejected_items 
        
    except Exception as e:
        logging.error(f"❌ Gemini API дедуплікації недоступний: {e}. Зупиняємо фільтрацію (Fail Closed)[cite: 6].")
        send_alert_to_admin(f"Gemini API дедуплікації недоступний: {e}") # ОНОВЛЕНО: Сповіщення про збій (Пункт 3)[cite: 6]
        return [], []

def prioritize_ukraine_news(backlog_items):
    ukraine_keywords = [
        r'\bukrajin', r'\bdijaspor', r'\bzelensk', r'\bzelenšk', r'\bkijev', 
        r'\bodesa', r'\bizbjeglic', r'\bhumanitar', r'\bmigrant',
        r'\bprivremen\w*\s+zaštit', 
        r'\bdirektiv\w*\s*2001'
    ]
    
    ukraine_news = []
    other_news = []
    
    for item in backlog_items:
        text_context = (item.get('title', '') + " " + item.get('description', '')).lower()
        if any(re.search(kw, text_context) for kw in ukraine_keywords):
            ukraine_news.append(item)
        else:
            other_news.append(item)
            
    return ukraine_news + other_news

def scrape_feeds(stats=None):
    history_urls = load_history(HISTORY_FILE)
    history_titles = load_history(TITLES_FILE)
    backlog = load_backlog()
    current_time = time.time()
    
    backlog = [item for item in backlog if current_time - item.get('timestamp', current_time) <= MAX_AGE_SECONDS]
    backlog_urls = [item['url'] for item in backlog]

    scraper = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False})

    for feed_item in get_rss_feeds():
        feed_url = feed_item['url']
        source_name = feed_item.get('name', get_source_name(feed_url))
        
        if stats is not None and "scraped_by_source" not in stats["funnel"]:
            stats["funnel"]["scraped_by_source"] = {}
            
        source_count = 0 
        
        try: 
            headers = {'Accept': 'application/rss+xml, application/xml, text/xml, */*'}
            response = scraper.get(feed_url, headers=headers, timeout=15)
            
            if response.status_code not in [200, 301, 302]:
                logging.error(f"❌ Помилка доступу до {feed_url}: HTTP статус {response.status_code}")
                if stats is not None: stats["errors_count"] += 1 # ОНОВЛЕНО: Облік помилок (Пункт 4)[cite: 6]
                continue
                
            feed = feedparser.parse(response.text)
            
            if not feed.entries:
                logging.warning(f"⚠️ Фід порожній (немає новин): {feed_url}")
                continue
                
        except Exception as e: 
            logging.error(f"❌ Збій парсингу {feed_url}: {e}")
            if stats is not None: stats["errors_count"] += 1 # ОНОВЛЕНО: Облік помилок (Пункт 4)[cite: 6]
            continue
            
        for entry in feed.entries[:15]: 
            if stats is not None: stats["funnel"]["scraped_total"] += 1
            source_count += 1
            
            # ОНОВЛЕНО: Нормалізація URL (Пункт 6)[cite: 6]
            url = normalize_url(entry.link)
            title = entry.title
            
            if url in history_urls or title in history_titles or url in backlog_urls: 
                if stats is not None and "already_seen" in stats["funnel"]:
                    stats["funnel"]["already_seen"] += 1
                continue
            
            article_time = time.time()
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                article_time = calendar.timegm(entry.published_parsed)
                if current_time - article_time > MAX_AGE_SECONDS: continue
            
            entry_tags = [tag.term.lower() for tag in getattr(entry, 'tags', []) if hasattr(tag, 'term')]
            blacklisted_categories = ['sport', 'košarka', 'nogomet', 'šport', 'scena', 'lifestyle', 'zabava', 'magazin']
            if any(cat in entry_tags for cat in blacklisted_categories):
                if stats is not None: stats["funnel"]["rejected_by_keywords"] += 1
                continue
            
            description = entry.get('summary', title)
            if 'content' in entry and len(entry.content) > 0:
                description = entry.content[0].get('value', description)

            if '...' in description[-15:] or '[...]' in description[-15:] or len(description) < 200:
                fetched_text = fetch_full_article_text(url, scraper)
                if fetched_text:
                    description = fetched_text
            
            if '...' in description[-10:] or '[...]' in description[-10:]:
                match = re.search(r'(.*[.!?])', description)
                if match: description = match.group(1)

            if not is_quality_article(title, description, url): 
                if stats is not None: stats["funnel"]["rejected_by_keywords"] += 1
                continue

            backlog.append({
                'title': title, 'description': description, 'url': url,
                'image_urls': extract_image_urls(entry), 'source': source_name, 'timestamp': article_time
            })
            backlog_urls.append(url)
            
        if stats is not None:
            stats["funnel"]["scraped_by_source"][source_name] = source_count
            
    save_backlog(backlog)

def process_backlog(stats=None):
    backlog = load_backlog()
    if not backlog: return

    ukraine_keywords = [
        r'\bukrajin', r'\bdijaspor', r'\bzelensk', r'\bzelenšk', r'\bkijev', 
        r'\bodesa', r'\bizbjeglic', r'\bhumanitar', r'\bmigrant',
        r'\bprivremen\w*\s+zaštit', 
        r'\bdirektiv\w*\s*2001'
    ]

    backlog.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
    all_feeds = get_rss_feeds()
    all_sources = list(dict.fromkeys(f.get('name', get_source_name(f['url'])) for f in all_feeds))
    all_sources.sort()
    if not all_sources: return
    
    ordered_sources = all_sources.copy()
    random.shuffle(ordered_sources)
    target_source = ordered_sources[0]
    
    logging.info(f"🎲 Рандомний порядок джерел на цей запуск: {ordered_sources}")

    candidate_batch = []
    used_urls = set()
    
    ukraine_items = prioritize_ukraine_news(backlog) 
    
    for item in ukraine_items:
        text_context = (item.get('title', '') + " " + item.get('description', '')).lower()
        if any(re.search(kw, text_context) for kw in ukraine_keywords):
            if item['url'] not in used_urls:
                candidate_batch.append(item)
                used_urls.add(item['url'])
        else:
            break
                
    while len(candidate_batch) < 25:
        added_this_round = False
        for src in ordered_sources:
            for item in backlog:
                if item.get('source') == src and item['url'] not in used_urls:
                    candidate_batch.append(item)
                    used_urls.add(item['url'])
                    added_this_round = True
                    break 
            if len(candidate_batch) >= 25: break
        if not added_this_round: break 
            
    candidate_batch = candidate_batch[:25]
    if not candidate_batch: return

    evaluated_urls = {item['url'] for item in candidate_batch}
    history_titles = load_history(TITLES_FILE)
    len_before = len(candidate_batch)
    
    candidate_batch, rejected_items = deduplicate_news(client, candidate_batch, history_titles, stats=stats)
    
    if stats is not None:
        stats["funnel"]["duplicates_removed_by_ai"] += (len_before - len(candidate_batch))

    batch = candidate_batch[:BATCH_SIZE]
    
    approved_urls = {item['url'] for item in candidate_batch}
    rejected_urls = evaluated_urls - approved_urls 
    sent_urls = set()

    if not batch: 
        remaining_backlog = [item for item in backlog if item['url'] not in rejected_urls]
        remaining_backlog.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
        save_backlog(remaining_backlog[:30])
        return
    
    used_sources_log = list(dict.fromkeys(item.get('source') for item in batch))
    logging.info(f"🔄 Обрано {len(batch)} новин. Головне джерело: {target_source}. Фактичні джерела: {', '.join(used_sources_log)}")

    prompt = f"Ось список із {len(batch)} новин. Ти — досвідчений головний редактор Telegram-каналу для українців, які живуть у Хорватії (діаспора, біженці). Твоє завдання — перекласти та адаптувати ці новини українською мовою.\n\n"
    prompt += """СУВОРІ ПРАВИЛА ПЕРЕКЛАДУ ТА ФОРМАТУВАННЯ:
    1. ГЕОГРАФІЯ: Зберігай оригінальну країну події. Якщо подія сталася в Хорватії — пиши про Хорватію, якщо в іншій країні (світові новини) — так і залишай. "Хорвати" — це місцеві жителі. НІКОЛИ не міняй місцями слова "хорвати"/"Хорватія" та "українці"/"Україна", якщо цього немає в оригінальному тексті.
    2. ДЕТАЛІЗАЦІЯ ДОКУМЕНТІВ (ДИРЕКТИВА 2001/55/ЄС): Якщо новина стосується тимчасового захисту, прав біженців, виплат чи документів, ПЕРЕКЛАДАЙ ЇЇ МАКСИМАЛЬНО ДЕТАЛЬНО. Не скорочуй жодної цифри, дати чи юридичної назви. Додай на початок заголовка емодзі 📄 або 🇪🇺.
    3. ПРІОРИТЕТ "УКРАЇНА": Якщо в оригінальній новині згадується Україна, українці, війна або біженці — обов'язково постав емодзі 🇺🇦 на самий початок заголовка та винеси цю інформацію у перший абзац, щоб привернути увагу.
    4. ПРИРОДНІСТЬ МОВИ ТА СЛОВНИК: Перекладай живою та природною українською мовою. УНИКАЙ ДОСЛІВНОГО ПЕРЕКЛАДУ (кальок) — передавай суть фрази, а не перекладай слово за словом. КАТЕГОРИЧНО ЗАБОРОНЕНО вигадувати неіснуючі слова (наприклад, замість кальки "бізшрний" чи "бізарний" використовуй існуючі аналоги: "дивний", "курйозний", "химерний"). Усі слова у твоєму тексті мають існувати в словнику української мови.«УВАЖНО ПЕРЕВІРЯЙ ОРФОГРАФІЮ: Звертай особливу увагу на використання апострофа (наприклад: "м'ясо", "комп'ютер"). Ніколи не пропускай літери.»
    5. ТОПОНІМИ ТА ІМЕНА: Усі хорватські імена, прізвища, назви вулиць та міст (наприклад, Задар, Спліт, Загреб) обов'язково транслітеруй українською мовою. Жодних латинських літер у тексті (крім хештегів у кінці).
    6. ЖУРНАЛІСТСЬКИЙ СТИЛЬ (АНТИ-КЛІКБЕЙТ): Пиши у стриманому стилі якісних медіа (як BBC). КАТЕГОРИЧНО ЗАБОРОНЕНО використовувати жовтий клікбейт та штучні епітети (наприклад, "зіркова", "шокуюча", "скандальна", "кривава"). Роби текст легким для читання. Абзаци мають бути короткими (не більше 3-4 речень). Уникай сухого поліцейського сленгу та дослівного перекладу юридичних термінів. Наприклад, "prekršajni odjel" адаптуй як "відділ з адміністративних правопорушень" (а не "проміжний відділ"). 
    7. НІЯКОГО ШІ-КОНТЕКСТУ: Видавай СУВОРО лише готовий текст новини. Жодних вступних фраз, привітань, коментарів чи висновків від себе. Зберігай абсолютну журналістську об'єктивність.
    8. АКЦЕНТИ: Виділяй жирним шрифтом (в тегах <b>) ключові суми штрафів, дати, імена посадовців або назви законів, щоб текст було зручно читати.
    9. ХЕШТЕГИ: В самому кінці тексту обов'язково додай 2-3 хештеги з нового рядка. 
    ПРАВИЛО ГЕОГРАФІЇ: Перший хештег — це ГЕОГРАФІЯ, і він має бути ТІЛЬКИ ОДИН. Якщо ти знаєш місто — пиши ТІЛЬКИ його латиницею (наприклад, #Zadar, #Split, #SeverinNaKupi). Якщо точне місто невідоме — пиши ТІЛЬКИ назву країни (наприклад, #Hrvatska або #Moldova). КАТЕГОРИЧНО ЗАБОРОНЕНО ставити хештег країни (наприклад, #Хорватія чи #Hrvatska), якщо ти вже поставив хештег конкретного міста!
    ТЕМАТИЧНІ ХЕШТЕГИ: Другі хештеги мають СУВОРО відповідати змісту статті (наприклад, #Економіка, #Суспільство, #Закон, #Політика, #Кримінал). Категорично ЗАБОРОНЕНО ставити хештеги #ТимчасовийЗахист або #Україна, якщо в самому тексті новини про це не йдеться.
    10. ОНОВЛЕННЯ ПОДІЙ: Якщо ти бачиш, що новина є продовженням чи оновленням попередніх подій (наприклад, завершення слідства, нові деталі аварії), додай на самий початок заголовка префікс 🔄 [ОНОВЛЕНО]:
    """
    
    for idx, item in enumerate(batch):
        images = item.get('image_urls', [])
        if not images and item.get('image_url'):
            images = [item.get('image_url')]
            
        has_image = "Так" if images else "Ні"
        prompt += f"=== НОВИНА ID {idx} ===\nМає картинку: {has_image}\nЗаголовок: {item['title']}\nОпис: {item['description']}\n\n"
    
    # ОНОВЛЕНО: Перехід на структурований JSON (Пункт 1)[cite: 6]
    prompt += f"""
    Для кожної новини виконай наступні вимоги:
    
    1. ЗАГОЛОВОК: Переклади оригінальний заголовок максимально точно та близько до тексту. КАТЕГОРИЧНО ЗАБОРОНЕНО вигадувати від себе "креатив", метафори чи клікбейт. Заголовок НІЯК не виділяй тегами <b>, просто напиши текст.
    
    2. ОБ'ЄМ ТА ПРЯМИЙ ПЕРЕКЛАД (АНТИ-ВОДА):
       - Твоє головне завдання — ПРЯМИЙ ПЕРЕКЛАД наявного тексту. Якщо в оригіналі лише 300 символів — переклади ці 300 символів і зупинись. КАТЕГОРИЧНО ЗАБОРОНЕНО штучно розтягувати текст, додавати "воду", висновки чи контекст, якого немає в оригіналі. КАТЕГОРИЧНО ЗАБОРОНЕНО вигадувати неіснуючі імена, цитати, точні цифри чи дати, яких не було в оригіналі.
       - ЛІМІТИ ТЕЛЕГРАМА: 
         Якщо "Має картинку: Так" -> суворий ліміт 900 символів. Якщо оригінал довший, стисни головну суть, щоб влізти в цей ліміт.
         Якщо "Має картинку: Ні" -> суворий ліміт 2500 символів. Якщо оригінал довший, передай головну суть до цього ліміту.
       - ОБРІЗАНИЙ ТЕКСТ В RSS: Іноді текст обривається на півслові або закінчується трьома крапками (...) через ліміти сайтів. Якщо бачиш обірване речення в кінці — просто видаліть його або заверши логічно. КАТЕГОРИЧНО ЗАБОРОНЕНО друкувати фрази з трьома крапками (наприклад, "Вулиця ...").
       
    3. СТРУКТУРА ТА ТЕГИ ДЛЯ ПОЛЯ 'content': Дроби текст на короткі абзаци (по 3-4 речення). Для розширених текстів використовуй марковані списки (з символом • або -). Використовуй ТІЛЬКИ ці теги: <b>, <i>, <u>, <s>. Ніяких тегів <p>, <br>, <h1>, <li> і ніякої Markdown-розмітки (без зірочок ** чи решіток ##).
    """

    time.sleep(15)
    try:
        response, generated_texts = generate_news_texts_json(client, prompt, len(batch), stats)
    except Exception as e:
        logging.error(f"❌ Не вдалося згенерувати тексти після 3 спроб: {e}. Скасування публікації.")
        if stats is not None: stats["errors_count"] += 1  
        return

    history_urls = load_history(HISTORY_FILE)
    history_titles = load_history(TITLES_FILE) 

    for idx, item in enumerate(batch):
        if idx >= len(generated_texts): break 
        
        translation = generated_texts[idx]
        
        if not validate_translation_simple(translation):
             continue
        
        # Формуємо фінальний текст із JSON структури
        formatted_text = f"<b>{translation['title']}</b>\n\n{translation['content']}\n\n{translation['hashtags']}"
        
        images = item.get('image_urls', [])
        if not images and item.get('image_url'):
            images = [item.get('image_url')]
        
        # ОНОВЛЕНО: Отримуємо тип публікації (Пункт 2)[cite: 6]
        success, pub_type = send_to_telegram(formatted_text, item['url'], item.get('source', 'Джерело'), images)
        
        if success:
            history_urls = save_to_history(history_urls, item['url'], HISTORY_FILE)
            history_titles = save_to_history(history_titles, item['title'], TITLES_FILE)
            sent_urls.add(item['url'])
            
            if stats is not None:
                stats["published"]["total"] += 1
                
                text_context = (item.get('title', '') + " " + item.get('description', '')).lower()
                if any(re.search(kw, text_context) for kw in ukraine_keywords):
                    stats["published"]["ukraine_priority"] += 1
                
                # Зараховуємо реальний тип публікації (Пункт 2)[cite: 6]
                if pub_type == "image": stats["published"]["with_images"] += 1
                else: stats["published"]["text_only"] += 1
                
                src = item.get('source', 'Джерело')
                stats["published"]["sources"][src] = stats["published"]["sources"].get(src, 0) + 1
                
            time.sleep(45)
        else:
            if stats is not None: stats["errors_count"] += 1

    remaining_backlog = []
    for item in backlog:
        if item['url'] not in evaluated_urls:
             remaining_backlog.append(item)
        elif item['url'] in approved_urls and item['url'] not in sent_urls:
             remaining_backlog.append(item)

    remaining_backlog.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
    save_backlog(remaining_backlog[:30])

def send_to_telegram(text, url, source_name, image_urls=None):
    # Хештеги тепер частина тексту (прийшли з JSON)
    if not image_urls: image_urls = []
    elif isinstance(image_urls, str): image_urls = [image_urls]

    crop_msg = "...\n\n👉 <i>Повні деталі читайте за посиланням нижче</i> 👇"
    safe_caption_limit = 920 
    
    if image_urls and len(text) > safe_caption_limit: 
        text = text[:safe_caption_limit] + crop_msg
    elif not image_urls and len(text) > 3900: 
        text = text[:3900] + crop_msg
    
    full_message = f"{text}\n\n🔗 Джерело: <a href='{url}'>{source_name}</a>"
    payload = {'chat_id': TELEGRAM_CHANNEL_ID}
    
    if len(image_urls) == 0:
        payload['parse_mode'] = 'HTML'
        payload['text'] = full_message
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        pub_type = "text" # ОНОВЛЕНО (Пункт 2)[cite: 6]
    elif len(image_urls) == 1:
        payload['parse_mode'] = 'HTML'
        payload['caption'] = full_message
        payload['photo'] = image_urls[0]
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        pub_type = "image"
    else:
        media = []
        for idx, img_url in enumerate(image_urls):
            media_item = {'type': 'photo', 'media': img_url}
            if idx == 0: 
                media_item['caption'] = full_message
                media_item['parse_mode'] = 'HTML'
            media.append(media_item)
        payload['media'] = json.dumps(media)
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMediaGroup"
        pub_type = "image"
    
    try:
        execute_telegram_request(api_url, payload)
        return True, pub_type # ОНОВЛЕНО (Пункт 2)[cite: 6]
    except Exception as e: 
        if len(image_urls) > 0:
            logging.warning(f"⚠️ Telegram відхилив медіа ({e}). Пробуємо відправити просто як текст...")
            payload_text = {'chat_id': TELEGRAM_CHANNEL_ID, 'text': full_message, 'parse_mode': 'HTML'}
            api_url_text = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            try:
                execute_telegram_request(api_url_text, payload_text)
                return True, "text" # ОНОВЛЕНО: Якщо фолбек успішний, це текстовий пост[cite: 6]
            except Exception as text_e:
                logging.error(f"❌ Критична помилка відправки тексту: {text_e}")
                return False, "error"
        else:
            logging.error(f"❌ Критична помилка Telegram API: {e}")
            return False, "error"

def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, GEMINI_API_KEY]):
        logging.error("Критична помилка: не знайдено базові токени у файлі .env!")
        sys.exit(1)
        
    start_time = time.time()
    
    stats = {
        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
        "execution_time": 0,
        "funnel": {
            "scraped_total": 0,
            "scraped_by_source": {},  
            "already_seen": 0,        
            "rejected_by_keywords": 0,
            "duplicates_removed_by_ai": 0,
            "rejection_reasons_list": []  
        },
        "backlog": {
            "total_size": 0,
            "sources": {}
        },
        "published": {
            "total": 0,
            "ukraine_priority": 0,
            "with_images": 0,
            "text_only": 0,
            "sources": {}
        },
        "models_used": {"gemini-3.5-flash-lite": 0, "gemini-3.6-flash": 0},
        "errors_count": 0,
        "gemini_retries": 0
    }

    try:
        scrape_feeds(stats)
        
        backlog = load_backlog()
        stats["backlog"]["total_size"] = len(backlog)
        
        source_counts = {}
        for item in backlog:
            src = item.get('source', 'Невідомо')
            source_counts[src] = source_counts.get(src, 0) + 1
        stats["backlog"]["sources"] = source_counts
        
        process_backlog(stats)
        
    except Exception as e:
        stats["errors_count"] += 1
        send_alert_to_admin(f"Скрипт впав з помилкою: {e}")
        critical_error = True # ОНОВЛЕНО: Сигнал для падіння скрипта (Пункт 5)[cite: 6]

    finally:
        trim_history(HISTORY_FILE, MAX_HISTORY_SIZE)
        trim_history(TITLES_FILE, MAX_HISTORY_SIZE)
        
        stats["execution_time"] = round(time.time() - start_time, 1)
        save_analytics(stats)
        
        if stats["published"]["total"] > 0 or stats["errors_count"] > 0:
            send_analytics_to_admin(stats)
            
        # ОНОВЛЕНО: Примусове падіння GitHub Action, якщо була помилка (Пункт 5)[cite: 6]
        if 'critical_error' in locals():
            sys.exit(1)

if __name__ == "__main__":
    main()