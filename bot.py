import os
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import psycopg2
from psycopg2 import pool
import telebot
from telebot import types

# --- ЛОГИРОВАНИЕ ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# --- КОНФИГ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
WEBAPP_URL = "https://prorab-bot-fmnz.onrender.com/app"

_missing = [name for name, val in [("BOT_TOKEN", BOT_TOKEN), ("DATABASE_URL", DATABASE_URL)] if not val]
if _missing:
    log.critical(f"Не заданы переменные окружения: {', '.join(_missing)}. Бот не может стартовать.")
    raise SystemExit(1)

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)

# --- ПУЛ СОЕДИНЕНИЙ БД ---
db_pool = psycopg2.pool.ThreadedConnectionPool(2, 20, DATABASE_URL)

def get_conn():
    return db_pool.getconn()

def put_conn(conn):
    db_pool.putconn(conn)

# --- ИНИЦИАЛИЗАЦИЯ БД ---
def init_db():
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS projects (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                name TEXT,
                status TEXT DEFAULT 'active'
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                project_name TEXT,
                category TEXT,
                item_name TEXT,
                quantity TEXT,
                unit TEXT,
                unit_price NUMERIC(15, 2),
                amount NUMERIC(15, 2),
                target_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        try:
            cur.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS unit TEXT;")
            cur.execute("ALTER TABLE transactions ALTER COLUMN unit_price TYPE NUMERIC(15, 2);")
            cur.execute("ALTER TABLE transactions ALTER COLUMN amount TYPE NUMERIC(15, 2);")
            cur.execute("ALTER TABLE transactions ALTER COLUMN created_at TYPE TIMESTAMP USING created_at::timestamp;")
            cur.execute("ALTER TABLE projects ADD CONSTRAINT unique_user_project UNIQUE (user_id, name);")
        except Exception as msg_err:
            log.info(f"[Миграция] Структура базы данных уже в актуальном состоянии: {msg_err}")
            conn.rollback()
            cur = conn.cursor()

        cur.execute('''
            CREATE TABLE IF NOT EXISTS user_states (
                user_id BIGINT PRIMARY KEY,
                project TEXT,
                category TEXT,
                action TEXT,
                is_archived BOOLEAN DEFAULT FALSE,
                last_msg_id BIGINT
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id    BIGINT PRIMARY KEY,
                first_seen TIMESTAMP DEFAULT NOW(),
                last_seen  TIMESTAMP DEFAULT NOW()
            )
        ''')
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_last_seen ON users(last_seen);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_transactions_user_project ON transactions(user_id, project_name);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id);")
        conn.commit()
        log.info("БД инициализирована успешно.")
    except Exception as e:
        conn.rollback()
        log.error(f"Ошибка инициализации БД: {e}")
        raise
    finally:
        put_conn(conn)

# --- УСТАНОВКА КНОПКИ МЕНЮ ---
def set_menu_button(chat_id=None):
    try:
        bot.set_chat_menu_button(
            chat_id=chat_id,
            menu_button=types.MenuButtonWebApp(
                text="📊 Прораб-ERP",
                web_app=types.WebAppInfo(url=WEBAPP_URL)
            )
        )
    except Exception as e:
        log.warning(f"Не удалось установить кнопку меню: {e}")

# --- /start ---
@bot.message_handler(commands=['start'])
def send_welcome(message):
    chat_id = message.chat.id

    # Устанавливаем постоянную кнопку меню
    set_menu_button(chat_id)

    # Inline кнопка для открытия Mini App
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(
        text="📊 Открыть Прораб-ERP",
        web_app=types.WebAppInfo(url=WEBAPP_URL)
    ))

    bot.send_message(
        chat_id,
        "🏗 *Прораб-ERP*\n\nНажмите кнопку чтобы открыть приложение:",
        parse_mode="Markdown",
        reply_markup=markup
    )

# --- ГОЛОСОВЫЕ СООБЩЕНИЯ ---
@bot.message_handler(content_types=['voice'])
def handle_voice(message):
    chat_id = message.chat.id
    try:
        bot.delete_message(chat_id, message.message_id)
    except Exception:
        pass
    bot.send_message(
        chat_id,
        "🎤 Голосовой ввод временно недоступен.\n\nИспользуйте Прораб-ERP через кнопку меню."
    )

# --- ВСЕ ТЕКСТОВЫЕ СООБЩЕНИЯ ---
@bot.message_handler(content_types=['text'])
def handle_text(message):
    chat_id = message.chat.id
    try:
        bot.delete_message(chat_id, message.message_id)
    except Exception:
        pass
    bot.send_message(
        chat_id,
        "🏗 *Прораб-ERP*\n\nОткройте приложение через кнопку меню.",
        parse_mode="Markdown"
    )

# --- ЗАПУСК ---
# bot.py не запускается напрямую.
# Запуск: uvicorn api:app --host 0.0.0.0 --port $PORT
# init_db() вызывается из api.py через on_startup()
