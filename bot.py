import os
import json
import base64
import logging
from datetime import datetime
import telebot
from telebot import types
import requests
import psycopg2
from psycopg2 import pool
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# ─── ЛОГГИРОВАНИЕ ───────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ─── КОНФИГ ─────────────────────────────────────────────────────────────────
BOT_TOKEN    = os.environ.get("BOT_TOKEN")
AI_API_KEY   = os.environ.get("AI_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")

_missing = [name for name, val in [("BOT_TOKEN", BOT_TOKEN), ("AI_API_KEY", AI_API_KEY), ("DATABASE_URL", DATABASE_URL)] if not val]
if _missing:
    log.critical(f"❌ Не заданы переменные окружения: {', '.join(_missing)}. Бот не может стартовать.")
    raise SystemExit(1)

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)

# ─── ПУЛ СОЕДИНЕНИЙ БД (исправление #1) ─────────────────────────────────────
db_pool = psycopg2.pool.ThreadedConnectionPool(2, 20, DATABASE_URL)

def get_conn():
    return db_pool.getconn()

def put_conn(conn):
    db_pool.putconn(conn)

# ─── HEALTH-CHECK СЕРВЕР ─────────────────────────────────────────────────────
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *args):
        pass

def run_health_check_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    log.info(f"Health check server on port {port}")
    server.serve_forever()

# ─── ИНИЦИАЛИЗАЦИЯ БД ────────────────────────────────────────────────────────
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
                unit_price REAL,
                amount REAL,
                target_name TEXT,
                created_at TEXT
            )
        ''')
        # Сохраняем состояния пользователей в БД (исправление #2)
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
        conn.commit()
        log.info("БД инициализирована успешно.")
    except Exception as e:
        conn.rollback()
        log.error(f"Ошибка инициализации БД: {e}")
        raise
    finally:
        put_conn(conn)

# ─── СОСТОЯНИЯ ПОЛЬЗОВАТЕЛЕЙ (персистентные) ────────────────────────────────
def get_state(chat_id: int) -> dict:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT project, category, action, is_archived, last_msg_id FROM user_states WHERE user_id = %s", (chat_id,))
        row = cur.fetchone()
        if row:
            return {
                'project':     row[0],
                'category':    row[1],
                'action':      row[2],
                'is_archived': row[3] or False,
                'last_msg_id': row[4]
            }
        else:
            # Создаём запись для нового пользователя
            cur.execute(
                "INSERT INTO user_states (user_id, project, category, action, is_archived, last_msg_id) "
                "VALUES (%s, NULL, NULL, NULL, FALSE, NULL)",
                (chat_id,)
            )
            conn.commit()
            return {'project': None, 'category': None, 'action': None, 'is_archived': False, 'last_msg_id': None}
    except Exception as e:
        log.error(f"get_state error: {e}")
        return {'project': None, 'category': None, 'action': None, 'is_archived': False, 'last_msg_id': None}
    finally:
        put_conn(conn)

def save_state(chat_id: int, state: dict):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO user_states (user_id, project, category, action, is_archived, last_msg_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                project     = EXCLUDED.project,
                category    = EXCLUDED.category,
                action      = EXCLUDED.action,
                is_archived = EXCLUDED.is_archived,
                last_msg_id = EXCLUDED.last_msg_id
        ''', (
            chat_id,
            state.get('project'),
            state.get('category'),
            state.get('action'),
            state.get('is_archived', False),
            state.get('last_msg_id')
        ))
        conn.commit()
    except Exception as e:
        conn.rollback()
        log.error(f"save_state error: {e}")
    finally:
        put_conn(conn)

# ─── ОТПРАВКА ЕДИНСТВЕННОГО СООБЩЕНИЯ (удаляет предыдущее) ──────────────────
def send_single(chat_id: int, text: str, reply_markup=None, parse_mode=None) -> telebot.types.Message:
    state = get_state(chat_id)
    if state.get('last_msg_id'):
        try:
            bot.delete_message(chat_id, state['last_msg_id'])
        except Exception:
            pass

    kwargs = {}
    if reply_markup: kwargs['reply_markup'] = reply_markup
    if parse_mode:   kwargs['parse_mode']   = parse_mode
    msg = bot.send_message(chat_id, text, **kwargs)

    state['last_msg_id'] = msg.message_id
    save_state(chat_id, state)
    return msg

# ─── ИИ: ПАРСИНГ ТЕКСТА ──────────────────────────────────────────────────────
def parse_smart_text(text: str, category: str):
    prompt = f"""
Ты — продвинутый аналитик строительных расходов в Узбекистане. Распарси текст для категории '{category}'.
Пользователь может прислать одну запись или СПИСОК (каждая с новой строки).

ПРАВИЛО ДЛЯ ЧИСЕЛ:
Если видишь "5000.000", "1500.000" — это миллионы (5 000 000, 1 500 000 сум). Преобразуй в полное число.

Верни строго чистый JSON-список объектов.

Форматы:
- material:  [{{"item_name":"...", "quantity":"... шт/м2/...", "unit_price": число, "amount": число}}]
- worker:    [{{"item_name":"За что (или 'Аванс')", "target_name":"Имя", "amount": число}}]
- road/unexpected/client: [{{"item_name":"Описание", "amount": число}}]

Текст: {text}
"""
    return _call_gemini_text(prompt)

# ─── ИИ: ПАРСИНГ ГОЛОСА ──────────────────────────────────────────────────────
def parse_smart_voice(voice_bytes: bytes, category: str):
    prompt = f"""
Ты — продвинутый аналитик строительных расходов в Узбекистане. Прослушай аудио и распарси данные для категории '{category}'.

ПРАВИЛО ДЛЯ ЧИСЕЛ: «пять миллионов» → 5000000, «полтора миллиона» → 1500000.

Верни строго чистый JSON-список объектов.

Форматы:
- material:  [{{"item_name":"...", "quantity":"...", "unit_price": число, "amount": число}}]
- worker:    [{{"item_name":"За что (или 'Аванс')", "target_name":"Имя", "amount": число}}]
- road/unexpected/client: [{{"item_name":"Описание", "amount": число}}]
"""
    audio_b64 = base64.b64encode(voice_bytes).decode('utf-8')
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={AI_API_KEY}"
    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inlineData": {"mimeType": "audio/ogg", "data": audio_b64}}
            ]
        }],
        "generationConfig": {"responseMimeType": "application/json"}
    }
    return _call_gemini_raw(url, payload)

def _call_gemini_text(prompt: str):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={AI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"}
    }
    return _call_gemini_raw(url, payload)

def _call_gemini_raw(url: str, payload: dict):
    try:
        resp = requests.post(url, headers={"Content-Type": "application/json"}, json=payload, timeout=30)
        resp.raise_for_status()
        raw = resp.json()['candidates'][0]['content']['parts'][0]['text'].strip()
        # Убираем markdown-обёртку если вдруг появилась
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        result = json.loads(raw)
        return result if isinstance(result, list) else [result]
    except requests.RequestException as e:
        log.error(f"Gemini HTTP error: {e}")
    except (KeyError, json.JSONDecodeError) as e:
        log.error(f"Gemini parse error: {e}")
    return None

# ─── БД: СОХРАНЕНИЕ ЗАПИСЕЙ (исправление #4 — была отсутствующей) ───────────
def save_entries_to_db(chat_id: int, project: str, category: str, entries: list):
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_conn()
    try:
        cur = conn.cursor()
        saved = 0
        for e in entries:
            try:
                amount = float(e.get('amount', 0) or 0)
            except (ValueError, TypeError):
                log.warning(f"Некорректный amount от Gemini: {e.get('amount')!r} — запись пропущена.")
                continue
            if amount <= 0:
                continue
            cur.execute('''
                INSERT INTO transactions
                    (user_id, project_name, category, item_name, quantity, unit_price, amount, target_name, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ''', (
                chat_id,
                project,
                category,
                e.get('item_name', '—'),
                e.get('quantity'),
                (lambda v: float(v) if v not in (None, '', 'null') else None)(e.get('unit_price')),
                amount,
                e.get('target_name'),
                today
            ))
            saved += 1
        conn.commit()
        return saved
    except Exception as e:
        conn.rollback()
        log.error(f"save_entries_to_db error: {e}")
        return 0
    finally:
        put_conn(conn)

# ─── БД: ИСТОРИЯ КАТЕГОРИИ ───────────────────────────────────────────────────
def get_category_history_text(chat_id: int, project: str, category: str) -> str:
    conn = get_conn()
    try:
        cur = conn.cursor()
        if category == 'worker':
            cur.execute(
                "SELECT created_at, target_name, amount, item_name FROM transactions "
                "WHERE project_name = %s AND category = %s ORDER BY id ASC",
                (project, category)
            )
            rows = cur.fetchall()
            if not rows:
                return "📖 *В этой категории пока пусто.*\n"
            res = "📖 *Взаиморасчёты:*\n"
            for r in rows:
                dt = datetime.strptime(r[0], "%Y-%m-%d").strftime("%d.%m")
                res += f"• {dt} — {r[1]} — {r[2]:,.0f} сум ({r[3]})\n"
        elif category == 'material':
            cur.execute(
                "SELECT created_at, item_name, quantity, unit_price, amount FROM transactions "
                "WHERE project_name = %s AND category = %s ORDER BY id ASC",
                (project, category)
            )
            rows = cur.fetchall()
            if not rows:
                return "📖 *В этой категории пока пусто.*\n"
            res = "📖 *Материалы:*\n"
            for r in rows:
                dt = datetime.strptime(r[0], "%Y-%m-%d").strftime("%d.%m")
                qty   = r[2] if r[2] else "1 шт"
                price = r[3] if r[3] else r[4]
                res += f"• {dt} — {r[1]}: {qty} × {price:,.0f} = {r[4]:,.0f} сум\n"
        else:
            cur.execute(
                "SELECT created_at, item_name, amount FROM transactions "
                "WHERE project_name = %s AND category = %s ORDER BY id ASC",
                (project, category)
            )
            rows = cur.fetchall()
            if not rows:
                return "📖 *В этой категории пока пусто.*\n"
            res = "📖 *Записи:*\n"
            for r in rows:
                dt = datetime.strptime(r[0], "%Y-%m-%d").strftime("%d.%m")
                res += f"• {dt} — {r[1]} — {r[2]:,.0f} сум\n"
        return res + "———————————————————\n"
    except Exception as e:
        log.error(f"get_category_history_text error: {e}")
        return "⚠️ Ошибка загрузки истории.\n"
    finally:
        put_conn(conn)

# ─── КЛАВИАТУРЫ ──────────────────────────────────────────────────────────────
def get_main_keyboard(chat_id: int):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM projects WHERE user_id = %s AND status = 'active' ORDER BY id", (chat_id,))
        for (name,) in cur.fetchall():
            markup.add(types.KeyboardButton(name))
    finally:
        put_conn(conn)
    markup.add(types.KeyboardButton("➕ Добавить новый объект"))
    markup.add(types.KeyboardButton("🗄 АРХИВ ЗАВЕРШЕННЫХ ОБЪЕКТОВ"))
    return markup

def get_archive_keyboard(chat_id: int):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM projects WHERE user_id = %s AND status = 'archived' ORDER BY id", (chat_id,))
        for (name,) in cur.fetchall():
            markup.add(types.KeyboardButton(name + " (Архив)"))
    finally:
        put_conn(conn)
    markup.add(types.KeyboardButton("⬅️ Назад к активным объектам"))
    return markup

def get_project_keyboard(is_archived=False):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(types.KeyboardButton("🧱 Материалы"), types.KeyboardButton("👷 Авансы рабочих"))
    markup.add(types.KeyboardButton("🚗 Дорожные расходы"), types.KeyboardButton("⚠️ Непредвиденные"))
    markup.add(types.KeyboardButton("💰 Оплата от клиента"))
    markup.add(types.KeyboardButton("🚀 📊 ОТЧЕТ ДЛЯ КЛИЕНТА"))
    if not is_archived:
        markup.add(types.KeyboardButton("⬅️ Назад к объектам"))
        markup.add(types.KeyboardButton("📦 СДАТЬ ОБЪЕКТ В АРХИВ"))
    else:
        markup.add(types.KeyboardButton("⬅️ Назад в архив"))
    return markup

def get_inside_category_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    markup.add(types.KeyboardButton("⬅️ Назад в меню объекта"))
    return markup

# ─── ДАШБОРД ОБЪЕКТА ─────────────────────────────────────────────────────────
def handle_project_menu_display(chat_id: int, state: dict):
    project = state['project']
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT category, SUM(amount) FROM transactions WHERE project_name = %s GROUP BY category",
            (project,)
        )
        sums = dict(cur.fetchall())
    except Exception as e:
        log.error(f"handle_project_menu_display error: {e}")
        sums = {}
    finally:
        put_conn(conn)

    client_total = sums.get('client', 0) or 0
    total_spent  = sum(sums.get(c, 0) or 0 for c in ('material', 'worker', 'road', 'unexpected'))
    balance      = client_total - total_spent
    status_emoji = "🟩" if balance >= 0 else "🟥"

    text = (
        f"📂 Объект: *{project.upper()}*\n"
        f"———————————————————\n"
        f"💰 Получено:  {client_total:,.0f} сум\n"
        f"📉 Потрачено: {total_spent:,.0f} сум\n"
        f"{status_emoji} Остаток: *{balance:,.0f} сум*\n"
        f"———————————————————\n"
        f"Что вносим или смотрим?"
    )
    send_single(chat_id, text, reply_markup=get_project_keyboard(is_archived=state.get('is_archived', False)), parse_mode="Markdown")

# ─── ОБРАБОТКА ТЕКСТОВОЙ ЗАПИСИ ──────────────────────────────────────────────
def process_construction_entry(message, project: str, category: str):
    chat_id = message.chat.id
    try:
        bot.delete_message(chat_id, message.message_id)
    except Exception:
        pass

    entries = parse_smart_text(message.text, category)
    if not entries:
        send_single(chat_id, "⚠️ Не удалось распознать. Напиши чётче (название + сумма).", reply_markup=get_inside_category_keyboard())
        return

    saved = save_entries_to_db(chat_id, project, category, entries)
    if saved == 0:
        send_single(chat_id, "⚠️ Записи не сохранены — проверь суммы (должны быть > 0).", reply_markup=get_inside_category_keyboard())
        return

    # Формируем подтверждение
    lines = [f"✅ Сохранено {saved} запис{'ь' if saved==1 else 'и' if saved<5 else 'ей'}:\n"]
    for e in entries:
        amount = e.get('amount', 0) or 0
        if amount <= 0:
            continue
        if category == 'material':
            lines.append(f"• {e.get('item_name','—')}: {e.get('quantity','?')} × {e.get('unit_price',0):,.0f} = {amount:,.0f} сум")
        elif category == 'worker':
            lines.append(f"• {e.get('target_name','?')}: {amount:,.0f} сум ({e.get('item_name','Аванс')})")
        else:
            lines.append(f"• {e.get('item_name','—')}: {amount:,.0f} сум")

    state = get_state(chat_id)
    history_text = get_category_history_text(chat_id, project, category)
    prompts = {
        "material":   "✍️ Ещё закупки (или ⬅️ Назад):",
        "worker":     "✍️ Ещё авансы (или ⬅️ Назад):",
        "road":       "✍️ Ещё дорожные расходы (или ⬅️ Назад):",
        "unexpected": "✍️ Ещё непредвиденные (или ⬅️ Назад):",
        "client":     "✍️ Ещё оплата от клиента (или ⬅️ Назад):"
    }
    full_text = "\n".join(lines) + f"\n\n{history_text}{prompts.get(category,'')}"
    send_single(chat_id, full_text, reply_markup=get_inside_category_keyboard(), parse_mode="Markdown")

# ─── ГЕНЕРАЦИЯ ОТЧЁТА ────────────────────────────────────────────────────────
def generate_pro_report(chat_id: int, project: str, is_archived: bool = False):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT category, item_name, quantity, unit_price, amount, target_name, created_at "
            "FROM transactions WHERE project_name = %s ORDER BY category, id",
            (project,)
        )
        rows = cur.fetchall()
    except Exception as e:
        log.error(f"generate_pro_report error: {e}")
        send_single(chat_id, "⚠️ Ошибка генерации отчёта.", reply_markup=get_project_keyboard(is_archived))
        return
    finally:
        put_conn(conn)

    if not rows:
        send_single(chat_id, "📊 Нет данных для отчёта.", reply_markup=get_project_keyboard(is_archived))
        return

    cat_labels = {
        'material':   '🧱 Материалы',
        'worker':     '👷 Авансы рабочих',
        'road':       '🚗 Дорожные расходы',
        'unexpected': '⚠️ Непредвиденные',
        'client':     '💰 Оплата от клиента'
    }
    cats = {}
    for r in rows:
        cats.setdefault(r[0], []).append(r)

    lines = [f"📊 *ОТЧЁТ: {project.upper()}*", f"📅 {datetime.now().strftime('%d.%m.%Y')}", ""]

    total_spent = 0
    client_total = 0

    for cat, label in cat_labels.items():
        if cat not in cats:
            continue
        lines.append(f"*{label}*")
        cat_sum = 0
        for r in cats[cat]:
            _, item, qty, price, amount, target, created = r
            dt = datetime.strptime(created, "%Y-%m-%d").strftime("%d.%m")
            if cat == 'material':
                qty_str = qty if qty else "1 шт"
                price_str = f"{price:,.0f}" if price else f"{amount:,.0f}"
                lines.append(f"  • {dt} {item}: {qty_str} × {price_str} = {amount:,.0f} сум")
            elif cat == 'worker':
                lines.append(f"  • {dt} {target}: {amount:,.0f} сум ({item})")
            else:
                lines.append(f"  • {dt} {item}: {amount:,.0f} сум")
            cat_sum += amount or 0
        lines.append(f"  *Итого: {cat_sum:,.0f} сум*\n")
        if cat == 'client':
            client_total += cat_sum
        else:
            total_spent += cat_sum

    balance = client_total - total_spent
    status  = "🟩 Прибыль" if balance >= 0 else "🟥 Перерасход"
    lines += [
        "———————————————————",
        f"💰 Получено от клиента: *{client_total:,.0f} сум*",
        f"📉 Всего потрачено:     *{total_spent:,.0f} сум*",
        f"{status}: *{abs(balance):,.0f} сум*"
    ]

    send_single(chat_id, "\n".join(lines), reply_markup=get_project_keyboard(is_archived), parse_mode="Markdown")

# ─── ОБРАБОТЧИК /start ───────────────────────────────────────────────────────
@bot.message_handler(commands=['start'])
def send_welcome(message):
    chat_id = message.chat.id
    try:
        bot.delete_message(chat_id, message.message_id)
    except Exception:
        pass
    state = {'project': None, 'category': None, 'action': None, 'is_archived': False, 'last_msg_id': None}
    save_state(chat_id, state)
    send_single(chat_id, "🏗 *Прораб-ERP запущена!*\nВыбери объект:", reply_markup=get_main_keyboard(chat_id), parse_mode="Markdown")

# ─── ГЛАВНЫЙ ОБРАБОТЧИК ТЕКСТА ───────────────────────────────────────────────
NAV_BUTTONS = {
    "🗄 АРХИВ ЗАВЕРШЕННЫХ ОБЪЕКТОВ", "➕ Добавить новый объект",
    "📦 СДАТЬ ОБЪЕКТ В АРХИВ", "🚀 📊 ОТЧЕТ ДЛЯ КЛИЕНТА",
    "🧱 Материалы", "👷 Авансы рабочих", "🚗 Дорожные расходы",
    "⚠️ Непредвиденные", "💰 Оплата от клиента",
    "⬅️ Назад в меню объекта", "⬅️ Назад к объектам",
    "⬅️ Назад к активным объектам", "⬅️ Назад в архив"
}

CATEGORIES_MAP = {
    "🧱 Материалы":        "material",
    "👷 Авансы рабочих":   "worker",
    "🚗 Дорожные расходы": "road",
    "⚠️ Непредвиденные":  "unexpected",
    "💰 Оплата от клиента":"client"
}

@bot.message_handler(func=lambda m: True)
def handle_all_text(message):
    chat_id = message.chat.id
    text    = message.text
    state   = get_state(chat_id)

    try:
        bot.delete_message(chat_id, message.message_id)
    except Exception:
        pass

    # ── Пользователь внутри категории и шлёт данные ──
    if state.get('category') and not state.get('is_archived') and text not in NAV_BUTTONS:
        process_construction_entry(message, state['project'], state['category'])
        return

    # ── Навигация ──
    if text == "⬅️ Назад в меню объекта":
        state['category'] = None
        save_state(chat_id, state)
        handle_project_menu_display(chat_id, state)
        return

    if text in ("⬅️ Назад к объектам", "⬅️ Назад к активным объектам"):
        state.update({'project': None, 'category': None, 'action': None, 'is_archived': False})
        save_state(chat_id, state)
        send_single(chat_id, "Выбери объект:", reply_markup=get_main_keyboard(chat_id))
        return

    if text == "⬅️ Назад в архив":
        state.update({'project': None, 'category': None, 'action': None, 'is_archived': True})
        save_state(chat_id, state)
        send_single(chat_id, "Каталог сданных объектов:", reply_markup=get_archive_keyboard(chat_id))
        return

    if text == "🗄 АРХИВ ЗАВЕРШЕННЫХ ОБЪЕКТОВ":
        send_single(chat_id, "📂 Архив:", reply_markup=get_archive_keyboard(chat_id))
        return

    if text == "➕ Добавить новый объект":
        state['action'] = 'waiting_for_project_name'
        save_state(chat_id, state)
        send_single(chat_id, "✍️ Напиши название нового объекта:")
        return

    if state.get('action') == 'waiting_for_project_name':
        project_name = text.strip()
        if not project_name:
            send_single(chat_id, "⚠️ Название не может быть пустым. Попробуй ещё раз:")
            return
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute("INSERT INTO projects (user_id, name, status) VALUES (%s, %s, 'active')", (chat_id, project_name))
            conn.commit()
        except Exception as e:
            conn.rollback()
            log.error(f"Add project error: {e}")
            send_single(chat_id, "⚠️ Ошибка создания объекта. Попробуй снова.", reply_markup=get_main_keyboard(chat_id))
            return
        finally:
            put_conn(conn)
        state['action'] = None
        save_state(chat_id, state)
        send_single(chat_id, f"✅ Объект *{project_name}* создан!", reply_markup=get_main_keyboard(chat_id), parse_mode="Markdown")
        return

    if text.endswith(" (Архив)"):
        pure_name = text[:-8]
        state.update({'project': pure_name, 'is_archived': True, 'category': None})
        save_state(chat_id, state)
        send_single(chat_id, f"🗃 Архивный объект: *{pure_name}*", reply_markup=get_project_keyboard(is_archived=True), parse_mode="Markdown")
        return

    if text == "📦 СДАТЬ ОБЪЕКТ В АРХИВ" and state.get('project'):
        name = state['project']
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE projects SET status = 'archived' WHERE name = %s AND user_id = %s", (name, chat_id))
            conn.commit()
        except Exception as e:
            conn.rollback()
            log.error(f"Archive project error: {e}")
        finally:
            put_conn(conn)
        state.update({'project': None, 'category': None})
        save_state(chat_id, state)
        send_single(chat_id, f"📦 Объект *{name}* перенесён в архив.", reply_markup=get_main_keyboard(chat_id), parse_mode="Markdown")
        return

    if text == "🚀 📊 ОТЧЕТ ДЛЯ КЛИЕНТА" and state.get('project'):
        generate_pro_report(chat_id, state['project'], is_archived=state.get('is_archived', False))
        return

    # ── Выбор активного объекта ──
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM projects WHERE user_id = %s AND status = 'active'", (chat_id,))
        active = [r[0] for r in cur.fetchall()]
    except Exception as e:
        log.error(f"Fetch projects error: {e}")
        active = []
    finally:
        put_conn(conn)

    if text in active:
        state.update({'project': text, 'is_archived': False, 'category': None})
        save_state(chat_id, state)
        handle_project_menu_display(chat_id, state)
        return

    # ── Выбор категории ──
    if state.get('project') and text in CATEGORIES_MAP:
        cat = CATEGORIES_MAP[text]
        if state.get('is_archived'):
            history = get_category_history_text(chat_id, state['project'], cat)
            send_single(chat_id, f"📍 [АРХИВ] {state['project']}\n\n{history}⚠️ Объект закрыт.",
                        reply_markup=get_project_keyboard(is_archived=True), parse_mode="Markdown")
            return
        state['category'] = cat
        save_state(chat_id, state)
        history = get_category_history_text(chat_id, state['project'], cat)
        prompts = {
            "material":   "✍️ Жду закупку материалов (текст или голос):",
            "worker":     "✍️ Жду аванс рабочего (текст или голос):",
            "road":       "✍️ Жду дорожный расход (текст или голос):",
            "unexpected": "✍️ Жду непредвиденный расход (текст или голос):",
            "client":     "✍️ Жду сумму от клиента (текст или голос):"
        }
        send_single(chat_id, f"📍 {state['project']}\n\n{history}{prompts[cat]}",
                    reply_markup=get_inside_category_keyboard(), parse_mode="Markdown")
        return

    send_single(chat_id, "⚠️ Выбери объект из меню ниже.", reply_markup=get_main_keyboard(chat_id))

# ─── ОБРАБОТЧИК ГОЛОСА (исправление #3 — был обрезан) ───────────────────────
@bot.message_handler(content_types=['voice'])
def handle_voice_entry(message):
    chat_id = message.chat.id
    state   = get_state(chat_id)

    try:
        bot.delete_message(chat_id, message.message_id)
    except Exception:
        pass

    if not state.get('project') or not state.get('category') or state.get('is_archived'):
        send_single(chat_id, "⚠️ Зайди в активный объект → категорию, и только потом надиктовывай голос.")
        return

    # Скачиваем аудио
    try:
        file_info  = bot.get_file(message.voice.file_id)
        file_url   = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
        voice_bytes = requests.get(file_url, timeout=15).content
    except Exception as e:
        log.error(f"Voice download error: {e}")
        send_single(chat_id, "⚠️ Не удалось скачать голосовое. Попробуй ещё раз.", reply_markup=get_inside_category_keyboard())
        return

    # Отправляем в Gemini
    entries = parse_smart_voice(voice_bytes, state['category'])
    if not entries:
        send_single(chat_id, "⚠️ Не удалось распознать голос. Попробуй надиктовать чётче.", reply_markup=get_inside_category_keyboard())
        return

    saved = save_entries_to_db(chat_id, state['project'], state['category'], entries)
    if saved == 0:
        send_single(chat_id, "⚠️ Записи не сохранены — проверь суммы в сообщении.", reply_markup=get_inside_category_keyboard())
        return

    lines = [f"🎤 Распознано и сохранено {saved} запис{'ь' if saved==1 else 'и' if saved<5 else 'ей'}:\n"]
    for e in entries:
        amount = e.get('amount', 0) or 0
        if amount <= 0:
            continue
        cat = state['category']
        if cat == 'material':
            lines.append(f"• {e.get('item_name','—')}: {e.get('quantity','?')} × {e.get('unit_price',0):,.0f} = {amount:,.0f} сум")
        elif cat == 'worker':
            lines.append(f"• {e.get('target_name','?')}: {amount:,.0f} сум ({e.get('item_name','Аванс')})")
        else:
            lines.append(f"• {e.get('item_name','—')}: {amount:,.0f} сум")

    history = get_category_history_text(chat_id, state['project'], state['category'])
    send_single(chat_id, "\n".join(lines) + f"\n\n{history}✍️ Можно добавить ещё (или ⬅️ Назад):",
                reply_markup=get_inside_category_keyboard(), parse_mode="Markdown")

# ─── ЗАПУСК ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    threading.Thread(target=run_health_check_server, daemon=True).start()
    log.info("Бот запущен. Ожидаю сообщения...")
    bot.infinity_polling(timeout=30, long_polling_timeout=30)
