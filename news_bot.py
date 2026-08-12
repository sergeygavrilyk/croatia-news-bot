import feedparser
import requests
import os
import time
from dotenv import load_dotenv
from google import genai # Нова офіційна бібліотека

# Завантажуємо змінні оточення з файлу .env
load_dotenv()

# ================= НАЛАШТУВАННЯ =================

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHANNEL_ID = os.getenv('TELEGRAM_CHANNEL_ID')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, GEMINI_API_KEY]):
    raise ValueError("Помилка: не знайдено токени у файлі .env!")

RSS_FEEDS = [
    'https://www.index.hr/rss',       # Index.hr
    'https://www.jutarnji.hr/rss',    # Jutarnji list
]

HISTORY_FILE = 'posted_news.txt'

# ================================================

# Ініціалізація нового клієнта Gemini
client = genai.Client(api_key=GEMINI_API_KEY)

def load_history():
    """Завантажує список вже опублікованих URL-адрес."""
    if not os.path.exists(HISTORY_FILE):
        return set()
    with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
        return set(line.strip() for line in f)

def save_to_history(url):
    """Зберігає URL опублікованої новини в історію."""
    with open(HISTORY_FILE, 'a', encoding='utf-8') as f:
        f.write(url + '\n')

def generate_telegram_post(title, description):
    """Відправляє текст до Gemini для перекладу та створення поста."""
    prompt = f"""
    Ти професійний редактор Telegram-каналу новин.
    Переклади цю новину з хорватської на українську мову та зроби короткий, адаптований пост.
    
    Оригінальний заголовок: {title}
    Оригінальний текст/опис: {description}

    Вимоги:
    1. Напиши привабливий український заголовок.
    2. Коротко виклади головну суть новини (1-3 абзаци).
    3. Додай 2-3 релевантні хештеги в кінці (#Хорватія #Новини тощо).
    4. НЕ пиши жодних вступних чи пояснювальних фраз. Видай ТІЛЬКИ готовий текст для публікації.
    """
    
    try:
        # Використовуємо новий синтаксис запиту
        response = client.models.generate_content(
            model='gemini-3.5-flash',
            contents=prompt
        )
        return response.text.strip()
    except Exception as e:
        print(f"Помилка Gemini API: {e}")
        return None

def send_to_telegram(text, url):
    """Публікує готовий текст у Telegram-канал."""
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    full_message = f"{text}\n\n🔗 [Джерело]({url})"
    
    payload = {
        'chat_id': TELEGRAM_CHANNEL_ID,
        'text': full_message
    }
    
    
    try:
        response = requests.post(api_url, data=payload)
        response.raise_for_status()
        return True
    except Exception as e:
        print(f"Помилка відправки в Telegram: {e}")
        return False

def main():
    print("Запуск перевірки новин...")
    history = load_history()
    
    for feed_url in RSS_FEEDS:
        print(f"Перевірка стрічки: {feed_url}")
        feed = feedparser.parse(feed_url)
        
        for entry in feed.entries[:5]:
            url = entry.link
            
            if url in history:
                continue
                
            title = entry.title
            description = entry.get('summary', title)
            
            print(f"Нова стаття: {title}")
            
            post_text = generate_telegram_post(title, description)
            
            if post_text:
                if send_to_telegram(post_text, url):
                    print("✅ Опубліковано успішно!")
                    save_to_history(url)
                    time.sleep(3)
                else:
                    print("❌ Не вдалося опублікувати.")
            else:
                print("❌ Не вдалося згенерувати текст.")
                
    print("Перевірку завершено.")

if __name__ == "__main__":
    main()