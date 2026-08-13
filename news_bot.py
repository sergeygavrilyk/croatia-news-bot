import feedparser
import requests
import os
import time
import calendar
import logging
import json
import math
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
BATCH_SIZE = 30 # МЕГА-БАТЧИНГ: обробляємо до 30 новин за 1 запит до AI
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
    """Розширена фільтрація новин: відсікає сміття, рекламу, клікбейт та короткі замітки"""
    unwanted_keywords = [
        'oglasi', 'sponzorirani', 'promo', 'redikul', 'horoskop', 
        'plaćeni oglas', 'nagradna igra', 'native', 'pr vijesti',
        'foto', 'video'
    ]
    
    title_lower = title.lower()
    desc_lower = description.lower() if description else ""
    
    for word in unwanted_keywords:
        if f" {word} " in f" {title_lower} " or f" {word} " in f" {desc_lower} ":
            return False
            
    if len(title.strip()) < 15:
        return False
    if len(desc_lower.strip()) < 20: 
        return False
        
    if "!!!" in title or "???" in title:
        return False
        
    letters = [c for c in title if c.isalpha()]
    if letters:
        caps_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
        if caps_ratio > 0.4: 
            return False
            
    return True

# ================= СИСТЕМА ДЕДУПЛІКАЦІЇ (ВЕКТОРИ + GEMINI) =================
def cosine_similarity(vec1, vec2):
    """Обчислює математичну схожість між двома векторами (від 0.0 до 1.0)"""
    dot_product = sum(a * b for a, b in zip(vec1, vec2))
    magnitude1 = math.sqrt(sum(a * a for a in vec1))
    magnitude2 = math.sqrt(sum(b * b for b in vec2))
    if magnitude1 == 0 or magnitude2 == 0:
        return 0.0
    return dot_product / (magnitude1 * magnitude2)

def filter_by_embeddings(client, articles, threshold=0.85):
    """Етап 1: Швидко відсіює точні семантичні дублікати за допомогою Embeddings API"""
    if not articles:
        return []
        
    logging.info("Триває Етап 1 (Дедуплікація): Векторний аналіз схожості...")
    texts = [article.get("title", "") for article in articles]
    
    try:
        response = client.models.embed_content(
            model="text-embedding-004",
            contents=texts
        )
        embeddings = [emb.values for emb in response.embeddings]
    except Exception as e:
        logging.error(f"Помилка генерації ембеддінгів: {e}")
        return articles # У разі збою пропускаємо цей етап без зупинки бота
    
    unique_articles = []
    for i, article in enumerate(articles):
        is_duplicate = False
        for unique_article in unique_articles:
            sim = cosine_similarity(embeddings[i], unique_article["embedding"])
            if sim >= threshold:
                is_duplicate = True
                logging.info(f"🗑️ [Вектор] Видалено дублікат: {article['title']} (Схожість: {sim:.2f})")
                break
        
        if not is_duplicate:
            article_copy = article.copy()
            article_copy["embedding"] = embeddings[i]
            unique_articles.append(article_copy)
            
    for u in unique_articles:
        del u["embedding"]
        
    return unique_articles

def filter_by_gemini(client, articles):
    """Етап 2: Мовна модель фінально вичищає складні смислові дублікати"""
    if not articles or len(articles) <= 1:
        return articles
        
    logging.info("Триває Етап 2 (Дедуплікація): Семантичний аналіз через Gemini...")
    
    prompt_data = [{"id": i, "title": a["title"]} for i, a in enumerate(articles)]
    
    prompt = f"""
    Ось список новин у форматі JSON (ID, Заголовок). 
    Твоє завдання: знайти новини, які висвітлюють одну й ту саму подію (наприклад, пожежа в одному місті, або однакова політична заява).
    З кожної групи дублікатів залиш лише ОДНУ новину з найінформативнішим заголовком.
    
    Поверни результат СУВОРО як масив JSON з ID новин, які треба ЗАЛИШИТИ. Більше ніякого тексту.
    
    Новини:
    {json.dumps(prompt_data, ensure_ascii=False, indent=2)}
    
    Приклад ідеальної відповіді:
    [0, 2, 5, 8]
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
            logging.info(f"🗑️ [Gemini] Видалено складних дублікатів: {removed_count}")
            
        return filtered_articles
        
    except Exception as e:
        logging.error(f"Помилка на етапі Gemini дедуплікації: {e}")
        return articles

def deduplicate_news(client, raw_articles):
    """Головний конвеєр об'єднаної дедуплікації"""
    if not raw_articles:
        return []
    
    logging.info(f"\n--- ПОЧАТОК ДЕДУПЛІКАЦІЇ ({len(raw_articles)} новин у вибірці) ---")
    stage1_articles = filter_by_embeddings(client, raw_articles, threshold=0.85)
    final_articles = filter_by_gemini(client, stage1_articles)
    logging.info(f"--- ДЕДУПЛІКАЦІЮ ЗАВЕРШЕНО: Залишилось унікальних новин: {len(final_articles)} ---\n")
    
    return final_articles


# ================= ФАЗА 1: ПАРСИНГ ТА ФІЛЬТРАЦІЯ =================
def scrape_feeds():
    logging.info("--- Етап 1: Парсинг стрічок та фільтрація ---")
    history_urls = load_history(HISTORY_FILE)
    history_titles = load_history(TITLES_FILE)
    backlog = load_backlog()
    
    current_time = time.time()
    backlog = [item for item in backlog if current_time - item.get('timestamp', current_time) <= MAX_AGE_SECONDS]
    
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
            
            article_time = time.time()
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                article_time = calendar.timegm(entry.published_parsed)
                if current_time - article_time > MAX_AGE_SECONDS: continue
            
            description = entry.get('summary', title)
            
            if not is_quality_article(title, description):
                logging.info(f"Відфільтровано як неякісну/рекламу: {title}")
                continue

            image_url = extract_image_url(entry)
            
            backlog.append({
                'title': title,
                'description': description,
                'url': url,
                'image_url': image_url,
                'source': source_name,
                'timestamp': article_time
            })
            backlog_urls.append(url)
            new_items_count += 1

    save_backlog(backlog)
    logging.info(f"📊 МЕТРИКА ПАРСИНГУ: Пройшли фільтрацію і додані в беклог: {new_items_count} | 📌 Всього у черзі: {len(backlog)}")


# ================= ФАЗА 2: МЕГА-БАТЧИНГ ТА API (КВОТУВАННЯ) =================
def process_backlog():
    backlog = load_backlog()
    if not backlog:
        logging.info("Беклог порожній. Немає що публікувати.")
        return

    backlog.sort(key=lambda x: x.get('timestamp', 0), reverse=True)

    QUOTAS = {
        'Національні': 10,
        'Регіональні': 10,
        'Економіка': 5,
        'Надзвичайні події': 1,
        'Погода': 1,
        'Концерти': 3
    }
    
    categorized_news = {cat: [] for cat in QUOTAS.keys()}
    categorized_news['Без категорії'] = [] 

    for item in backlog:
        cat = item.get('category', 'Без категорії')
        if cat in categorized_news:
            categorized_news[cat].append(item)
        else:
            categorized_news['Без категорії'].append(item)

    batch = []
    remaining_slots = 0 

    for cat, limit in QUOTAS.items():
        selected = categorized_news[cat][:limit]
        batch.extend(selected)
        
        categorized_news[cat] = categorized_news[cat][len(selected):]
        remaining_slots += (limit - len(selected))

    if remaining_slots > 0:
        fallback_categories = ['Національні', 'Регіональні', 'Економіка', 'Без категорії']
        for cat in fallback_categories:
            if remaining_slots <= 0:
                break
            available = categorized_news[cat]
            if available:
                fillers = available[:remaining_slots]
                batch.extend(fillers)
                categorized_news[cat] = categorized_news[cat][len(fillers):]
                remaining_slots -= len(fillers)

    batch.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
    batch = batch[:BATCH_SIZE]

    logging.info(f"--- Етап 2: Відібрано {len(batch)} новин за квотами ---")

    # --- ЗАСТОСОВУЄМО ДВОСТУПЕНЕВИЙ ФІЛЬТР ДЕДУПЛІКАЦІЇ ---
    batch = deduplicate_news(client, batch)

    if not batch:
        logging.info("Після дедуплікації не залишилося новин для публікації.")
        return

    prompt = f"Ось список із {len(batch)} новин. Твоє завдання — перекласти кожну українською мовою та адаптувати для Telegram-посту.\n\n"
    
    for idx, item in enumerate(batch):
        cat = item.get('category', 'Новини')
        cat_hashtag = cat.replace(" ", "") 
        
        prompt += f"=== НОВИНА {idx+1} ===\nКатегорія: #{cat_hashtag}\nЗаголовок: {item['title']}\nОпис: {item['description']}\n\n"
    
    prompt += f"""
    Для кожної новини зроби:
    1. Привабливий заголовок (в тегах <b>).
    2. Коротко суть (1-2 абзаци). Постарайся писати лаконічно, бо новин багато.
    3. Хештеги: ПЕРШИМ обов'язково постав той хештег Категорії, який вказаний вище, а після нього додай ще 1-2 свої за темою.
    4. Без зайвого форматування Markdown (* або _), тільки базові HTML-теги.
    
    ВАЖЛИВО: Обов'язково розділяй відповіді для кожної новини строго рядком `|||`. 
    У тебе має вийти рівно {len(batch)} блоків тексту, розділених тегом `|||`. Не пиши жодних вступних чи завершальних слів.
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
            time.sleep(10)
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