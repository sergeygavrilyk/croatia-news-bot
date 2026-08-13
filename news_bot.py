import feedparser
import requests
import os
import time
import calendar
import logging
import json
import re  # <--- Додайте цей рядок
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
ANALYTICS_FILE = 'analytics.json' # <--- ДОДАТИ ЦЕЙ РЯДОК

MAX_HISTORY_SIZE = 1000 
MAX_AGE_SECONDS = 43200 # 12 годин (тільки свіжі новини)
BATCH_SIZE = 3 # 3 новини за один запуск
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
def load_analytics():
    if not os.path.exists(ANALYTICS_FILE): return []
    try:
        with open(ANALYTICS_FILE, 'r', encoding='utf-8') as f: return json.load(f)
    except: return []

def save_analytics(stats):
    history = load_analytics()
    history.append(stats)
    # Зберігаємо лише останні 100 запусків, щоб файл не розростався
    with open(ANALYTICS_FILE, 'w', encoding='utf-8') as f:
        json.dump(history[-100:], f, ensure_ascii=False, indent=4)

def send_analytics_to_admin(stats):
    if not ADMIN_CHAT_ID: return
    message = (
        f"📊 <b>Звіт про роботу бота</b>\n\n"
        f"⏱ Час виконання: {stats['execution_time']} сек\n"
        f"📥 Новин у беклозі: {stats['backlog_size']}\n"
        f"🗑 Відсіяно дублікатів: {stats['duplicates_removed']}\n"
        f"✅ Опубліковано: {stats['published_count']}\n"
        f"⚠️ Помилок: {stats['errors_count']}"
    )
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(api_url, data={'chat_id': ADMIN_CHAT_ID, 'text': message, 'parse_mode': 'HTML'})
    except: pass

def get_rss_feeds():
    if not os.path.exists(FEEDS_FILE): return []
    try:
        with open(FEEDS_FILE, 'r', encoding='utf-8') as f: return json.load(f)
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
    except: return "Джерело"

def is_quality_article(title, description, url):
    unwanted_keywords = [
        'oglasi', 'sponzorirani', 'promo', 'redikul', 'horoskop', 'plaćeni oglas', 
        'nagradna igra', 'native', 'pr vijesti', 'foto', 'video',
        'sport', 'nogomet', 'košarka', 'tenis', 'utakmica', 'hnl', 'liga prvaka'
    ]
    # Об'єднуємо заголовок, опис та URL в один рядок для перевірки
    text_to_check = f"{title} {description} {url}".lower()
    
    for word in unwanted_keywords:
        # \b шукає слово цілком, ігноруючи коми, крапки та слеші
        if re.search(rf'\b{word}\b', text_to_check):
            return False
            
    if len(title.strip()) < 15 or len(text_to_check.strip()) < 20: return False
    if "!!!" in title or "???" in title: return False
    letters = [c for c in title if c.isalpha()]
    if letters and (sum(1 for c in letters if c.isupper()) / len(letters)) > 0.4: return False
    return True

# ================= СИСТЕМА ДЕДУПЛІКАЦІЇ (ТІЛЬКИ GEMINI) =================
def deduplicate_news(client, articles, history_titles=None):
    """Мовна модель вичищає смислові дублікати та звіряє з історією публікацій"""
    if not articles: 
        return articles
        
    logging.info("Аналіз на дублікати (включно з історією) через Gemini...")
    
    # Беремо 15 останніх заголовків, які ми вже публікували
    recent_history = history_titles[-15:] if history_titles else []
    prompt_data = [{"id": i, "title": a["title"]} for i, a in enumerate(articles)]
    
    prompt = f"""
    Ось список новин (ID, Заголовок), які претендують на публікацію:
    {json.dumps(prompt_data, ensure_ascii=False, indent=2)}
    
    Ось ІСТОРІЯ - список із {len(recent_history)} останніх опублікованих нами новин:
    {json.dumps(recent_history, ensure_ascii=False, indent=2)}
    
    Твоє завдання:
    1. ЗНАЙТИ ВНУТРІШНІ ДУБЛІКАТИ: Якщо кілька новин-претендентів описують одну подію (наприклад, скандал із відходами, заяви різних політиків про одне й те саме), залиши серед них лише ОДНУ.
    2. ПОРІВНЯТИ З ІСТОРІЄЮ: Якщо новина-претендент стосується події, про яку ми ВЖЕ писали в Історії, її треба ВІДХИЛИТИ повністю.
    
    Поверни результат СУВОРО як масив JSON з ID новин, які треба ЗАЛИШИТИ. Більше ніякого тексту.
    """
    
    try:
        response = client.models.generate_content(
            model='gemini-3.5-flash-lite', 
            contents=prompt,
            config={"response_mime_type": "application/json"}
        )
        
        keep_ids = json.loads(response.text)
        filtered_articles = [articles[i] for i in keep_ids if i < len(articles)]
        removed_count = len(articles) - len(filtered_articles)
        if removed_count > 0:
            logging.info(f"🗑️ [Gemini] Відсіяно як дублікати (або вже було): {removed_count}")
        return filtered_articles
    except Exception as e:
        logging.error(f"Помилка дедуплікації: {e}")
        return articles

def prioritize_ukraine_news(backlog_items):
    """Шукає новини про Україну та ставить їх у пріоритет (на початок списку)"""
    keywords = ['ukrajin', 'zelensk', 'kijev', 'rat']
    ukraine_news = []
    other_news = []
    
    for item in backlog_items:
        text_context = (item.get('title', '') + " " + item.get('description', '')).lower()
        if any(kw in text_context for kw in keywords):
            ukraine_news.append(item)
        else:
            other_news.append(item)
            
    return ukraine_news + other_news

def scrape_feeds():
    history_urls = load_history(HISTORY_FILE)
    history_titles = load_history(TITLES_FILE)
    backlog = load_backlog()
    current_time = time.time()
    
    backlog = [item for item in backlog if current_time - item.get('timestamp', current_time) <= MAX_AGE_SECONDS]
    backlog_urls = [item['url'] for item in backlog]

    for feed_item in get_rss_feeds():
        feed_url = feed_item['url']
        source_name = feed_item.get('name', get_source_name(feed_url))
        try: feed = feedparser.parse(feed_url)
        except: continue
            
        for entry in feed.entries[:15]: 
            url = entry.link
            title = entry.title
            if url in history_urls or title in history_titles or url in backlog_urls: continue
            
            article_time = time.time()
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                article_time = calendar.timegm(entry.published_parsed)
                if current_time - article_time > MAX_AGE_SECONDS: continue
            
            description = entry.get('summary', title)
            if not is_quality_article(title, description, url): continue

            backlog.append({
                'title': title, 'description': description, 'url': url,
                'image_url': extract_image_url(entry), 'source': source_name, 'timestamp': article_time
            })
            backlog_urls.append(url)
    save_backlog(backlog)

def process_backlog(stats=None):
    backlog = load_backlog()
    if not backlog: return

    # 1. Сортуємо всі новини за часом (найсвіжіші зверху)
    backlog.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
    
    # 2. Підіймаємо новини про Україну на перше місце
    backlog = prioritize_ukraine_news(backlog)

    # 3. Відбираємо топ-15 найсвіжіших для глибокого аналізу
    candidate_batch = backlog[:15]
    history_titles = load_history(TITLES_FILE) # Завантажуємо історію

    # 4. Перевірка на дублікати (включно з історією минулих публікацій)
    len_before = len(candidate_batch)
    candidate_batch = deduplicate_news(client, candidate_batch, history_titles)
    if stats is not None:
        stats["duplicates_removed"] += (len_before - len(candidate_batch))

    # 5. З відфільтрованих беремо максимум BATCH_SIZE з унікальних джерел
    batch = []
    used_sources = set()
    for item in candidate_batch:
        source = item.get('source')
        if source not in used_sources:
            batch.append(item)
            used_sources.add(source)
        if len(batch) >= BATCH_SIZE:
            break

    if not batch: return

    # СУВОРИЙ ПРОМПТ РЕДАКТОРА
    prompt = f"Ось список із {len(batch)} новин. Ти — досвідчений головний редактор Telegram-каналу для українців, які живуть у Хорватії (діаспора, біженці). Твоє завдання — перекласти та адаптувати ці новини українською мовою.\n\n"
    prompt += """СУВОРІ ПРАВИЛА КОНТЕКСТУ ТА ПРІОРИТЕТІВ:
    1. ГЕОГРАФІЯ: Всі події відбуваються в Хорватії або світі. "Хорвати" — це місцеві жителі. НІКОЛИ не замінюй слова "хорвати" або "Хорватія" на "українці" чи "Україна", якщо цього немає в оригінальному тексті. Зберігай оригінальний контекст!
    2. ПРІОРИТЕТ "УКРАЇНА": Якщо в оригінальній новині згадується Україна, війна або біженці — постав емодзі 🇺🇦 на самий початок заголовка та винеси цю інформацію у перший абзац.
    """
    
    for idx, item in enumerate(batch):
        has_image = "Так" if item.get('image_url') else "Ні"
        prompt += f"=== НОВИНА {idx+1} ===\nМає картинку: {has_image}\nЗаголовок: {item['title']}\nОпис: {item['description']}\n\n"
    
    prompt += f"""
    Для кожної новини зроби:
    1. Привабливий заголовок (в тегах <b>).
    2. Об'єм тексту СУВОРО залежить від параметра "Має картинку":
       - Якщо "Має картинку: Так" -> напиши детальний текст на 800–900 символів.
       - Якщо "Має картинку: Ні" -> напиши великий лонгрід до 3000-3500 символів (додай деталі, передісторію).
    3. Хештеги: Напиши 1-3 релевантні хештеги для кожної новини.
    4. Без зайвого форматування Markdown (* або _), тільки базові HTML-теги.
    
    ВАЖЛИВО: Обов'язково розділяй відповіді для кожної новини строго рядком `|||`. Не пиши жодних вступних чи завершальних слів.
    """

    generated_texts = []
    
    # Пауза перед викликом для захисту від лімітів RPM
    logging.info("Очікування перед викликом Gemini API...")
    time.sleep(15)

    for attempt in range(3):
        try:
            response = client.models.generate_content(model='gemini-3.5-flash-lite', contents=prompt)
            generated_texts = [t.strip() for t in response.text.strip().split('|||') if t.strip()]
            break
        except Exception as e:
            logging.warning(f"Помилка Gemini API: {e}. Очікування 60с (Спроба {attempt+1}/3)")
            time.sleep(60)
    
    if not generated_texts: return

    history_urls = load_history(HISTORY_FILE)
    history_titles = load_history(TITLES_FILE) # Оновлюємо історію перед збереженням
    successful_urls = []

    for idx, item in enumerate(batch):
        if idx >= len(generated_texts): break 
        if send_to_telegram(generated_texts[idx], item['url'], item.get('source', 'Джерело'), item['image_url']):
            history_urls = save_to_history(history_urls, item['url'], HISTORY_FILE)
            history_titles = save_to_history(history_titles, item['title'], TITLES_FILE)
            successful_urls.append(item['url'])
            
            # Збільшуємо лічильник успішних публікацій
            if stats is not None:
                stats["published_count"] += 1
                
            time.sleep(45)

    # 4. ЧИСТКА БЕКЛОГУ (зберігаємо лише топ-30 найсвіжіших)
    remaining_backlog = [item for item in backlog if item['url'] not in successful_urls]
    remaining_backlog.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
    save_backlog(remaining_backlog[:30])

def send_to_telegram(text, url, source_name, image_url=None):
    if image_url and len(text) > 920: text = text[:920] + "...\n<i>(Текст обрізано)</i>"
    elif not image_url and len(text) > 3900: text = text[:3900] + "...\n<i>(Текст обрізано)</i>"
    
    full_message = f"{text}\n\n🔗 Джерело: <a href='{url}'>{source_name}</a>"
    payload = {'chat_id': TELEGRAM_CHANNEL_ID, 'parse_mode': 'HTML'}
    
    if image_url:
        payload['photo'] = image_url
        payload['caption'] = full_message
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    else:
        payload['text'] = full_message
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    try:
        return requests.post(api_url, data=payload).status_code == 200
    except: return False

def main():
    start_time = time.time()
    
    # Глобальний словник для збору статистики поточного запуску
    stats = {
        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
        "backlog_size": 0,
        "duplicates_removed": 0,
        "published_count": 0,
        "errors_count": 0,
        "execution_time": 0
    }

    try:
        scrape_feeds()
        stats["backlog_size"] = len(load_backlog())
        
        # Ми передамо stats у process_backlog, щоб там оновити лічильники
        process_backlog(stats)
        
    except Exception as e:
        stats["errors_count"] += 1
        send_alert_to_admin(f"Скрипт впав з помилкою: {e}")
    finally:
        stats["execution_time"] = round(time.time() - start_time, 1)
        save_analytics(stats)
        
        # Відправляємо звіт в адмінку, тільки якщо щось було опубліковано або були помилки
        if stats["published_count"] > 0 or stats["errors_count"] > 0:
            send_analytics_to_admin(stats)

if __name__ == "__main__":
    main()