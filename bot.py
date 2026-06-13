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

# --- ЛОГИРОВАНИЕ ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# --- КОНФИГ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
AI_API_KEY = os.environ.get("AI_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")

_missing = [name for name, val in [("BOT_TOKEN", BOT_TOKEN), ("AI_API_KEY", AI_API_KEY), ("DATABASE_URL", DATABASE_URL)] if not val]
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

def format_db_date(date_val) -> str:
    if isinstance(date_val, datetime):
        return date_val.strftime("%d.%m")
    elif isinstance(date_val, str):
        try:
            return datetime.strptime(date_val.split()[0], "%Y-%m-%d").strftime("%d.%m")
        except Exception:
            return date_val[:5]
    return "--.--"

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

def get_state(chat_id: int) -> dict:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT project, category, action, is_archived, last_msg_id FROM user_states WHERE user_id = %s", (chat_id,))
        row = cur.fetchone()
        if row:
            return {'project': row[0], 'category': row[1], 'action': row[2], 'is_archived': row[3] or False, 'last_msg_id': row[4]}
        else:
            cur.execute("INSERT INTO user_states (user_id, project, category, action, is_archived, last_msg_id) VALUES (%s, NULL, NULL, NULL, FALSE, NULL)", (chat_id,))
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
                project = EXCLUDED.project,
                category = EXCLUDED.category,
                action = EXCLUDED.action,
                is_archived = EXCLUDED.is_archived,
                last_msg_id = EXCLUDED.last_msg_id
        ''', (chat_id, state.get('project'), state.get('category'), state.get('action'), state.get('is_archived', False), state.get('last_msg_id')))
        conn.commit()
    except Exception as e:
        conn.rollback()
        log.error(f"save_state error: {e}")
    finally:
        put_conn(conn)

def send_single(chat_id: int, text: str, reply_markup=None, parse_mode=None) -> telebot.types.Message:
    state = get_state(chat_id)
    if state.get('last_msg_id'):
        try:
            bot.delete_message(chat_id, state['last_msg_id'])
        except Exception:
            pass
    kwargs = {}
    if reply_markup:
        kwargs['reply_markup'] = reply_markup
    if parse_mode:
        kwargs['parse_mode'] = parse_mode
    msg = bot.send_message(chat_id, text, **kwargs)
    state['last_msg_id'] = msg.message_id
    save_state(chat_id, state)
    return msg

def parse_smart_text(text: str, category: str):
    prompt = f"""
Ты — продвинутый аналитик строительных расходов в Узбекистане.
Распарси текст для категории '{category}'.
Пользователь может присылать одну запись или СПИСОК (каждая с новой строки).

ПРАВИЛО ДЛЯ ЧИСЕЛ: Если видишь "5000.000", "1500.000" — это миллионы (5 000 000, 1 500 000 сум). Преобразуй в полное число.

ПРАВИЛО ДЛЯ ЕДИНИЦ ИЗМЕРЕНИЯ (только для категории material):
Обязательно разделяй количество и единицу измерения.
Например: "10 мешков цемента" -> item_name: "цемент", quantity: "10", unit: "мешок"
"арматура 50кг" -> item_name: "арматура", quantity: "50", unit: "кг"
Если единица измерения не указана явно, пиши unit: "шт".

Верни строго чистый JSON-список объектов.
Форматы:

material: [{{"item_name":"...", "quantity":"...", "unit":"...", "unit_price": число, "amount": число}}]
worker: [{{"item_name":"За что (или 'Аванс')", "target_name":"Имя", "amount": число}}]
road/unexpected/client: [{{"item_name":"Описание", "amount": число}}]

Текст: {text}
"""
    return _call_gemini_text(prompt)

def parse_smart_voice(voice_bytes: bytes, category: str):
    prompt = f"""
Ты — продвинутый аналитик строительных расходов в Узбекистане.
Прослушай аудио и распарси данные для категории '{category}'.

ПРАВИЛО ДЛЯ ЧИСЕЛ: «пять миллионов» → 5000000, «полтора миллиона» → 1500000.

СТРОИТЕЛЬНЫЕ ТЕРМИНЫ — распознавай точно:
- "гипсокартон", "подвес", "профиль", "арматура", "цемент", "кирпич", "плитка"
- "саморез", "дюбель", "анкер", "уголок", "швеллер", "сетка", "утеплитель"

ПРАВИЛО ДЛЯ ЕДИНИЦ ИЗМЕРЕНИЯ (только для категории material):
Разделяй количество и единицу измерения.

Верни строго чистый JSON-список объектов.
Форматы:

material: [{{"item_name":"...", "quantity":"...", "unit":"...", "unit_price": число, "amount": число}}]
worker: [{{"item_name":"За что (или 'Аванс')", "target_name":"Имя", "amount": число}}]
road/unexpected/client: [{{"item_name":"Описание", "amount": число}}]
"""
    audio_b64 = base64.b64encode(voice_bytes).decode('utf-8')
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={AI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}, {"inlineData": {"mimeType": "audio/ogg", "data": audio_b64}}]}],
        "generationConfig": {"responseMimeType": "application/json"}
    }
    return _call_gemini_raw(url, payload)

def _call_gemini_text(prompt: str):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={AI_API_KEY}"
    payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"responseMimeType": "application/json"}}
    return _call_gemini_raw(url, payload)

def _call_gemini_raw(url: str, payload: dict):
    try:
        resp = requests.post(url, headers={"Content-Type": "application/json"}, json=payload, timeout=30)
        resp.raise_for_status()
        resp_json = resp.json()
        if 'candidates' not in resp_json or not resp_json['candidates']:
            return None
        candidate = resp_json['candidates'][0]
        if 'content' not in candidate or 'parts' not in candidate['content'] or not candidate['content']['parts']:
            return None
        raw = candidate['content']['parts'][0].get('text', '').strip()
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        result = json.loads(raw)
        return result if isinstance(result, list) else [result]
    except requests.RequestException as e:
        log.error(f"Gemini HTTP error: {e}")
    except (KeyError, json.JSONDecodeError) as e:
        log.error(f"Gemini parse error: {e}")
    return None

def save_entries_to_db(chat_id: int, project: str, category: str, entries: list):
    conn = get_conn()
    try:
        cur = conn.cursor()
        saved = 0
        for e in entries:
            try:
                amount = float(e.get('amount', 0) or 0)
            except (ValueError, TypeError):
                continue
            if amount <= 0:
                continue
            cur.execute('''
                INSERT INTO transactions (user_id, project_name, category, item_name, quantity, unit, unit_price, amount, target_name)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ''', (
                chat_id, project, category,
                e.get('item_name', '—'), e.get('quantity'),
                e.get('unit', 'шт') if category == 'material' else None,
                (lambda v: float(v) if v not in (None, '', 'null') else None)(e.get('unit_price')),
                amount, e.get('target_name')
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

def show_category_page(chat_id: int, project: str, category: str, prefix_text: str = ""):
    conn = get_conn()
    inline_markup = types.InlineKeyboardMarkup(row_width=2)
    prompts = {
        "material": "✌️ Жду закупку материалов (текст или голос):",
        "worker": "✌️ Жду аванс рабочего (текст или голос):",
        "road": "✌️ Жду дорожный расход (текст или голос):",
        "unexpected": "✌️ Жду непредвиденный расход (текст или голос):",
        "client": "✌️ Жду сумму от клиента (текст или голос):"
    }
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, created_at, item_name, quantity, unit, unit_price, amount, target_name "
            "FROM transactions WHERE user_id = %s AND project_name = %s AND category = %s ORDER BY id ASC",
            (chat_id, project, category)
        )
        rows = cur.fetchall()
        if category == 'worker':
            res = "📖 *Взаиморасчёты:*\n"
            for r in rows:
                dt = format_db_date(r[1])
                res += f"• {dt} — {r[7]} — {float(r[6]):,.0f} сум ({r[2]})\n"
        elif category == 'material':
            res = "📖 *Материалы:*\n"
            for r in rows:
                dt = format_db_date(r[1])
                qty = r[3] if r[3] else "1"
                unit = r[4] if r[4] else "шт"
                price = float(r[5]) if r[5] else float(r[6])
                res += f"• {dt} — {r[2]}: {qty} {unit} × {price:,.0f} = {float(r[6]):,.0f} сум\n"
        else:
            res = "📖 *Записи:*\n"
            for r in rows:
                dt = format_db_date(r[1])
                res += f"• {dt} — {r[2]} — {float(r[6]):,.0f} сум\n"
        if not rows:
            res = "📖 *В этой категории пока пусто.*\n"

        recent_rows = rows[-6:]
        if recent_rows:
            for r in recent_rows:
                t_id, _, item_name, _, _, _, amount, target_name = r
                name = target_name if category == 'worker' else item_name
                if not name or name == '—':
                    name = "Запись"
                if len(name) > 12:
                    name = name[:10] + ".."
                btn_edit = types.InlineKeyboardButton(text=f"✏️ {name} ({float(amount):,.0f})", callback_data=f"ed_{t_id}")
                btn_del = types.InlineKeyboardButton(text="❌ Удалить", callback_data=f"del_{t_id}")
                inline_markup.row(btn_edit, btn_del)
    except Exception as e:
        log.error(f"show_category_page error: {e}")
        res = "⚠️ Ошибка загрузки истории.\n"
    finally:
        put_conn(conn)

    full_text = ""
    if prefix_text:
        full_text += prefix_text + "\n\n"
    full_text += res + "——————————————————\n" + prompts.get(category, "")
    inline_markup.add(types.InlineKeyboardButton("⬅️ Назад в меню объекта", callback_data="go_back_project"))
    bot.send_message(chat_id, full_text, reply_markup=inline_markup, parse_mode="Markdown")

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
    markup.add(types.KeyboardButton("🗄️ АРХИВ ЗАВЕРШЁННЫХ ОБЪЕКТОВ"))
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
    markup.add(types.KeyboardButton("🚀 📊 ОТЧЁТ ДЛЯ КЛИЕНТА"))
    if not is_archived:
        markup.add(types.KeyboardButton("⬅️ Назад к объектам"))
        markup.add(types.KeyboardButton("📦 СДАТЬ ОБЪЕКТ В АРХИВ"))
    else:
        markup.add(types.KeyboardButton("⬅️ Назад в архив"))
        markup.add(types.KeyboardButton("🗑️ УДАЛИТЬ ОБЪЕКТ НАВСЕГДА"))
    return markup

def get_inside_category_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    markup.add(types.KeyboardButton("⬅️ Назад в меню объекта"))
    return markup

def handle_project_menu_display(chat_id: int, state: dict):
    project = state['project']
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT category, SUM(amount) FROM transactions WHERE user_id = %s AND project_name = %s GROUP BY category",
            (chat_id, project)
        )
        sums = dict(cur.fetchall())
    except Exception as e:
        log.error(f"handle_project_menu_display error: {e}")
        sums = {}
    finally:
        put_conn(conn)

    client_total = float(sums.get('client', 0) or 0)
    total_spent = sum(float(sums.get(c, 0) or 0) for c in ('material', 'worker', 'road', 'unexpected'))
    balance = client_total - total_spent
    status_emoji = "🟩" if balance >= 0 else "🟥"
    text = (
        f"📂 Объект: *{(project or 'Без названия').upper()}*\n"
        f"——————————————————\n"
        f"💰 Получено: {client_total:,.0f} сум\n"
        f"📉 Потрачено: {total_spent:,.0f} сум\n"
        f"{status_emoji} Остаток: *{balance:,.0f} сум*\n"
        f"——————————————————\n"
        f"Что вносим или смотрим?"
    )
    send_single(chat_id, text, reply_markup=get_project_keyboard(is_archived=state.get('is_archived', False)), parse_mode="Markdown")

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
    lines = [f"✅ Сохранено {saved} записей:\n"]
    for e in entries:
        amount = e.get('amount', 0) or 0
        if amount <= 0:
            continue
        if category == 'material':
            lines.append(f"• {e.get('item_name','—')}: {e.get('quantity','?')} {e.get('unit','шт')} × {e.get('unit_price',0):,.0f} = {amount:,.0f} сум")
        elif category == 'worker':
            lines.append(f"• {e.get('target_name','?')}: {amount:,.0f} сум ({e.get('item_name','Аванс')})")
        else:
            lines.append(f"• {e.get('item_name','—')}: {amount:,.0f} сум")
    show_category_page(chat_id, project, category, prefix_text="\n".join(lines))

def process_voice_entry(message, project: str, category: str):
    chat_id = message.chat.id
    try:
        bot.delete_message(chat_id, message.message_id)
    except Exception:
        pass
    try:
        file_info = bot.get_file(message.voice.file_id)
        voice_bytes = bot.download_file(file_info.file_path)
    except Exception as e:
        log.error(f"Ошибка скачивания голоса: {e}")
        send_single(chat_id, "⚠️ Не удалось получить голосовое сообщение.", reply_markup=get_inside_category_keyboard())
        return
    entries = parse_smart_voice(voice_bytes, category)
    if not entries:
        send_single(chat_id, "⚠️ Не удалось распознать голос.", reply_markup=get_inside_category_keyboard())
        return
    saved = save_entries_to_db(chat_id, project, category, entries)
    if saved == 0:
        send_single(chat_id, "⚠️ Записи не сохранены.", reply_markup=get_inside_category_keyboard())
        return
    lines = [f"✅ Сохранено {saved} записей (голос):\n"]
    for e in entries:
        amount = e.get('amount', 0) or 0
        if amount <= 0:
            continue
        if category == 'material':
            lines.append(f"• {e.get('item_name','—')}: {e.get('quantity','?')} {e.get('unit','шт')} × {e.get('unit_price',0):,.0f} = {amount:,.0f} сум")
        elif category == 'worker':
            lines.append(f"• {e.get('target_name','?')}: {amount:,.0f} сум ({e.get('item_name','Аванс')})")
        else:
            lines.append(f"• {e.get('item_name','—')}: {amount:,.0f} сум")
    show_category_page(chat_id, project, category, prefix_text="\n".join(lines))

def generate_pro_report(chat_id: int, project: str, is_archived: bool = False):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT category, item_name, quantity, unit, unit_price, amount, target_name, created_at "
            "FROM transactions WHERE user_id = %s AND project_name = %s ORDER BY category, id", (chat_id, project)
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
        'material': '🧱 Материалы', 'worker': '👷 Авансы рабочих',
        'road': '🚗 Дорожные расходы', 'unexpected': '⚠️ Непредвиденные', 'client': '💰 Оплата от клиента'
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
            _, item, qty, unit, price, amount, target, created = r
            dt = format_db_date(created)
            amount_val = float(amount) if amount else 0.0
            if cat == 'material':
                qty_str = qty if qty else "1"
                unit_str = unit if unit else "шт"
                price_str = f"{float(price):,.0f}" if price else f"{amount_val:,.0f}"
                lines.append(f" • {dt} {item}: {qty_str} {unit_str} × {price_str} = {amount_val:,.0f} сум")
            elif cat == 'worker':
                lines.append(f" • {dt} {target}: {amount_val:,.0f} сум ({item})")
            else:
                lines.append(f" • {dt} {item}: {amount_val:,.0f} сум")
            cat_sum += amount_val
        lines.append(f" *Итого: {cat_sum:,.0f} сум*\n")
        if cat == 'client':
            client_total += cat_sum
        else:
            total_spent += cat_sum

    balance = client_total - total_spent
    status = "🟩 Прибыль" if balance >= 0 else "🟥 Перерасход"
    lines += [
        "——————————————————",
        f"💰 Получено от клиента: *{client_total:,.0f} сум*",
        f"📉 Всего потрачено: *{total_spent:,.0f} сум*",
        f"{status}: *{abs(balance):,.0f} сум*"
    ]
    send_single(chat_id, "\n".join(lines), reply_markup=get_project_keyboard(is_archived), parse_mode="Markdown")

@bot.message_handler(commands=['start'])
def send_welcome(message):
    chat_id = message.chat.id
    try:
        bot.delete_message(chat_id, message.message_id)
    except Exception:
        pass
    state = {'project': None, 'category': None, 'action': None, 'is_archived': False, 'last_msg_id': None}
    save_state(chat_id, state)
    send_single(chat_id, "🏗️ Прораб-ERP запущена!\nВыбери объект:", reply_markup=get_main_keyboard(chat_id), parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    chat_id = call.message.chat.id
    data = call.data
    state = get_state(chat_id)
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    if data.startswith("del_"):
        t_id = int(data.split("_")[1])
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT category FROM transactions WHERE id = %s AND user_id = %s", (t_id, chat_id))
            row = cur.fetchone()
            if row:
                cat = row[0]
                cur.execute("DELETE FROM transactions WHERE id = %s AND user_id = %s", (t_id, chat_id))
                conn.commit()
                show_category_page(chat_id, state['project'], cat, prefix_text="❌ Запись успешно удалена.")
        except Exception as e:
            log.error(f"Callback delete error: {e}")
        finally:
            put_conn(conn)

    elif data.startswith("ed_"):
        t_id = int(data.split("_")[1])
        inline_edit = types.InlineKeyboardMarkup(row_width=1)
        inline_edit.add(
            types.InlineKeyboardButton("📝 Изменить название", callback_data=f"field_{t_id}_item"),
            types.InlineKeyboardButton("💰 Изменить сумму", callback_data=f"field_{t_id}_amount"),
            types.InlineKeyboardButton("🔙 Отмена", callback_data="cancel_edit")
        )
        bot.send_message(chat_id, "✏️ Что изменить?", reply_markup=inline_edit)

    elif data.startswith("field_"):
        parts = data.split("_")
        t_id = int(parts[1])
        field = parts[2]
        state['action'] = f"waiting_for_edit_{t_id}_{field}"
        save_state(chat_id, state)
        hint = "📝 Введи новое название:" if field == "item" else "💰 Введи новую сумму:"
        back_markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
        back_markup.add(types.KeyboardButton("⬅️ Назад в меню объекта"))
        bot.send_message(chat_id, hint, reply_markup=back_markup)

    elif data in ("cancel_edit", "go_back_project"):
        state['category'] = None
        state['action'] = None
        save_state(chat_id, state)
        handle_project_menu_display(chat_id, state)

NAV_BUTTONS = {
    "🗄️ АРХИВ ЗАВЕРШЁННЫХ ОБЪЕКТОВ", "➕ Добавить новый объект", "📦 СДАТЬ ОБЪЕКТ В АРХИВ",
    "🚀 📊 ОТЧЁТ ДЛЯ КЛИЕНТА", "🧱 Материалы", "👷 Авансы рабочих", "🚗 Дорожные расходы",
    "⚠️ Непредвиденные", "💰 Оплата от клиента", "⬅️ Назад в меню объекта", "⬅️ Назад к объектам",
    "⬅️ Назад к активным объектам", "⬅️ Назад в архив", "🗑️ УДАЛИТЬ ОБЪЕКТ НАВСЕГДА"
}

CATEGORIES_MAP = {
    "🧱 Материалы": "material", "👷 Авансы рабочих": "worker", "🚗 Дорожные расходы": "road",
    "⚠️ Непредвиденные": "unexpected", "💰 Оплата от клиента": "client"
}

def handle_edit_action(chat_id: int, text: str, state: dict, action: str):
    parts = action.split("_")
    t_id = int(parts[3])
    field = parts[4]
    new_val = text.strip()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT category, quantity, unit, unit_price, amount FROM transactions WHERE id = %s AND user_id = %s", (t_id, chat_id))
        row = cur.fetchone()
        if row:
            cat, old_qty, old_unit, old_price, old_amount = row
            if field == "item":
                cur.execute("UPDATE transactions SET item_name = %s WHERE id = %s AND user_id = %s", (new_val, t_id, chat_id))
            elif field == "amount":
                if "." in new_val and len(new_val.split(".")[1]) == 3:
                    try:
                        amount_val = float(new_val.replace(".", ""))
                    except ValueError:
                        amount_val = float(new_val.replace(" ", ""))
                else:
                    amount_val = float(new_val.replace(" ", ""))
                cur.execute("UPDATE transactions SET amount = %s WHERE id = %s AND user_id = %s", (amount_val, t_id, chat_id))
            conn.commit()
            state['action'] = None
            save_state(chat_id, state)
            show_category_page(chat_id, state['project'], cat, prefix_text="✅ Запись успешно обновлена!")
    except Exception as e:
        log.error(f"Error updating field: {e}")
        send_single(chat_id, "⚠️ Ошибка обновления данных.")
    finally:
        put_conn(conn)

def handle_navigation_and_projects(chat_id: int, text: str, state: dict):
    if text == "⬅️ Назад в меню объекта":
        state['category'] = None
        state['action'] = None
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

    if text == "🗄️ АРХИВ ЗАВЕРШЁННЫХ ОБЪЕКТОВ":
        send_single(chat_id, "📂 Архив:", reply_markup=get_archive_keyboard(chat_id))
        return

    if text == "➕ Добавить новый объект":
        state['action'] = 'waiting_for_project_name'
        save_state(chat_id, state)
        send_single(chat_id, "✌️ Напиши название нового объекта:")
        return

    if state.get('action') == 'waiting_for_project_name':
        project_name = text.strip()
        if not project_name:
            send_single(chat_id, "⚠️ Название не может быть пустым.")
            return
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM projects WHERE user_id = %s AND name = %s AND status = 'active'", (chat_id, project_name))
            if cur.fetchone():
                send_single(chat_id, f"⚠️ Активный объект *{project_name}* уже существует.", parse_mode="Markdown")
                return
            cur.execute("INSERT INTO projects (user_id, name, status) VALUES (%s, %s, 'active')", (chat_id, project_name))
            conn.commit()
        except Exception as e:
            conn.rollback()
            log.error(f"Add project error: {e}")
            send_single(chat_id, "⚠️ Ошибка создания объекта.", reply_markup=get_main_keyboard(chat_id))
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
        send_single(chat_id, f"🗄️ Архивный объект: *{pure_name}*", reply_markup=get_project_keyboard(is_archived=True), parse_mode="Markdown")
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

    if text == "🚀 📊 ОТЧЁТ ДЛЯ КЛИЕНТА" and state.get('project'):
        generate_pro_report(chat_id, state['project'], is_archived=state.get('is_archived', False))
        return

    if text == "🗑️ УДАЛИТЬ ОБЪЕКТ НАВСЕГДА" and state.get('project') and state.get('is_archived'):
        name = state['project']
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM transactions WHERE project_name = %s AND user_id = %s", (name, chat_id))
            cur.execute("DELETE FROM projects WHERE name = %s AND user_id = %s", (name, chat_id))
            conn.commit()
        except Exception as e:
            conn.rollback()
            log.error(f"Delete project error: {e}")
        finally:
            put_conn(conn)
        state.update({'project': None, 'category': None, 'is_archived': True})
        save_state(chat_id, state)
        send_single(chat_id, f"🗑️ Объект *{name}* удалён навсегда.", reply_markup=get_archive_keyboard(chat_id), parse_mode="Markdown")
        return

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

    if state.get('project') and text in CATEGORIES_MAP:
        cat = CATEGORIES_MAP[text]
        if state.get('is_archived'):
            conn = get_conn()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT item_name, quantity, unit, unit_price, amount, target_name, created_at "
                    "FROM transactions WHERE user_id = %s AND project_name = %s AND category = %s ORDER BY id ASC",
                    (chat_id, state['project'], cat)
                )
                rows = cur.fetchall()
                if cat == 'worker':
                    history = "📖 *Взаиморасчёты:*\n"
                    for r in rows:
                        dt = format_db_date(r[6])
                        history += f"• {dt} — {r[5]} — {float(r[4]):,.0f} сум ({r[0]})\n"
                elif cat == 'material':
                    history = "📖 *Материалы:*\n"
                    for r in rows:
                        dt = format_db_date(r[6])
                        price_val = float(r[3]) if r[3] else float(r[4])
                        history += f"• {dt} - {r[0]}:\n{r[1] or '1'} {r[2] or 'шт'} × {price_val:,.0f} = {float(r[4]):,.0f}\n"
                else:
                    history = "📖 *Записи:*\n"
                    for r in rows:
                        dt = format_db_date(r[6])
                        history += f"• {dt} — {r[0]} — {float(r[4]):,.0f} сум\n"
                if not rows:
                    history = "📖 *В этой категории пусто.*\n"
            except Exception as e:
                log.error(f"Archive category view error: {e}")
                history = "⚠️ Ошибка загрузки данных.\n"
            finally:
                put_conn(conn)
            send_single(chat_id, history, reply_markup=get_project_keyboard(is_archived=True), parse_mode="Markdown")
        else:
            state['category'] = cat
            save_state(chat_id, state)
            show_category_page(chat_id, state['project'], cat)
        return

@bot.message_handler(commands=["app"])
def send_webapp_link(message):
    chat_id = message.chat.id
    try:
        bot.delete_message(chat_id, message.message_id)
    except Exception:
        pass
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(
        "📊 Открыть Прораб-ERP",
        web_app=types.WebAppInfo(url="https://prorab-bot-fmnz.onrender.com/app")
    ))
    bot.send_message(chat_id, "📱 Нажми кнопку чтобы открыть веб-интерфейс:", reply_markup=markup)

@bot.message_handler(content_types=['text'])
def handle_text(message):
    chat_id = message.chat.id
    text = message.text
    if text and text.startswith('/app'):
        send_webapp_link(message)
        return
    state = get_state(chat_id)

    if state.get('action') and state['action'].startswith('waiting_for_edit_'):
        try:
            bot.delete_message(chat_id, message.message_id)
        except Exception:
            pass
        handle_edit_action(chat_id, text, state, state['action'])
        return

    if text in NAV_BUTTONS or state.get('action') == 'waiting_for_project_name' or text.endswith(" (Архив)"):
        try:
            bot.delete_message(chat_id, message.message_id)
        except Exception:
            pass
        handle_navigation_and_projects(chat_id, text, state)
        return

    if state.get('project') and state.get('category') and not state.get('is_archived'):
        process_construction_entry(message, state['project'], state['category'])
        return

    handle_navigation_and_projects(chat_id, text, state)

@bot.message_handler(content_types=['voice'])
def handle_voice(message):
    chat_id = message.chat.id
    state = get_state(chat_id)
    if state.get('project') and state.get('category') and not state.get('is_archived'):
        process_voice_entry(message, state['project'], state['category'])
    else:
        try:
            bot.delete_message(chat_id, message.message_id)
        except Exception:
            pass
        send_single(chat_id, "⚠️ Сначала выбери объект и категорию.", reply_markup=get_main_keyboard(chat_id))



# --- ЗАПУСК ---
if __name__ == "__main__":
    log.info("Инициализация базы данных...")
    init_db()

    log.info("Бот запущен.")

    if __name__ == "__main__":
    log.info("Бот запущен (Render режим без polling)")
