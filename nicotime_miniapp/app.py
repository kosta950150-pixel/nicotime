import os, hmac, hashlib, time, json, sqlite3, secrets
from pathlib import Path
from urllib.parse import parse_qsl
from typing import Optional
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()
APP_SECRET = os.getenv("APP_SECRET", "")
ADMIN_ID = os.getenv("ADMIN_TELEGRAM_ID", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DEV_MODE = os.getenv("DEV_MODE", "false").lower() == "true"
DB_PATH = os.getenv("DATABASE_PATH", "./data/nicotime.db")
TOKEN_TTL = 60 * 60 * 12
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="NICOTIME Admin API", version="1.0.0")
app.mount("/static", StaticFiles(directory="static"), name="static")

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS categories (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL UNIQUE,
          sort_order INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS products (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE RESTRICT,
          name TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          price REAL NOT NULL DEFAULT 0,
          discount_percent INTEGER NOT NULL DEFAULT 0,
          is_new INTEGER NOT NULL DEFAULT 0,
          is_active INTEGER NOT NULL DEFAULT 1,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS variants (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
          name TEXT NOT NULL,
          attributes TEXT NOT NULL DEFAULT '',
          stock INTEGER NOT NULL DEFAULT 0,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS admin_sessions (
          token_hash TEXT PRIMARY KEY,
          telegram_id TEXT NOT NULL,
          expires_at INTEGER NOT NULL
        );
        """)
        count = c.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
        if count == 0:
            c.executemany("INSERT INTO categories(name,sort_order) VALUES(?,?)",
                [("Жидкости",1),("Расходники",2),("Подсистемы",3),("Одноразовые подсистемы",4)])

init_db()

class LoginBody(BaseModel):
    init_data: str = ""
    password: str

class ProductBody(BaseModel):
    category_id: int
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    price: float = Field(ge=0)
    discount_percent: int = Field(default=0, ge=0, le=100)
    is_new: bool = False
    is_active: bool = True

class VariantBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    attributes: str = ""
    stock: int = Field(default=0, ge=0)

def verify_telegram_init_data(init_data: str) -> Optional[str]:
    if DEV_MODE and not init_data:
        return ADMIN_ID or "local-dev"
    if not BOT_TOKEN or not init_data:
        return None
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None
        auth_date = int(parsed.get("auth_date", "0"))
        if abs(time.time() - auth_date) > 86400:
            return None
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated, received_hash):
            return None
        user = json.loads(parsed.get("user", "{}"))
        return str(user.get("id", ""))
    except Exception:
        return None

def issue_session(telegram_id: str) -> str:
    token = secrets.token_urlsafe(40)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with db() as c:
        c.execute("DELETE FROM admin_sessions WHERE expires_at < ?", (int(time.time()),))
        c.execute("INSERT INTO admin_sessions(token_hash,telegram_id,expires_at) VALUES(?,?,?)",
                  (token_hash, telegram_id, int(time.time()) + TOKEN_TTL))
    return token

def require_admin(authorization: Optional[str] = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Требуется вход администратора")
    token = authorization[7:]
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with db() as c:
        row = c.execute("SELECT telegram_id,expires_at FROM admin_sessions WHERE token_hash=?",
                        (token_hash,)).fetchone()
    if not row or row["expires_at"] < int(time.time()):
        raise HTTPException(401, "Сессия истекла. Войдите снова.")
    return row["telegram_id"]

@app.get("/")
def index():
    return FileResponse("static/index.html")

@app.get("/api/health")
def health():
    return {"ok": True, "service": "NICOTIME", "time": int(time.time())}

@app.post("/api/login")
def login(body: LoginBody):
    telegram_id = verify_telegram_init_data(body.init_data)
    if not telegram_id:
        raise HTTPException(401, "Не удалось подтвердить Telegram. Откройте приложение через бота.")
    if not ADMIN_ID or telegram_id != ADMIN_ID:
        raise HTTPException(403, "У этого Telegram-аккаунта нет прав администратора.")
    if not ADMIN_PASSWORD or not secrets.compare_digest(body.password, ADMIN_PASSWORD):
        raise HTTPException(403, "Неверный пароль.")
    return {"token": issue_session(telegram_id), "admin": True}

@app.post("/api/logout")
def logout(admin=Depends(require_admin), authorization: str = Header(default="")):
    token = authorization[7:] if authorization.startswith("Bearer ") else ""
    with db() as c:
        c.execute("DELETE FROM admin_sessions WHERE token_hash=?",
                  (hashlib.sha256(token.encode()).hexdigest(),))
    return {"ok": True}

@app.get("/api/categories")
def categories(admin=Depends(require_admin)):
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM categories ORDER BY sort_order,id")]

@app.get("/api/products")
def products(admin=Depends(require_admin), category_id: Optional[int] = None, q: str = ""):
    sql = """SELECT p.*, c.name AS category_name,
             CASE WHEN EXISTS(SELECT 1 FROM variants v WHERE v.product_id=p.id AND v.stock>0)
                  THEN 1 ELSE 0 END AS has_stock,
             (SELECT COALESCE(SUM(stock),0) FROM variants v WHERE v.product_id=p.id) AS total_stock
             FROM products p JOIN categories c ON c.id=p.category_id WHERE 1=1"""
    args = []
    if category_id:
        sql += " AND p.category_id=?"; args.append(category_id)
    if q:
        sql += " AND p.name LIKE ?"; args.append(f"%{q}%")
    sql += " ORDER BY p.updated_at DESC,p.id DESC"
    with db() as c:
        result = []
        for row in c.execute(sql, args):
            item = dict(row)
            item["variants"] = [dict(v) for v in c.execute(
                "SELECT * FROM variants WHERE product_id=? ORDER BY id", (item["id"],))]
            result.append(item)
        return result

@app.post("/api/products")
def create_product(body: ProductBody, admin=Depends(require_admin)):
    with db() as c:
        if not c.execute("SELECT id FROM categories WHERE id=?", (body.category_id,)).fetchone():
            raise HTTPException(404, "Категория не найдена")
        cur = c.execute("""INSERT INTO products(category_id,name,description,price,discount_percent,is_new,is_active)
            VALUES(?,?,?,?,?,?,?)""",
            (body.category_id,body.name.strip(),body.description,body.price,body.discount_percent,
             int(body.is_new),int(body.is_active)))
        return {"id": cur.lastrowid}

@app.put("/api/products/{product_id}")
def update_product(product_id: int, body: ProductBody, admin=Depends(require_admin)):
    with db() as c:
        cur = c.execute("""UPDATE products SET category_id=?,name=?,description=?,price=?,
            discount_percent=?,is_new=?,is_active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (body.category_id,body.name.strip(),body.description,body.price,body.discount_percent,
             int(body.is_new),int(body.is_active),product_id))
        if cur.rowcount == 0: raise HTTPException(404, "Товар не найден")
    return {"ok": True}

@app.delete("/api/products/{product_id}")
def delete_product(product_id: int, admin=Depends(require_admin)):
    with db() as c:
        cur = c.execute("DELETE FROM products WHERE id=?", (product_id,))
        if cur.rowcount == 0: raise HTTPException(404, "Товар не найден")
    return {"ok": True}

@app.post("/api/products/{product_id}/variants")
def add_variant(product_id: int, body: VariantBody, admin=Depends(require_admin)):
    with db() as c:
        if not c.execute("SELECT id FROM products WHERE id=?", (product_id,)).fetchone():
            raise HTTPException(404, "Товар не найден")
        cur = c.execute("INSERT INTO variants(product_id,name,attributes,stock) VALUES(?,?,?,?)",
                        (product_id,body.name.strip(),body.attributes,body.stock))
        c.execute("UPDATE products SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (product_id,))
        return {"id": cur.lastrowid}

@app.put("/api/variants/{variant_id}")
def update_variant(variant_id: int, body: VariantBody, admin=Depends(require_admin)):
    with db() as c:
        cur = c.execute("""UPDATE variants SET name=?,attributes=?,stock=?,updated_at=CURRENT_TIMESTAMP
            WHERE id=?""", (body.name.strip(),body.attributes,body.stock,variant_id))
        if cur.rowcount == 0: raise HTTPException(404, "Вариант не найден")
        product_id = c.execute("SELECT product_id FROM variants WHERE id=?", (variant_id,)).fetchone()[0]
        c.execute("UPDATE products SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (product_id,))
    return {"ok": True}

@app.delete("/api/variants/{variant_id}")
def delete_variant(variant_id: int, admin=Depends(require_admin)):
    with db() as c:
        row = c.execute("SELECT product_id FROM variants WHERE id=?", (variant_id,)).fetchone()
        if not row: raise HTTPException(404, "Вариант не найден")
        c.execute("DELETE FROM variants WHERE id=?", (variant_id,))
        c.execute("UPDATE products SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (row["product_id"],))
    return {"ok": True}
