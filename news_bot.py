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
MAX_AGE_SECONDS = 43200 # 12 годин (тільки свіжі новини)
BATCH_SIZE = 5 # 5 новин за один запуск

# Налаштування моделей для fallback-механізму
PRIMARY_MODEL = 'gemini-3.5-flash-lite'
FALLBACK_MODEL = 'gemini-3.5-flash'
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

def is_quality_article(title, description):
    unwanted_keywords = ['oglasi', 'sponzorirani', 'promo', 'redikul', 'horoskop', 'plaćeni oglas', 'nagradna igra', 'native', 'pr vijesti', 'foto', 'video']
    title_lower = title.lower()
    desc_lower = description.lower() if description else ""
    for word in unwanted_keywords:
        if f" {word} " in f" {title_lower} " or f" {word} " in f" {desc_lower} ": return False
    if len(title.strip()) < 15 or len(desc_lower.strip()) < 20: return False
    if "!!!" in title or "???" in title: return False
    letters = [c for c in title if c.isalpha()]
    if letters and (sum(1 for c in letters if c.isupper()) / len(letters)) > 0.4: return False
    return True

# ================= СИСТЕМА ДЕДУПЛІКАЦІЇ (ТІЛЬКИ GEMINI) =================
def deduplicate_news(client, articles):
    """Мовна модель вичищає смислові дублікати серед відібраних новин"""
    if not articles or len(articles) <= 1: 
        return articles
        
    logging.info("Аналіз на дублікати через Gemini...")
    prompt_data = [{"id": i, "title": a["title"]} for i, a in enumerate(articles)]
    
    prompt = f"""
    Ось список новин у форматі JSON (ID, Заголовок). 
    Твоє завдання: знайти новини, які висвітлюють одну й ту саму подію (наприклад, пожежа в одному місті, або однакова політична заява).
    З кожної групи дублікатів залиш лише ОДНУ новину з найінформативнішим заголовком.
    
    Поверни результат СУВОРО як масив JSON з ID новин, які треба ЗАЛИШИТИ. Більше ніякого тексту.
    
    Новини:
    {json.dumps(prompt_data, ensure_ascii=False, indent=2)}
    
    Приклад ідеальної відповіді:
    [0, 2, 4]
    """
    
    try:
        response = client.models.generate_content(
            model=PRIMARY_MODEL, 
            contents=prompt,
            config={"response_mime_type": "application/json"}
        )
        
        keep_ids = json.loads(response.text)
        filtered_articles = [articles[i] for i in keep_ids if i < len(articles)]
        removed_count = len(articles) - len(filtered_articles)
        if removed_count > 0:
            logging.info(f"🗑️ [Gemini] Видалено дублікатів: {removed_count}")
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
            if not is_quality_article(title, description): continue

            backlog.append({
                'title': title, 'description': description, 'url': url,
                'image_url': extract_image_url(entry), 'source': source_name, 'timestamp': article_time,
                'category': feed_item.get('category', 'Без категорії')
            })
            backlog_urls.append(url)
    save_backlog(backlog)

def process_backlog():
    backlog = load_backlog()
    if not backlog: return

    # 1. Сортуємо всі новини за часом (найсвіжіші зверху)
    backlog.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
    
    # 2. Підіймаємо новини про Україну на перше місце
    backlog = prioritize_ukraine_news(backlog)

    # 3. Розподіл по категоріям (квоти: 2 + 1 + 1 + 1 = 5)
    QUOTAS = {'Національні': 2, 'Регіональні': 1, 'Економіка': 1, 'Надзвичайні події': 1}
    categorized_news = {cat: [] for cat in QUOTAS.keys()}
    categorized_news['Інші'] = [] 

    for item in backlog:
        cat = item.get('category', 'Інші')
        if cat in categorized_news: categorized_news[cat].append(item)
        else: categorized_news['Інші'].append(item)

    batch = []
    remaining_slots = 0 

    for cat, limit in QUOTAS.items():
        selected = categorized_news[cat][:limit]
        batch.extend(selected)
        categorized_news[cat] = categorized_news[cat][len(selected):]
        remaining_slots += (limit - len(selected))

    # Добираємо залишок
    if remaining_slots > 0:
        leftovers = []
        for cat in categorized_news.values(): leftovers.extend(cat)
        leftovers = prioritize_ukraine_news(sorted(leftovers, key=lambda x: x.get('timestamp', 0), reverse=True))
        batch.extend(leftovers[:remaining_slots])

    # Фінальне сортування і обрізка до 5
    batch = prioritize_ukraine_news(batch)[:BATCH_SIZE]
    batch = deduplicate_news(client, batch)

    if not batch: return

    # СУВОРИЙ ПРОМПТ РЕДАКТОРА
    prompt = f"Ось список із {len(batch)} новин. Ти — досвідчений головний редактор Telegram-каналу для українців, які живуть у Хорватії (діаспора, біженці). Твоє завдання — перекласти та адаптувати ці новини українською мовою.\n\n"
    prompt += """СУВОРІ ПРАВИЛА КОНТЕКСТУ ТА ПРІОРИТЕТІВ:
    1. ГЕОГРАФІЯ: Всі події відбуваються в Хорватії або світі. "Хорвати" — це місцеві жителі. НІКОЛИ не замінюй слова "хорвати" або "Хорватія" на "українці" чи "Україна", якщо цього немає в оригінальному тексті. Зберігай оригінальний контекст!
    2. ПРІОРИТЕТ "УКРАЇНА": Якщо в оригінальній новині згадується Україна, війна або біженці — постав емодзі 🇺🇦 на самий початок заголовка та винеси цю інформацію у перший абзац.
    """
    
    for idx, item in enumerate(batch):
        cat = item.get('category', 'Новини')
        cat_hashtag = cat.replace(" ", "") 
        has_image = "Так" if item.get('image_url') else "Ні"
        prompt += f"=== НОВИНА {idx+1} ===\nКатегорія: #{cat_hashtag}\nМає картинку: {has_image}\nЗаголовок: {item['title']}\nОпис: {item['description']}\n\n"
    
    prompt += f"""
    Для кожної новини зроби:
    1. Привабливий заголовок (в тегах <b>).
    2. Об'єм тексту СУВОРО залежить від параметра "Має картинку":
       - Якщо "Має картинку: Так" -> напиши детальний текст на 800–900 символів.
       - Якщо "Має картинку: Ні" -> напиши великий лонгрід до 3000-3500 символів (додай деталі, передісторію).
    3. Хештеги: ПЕРШИМ обов'язково постав той хештег Категорії, який вказаний вище, а після нього додай ще 1-2 свої за темою.
    4. Без зайвого форматування Markdown (* або _), тільки базові HTML-теги.
    
    ВАЖЛИВО: Обов'язково розділяй відповіді для кожної новини строго рядком `|||`. Не пиши жодних вступних чи завершальних слів.
    """

    generated_texts = []
    models_to_try = [PRIMARY_MODEL, FALLBACK_MODEL]

    # БЕЗПЕЧНА ПАУЗА ДЛЯ ЗАХИСТУ ВІД ЛІМІТУ RPM (5 запитів/хв)
    logging.info("Очікування перед викликом Gemini API (захист від RPM ліміту)...")
    time.sleep(15)

    for model_name in models_to_try:
        success = False
        for attempt in range(2):
            try:
                logging.info(f"Спроба генерації через модель: {model_name} (Спроба {attempt + 1})")
                response = client.models.generate_content(model=model_name, contents=prompt)
                
                raw_text = response.text.strip()
                generated_texts = [t.strip() for t in raw_text.split('|||') if t.strip()]
                
                if generated_texts:
                    logging.info(f"Успішно згенеровано за допомогою моделі: {model_name}")
                    success = True
                    break
            except Exception as e:
                error_str = str(e)
                if '429' in error_str or 'RESOURCE_EXHAUSTED' in error_str:
                    logging.warning(f"Ліміти вичерпано для {model_name}. Пауза 30с... (Спроба {attempt + 1}/2)")
                    time.sleep(30)
                else:
                    logging.error(f"Помилка моделі {model_name}: {e}")
                    break
        
        if success:
            break
        else:
            logging.warning(f"⚠️ Перемикаємося на резервну модель ({FALLBACK_MODEL})...")

    if not generated_texts:
        logging.error("Не вдалося отримати текст ні від основної, ні від резервної моделі.")
        return

    history_urls = load_history(HISTORY_FILE)
    history_titles = load_history(TITLES_FILE)
    successful_urls = []

    for idx, item in enumerate(batch):
        if idx >= len(generated_texts): break 
        if send_to_telegram(generated_texts[idx], item['url'], item.get('source', 'Джерело'), item['image_url']):
            history_urls = save_to_history(history_urls, item['url'], HISTORY_FILE)
            history_titles = save_to_history(history_titles, item['title'], TITLES_FILE)
            successful_urls.append(item['url'])
            # Затримка між публікаціями в Telegram (тримаємо в межах 45 секунд, щоб не перевищити ліміти GitHub)
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
    scrape_feeds()
    process_backlog()

if __name__ == "__main__":
    try: main()
    except Exception as e: send_alert_to_admin(f"Скрипт впав з помилкою: {e}")