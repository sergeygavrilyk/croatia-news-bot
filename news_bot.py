import feedparser
import requests
import os
import time
import calendar
import logging
import json
from urllib.parse import urlparse
from dotenv import load_dotenv
from google import genai

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

if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, GEMINI_API_KEY]):
    logging.error("Критична помилка: не знайдено базові токени у файлі .env!")
    exit(1)

HISTORY_FILE = 'posted_news.txt'
TITLES_FILE = 'posted_titles.txt'
BACKLOG_FILE = 'news_backlog.json'
FEEDS_FILE = 'feeds.json'

MAX_HISTORY_SIZE = 1000 
MAX_AGE_SECONDS = 86400 # 24 години
BATCH_SIZE = 15 # МЕГА-БАТЧИНГ: обробляємо до 15 новин за 1 запит до AI
# ===================================================

client = genai.Client(api_key=GEMINI_API_KEY)

def send_alert_to_admin(message):
    if not ADMIN_CHAT_ID: return
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(api_url, data={'chat_id': ADMIN_CHAT_ID, 'text': f"⚠️ ALARM: {message}"})
    except: pass

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
        with open(BACKLOG_FILE, 'r', encoding='utf-8') as f: return json.load(f)
    except: return []

def save_backlog(backlog):
    with open(BACKLOG_FILE, 'w', encoding='utf-8') as f:
        json.dump(backlog, f, ensure_ascii=False, indent=4)

def get_rss_feeds():
    if not os.path.exists(FEEDS_FILE):
        return []
    try:
        with open(FEEDS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"Помилка читання feeds.json: {e}")
        return []

def extract_image_url(entry):
    if 'media_content' in entry and len(entry.media_content) > 0:
        return entry.media_content[0].get('url')
    if 'enclosures' in entry and len(entry.enclosures) > 0:
        for enc in entry.enclosures:
            if enc.get('type', '').startswith('image/'): return enc.get('href')
    return None

def get_source_name(url):
    try:
        parsed_uri = urlparse(url)
        domain = parsed_uri.netloc
        if domain.startswith('www.'): domain = domain[4:]
        return domain.capitalize()
    except:
        return "Джерело"

# ================= РОЗУМНА ФІЛЬТРАЦІЯ (PYTHON) =================
def is_quality_article(title, description):
    """Фільтрує новини, відсікаючи сміття, рекламу та короткі замітки"""
    unwanted_keywords = ['oglasi', 'sponzorirani', 'promo', 'redikul', 'horoskop', 'plaćeni oglas']
    title_lower = title.lower()
    
    # Відсікаємо за стоп-словами
    for word in unwanted_keywords:
        if word in title_lower:
            return False
            
    # Відсікаємо занадто короткі заголовки (менше 15 символів)
    if len(title.strip()) < 15:
        return False
        
    return True

# ================= ФАЗА 1: ПАРСИНГ ТА ФІЛЬТРАЦІЯ =================
def scrape_feeds():
    logging.info("--- Етап 1: Парсинг стрічок та фільтрація ---")
    history_urls = load_history(HISTORY_FILE)
    history_titles = load_history(TITLES_FILE)
    backlog = load_backlog()
    
    backlog_urls = [item['url'] for item in backlog]
    new_items_count = 0

    feeds_list = get_rss_feeds()

    for feed_item in feeds_list:
        feed_url = feed_item['url']
        source_name = feed_item.get('name', get_source_name(feed_url))
        
        try: feed = feedparser.parse(feed_url)
        except Exception as e:
            logging.error(f"Не вдалося зчитати RSS {feed_url}: {e}")
            continue
            
        for entry in feed.entries[:15]: 
            url = entry.link
            title = entry.title
            
            if url in history_urls or title in history_titles or url in backlog_urls:
                continue
                
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                article_time = calendar.timegm(entry.published_parsed)
                if time.time() - article_time > MAX_AGE_SECONDS: continue
            
            description = entry.get('summary', title)
            
            # ЗАСТОСОВУЄМО РОЗУМНУ ФІЛЬТРАЦІЮ ПЕРЕД ЗБЕРЕЖЕННЯМ
            if not is_quality_article(title, description):
                logging.info(f"Відфільтровано як неякісну/рекламу: {title}")
                continue

            image_url = extract_image_url(entry)
            
            backlog.append({
                'title': title,
                'description': description,
                'url': url,
                'image_url': image_url,
                'source': source_name
            })
            backlog_urls.append(url)
            new_items_count += 1

    save_backlog(backlog)
    logging.info(f"📊 МЕТРИКА ПАРСИНГУ: Пройшли фільтрацію і додані в беклог: {new_items_count} | 📌 Всього у черзі: {len(backlog)}")

# ================= ФАЗА 2: МЕГА-БАТЧИНГ ТА API =================
def process_backlog():
    backlog = load_backlog()
    if not backlog:
        logging.info("Беклог порожній. Немає що публікувати.")
        return

    # Беремо великий батч (до 15 новин)
    batch = backlog[:BATCH_SIZE]
    logging.info(f"--- Етап 2: Мега-батч обробка ({len(batch)} новин одним запитом) ---")

    prompt = f"Ось список із {len(batch)} новин. Твоє завдання — перекласти кожну українською мовою та адаптувати для Telegram-посту.\n\n"
    for idx, item in enumerate(batch):
        prompt += f"=== НОВИНА {idx+1} ===\nЗаголовок: {item['title']}\nОпис: {item['description']}\n\n"
    
    prompt += f"""
    Для кожної новини зроби:
    1. Привабливий заголовок (в тегах <b>).
    2. Коротко суть (1-2 абзаци).
    3. 2-3 релевантні хештеги.
    4. Без зайвого форматування Markdown (* або _), тільки базові HTML-теги.
    
    ВАЖЛИВО: Обов'язково розділяй відповіді для кожної новини строго рядком `|||`. 
    У тебе має вийти рівно {len(batch)} блоків тексту, розділених тегом `|||`. Не пиши жодних вступних чи завершальних слів загалом.
    """

    generated_texts = []
    start_time = time.time()

    for attempt in range(3):
        try:
            logging.info("Відправка мега-пакетного запиту до Gemini API...")
            response = client.models.generate_content(
                model='gemini-3.5-flash-lite',
                contents=prompt
            )
            
            latency = time.time() - start_time
            
            if hasattr(response, 'usage_metadata') and response.usage_metadata:
                meta = response.usage_metadata
                prompt_tokens = getattr(meta, 'prompt_token_count', 0)
                candidates_tokens = getattr(meta, 'candidates_token_count', 0)
                total_tokens = getattr(meta, 'total_token_count', 0)
                
                logging.info(
                    f"📊 МЕТРИКА GEMINI API: Latency = {latency:.2f}с | "
                    f"Токени: prompt={prompt_tokens}, output={candidates_tokens}, total={total_tokens}"
                )

            raw_text = response.text.strip()
            generated_texts = [t.strip() for t in raw_text.split('|||') if t.strip()]
            break
            
        except Exception as e:
            if '429' in str(e) or 'RESOURCE_EXHAUSTED' in str(e):
                logging.warning(f"Ліміт API. Пауза 60с... (Спроба {attempt + 1}/3)")
                time.sleep(60)
            else:
                logging.error(f"Помилка Gemini API: {e}")
                break
    
    if not generated_texts:
        logging.error("Не вдалося отримати/обробити мега-пакет від Gemini.")
        return

    history_urls = load_history(HISTORY_FILE)
    history_titles = load_history(TITLES_FILE)

    successful_urls = []
    published_count = 0 

    for idx, item in enumerate(batch):
        if idx >= len(generated_texts): break 
        
        post_text = generated_texts[idx]
        source_name = item.get('source', 'Джерело')
        
        if send_to_telegram(post_text, item['url'], source_name, item['image_url']):
            logging.info(f"✅ Опубліковано [{source_name}]: {item['title']}")
            history_urls = save_to_history(history_urls, item['url'], HISTORY_FILE)
            history_titles = save_to_history(history_titles, item['title'], TITLES_FILE)
            successful_urls.append(item['url'])
            published_count += 1
            time.sleep(10) # Коротка пауза між повідомленнями, щоб не отримати спам-блок від Telegram
        else:
            logging.error(f"❌ Помилка публікації: {item['title']}")

    remaining_backlog = [item for item in backlog if item['url'] not in successful_urls]
    save_backlog(remaining_backlog)
    
    logging.info(f"📊 МЕТРИКА PUBLISHER: Опубліковано {published_count}/{len(batch)} статей. 📌 Залишилось у черзі: {len(remaining_backlog)}.")

def send_to_telegram(text, url, source_name, image_url=None):
    if image_url and len(text) > 850: text = text[:850] + "...\n<i>(Текст обрізано)</i>"
    
    full_message = f"{text}\n\n🔗 Джерело: <a href='{url}'>{source_name}</a>"
    
    payload = {'chat_id': TELEGRAM_CHANNEL_ID, 'parse_mode': 'HTML'}
    
    if image_url:
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        payload['photo'] = image_url
        payload['caption'] = full_message
    else:
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload['text'] = full_message
    
    try:
        res = requests.post(api_url, data=payload)
        if res.status_code != 200:
            logging.error(f"Telegram Error: {res.text}")
            return False
        return True
    except Exception as e:
        logging.error(f"Network Error: {e}")
        return False

def main():
    scrape_feeds()
    process_backlog()

if __name__ == "__main__":
    try: main()
    except Exception as e:
        logging.critical(f"Глобальна помилка скрипта: {e}")
        send_alert_to_admin(f"Скрипт впав з помилкою: {e}")