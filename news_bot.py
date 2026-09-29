import feedparser
import requests
import os
import time
import calendar
import logging
import json
import re
import random
from urllib.parse import urlparse
from dotenv import load_dotenv
from google import genai
from google.genai import types  # ДОДАЄМО ЦЕ
import cloudscraper  

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
# ===================================================

client = genai.Client(api_key=GEMINI_API_KEY)


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

def send_alert_to_admin(message):
    if not ADMIN_CHAT_ID: 
        return
        
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        response = requests.post(api_url, data={'chat_id': ADMIN_CHAT_ID, 'text': f"⚠️ ALARM: {message}"})
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
        with open(BACKLOG_FILE, 'r', encoding='utf-8') as f: return json.load(f)
    except Exception as e:
        logging.warning(f"⚠️ Не вдалося завантажити {BACKLOG_FILE} (можливо, файл пошкоджений): {e}. Починаємо з пустого списку.")
        return []

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
    with open(ANALYTICS_FILE, 'w', encoding='utf-8') as f:
        json.dump(history[-100:], f, ensure_ascii=False, indent=4)

def send_analytics_to_admin(stats):
    if not ADMIN_CHAT_ID: 
        logging.warning("ADMIN_CHAT_ID не налаштовано")
        return
        
    scraped_src_str = ", ".join([f"{k}: {v}" for k, v in stats['funnel'].get('scraped_by_source', {}).items()]) if stats['funnel'].get('scraped_by_source') else "Немає"
    backlog_src_str = ", ".join([f"{k}: {v}" for k, v in stats['backlog']['sources'].items()]) if stats['backlog']['sources'] else "Порожньо"
    pub_src_str = ", ".join([f"{k}: {v}" for k, v in stats['published']['sources'].items()]) if stats['published']['sources'] else "Немає"

    # Формуємо блок із деталями відхилень Gemini, якщо вони є
    reasons_list = stats['funnel'].get('rejection_reasons_list', [])
    reasons_str = "\n".join(reasons_list[:10]) if reasons_list else "Нічого не відсіяно або немає деталей"
    if len(reasons_list) > 10:
        reasons_str += f"\n...і ще відхилених: {len(reasons_list) - 10}"

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
        f"❌ <b>Чому ШІ відхилив новини цього разу:</b>\n"
        f"{reasons_str}\n\n"
        f"📥 <b>Беклог (очікують ШІ-валідації: {stats['backlog']['total_size']}):</b>\n"
        f"▪️ {backlog_src_str}\n\n"
        f"✅ <b>Опубліковано: {stats['published']['total']}</b>\n"
        f"▪️ Формат: З фото ({stats['published']['with_images']}) | Текст ({stats['published']['text_only']})\n"
        f"▪️ Пріоритет 🇺🇦: {stats['published']['ukraine_priority']}\n"
        f"▪️ Джерела: {pub_src_str}"
    )
    
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        response = requests.post(api_url, data={'chat_id': ADMIN_CHAT_ID, 'text': message, 'parse_mode': 'HTML'})
        if response.status_code != 200:
            logging.error(f"Помилка відправки в Telegram: {response.text}")
    except Exception as e: 
        logging.error(f"Збій з'єднання з Telegram API: {e}")

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
    unwanted_url_categories = [
        '/sport/', '/scena/', '/lifestyle/', '/magazin/'
    ]
    url_lower = url.lower()
    if any(category in url_lower for category in unwanted_url_categories):
        return False

    unwanted_keywords = [
        'oglasi', 'sponzorirani', 'promo', 'redikul', 'horoskop', 'plaćeni oglas', 
        'nagradna igra', 'native', 'pr vijesti', 'foto', 'video',
        'sport', 'nogomet', 'košarka', 'tenis', 'utakmica', 'hnl', 'liga prvaka'
    ]
    
    text_to_check = f"{title} {description} {url}".lower()
    
    for word in unwanted_keywords:
        if re.search(rf'\b{word}\b', text_to_check):
            return False
            
    if len(title.strip()) < 15 or len(text_to_check.strip()) < 20: return False
    if "!!!" in title or "???" in title: return False
    
    letters = [c for c in title if c.isalpha()]
    if letters and (sum(1 for c in letters if c.isupper()) / len(letters)) > 0.4: return False
    
    return True

def deduplicate_news(client, articles, history_titles=None, stats=None):
    if not articles: return articles
        
    logging.info("Аналіз на дублікати (включно з історією) через Gemini...")
    
    recent_history = history_titles[-75:] if history_titles else []
    prompt_data = [{"id": i, "title": a["title"], "description": a.get("description", "")} for i, a in enumerate(articles)]
    # 1. ДОДАЄМО ЛОГУВАННЯ ЗАГОЛОВКІВ (Щоб ви бачили, що саме він відхиляє)
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
    - АЛЕ якщо це СУТТЄВЕ ОНОВЛЕННЯ вже знайомої події (наприклад: кардинально зросла кількість жертв, знайдено злочинця, ухвалено закон, який вчора лише обговорювався, або є офіційна реакція влади на кризу) — ОБОВ'ЯЗКОВО ЗАЛИШАЙ.

    КРОК 4. СТОП-ТЕМИ (ЩО БЕЗЖАЛЬНО ВІДХИЛЯТИ): 
    1. СПОРТ: Будь-які матчі, турніри, результати, трансфери, спортсмени.
    2. РЕКЛАМА: Спонсорські матеріали, промо-акції, розіграші.
    3. ПОБУТ ЧУЖИХ МІСТ: Ремонти доріг, відключення води, локальні затори, страйки комунальників, дрібні ДТП у Загребі, Спліті, Рієці чи БУДЬ-ЯКОМУ іншому місті, ОКРІМ Задара.
    4. СВІТОВИЙ ШУМ ТА ЛАЙФСТАЙЛ: Абстрактна наука (як-от дослідження Гарварда), поради дієтологів, рецепти, психологія, шоу-бізнес та події в третіх країнах, ЯКЩО вони не стосуються безпосередньо Хорватії чи України.

    КРОК 5. ТЕМАТИКА (ЩО ОБОВ'ЯЗКОВО ЗАЛИШИТИ): 
    Залишай ТІЛЬКИ ті новини, які пройшли Кроки 1-4 і належать до однієї з трьох категорій:
    1. ЗАДАР (Пріоритет №1): Побут, перекриття вулиць, тарифи, ремонти, відключення світла/води, погода, місцевий кримінал та події в місті Задар і Задарській жупанії. 
    2. ХОРВАТІЯ (Національний рівень): Важливі події всієї країни — великий кримінал, політика, економіка (ціни, податки, будівництво), надзвичайні ситуації.
    3. УКРАЇНА ТА БІЖЕНЦІ: Головні події в Україні (війна, енергетика), рішення ЄС (Директива 2001/55), виплати та правила перетину кордону.

    <CANDIDATES>
    {json.dumps(prompt_data, ensure_ascii=False, indent=2)}
    </CANDIDATES>

    <HISTORY>
    {json.dumps(recent_history, ensure_ascii=False, indent=2)}
    </HISTORY>
    
    Поверни результат СУВОРО у форматі валідного JSON-об'єкта з двома ключами:
    1. "keep": масив ID новин, які треба ЗАЛИШИТИ (наприклад, [0, 2]).
    2. "rejected": масив об'єктів для відхилених новин, де кожен містить "id" та коротку українську "reason" (наприклад: "Спорт", "Дублікат", "Побут Спліта", "Вже є в історії", "Світовий шум").
    
    Більше ніякого тексту, тільки чистий JSON.
    """

    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model='gemini-3.5-flash-lite', 
                contents=prompt,
                # 2. ДОДАЄМО ТЕМПЕРАТУРУ 0.0 ДЛЯ СТАБІЛЬНОСТІ
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.0, 
                    safety_settings=[
                        types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
                    ]
                )
            )
            
            # ДОДАЄМО ЗАХИСТ ТУТ
            if not response or not getattr(response, 'text', None):
                raise ValueError("Gemini повернув порожню відповідь (None) під час дедуплікації.")

            logging.info(f"Промпт до Gemini містив {len(articles)} новин.")
            text_response = response.text.strip()
            logging.info(f"Відповідь Gemini: {text_response}")
            
            # Очистка від Markdown-блоків, якщо вони є
            if "```json" in text_response:
                text_response = text_response.split("```json")[1].split("```")[0].strip()
            elif "```" in text_response:
                text_response = text_response.split("```")[1].split("```")[0].strip()

            data = json.loads(text_response)  
            
            keep_ids = [int(item) for item in data.get("keep", []) if str(item).isdigit()]
            rejected_items = data.get("rejected", [])
            
            filtered_articles = [articles[i] for i in keep_ids if i < len(articles)]  
            
            # Збираємо причини відхилень для аналітики без БД
            rejection_log = []
            for item in rejected_items:
                if isinstance(item, dict) and 'id' in item:
                    idx = int(item['id'])
                    reason = item.get('reason', 'Не вказано')
                    if idx < len(articles):
                        title = articles[idx]['title']
                        rejection_log.append(f"• {title[:40]}... ➡️ <b>{reason}</b>")
            
            # Зберігаємо у словник статистики
            if stats is not None:
                stats["funnel"]["rejection_reasons_list"] = rejection_log

            removed_count = len(articles) - len(filtered_articles)
            if removed_count > 0: 
                logging.info(f"🗑️ Відсіяно ШІ (дублі/спам/історія): {removed_count}")
                for log_entry in rejection_log:
                    logging.info(f"   {log_entry.replace('<b>', '').replace('</b>', '')}")
                    
            return filtered_articles
            
        except Exception as e:
            logging.error(f"Помилка дедуплікації (спроба {attempt+1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                sleep_time = 10 * (2 ** attempt)  # експоненційний ретрай
                time.sleep(min(sleep_time, 60))
            else:
                logging.error(f"❌ Gemini API дедуплікації недоступний після {max_retries} спроб. Пропускаємо фільтрацію без втрати новин.")
                return articles

def prioritize_ukraine_news(backlog_items):
    # Очищений і значно точніший Regex-список
    ukraine_keywords = [
        r'\bukrajin', r'\bdijaspor', r'\bzelensk', r'\bzelenšk', r'\bkijev', 
        r'\bodesa', r'\bizbjeglic', r'\bhumanitar', r'\bmigrant',
        # Шукаємо комбінації слів (наприклад, privremena zaštita, privremenu zaštitu)
        r'\bprivremen\w*\s+zaštit', 
        # Директива 2001/55
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
            
        source_count = 0  # ВИПРАВЛЕНО: Ініціалізуємо змінну перед циклом
        
        try: 
            headers = {
                'Accept': 'application/rss+xml, application/xml, text/xml, */*'
            }
            response = scraper.get(feed_url, headers=headers, timeout=15)
            
            if response.status_code not in [200, 301, 302]:
                logging.error(f"❌ Помилка доступу до {feed_url}: HTTP статус {response.status_code}")
                continue
                
            feed = feedparser.parse(response.text)
            
            if not feed.entries:
                logging.warning(f"⚠️ Фід порожній (немає новин): {feed_url}")
                continue
                
        except Exception as e: 
            logging.error(f"❌ Збій парсингу {feed_url}: {e}")
            continue
            
        for entry in feed.entries[:15]: 
            if stats is not None: stats["funnel"]["scraped_total"] += 1
            source_count += 1
            
            url = entry.link
            title = entry.title
            
            if url in history_urls or title in history_titles or url in backlog_urls: 
                if stats is not None and "already_seen" in stats["funnel"]:
                    stats["funnel"]["already_seen"] += 1
                continue
            
            article_time = time.time()
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                article_time = calendar.timegm(entry.published_parsed)
                if current_time - article_time > MAX_AGE_SECONDS: continue
            
            description = entry.get('summary', title)
            if not is_quality_article(title, description, url): 
                if stats is not None: stats["funnel"]["rejected_by_keywords"] += 1
                continue

            backlog.append({
                'title': title, 'description': description, 'url': url,
                'image_url': extract_image_url(entry), 'source': source_name, 'timestamp': article_time
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
    
    import random
    
    ordered_sources = all_sources.copy()
    random.shuffle(ordered_sources) # Випадково перемішуємо джерела при кожному запуску
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
            # Зустріли першу не-українську новину — далі йдуть звичайні, виходимо з циклу
            break
                
    # --- ВИПРАВЛЕНИЙ АЛГОРИТМ ЧЕРГИ (Рівномірний розподіл по 1 новині) ---
    while len(candidate_batch) < 25:
        added_this_round = False
        for src in ordered_sources:
            for item in backlog:
                if item.get('source') == src and item['url'] not in used_urls:
                    candidate_batch.append(item)
                    used_urls.add(item['url'])
                    added_this_round = True
                    break  # Взяли 1 новину з джерела — йдемо до наступного джерела
            if len(candidate_batch) >= 25: break
        if not added_this_round: break # Якщо всі джерела порожні — виходимо
            
    candidate_batch = candidate_batch[:25]
    if not candidate_batch: return

    evaluated_urls = {item['url'] for item in candidate_batch}

    history_titles = load_history(TITLES_FILE)
    len_before = len(candidate_batch)
    candidate_batch = deduplicate_news(client, candidate_batch, history_titles, stats=stats)
    
    if stats is not None:
        stats["funnel"]["duplicates_removed_by_ai"] += (len_before - len(candidate_batch))

    batch = candidate_batch[:BATCH_SIZE]
    if not batch: return
    
    used_sources_log = list(dict.fromkeys(item.get('source') for item in batch))
    logging.info(f"🔄 Обрано {len(batch)} новин. Головне джерело: {target_source}. Фактичні джерела: {', '.join(used_sources_log)}")

    prompt = f"Ось список із {len(batch)} новин. Ти — досвідчений головний редактор Telegram-каналу для українців, які живуть у Хорватії (діаспора, біженці). Твоє завдання — перекласти та адаптувати ці новини українською мовою.\n\n"
    prompt += """СУВОРІ ПРАВИЛА ПЕРЕКЛАДУ ТА ФОРМАТУВАННЯ:
    1. ГЕОГРАФІЯ: Зберігай оригінальну країну події. Якщо подія сталася в Хорватії — пиши про Хорватію, якщо в іншій країні (світові новини) — так і залишай. "Хорвати" — це місцеві жителі. НІКОЛИ не міняй місцями слова "хорвати"/"Хорватія" та "українці"/"Україна", якщо цього немає в оригінальному тексті.
    2. ДЕТАЛІЗАЦІЯ ДОКУМЕНТІВ (ДИРЕКТИВА 2001/55/ЄС): Якщо новина стосується тимчасового захисту, прав біженців, виплат чи документів, ПЕРЕКЛАДАЙ ЇЇ МАКСИМАЛЬНО ДЕТАЛЬНО. Не скорочуй жодної цифри, дати чи юридичної назви. Додай на початок заголовка емодзі 📄 або 🇪🇺.
    3. ПРІОРИТЕТ "УКРАЇНА": Якщо в оригінальній новині згадується Україна, українці, війна або біженці — обов'язково постав емодзі 🇺🇦 на самий початок заголовка та винеси цю інформацію у перший абзац, щоб привернути увагу.
    4. ПРИРОДНІСТЬ МОВИ ТА СЛОВНИК: Перекладай живою та природною українською мовою. КАТЕГОРИЧНО ЗАБОРОНЕНО вигадувати неіснуючі слова (наприклад, замість кальки "бізшрний" чи "бізарний" використовуй існуючі аналоги: "дивний", "курйозний", "химерний"). Усі слова у твоєму тексті мають існувати в словнику української мови.
    5. ТОПОНІМИ ТА ІМЕНА: Усі хорватські імена, прізвища, назви вулиць та міст (наприклад, Задар, Спліт, Загреб) обов'язково транслітеруй українською мовою. Жодних латинських літер у тексті (крім хештегів у кінці).
    6. ЖУРНАЛІСТСЬКИЙ СТИЛЬ (АНТИ-БЮРОКРАТІЯ): Пиши у стилі сучасного Telegram-каналу. Уникай сухого поліцейського сленгу та дослівного перекладу юридичних термінів. Наприклад, "prekršajni odjel" адаптуй як "відділ з адміністративних правопорушень" (а не "проміжний відділ"). Роби текст легким для читання. Абзаци мають бути короткими (не більше 3-4 речень).
    7. НІЯКОГО ШІ-КОНТЕКСТУ: Видавай СУВОРО лише готовий текст новини. Жодних вступних фраз, привітань, коментарів чи висновків від себе. Зберігай абсолютну журналістську об'єктивність.
    8. АКЦЕНТИ: Виділяй жирним шрифтом (в тегах <b>) ключові суми штрафів, дати, імена посадовців або назви законів, щоб текст було зручно читати.
    9. ХЕШТЕГИ: В самому кінці тексту обов'язково додай 2-3 хештеги з нового рядка. 
    ПРАВИЛО ГЕОГРАФІЇ: Перший хештег — це ГЕОГРАФІЯ, і він має бути ТІЛЬКИ ОДИН. Якщо ти знаєш місто — пиши ТІЛЬКИ його латиницею (наприклад, #Zadar, #Split, #SeverinNaKupi). Якщо точне місто невідоме — пиши ТІЛЬКИ назву країни (наприклад, #Hrvatska або #Moldova). КАТЕГОРИЧНО ЗАБОРОНЕНО ставити хештег країни (наприклад, #Хорватія чи #Hrvatska), якщо ти вже поставив хештег конкретного міста!
    ТЕМАТИЧНІ ХЕШТЕГИ: Другі хештеги мають СУВОРО відповідати змісту статті (наприклад, #Економіка, #Суспільство, #Закон, #Політика, #Кримінал). Категорично ЗАБОРОНЕНО ставити хештеги #ТимчасовийЗахист або #Україна, якщо в самому тексті новини про це не йдеться.
    10. ОНОВЛЕННЯ ПОДІЙ: Якщо ти бачиш, що новина є продовженням чи оновленням попередніх подій (наприклад, завершення слідства, нові деталі аварії), додай на самий початок заголовка префікс 🔄 [ОНОВЛЕНО]:
    """
    
    for idx, item in enumerate(batch):
        has_image = "Так" if item.get('image_url') else "Ні"
        prompt += f"=== НОВИНА {idx+1} ===\nМає картинку: {has_image}\nЗаголовок: {item['title']}\nОпис: {item['description']}\n\n"
    
    prompt += f"""
    Для кожної новини виконай наступні вимоги:
    
    1. ЗАГОЛОВОК: Зроби привабливий, змістовний заголовок (обов'язково виділений тегами <b>).
    
    2. ОБ'ЄМ ТА КОНТЕКСТ:
       - Якщо "Має картинку: Так" -> напиши лаконічний текст ДО 900 символів. Якщо суть події повністю розкрита за 500-600 символів — зупинись, не "лий воду" і не розтягуй штучно.
       - Якщо "Має картинку: Ні" -> напиши розширений текст ДО 2500 символів. Якщо тема повністю вичерпана за 1500 символів — не додавай зайвого, головне — інформативність, а не обсяг.
       УВАГА: Розширюй текст ВИКЛЮЧНО за рахунок пояснення контексту події та її значення. КАТЕГОРИЧНО ЗАБОРОНЕНО вигадувати неіснуючі імена, цитати, точні цифри чи дати, яких не було в оригіналі.
       
    3. СТРУКТУРА: Дроби текст на короткі абзаци (по 3-4 речення). Для розширених текстів використовуй марковані списки (з символом • або -), щоб інформацію було зручно читати.
    
    4. TELEGRAM HTML: Використовуй ТІЛЬКИ ці теги: <b>, <i>, <u>, <s>. Ніяких тегів <p>, <br>, <h1>, <li> і ніякої Markdown-розмітки (без зірочок ** чи решіток ##).
    
    5. ФОРМАТ ВИВОДУ: Розділяй готові тексти новин СУВОРО рядком `|||`. Не додавай жодних своїх коментарів на початку чи в кінці всієї відповіді.
    """

    generated_texts = []
    logging.info("Очікування перед викликом Gemini API...")
    time.sleep(15)

    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model='gemini-3.5-flash-lite', 
                contents=prompt,
                config=types.GenerateContentConfig(
                    safety_settings=[
                        types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
                    ]
                )
            )
            
            # 1. СПОЧАТКУ ЗАХИСТ ВІД ПОРОЖНЬОЇ ВІДПОВІДІ (Safety Filters)
            if not response or not getattr(response, 'text', None):
                raise ValueError("Gemini повернув порожню відповідь (None). Ймовірно, спрацювали фільтри безпеки на вміст новин.")
            
            # 2. ЯКЩО ВСЕ ОК - ТОДІ ЛОГУЄМО І ПРАЦЮЄМО З ТЕКСТОМ
            logging.info(f"Текст від Gemini:\n{response.text}") 
            
            # --- ЖОРСТКЕ ВИДАЛЕННЯ ГАЛЮЦИНАЦІЙ ШІ ---
# Іврит, арабська та зайві наголоси
clean_text = clean_text.replace('ב', 'б').replace('ب', 'б').replace('є́', 'є').replace('і́', 'і')
# Грецькі літери-двійники
clean_text = clean_text.replace('ά', 'а').replace('α', 'а').replace('ό', 'о').replace('ο', 'о')
clean_text = clean_text.replace('έ', 'е').replace('ε', 'е').replace('ί', 'і').replace('ι', 'і')
clean_text = clean_text.replace('ή', 'и').replace('η', 'и').replace('ν', 'н').replace('Ν', 'Н')
            generated_texts = [t.strip() for t in clean_text.strip().split('|||') if t.strip()]
            
            if len(generated_texts) == len(batch):
                break
            else:
                logging.warning(f"Gemini повернув {len(generated_texts)} текстів, а треба {len(batch)}. Повторна спроба...")
                if stats is not None: stats["gemini_retries"] += 1
                time.sleep(15)
                
        except Exception as e:
            logging.warning(f"Помилка Gemini API: {e}. Очікування 60с (Спроба {attempt+1}/3)")
            if stats is not None: stats["gemini_retries"] += 1
            time.sleep(60)
            
    if not generated_texts: 
        logging.error("❌ Не вдалося згенерувати тексти після 3 спроб. Скасування публікації.")
        if stats is not None: stats["errors_count"] += 1  
        return

    history_urls = load_history(HISTORY_FILE)
    history_titles = load_history(TITLES_FILE) 
    successful_urls = []

    for idx, item in enumerate(batch):
        if idx >= len(generated_texts): break 
        
        # Передаємо порожні tags, оскільки Gemini тепер генерує їх прямо в тексті
        if send_to_telegram(generated_texts[idx], item['url'], item.get('source', 'Джерело'), item['image_url'], tags=""):
            history_urls = save_to_history(history_urls, item['url'], HISTORY_FILE)
            history_titles = save_to_history(history_titles, item['title'], TITLES_FILE)
            successful_urls.append(item['url'])
            
            if stats is not None:
                stats["published"]["total"] += 1
                
                text_context = (item.get('title', '') + " " + item.get('description', '')).lower()
                if any(re.search(kw, text_context) for kw in ukraine_keywords):
                    stats["published"]["ukraine_priority"] += 1
                
                if item.get('image_url'): stats["published"]["with_images"] += 1
                else: stats["published"]["text_only"] += 1
                
                src = item.get('source', 'Джерело')
                stats["published"]["sources"][src] = stats["published"]["sources"].get(src, 0) + 1
                
            time.sleep(45)
        else:
            if stats is not None: stats["errors_count"] += 1

    # Зберігаємо список URL новин, які пройшли ШІ-фільтр (і залишаються в роботі або були відправлені)
    # Усі новини, які були в candidate_batch, але відсіяні Gemini або відправлені, 
    # вилучаємо з беклогу, щоб вони не крутилися там повторно.
    approved_urls = {item['url'] for item in candidate_batch}
    sent_urls = set(successful_urls)
    
    # Залишаємо в беклозі лише ті новини з candidate_batch, які ШІ залишив (keep), але які ще не пішли в батч на відправку,
    # А ТАКОЖ ті новини з загального беклогу, які взагалі не брали участь у цьому батчі (не увійшли в candidate_batch).
    candidate_urls = {item['url'] for item in candidate_batch}
    rejected_urls = candidate_urls - approved_urls
    
    remaining_backlog = [item for item in backlog if item['url'] not in evaluated_urls or (item['url'] in approved_urls and item['url'] not in sent_urls)]
    
    remaining_backlog.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
    save_backlog(remaining_backlog[:30])

def send_to_telegram(text, url, source_name, image_url=None, tags=""):
    # Фінальний рубіж очищення від галюцинацій ШІ
    # 1. ФІКС ГАЛЮЦИНАЦІЙ ШІ
    text = text.replace('ב', 'б').replace('ب', 'б').replace('є́', 'є').replace('і́', 'і')
# Видаляємо грецькі літери-двійники (з наголосами та без)
text = text.replace('ά', 'а').replace('α', 'а').replace('ό', 'о').replace('ο', 'о')
text = text.replace('έ', 'е').replace('ε', 'е').replace('ί', 'і').replace('ι', 'і')
text = text.replace('ή', 'и').replace('η', 'и').replace('ν', 'н').replace('Ν', 'Н')
    
    # Розумне витягування хештегів, згенерованих Gemini в кінці тексту
    extracted_tags = ""
    parts = text.split('\n\n')
    # Перевіряємо, чи останній абзац містить хештеги
    if parts and '#' in parts[-1]:
        extracted_tags = "\n\n" + parts[-1].strip()
        text = '\n\n'.join(parts[:-1]) # Залишаємо основний текст без хештегів
    
    crop_msg = "...\n\n👉 <i>Повні деталі читайте за посиланням нижче</i> 👇"
    
    # Динамічний ліміт: враховуємо довжину хештегів, щоб вони помістилися
    safe_caption_limit = 920 - len(extracted_tags) - len(tags)
    
    # Обрізка тексту
    if image_url and len(text) > safe_caption_limit: 
        text = text[:safe_caption_limit] + crop_msg
    elif not image_url and len(text) > 3900: 
        text = text[:3900] + crop_msg
    
    # Збираємо пазл назад: Обрізаний текст + Хештеги + Джерело
    full_message = f"{text}{extracted_tags}{tags}\n\n🔗 Джерело: <a href='{url}'>{source_name}</a>"
    
    payload = {'chat_id': TELEGRAM_CHANNEL_ID, 'parse_mode': 'HTML'}
    
    if image_url:
        payload['photo'] = image_url
        payload['caption'] = full_message
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    else:
        payload['text'] = full_message
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    try:
        resp = requests.post(api_url, data=payload)
        
        # ЗАХИСТ ВІД ЗЛАМАНИХ ТЕГІВ: Якщо Telegram лається на розмітку
        if resp.status_code == 400 and "parse entities" in resp.text.lower():
            logging.warning("⚠️ Помилка HTML-розмітки (ймовірно через обрізку тексту). Відправляємо без HTML...")
            del payload['parse_mode'] # Видаляємо вимогу HTML
            resp = requests.post(api_url, data=payload) # Пробуємо ще раз
            
        if resp.status_code != 200:
            logging.error(f"❌ Telegram відхилив повідомлення: {resp.text}")
            
        return resp.status_code == 200
    except Exception as e: 
        logging.error(f"❌ Критична помилка відправки в Telegram: {e}")
        return False

def main():
    # Перевіряємо токени тут, коли скрипт безпосередньо запускається
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, GEMINI_API_KEY]):
        logging.error("Критична помилка: не знайдено базові токени у файлі .env!")
        exit(1)
    start_time = time.time()
    
    stats = {
        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
        "execution_time": 0,
        "funnel": {
            "scraped_total": 0,
            "scraped_by_source": {},  # ВИПРАВЛЕНО: Додано відсутній лічильник
            "already_seen": 0,        # ВИПРАВЛЕНО: Додано відсутній лічильник
            "rejected_by_keywords": 0,
            "duplicates_removed_by_ai": 0,
            "rejection_reasons_list": []  # <--- Додано для консистентності
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

    finally:
        trim_history(HISTORY_FILE, MAX_HISTORY_SIZE)
        trim_history(TITLES_FILE, MAX_HISTORY_SIZE)
        
        stats["execution_time"] = round(time.time() - start_time, 1)
        save_analytics(stats)
        
        if stats["published"]["total"] > 0 or stats["errors_count"] > 0:
            send_analytics_to_admin(stats)

if __name__ == "__main__":
    main()