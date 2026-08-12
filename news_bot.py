import feedparser
import requests
import os
import time
import calendar
import logging
import json
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

# Можна додавати до 20+ джерел
RSS_FEEDS = [
    'https://www.index.hr/rss',       
    'https://www.jutarnji.hr/rss',    
]

HISTORY_FILE = 'posted_news.txt'
TITLES_FILE = 'posted_titles.txt'
BACKLOG_FILE = 'news_backlog.json' # Файл для черги новин

MAX_HISTORY_SIZE = 1000 
MAX_AGE_SECONDS = 86400 # 24 години
BATCH_SIZE = 3 # Кількість новин для обробки за один запит (WIP-ліміт)
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
    """Завантажує чергу новин з JSON"""
    if not os.path.exists(BACKLOG_FILE): return []
    try:
        with open(BACKLOG_FILE, 'r', encoding='utf-8') as f: return json.load(f)
    except: return []

def save_backlog(backlog):
    """Зберігає чергу новин у JSON"""
    with open(BACKLOG_FILE, 'w', encoding='utf-8') as f:
        json.dump(backlog, f, ensure_ascii=False, indent=4)

def extract_image_url(entry):
    if 'media_content' in entry and len(entry.media_content) > 0:
        return entry.media_content[0].get('url')
    if 'enclosures' in entry and len(entry.enclosures) > 0:
        for enc in entry.enclosures:
            if enc.get('type', '').startswith('image/'): return enc.get('href')
    return None

# ================= ФАЗА 1: ПАРСИНГ =================
def scrape_feeds():
    """Збирає всі свіжі новини і додає їх у беклог"""
    logging.info("--- Етап 1: Парсинг стрічок ---")
    history_urls = load_history(HISTORY_FILE)
    history_titles = load_history(TITLES_FILE)
    backlog = load_backlog()
    
    backlog_urls = [item['url'] for item in backlog]
    new_items_count = 0

    for feed_url in RSS_FEEDS:
        try: feed = feedparser.parse(feed_url)
        except Exception as e:
            logging.error(f"Не вдалося зчитати RSS {feed_url}: {e}")
            continue
            
        for entry in feed.entries[:10]: # Перевіряємо топ-10 кожної стрічки
            url = entry.link
            title = entry.title
            
            # Фільтр дублікатів (історія + поточний беклог)
            if url in history_urls or title in history_titles or url in backlog_urls:
                continue
                
            # Фільтр свіжості
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                article_time = calendar.timegm(entry.published_parsed)
                if time.time() - article_time > MAX_AGE_SECONDS: continue
            
            description = entry.get('summary', title)
            image_url = extract_image_url(entry)
            
            # Додаємо в чергу
            backlog.append({
                'title': title,
                'description': description,
                'url': url,
                'image_url': image_url
            })
            backlog_urls.append(url)
            new_items_count += 1
            logging.info(f"Додано в беклог: {title}")

    save_backlog(backlog)
    logging.info(f"Парсинг завершено. Нових статей у черзі: {new_items_count}. Загалом у беклозі: {len(backlog)}.")

# ================= ФАЗА 2: БАТЧИНГ ТА ПУБЛІКАЦІЯ =================
def process_backlog():
    """Бере батч новин з черги, перекладає та публікує"""
    backlog = load_backlog()
    if not backlog:
        logging.info("Беклог порожній. Немає що публікувати.")
        return

    # Беремо порцію (батч) завдань
    batch = backlog[:BATCH_SIZE]
    logging.info(f"--- Етап 2: Обробка батчу ({len(batch)} новин) ---")

    # Формуємо єдиний промпт для всіх новин у батчі
    prompt = "Ось список новин:\n\n"
    for idx, item in enumerate(batch):
        prompt += f"--- НОВИНА {idx+1} ---\nЗаголовок: {item['title']}\nТекст: {item['description']}\n\n"
    
    prompt += """
    Твоє завдання:
    Переклади кожну новину українською мовою та зроби короткий адаптований пост.
    Для кожної новини:
    1. Напиши привабливий заголовок (в тегах <b>).
    2. Коротко виклади суть (1-2 абзаци).
    3. Додай 2-3 релевантні хештеги в кінці.
    4. Не використовуй Markdown (* або _), тільки базові HTML-теги.
    
    ВАЖЛИВО: Розділяй відповіді для кожної новини строго рядком `|||`. 
    Не пиши жодних вступних чи завершальних слів загалом. Тільки текст постів та роздільники `|||`.
    """

    # Запит до Gemini
    generated_texts = []
    for attempt in range(3):
        try:
            logging.info("Відправка пакетного запиту до Gemini API...")
            response = client.models.generate_content(
                model='gemini-3.5-flash-lite', # Використовуємо легку модель
                contents=prompt
            )
            raw_text = response.text.strip()
            # Розрізаємо відповідь по нашому роздільнику
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
        logging.error("Не вдалося отримати/обробити пакет від Gemini.")
        return

    if len(generated_texts) != len(batch):
        logging.warning(f"Кількість відповідей ({len(generated_texts)}) не співпадає з розміром батчу ({len(batch)}). Можлива втрата даних.")

    history_urls = load_history(HISTORY_FILE)
    history_titles = load_history(TITLES_FILE)

    # Публікація кожної новини з батчу
    successful_urls = []
    for idx, item in enumerate(batch):
        if idx >= len(generated_texts): break # Якщо Gemini повернув менше новин
        
        post_text = generated_texts[idx]
        if send_to_telegram(post_text, item['url'], item['image_url']):
            logging.info(f"✅ Опубліковано: {item['title']}")
            history_urls = save_to_history(history_urls, item['url'], HISTORY_FILE)
            history_titles = save_to_history(history_titles, item['title'], TITLES_FILE)
            successful_urls.append(item['url'])
            time.sleep(10) # Коротка пауза між публікаціями в Telegram
        else:
            logging.error(f"❌ Помилка публікації: {item['title']}")

    # Видаляємо успішно опубліковані новини з беклогу
    remaining_backlog = [item for item in backlog if item['url'] not in successful_urls]
    save_backlog(remaining_backlog)
    logging.info(f"Батч оброблено. У черзі залишилось: {len(remaining_backlog)}.")

def send_to_telegram(text, url, image_url=None):
    if image_url and len(text) > 850: text = text[:850] + "...\n<i>(Текст обрізано)</i>"
    full_message = f"{text}\n\n🔗 <a href='{url}'>Джерело новини</a>"
    
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