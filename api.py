"""
api.py — FastAPI маршруты для веб-интерфейса Прораб-ERP
Подключается к той же БД что и bot.py
"""

import os
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
import psycopg2
from psycopg2 import pool
from datetime import datetime
import telebot
from bot import bot, init_db

# --- КОНФИГ ---
DATABASE_URL = os.environ.get("DATABASE_URL")

# --- ПУЛ БД (тот же DATABASE_URL что в bot.py) ---
db_pool = psycopg2.pool.ThreadedConnectionPool(1, 10, DATABASE_URL)

def get_conn():
    return db_pool.getconn()

def put_conn(conn):
    db_pool.putconn(conn)

# --- FASTAPI ---
app = FastAPI(title="Прораб-ERP API")

# CORS — разрешаем запросы из Telegram Mini App
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- ОТДАЁМ HTML ФАЙЛ ---
@app.get("/app")
def serve_webapp():
    return FileResponse("prorab-webapp.html")

# --- ВСПОМОГАТЕЛЬНЫЕ ---
def format_date(date_val) -> str:
    if isinstance(date_val, datetime):
        return date_val.strftime("%d.%m")
    elif isinstance(date_val, str):
        try:
            return datetime.strptime(date_val.split()[0], "%Y-%m-%d").strftime("%d.%m")
        except Exception:
            return date_val[:5]
    return "--"

# ─── МАРШРУТЫ ───────────────────────────────────────────

# GET /api/projects?user_id=123456
@app.get("/api/projects")
def get_projects(user_id: int):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, name, status FROM projects WHERE user_id = %s AND status = 'active' ORDER BY id",
            (user_id,)
        )
        rows = cur.fetchall()
        return [{"id": r[0], "name": r[1], "status": r[2]} for r in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        put_conn(conn)


# GET /api/projects/archived?user_id=123456
@app.get("/api/projects/archived")
def get_archived_projects(user_id: int):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, name, status FROM projects WHERE user_id = %s AND status = 'archived' ORDER BY id",
            (user_id,)
        )
        rows = cur.fetchall()
        return [{"id": r[0], "name": r[1], "status": r[2]} for r in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        put_conn(conn)


# GET /api/projects/{project_name}/summary?user_id=123456
@app.get("/api/projects/{project_name}/summary")
def get_project_summary(project_name: str, user_id: int):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT category, SUM(amount) FROM transactions WHERE user_id = %s AND project_name = %s GROUP BY category",
            (user_id, project_name)
        )
        sums = dict(cur.fetchall())

        spent_cats = ["material", "worker", "road", "unexpected"]
        total_spent = sum(float(sums.get(c, 0) or 0) for c in spent_cats)
        total_client = float(sums.get("client", 0) or 0)
        my_profit = float(sums.get("profit", 0) or 0)
        balance = total_client - total_spent

        cat_breakdown = {}
        for cat in spent_cats:
            if sums.get(cat):
                cat_breakdown[cat] = float(sums[cat])

        return {
            "project_name": project_name,
            "total_spent": total_spent,
            "total_client": total_client,
            "my_profit": my_profit,
            "balance": balance,
            "categories": cat_breakdown,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        put_conn(conn)


# GET /api/transactions?user_id=123456&project_name=ЖК Dream Park
@app.get("/api/transactions")
def get_transactions(user_id: int, project_name: str):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, category, item_name, quantity, unit,
                   unit_price, amount, target_name, created_at
            FROM transactions
            WHERE user_id = %s AND project_name = %s
            ORDER BY id DESC
            """,
            (user_id, project_name)
        )
        rows = cur.fetchall()
        result = []
        for r in rows:
            result.append({
                "id": r[0],
                "category": r[1],
                "item_name": r[2],
                "quantity": r[3],
                "unit": r[4],
                "unit_price": float(r[5]) if r[5] else None,
                "amount": float(r[6]) if r[6] else 0,
                "target_name": r[7],
                "date": format_date(r[8]),
            })
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        put_conn(conn)


# POST /api/transactions/delete
class DeleteRequest(BaseModel):
    user_id: int
    transaction_id: int

@app.post("/api/transactions/delete")
def delete_transaction(req: DeleteRequest):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM transactions WHERE id = %s AND user_id = %s",
            (req.transaction_id, req.user_id)
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Запись не найдена")
        conn.commit()
        return {"ok": True}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        put_conn(conn)


# POST /api/projects/archive
class ArchiveRequest(BaseModel):
    user_id: int
    project_name: str

@app.post("/api/projects/archive")
def archive_project(req: ArchiveRequest):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE projects SET status = 'archived' WHERE user_id = %s AND name = %s",
            (req.user_id, req.project_name)
        )
        conn.commit()
        return {"ok": True}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        put_conn(conn)


# POST /api/projects/restore
@app.post("/api/projects/restore")
def restore_project(req: ArchiveRequest):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE projects SET status = 'active' WHERE user_id = %s AND name = %s",
            (req.user_id, req.project_name)
        )
        conn.commit()
        return {"ok": True}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        put_conn(conn)


# POST /api/projects/delete
@app.post("/api/projects/delete")
def delete_project(req: ArchiveRequest):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM transactions WHERE user_id = %s AND project_name = %s",
            (req.user_id, req.project_name)
        )
        cur.execute(
            "DELETE FROM projects WHERE user_id = %s AND name = %s AND status = 'archived'",
            (req.user_id, req.project_name)
        )
        conn.commit()
        return {"ok": True}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        put_conn(conn)


# POST /api/projects/create
class CreateProjectRequest(BaseModel):
    user_id: int
    project_name: str

@app.post("/api/projects/create")
def create_project(req: CreateProjectRequest):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO projects (user_id, name, status)
            VALUES (%s, %s, 'active')
            ON CONFLICT (user_id, name) DO NOTHING
            """,
            (req.user_id, req.project_name)
        )
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=409, detail="Объект с таким именем уже существует")
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        put_conn(conn)


# POST /api/transactions/create
class CreateTransactionRequest(BaseModel):
    user_id: int
    project_name: str
    category: str
    item_name: str
    quantity: Optional[str] = None
    unit: Optional[str] = None
    unit_price: Optional[float] = None
    amount: float
    target_name: Optional[str] = None

@app.post("/api/transactions/create")
def create_transaction(req: CreateTransactionRequest):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO transactions (
                user_id, project_name, category,
                item_name, quantity, unit,
                unit_price, amount, target_name
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                req.user_id, req.project_name, req.category,
                req.item_name, req.quantity, req.unit,
                req.unit_price, req.amount, req.target_name
            )
        )
        conn.commit()
        return {"ok": True}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        put_conn(conn)


# ─── КАЛЬКУЛЯТОРЫ ───────────────────────────────────────────

# POST /api/calculations/save — сохранить расчёт
class SaveCalculationRequest(BaseModel):
    user_id: int
    calc_type: str
    calc_name: Optional[str] = None
    project_id: Optional[int] = None
    input_data: dict
    result_data: dict

@app.post("/api/calculations/save")
def save_calculation(req: SaveCalculationRequest):
    conn = get_conn()
    try:
        import json
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO calculations (user_id, calc_type, calc_name, project_id, input_data, result_data)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                req.user_id,
                req.calc_type,
                req.calc_name,
                req.project_id,
                json.dumps(req.input_data),
                json.dumps(req.result_data),
            )
        )
        new_id = cur.fetchone()[0]
        conn.commit()
        return {"ok": True, "id": new_id}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        put_conn(conn)


# GET /api/calculations?user_id=123456 — список расчётов
@app.get("/api/calculations")
def get_calculations(user_id: int):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, calc_type, calc_name, project_id, result_data, created_at
            FROM calculations
            WHERE user_id = %s
            ORDER BY created_at DESC
            """,
            (user_id,)
        )
        rows = cur.fetchall()
        result = []
        for r in rows:
            result.append({
                "id": r[0],
                "calc_type": r[1],
                "calc_name": r[2],
                "project_id": r[3],
                "result_data": r[4],
                "date": format_date(r[5]),
            })
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        put_conn(conn)


# GET /api/calculations/{calc_id}?user_id=123456 — один расчёт
@app.get("/api/calculations/{calc_id}")
def get_calculation(calc_id: int, user_id: int):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, calc_type, calc_name, project_id, input_data, result_data, created_at
            FROM calculations
            WHERE id = %s AND user_id = %s
            """,
            (calc_id, user_id)
        )
        r = cur.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Расчёт не найден")
        return {
            "id": r[0],
            "calc_type": r[1],
            "calc_name": r[2],
            "project_id": r[3],
            "input_data": r[4],
            "result_data": r[5],
            "date": format_date(r[6]),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        put_conn(conn)


# POST /api/calculations/delete — удалить расчёт
class DeleteCalculationRequest(BaseModel):
    user_id: int
    calculation_id: int

@app.post("/api/calculations/delete")
def delete_calculation(req: DeleteCalculationRequest):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM calculations WHERE id = %s AND user_id = %s",
            (req.calculation_id, req.user_id)
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Расчёт не найден")
        conn.commit()
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        put_conn(conn)


# GET /health
@app.get("/health")
def health():
    return {"status": "ok"}


# POST /webhook — приём обновлений от Telegram
@app.post("/webhook")
async def webhook(request: Request):
    import json
    data = await request.json()
    update = telebot.types.Update.de_json(data)
    bot.process_new_updates([update])
    return {"ok": True}


# --- СТАРТ ---
@app.on_event("startup")
def on_startup():
    init_db()
