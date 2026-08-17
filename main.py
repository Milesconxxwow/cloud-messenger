import json
import os
import shutil
import uuid
import hashlib
import aiosqlite
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

app = FastAPI(title="Cipher - Discord Edition")

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = "messenger.db"
DEV_USERNAMES = ["milesconxxwow", "miles"]

def is_dev(username: str) -> bool:
    return username.lower().strip().lstrip("@") in DEV_USERNAMES

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                username TEXT UNIQUE NOT NULL,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                avatar_url TEXT DEFAULT '',
                custom_status TEXT DEFAULT '🎮 Discord Edition',
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_tag TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                creator_username TEXT NOT NULL,
                avatar_url TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_username TEXT NOT NULL,
                sender_name TEXT NOT NULL,
                target TEXT NOT NULL,
                text TEXT DEFAULT '',
                msg_type TEXT DEFAULT 'text',
                file_url TEXT DEFAULT '',
                file_name TEXT DEFAULT '',
                reply_to_id INTEGER DEFAULT 0,
                reply_to_text TEXT DEFAULT '',
                reply_to_sender TEXT DEFAULT '',
                reactions TEXT DEFAULT '{}',
                is_edited INTEGER DEFAULT 0,
                is_deleted INTEGER DEFAULT 0,
                timestamp TEXT NOT NULL,
                avatar_url TEXT DEFAULT ''
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS blocks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                blocker TEXT NOT NULL,
                blocked TEXT NOT NULL,
                UNIQUE(blocker, blocked)
            )
        """)
        await db.commit()

class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, WebSocket] = {}

    async def connect(self, username: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[username] = websocket
        await self.broadcast_online()

    async def disconnect(self, username: str):
        if username in self.active_connections:
            del self.active_connections[username]
        await self.broadcast_online()

    async def broadcast_online(self):
        online = list(self.active_connections.keys())
        payload = json.dumps({"type": "online_list", "users": online})
        for conn in list(self.active_connections.values()):
            try:
                await conn.send_text(payload)
            except Exception:
                pass

    async def send_to_user(self, message: dict, recipient_username: str):
        if recipient_username in self.active_connections:
            try:
                await self.active_connections[recipient_username].send_text(json.dumps(message))
            except Exception:
                pass

    async def broadcast(self, message: dict, sender_username: str = None):
        for uname, conn in list(self.active_connections.items()):
            if uname != sender_username:
                try:
                    await conn.send_text(json.dumps(message))
                except Exception:
                    pass

manager = ConnectionManager()

@app.on_event("startup")
async def startup():
    await init_db()

class RegisterModel(BaseModel):
    email: str
    username: str
    display_name: str
    password: str

class LoginModel(BaseModel):
    login: str
    password: str

class BlockModel(BaseModel):
    blocker: str
    blocked: str

@app.post("/api/register")
async def register(data: RegisterModel):
    email = data.email.strip().lower()
    uname = data.username.strip().lstrip("@").lower()
    name = data.display_name.strip() or uname
    pwd = data.password.strip()

    if not email or not uname or not pwd:
        return {"status": "error", "message": "Заполните все поля"}
    if len(uname) < 3:
        return {"status": "error", "message": "Юзернейм от 3 символов"}
    if len(pwd) < 4:
        return {"status": "error", "message": "Пароль от 4 символов"}

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id FROM users WHERE email = ? OR username = ?", (email, uname))
        if await cur.fetchone():
            return {"status": "error", "message": "Email или юзернейм уже заняты"}

        pwd_hash = hash_password(pwd)
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        await db.execute(
            "INSERT INTO users (email, username, display_name, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
            (email, uname, name, pwd_hash, created_at)
        )
        await db.commit()
        return {
            "status": "ok",
            "username": uname,
            "display_name": name,
            "email": email,
            "avatar_url": "",
            "is_dev": is_dev(uname)
        }

@app.post("/api/login")
async def login(data: LoginModel):
    login_val = data.login.strip().lstrip("@").lower()
    pwd_hash = hash_password(data.password.strip())

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """SELECT username, display_name, email, avatar_url 
               FROM users 
               WHERE (email = ? OR username = ?) AND password_hash = ?""",
            (login_val, login_val, pwd_hash)
        )
        user = await cur.fetchone()
        if user:
            return {
                "status": "ok",
                "username": user[0],
                "display_name": user[1],
                "email": user[2],
                "avatar_url": user[3] or "",
                "is_dev": is_dev(user[0])
            }
        return {"status": "error", "message": "Неверный логин или пароль"}

@app.post("/api/upload_file")
async def upload_file_endpoint(file: UploadFile = File(...)):
    ext = file.filename.split(".")[-1].lower() if "." in file.filename else "bin"
    unique_name = f"file_{uuid.uuid4().hex[:12]}.{ext}"
    file_path = os.path.join(UPLOAD_DIR, unique_name)

    with open(file_path, "wb") as buf:
        shutil.copyfileobj(file.file, buf)

    is_img = ext in ["jpg", "jpeg", "png", "gif", "webp"]
    is_audio = ext in ["webm", "ogg", "mp3", "wav", "m4a"]
    file_type = "image" if is_img else ("voice" if is_audio else "document")

    return {
        "status": "ok",
        "url": f"/uploads/{unique_name}",
        "file_name": file.filename,
        "msg_type": file_type
    }

@app.post("/api/upload_avatar")
async def upload_avatar(username: str = Form(...), file: UploadFile = File(...)):
    ext = file.filename.split(".")[-1].lower() if "." in file.filename else "jpg"
    filename = f"avatar_{username}_{uuid.uuid4().hex[:8]}.{ext}"
    file_path = os.path.join(UPLOAD_DIR, filename)

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    avatar_url = f"/uploads/{filename}"

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET avatar_url = ? WHERE username = ?", (avatar_url, username))
        await db.commit()

    return {"status": "ok", "avatar_url": avatar_url}

@app.post("/api/create_channel")
async def create_channel(tag: str = Form(...), name: str = Form(...), desc: str = Form(""), creator: str = Form(...), file: UploadFile = File(None)):
    clean_tag = tag.strip().lstrip("#").lower()
    clean_name = name.strip()
    if not clean_tag or not clean_name:
        return {"status": "error", "message": "Укажите название и тег сервера/канала"}

    avatar_url = ""
    if file and file.filename:
        ext = file.filename.split(".")[-1].lower()
        filename = f"chan_{clean_tag}_{uuid.uuid4().hex[:8]}.{ext}"
        path = os.path.join(UPLOAD_DIR, filename)
        with open(path, "wb") as buf:
            shutil.copyfileobj(file.file, buf)
        avatar_url = f"/uploads/{filename}"

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id FROM channels WHERE channel_tag = ?", (clean_tag,))
        if await cur.fetchone():
            return {"status": "error", "message": "Канал с таким тегом уже существует"}

        created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        await db.execute(
            "INSERT INTO channels (channel_tag, name, description, creator_username, avatar_url, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (clean_tag, clean_name, desc, creator, avatar_url, created_at)
        )
        await db.commit()

    return {"status": "ok", "channel_tag": clean_tag, "name": clean_name, "avatar_url": avatar_url}

@app.get("/api/user_info")
async def get_user_info(username: str, current_user: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT username, display_name, email, avatar_url, created_at, custom_status FROM users WHERE username = ?",
            (username.strip().lstrip("@"),)
        )
        row = await cur.fetchone()
        if not row:
            return {"status": "not_found"}
        
        cur_blk = await db.execute("SELECT id FROM blocks WHERE blocker = ? AND blocked = ?", (current_user, row[0]))
        is_blocked = bool(await cur_blk.fetchone())

        return {
            "status": "ok",
            "username": row[0],
            "display_name": row[1],
            "email": row[2],
            "avatar_url": row[3] or "",
            "created_at": row[4],
            "custom_status": row[5] or "🎮 Discord Edition",
            "is_dev": is_dev(row[0]),
            "is_blocked": is_blocked
        }

@app.post("/api/toggle_block")
async def toggle_block(data: BlockModel):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id FROM blocks WHERE blocker = ? AND blocked = ?", (data.blocker, data.blocked))
        row = await cur.fetchone()
        if row:
            await db.execute("DELETE FROM blocks WHERE blocker = ? AND blocked = ?", (data.blocker, data.blocked))
            await db.commit()
            return {"status": "ok", "blocked": False}
        else:
            await db.execute("INSERT INTO blocks (blocker, blocked) VALUES (?, ?)", (data.blocker, data.blocked))
            await db.commit()
            return {"status": "ok", "blocked": True}

@app.get("/api/chats")
async def get_user_chats(username: str):
    async with aiosqlite.connect(DB_PATH) as db:
        chats = []
        channels = []

        cur_blk = await db.execute("SELECT blocked FROM blocks WHERE blocker = ?", (username,))
        blocked_list = [r[0] for r in await cur_blk.fetchall()]

        # Сервера / Каналы
        cur_ch = await db.execute("SELECT channel_tag, name, avatar_url FROM channels")
        for ch in await cur_ch.fetchall():
            channels.append({
                "key": f"#{ch[0]}",
                "name": ch[1],
                "tag": f"#{ch[0]}",
                "avatar": ch[2] or ""
            })

        # Все пользователи для списка друзей и ЛС
        cur_all_users = await db.execute("SELECT username, display_name, avatar_url, custom_status FROM users WHERE username != ?", (username,))
        for u in await cur_all_users.fetchall():
            cur_last = await db.execute("""
                SELECT text, msg_type, timestamp FROM messages 
                WHERE ((sender_username = ? AND target = ?) OR (sender_username = ? AND target = ?)) AND is_deleted = 0
                ORDER BY id DESC LIMIT 1
            """, (username, u[0], u[0], username))
            last = await cur_last.fetchone()
            last_text = ""
            if last:
                last_text = "🎙️ Голосовое" if last[1] == "voice" else ("📷 Фотография" if last[1] == "image" else (last[0] or "📎 Файл"))

            chats.append({
                "key": u[0],
                "name": u[1],
                "tag": f"@{u[0]}",
                "type": "user",
                "avatar": u[2] or "",
                "activity": u[3] or "🎮 Discord Edition",
                "last_msg": last_text,
                "time": last[2] if last else "",
                "is_dev": is_dev(u[0]),
                "is_blocked": u[0] in blocked_list
            })

        return {"chats": chats, "channels": channels}

@app.get("/api/search")
async def search(q: str):
    query = f"%{q.strip().lstrip('@').lstrip('#').lower()}%"
    async with aiosqlite.connect(DB_PATH) as db:
        cur_u = await db.execute("SELECT username, display_name, avatar_url FROM users WHERE username LIKE ? OR display_name LIKE ? LIMIT 10", (query, query))
        users = [{"type": "user", "key": r[0], "tag": f"@{r[0]}", "name": r[1], "avatar": r[2], "is_dev": is_dev(r[0])} for r in await cur_u.fetchall()]

        cur_c = await db.execute("SELECT channel_tag, name, avatar_url, description FROM channels WHERE channel_tag LIKE ? OR name LIKE ? LIMIT 10", (query, query))
        channels = [{"type": "channel", "key": f"#{r[0]}", "tag": f"#{r[0]}", "name": r[1], "avatar": r[2], "desc": r[3], "is_dev": False} for r in await cur_c.fetchall()]

        return {"results": users + channels}

@app.get("/api/history")
async def get_history(user: str, target: str):
    async with aiosqlite.connect(DB_PATH) as db:
        if target.startswith("#"):
            cur = await db.execute(
                """SELECT id, sender_username, sender_name, target, text, msg_type, file_url, file_name, 
                          reply_to_id, reply_to_text, reply_to_sender, reactions, is_edited, is_deleted, timestamp, avatar_url 
                   FROM messages WHERE target = ? AND is_deleted = 0 ORDER BY id ASC LIMIT 250""",
                (target,)
            )
        else:
            cur = await db.execute(
                """SELECT id, sender_username, sender_name, target, text, msg_type, file_url, file_name, 
                          reply_to_id, reply_to_text, reply_to_sender, reactions, is_edited, is_deleted, timestamp, avatar_url 
                   FROM messages 
                   WHERE ((sender_username = ? AND target = ?) OR (sender_username = ? AND target = ?)) 
                     AND is_deleted = 0
                   ORDER BY id ASC LIMIT 250""",
                (user, target, target, user)
            )
        rows = await cur.fetchall()
        return [{
            "id": r[0],
            "sender_username": r[1],
            "sender_name": r[2],
            "target": r[3],
            "text": r[4],
            "msg_type": r[5],
            "file_url": r[6],
            "file_name": r[7],
            "reply_to_id": r[8],
            "reply_to_text": r[9],
            "reply_to_sender": r[10],
            "reactions": json.loads(r[11] or "{}"),
            "is_edited": bool(r[12]),
            "time": r[14],
            "avatar": r[15],
            "is_dev": is_dev(r[1])
        } for r in rows]

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Discord</title>
    <style>
        :root {
            --bg-guilds: #1e1f22;
            --bg-sidebar: #2b2d31;
            --bg-main: #313338;
            --bg-members: #2b2d31;
            --bg-user-bar: #232428;
            --bg-input: #383a40;
            --bg-hover: #35373c;
            --bg-active: #404249;
            --text-normal: #dbdee1;
            --text-muted: #949ba4;
            --text-link: #00a8fc;
            --brand: #5865f2;
            --brand-hover: #4752c4;
            --green: #23a55a;
            --red: #f23f43;
            --yellow: #f0b232;
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            font-family: "gg sans", "Noto Sans", "Helvetica Neue", Helvetica, Arial, sans-serif;
            -webkit-tap-highlight-color: transparent;
            user-select: none;
        }

        body, html {
            height: 100%;
            background: var(--bg-main);
            color: var(--text-normal);
            overflow: hidden;
        }

        .dev-badge {
            background: linear-gradient(135deg, #8b5cf6, #ec4899);
            color: #fff;
            font-size: 0.62rem;
            font-weight: 800;
            padding: 1px 5px;
            border-radius: 4px;
            letter-spacing: 0.4px;
            display: inline-flex;
            align-items: center;
            vertical-align: middle;
            margin-left: 4px;
        }

        #app-layout {
            display: flex;
            height: 100%;
            width: 100%;
        }

        /* 1. КОЛОНКА СЕРВЕРОВ (72px) */
        #guilds-bar {
            width: 72px;
            background: var(--bg-guilds);
            display: flex;
            flex-direction: column;
            align-items: center;
            padding: 12px 0;
            gap: 8px;
            flex-shrink: 0;
            overflow-y: auto;
        }
        .guild-icon {
            width: 48px;
            height: 48px;
            border-radius: 50%;
            background: var(--bg-sidebar);
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            transition: border-radius 0.2s, background-color 0.2s;
            position: relative;
            color: var(--text-normal);
            font-weight: bold;
            font-size: 1.1rem;
        }
        .guild-icon:hover, .guild-icon.active {
            border-radius: 16px;
            background: var(--brand);
            color: #fff;
        }
        .guild-icon.dm-btn { background: #5865f2; }
        .guild-icon img { width: 100%; height: 100%; border-radius: inherit; object-fit: cover; }
        .guild-sep { width: 32px; height: 2px; background: #35363c; border-radius: 1px; margin: 4px 0; }

        /* 2. САЙДБАР ЛИЧНЫХ СООБЩЕНИЙ / ДРУЗЕЙ (240px) */
        #sidebar {
            width: 240px;
            background: var(--bg-sidebar);
            display: flex;
            flex-direction: column;
            flex-shrink: 0;
            height: 100%;
        }
        .search-btn-wrapper {
            padding: 10px;
            border-bottom: 2px solid #202225;
        }
        .search-btn {
            background: var(--bg-guilds);
            color: var(--text-muted);
            border: none;
            width: 100%;
            padding: 8px 10px;
            border-radius: 4px;
            font-size: 0.85rem;
            cursor: pointer;
            text-align: left;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }

        .sidebar-menu {
            padding: 8px;
            display: flex;
            flex-direction: column;
            gap: 2px;
        }
        .nav-item {
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 10px 12px;
            border-radius: 4px;
            color: var(--text-muted);
            font-size: 0.95rem;
            font-weight: 500;
            cursor: pointer;
            transition: 0.15s;
        }
        .nav-item:hover { background: var(--bg-hover); color: var(--text-normal); }
        .nav-item.active { background: var(--bg-active); color: #fff; }

        .dms-header {
            padding: 18px 14px 6px 14px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            color: var(--text-muted);
            font-size: 0.75rem;
            font-weight: 700;
            letter-spacing: 0.5px;
        }
        .dms-header span.plus { cursor: pointer; font-size: 1.1rem; }
        .dms-header span.plus:hover { color: #fff; }

        .dms-list {
            flex: 1;
            overflow-y: auto;
            padding: 0 8px;
            display: flex;
            flex-direction: column;
            gap: 2px;
        }
        .dm-item {
            display: flex;
            align-items: center;
            padding: 8px 10px;
            border-radius: 4px;
            cursor: pointer;
            color: var(--text-muted);
            gap: 10px;
            transition: 0.15s;
        }
        .dm-item:hover { background: var(--bg-hover); color: var(--text-normal); }
        .dm-item.active { background: var(--bg-active); color: #fff; }

        .avatar-wrap {
            position: relative;
            width: 32px;
            height: 32px;
            flex-shrink: 0;
        }
        .avatar-wrap.large { width: 40px; height: 40px; }
        .avatar-img {
            width: 100%;
            height: 100%;
            border-radius: 50%;
            object-fit: cover;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: bold;
            color: #fff;
            background: #5865f2;
        }
        .status-dot {
            position: absolute;
            bottom: -2px;
            right: -2px;
            width: 12px;
            height: 12px;
            border-radius: 50%;
            background: #80848e;
            border: 3px solid var(--bg-sidebar);
        }
        .status-dot.online { background: var(--green); }

        /* Панель текущего профиля внизу сайдбара */
        .current-user-bar {
            height: 54px;
            background: var(--bg-user-bar);
            padding: 0 8px;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }
        .current-user-info {
            display: flex;
            align-items: center;
            gap: 8px;
            cursor: pointer;
            padding: 4px;
            border-radius: 4px;
            max-width: 120px;
        }
        .current-user-info:hover { background: var(--bg-hover); }
        .user-panel-btns { display: flex; gap: 2px; }
        .panel-icon-btn {
            background: transparent;
            border: none;
            color: var(--text-muted);
            width: 32px;
            height: 32px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 1rem;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .panel-icon-btn:hover { background: var(--bg-hover); color: var(--text-normal); }
        .panel-icon-btn.muted { color: var(--red); }

        /* 3. ЦЕНТРАЛЬНАЯ ОБЛАСТЬ (Чат или вкладка «Друзья») */
        #main-view {
            flex: 1;
            display: flex;
            flex-direction: column;
            background: var(--bg-main);
            height: 100%;
            min-width: 0;
        }

        .top-navbar {
            height: 48px;
            border-bottom: 2px solid #202225;
            padding: 0 16px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-shrink: 0;
        }
        .navbar-left { display: flex; align-items: center; gap: 16px; }
        .navbar-title {
            font-weight: 700;
            color: #fff;
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 0.95rem;
        }
        .tab-btn {
            background: transparent;
            border: none;
            color: var(--text-muted);
            padding: 4px 8px;
            border-radius: 4px;
            font-weight: 600;
            font-size: 0.9rem;
            cursor: pointer;
        }
        .tab-btn:hover { background: var(--bg-hover); color: var(--text-normal); }
        .tab-btn.active { background: var(--bg-active); color: #fff; }
        .tab-btn.add-friend { background: var(--green); color: #fff; }

        .navbar-right { display: flex; align-items: center; gap: 12px; }
        .top-action-btn {
            background: transparent;
            border: none;
            color: var(--text-muted);
            font-size: 1.25rem;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .top-action-btn:hover { color: var(--text-normal); }

        /* Вкладка друзей (Список) */
        #friends-view {
            flex: 1;
            display: flex;
            overflow: hidden;
        }
        .friends-column {
            flex: 1;
            padding: 16px 24px;
            overflow-y: auto;
        }
        .friends-search-box {
            background: var(--bg-guilds);
            border-radius: 6px;
            padding: 8px 12px;
            display: flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 20px;
        }
        .friends-search-box input {
            background: transparent;
            border: none;
            color: #fff;
            outline: none;
            width: 100%;
            font-size: 0.9rem;
        }

        .friend-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 10px 12px;
            border-top: 1px solid rgba(255,255,255,0.06);
            border-radius: 8px;
            cursor: pointer;
        }
        .friend-row:hover { background: var(--bg-hover); }
        .friend-meta { display: flex; align-items: center; gap: 12px; }
        .friend-actions { display: flex; gap: 8px; }
        .circle-action-btn {
            width: 36px;
            height: 36px;
            border-radius: 50%;
            background: var(--bg-guilds);
            border: none;
            color: var(--text-muted);
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            font-size: 1rem;
        }
        .circle-action-btn:hover { background: #111214; color: #fff; }

        /* 4. ПРАВАЯ КОЛОНКА «АКТИВНЫЕ КОНТАКТЫ» (340px) */
        #activity-sidebar {
            width: 340px;
            border-left: 1px solid rgba(255,255,255,0.06);
            padding: 16px;
            display: flex;
            flex-direction: column;
            gap: 16px;
            overflow-y: auto;
        }
        .activity-card {
            background: var(--bg-sidebar);
            border-radius: 8px;
            padding: 14px;
            display: flex;
            gap: 12px;
        }

        /* Область переписки (Чат) */
        #chat-view {
            flex: 1;
            display: none;
            flex-direction: column;
            height: 100%;
            position: relative;
        }
        .messages-box {
            flex: 1;
            overflow-y: auto;
            padding: 20px;
            display: flex;
            flex-direction: column;
            gap: 14px;
        }
        .discord-msg {
            display: flex;
            gap: 16px;
            padding: 2px 0;
            user-select: text;
        }
        .discord-msg:hover { background: rgba(0,0,0,0.07); margin: 0 -20px; padding: 2px 20px; }
        .msg-content-wrap { flex: 1; }
        .msg-author-row { display: flex; align-items: baseline; gap: 8px; margin-bottom: 4px; }
        .msg-author { font-weight: 600; color: #fff; cursor: pointer; }
        .msg-author:hover { text-decoration: underline; }
        .msg-time { font-size: 0.72rem; color: var(--text-muted); }
        .msg-body-text { color: var(--text-normal); font-size: 0.94rem; line-height: 1.4; word-break: break-word; }

        .chat-input-container {
            padding: 0 16px 20px 16px;
            flex-shrink: 0;
        }
        .chat-input-bar {
            background: var(--bg-input);
            border-radius: 8px;
            padding: 0 14px;
            display: flex;
            align-items: center;
            gap: 12px;
        }
        .chat-input-bar input {
            flex: 1;
            background: transparent;
            border: none;
            padding: 12px 0;
            color: #fff;
            outline: none;
            font-size: 0.95rem;
        }

        /* 📞 ОВЕРЛЕЙ ЗВОНКА (WebRTC Call Screen) */
        #call-overlay {
            position: absolute;
            inset: 0;
            background: #111214;
            display: none;
            flex-direction: column;
            align-items: center;
            justify-content: space-between;
            padding: 40px 20px;
            z-index: 500;
        }
        .call-user-avatar {
            width: 110px;
            height: 110px;
            border-radius: 50%;
            object-fit: cover;
            border: 4px solid var(--brand);
            box-shadow: 0 0 25px rgba(88, 101, 242, 0.5);
            animation: pulseCall 2s infinite;
        }
        .call-controls { display: flex; gap: 18px; }
        .call-btn {
            width: 56px;
            height: 56px;
            border-radius: 50%;
            border: none;
            color: #fff;
            font-size: 1.4rem;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .call-btn.end { background: var(--red); }
        .call-btn.mute { background: var(--bg-sidebar); }

        /* Всплывающее окно входящего звонка */
        #incoming-call-modal {
            position: fixed;
            top: 20px;
            right: 20px;
            background: var(--bg-sidebar);
            border: 2px solid var(--brand);
            box-shadow: 0 10px 30px rgba(0,0,0,0.8);
            border-radius: 12px;
            padding: 16px 20px;
            display: none;
            align-items: center;
            gap: 16px;
            z-index: 10000;
            animation: slideIn 0.3s ease-out;
        }

        /* Модальные окна */
        .modal-bg {
            position: fixed;
            inset: 0;
            background: rgba(0, 0, 0, 0.85);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 9999;
        }
        .modal-card {
            background: var(--bg-main);
            padding: 28px;
            border-radius: 8px;
            width: 90%;
            max-width: 420px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.6);
        }
        .modal-card input {
            width: 100%;
            background: var(--bg-guilds);
            border: none;
            padding: 12px;
            border-radius: 4px;
            color: #fff;
            outline: none;
            margin-bottom: 14px;
        }
        .btn-brand {
            background: var(--brand);
            color: #fff;
            border: none;
            width: 100%;
            padding: 12px;
            border-radius: 4px;
            font-weight: 600;
            cursor: pointer;
        }

        @keyframes pulseCall {
            0% { transform: scale(1); box-shadow: 0 0 0 0 rgba(35, 165, 90, 0.7); }
            70% { transform: scale(1.05); box-shadow: 0 0 0 20px rgba(35, 165, 90, 0); }
            100% { transform: scale(1); box-shadow: 0 0 0 0 rgba(35, 165, 90, 0); }
        }
        @keyframes slideIn { from { transform: translateY(-30px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }
    </style>
</head>
<body>

<!-- Всплывающий входящий звонок -->
<div id="incoming-call-modal">
    <div class="avatar-wrap large">
        <div class="avatar-img" id="caller-avatar">?</div>
    </div>
    <div>
        <div id="caller-name" style="font-weight:bold; font-size:1rem; color:#fff;">Пользователь</div>
        <div style="font-size:0.8rem; color:var(--text-muted);">Входящий голосовой вызов...</div>
    </div>
    <div style="display:flex; gap:8px;">
        <button class="call-btn" style="background:var(--green); width:42px; height:42px;" onclick="acceptIncomingCall()">📞</button>
        <button class="call-btn end" style="width:42px; height:42px;" onclick="declineIncomingCall()">✕</button>
    </div>
</div>

<!-- Модалка Авторизации -->
<div id="auth-modal" class="modal-bg">
    <div class="modal-card">
        <h2 style="text-align:center; margin-bottom:6px; color:#fff;" id="auth-title">С возвращением!</h2>
        <p style="text-align:center; color:var(--text-muted); font-size:0.85rem; margin-bottom:20px;">Мы так рады видеть вас снова!</p>
        
        <input type="text" id="auth-login" placeholder="Email или @юзернейм">
        <input type="text" id="auth-name" placeholder="Отображаемое имя (например, Miles)" style="display:none;">
        <input type="text" id="auth-username" placeholder="Уникальный @юзернейм" style="display:none;">
        <input type="password" id="auth-pwd" placeholder="Пароль">
        
        <button class="btn-brand" onclick="submitAuth()" id="auth-btn">Вход</button>
        <p style="text-align:center; color:var(--text-link); font-size:0.85rem; margin-top:14px; cursor:pointer;" onclick="toggleAuth()" id="auth-toggle">Нужна учетная запись? Зарегистрироваться</p>
    </div>
</div>

<!-- Модалка Профиля -->
<div id="profile-modal" class="modal-bg" style="display:none;">
    <div class="modal-card" style="text-align:center;">
        <h3 style="margin-bottom:14px; color:#fff;">Настройки профиля</h3>
        <div id="profile-avatar-preview" style="width:80px; height:80px; border-radius:50%; margin:0 auto 12px auto; overflow:hidden;"></div>
        <h3 id="profile-name"></h3>
        <p id="profile-tag" style="color:var(--text-muted); font-size:0.85rem; margin-bottom:16px;"></p>
        <input type="file" id="profile-file" accept="image/*" style="margin-bottom:12px;">
        <button class="btn-brand" onclick="uploadUserAvatar()">Сохранить аватарку</button>
        <button class="btn-brand" style="background:var(--red); margin-top:8px;" onclick="logout()">Выйти из аккаунта</button>
        <button class="btn-brand" style="background:transparent; color:var(--text-muted); margin-top:4px;" onclick="document.getElementById('profile-modal').style.display='none'">Закрыть</button>
    </div>
</div>

<div id="app-layout">
    <!-- 1. КОЛОНКА СЕРВЕРОВ -->
    <div id="guilds-bar">
        <div class="guild-icon dm-btn active" title="Личные сообщения" onclick="showFriendsTab()">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="#fff"><path d="M19.73 4.87a18.2 18.2 0 0 0-4.6-1.44c-.2.35-.4.8-.56 1.18a16.7 16.7 0 0 0-5.14 0c-.16-.38-.36-.83-.56-1.18-1.63.26-3.2.75-4.6 1.44A19 19 0 0 0 .96 17.7a18.4 18.4 0 0 0 5.63 2.87c.45-.6.85-1.25 1.2-1.93-1-.38-1.94-.9-2.8-1.53.24-.17.47-.35.7-.54 5.4 2.5 11.26 2.5 16.6 0 .23.19.46.37.7.54-.86.63-1.8 1.15-2.8 1.53.35.68.75 1.33 1.2 1.93a18.4 18.4 0 0 0 5.63-2.87c1.37-6.24-.3-11.66-2.08-12.83zM8.52 14.9c-1.03 0-1.89-.95-1.89-2.12s.83-2.13 1.9-2.13c1.06 0 1.9.96 1.89 2.13 0 1.17-.83 2.12-1.9 2.12zm6.96 0c-1.03 0-1.89-.95-1.89-2.12s.84-2.13 1.9-2.13c1.06 0 1.9.96 1.89 2.13 0 1.17-.83 2.12-1.9 2.12z"/></svg>
        </div>
        <div class="guild-sep"></div>
        <div id="servers-list" style="display:flex; flex-direction:column; gap:8px;"></div>
        <div class="guild-icon" title="Создать сервер/канал" onclick="createChannelPrompt()">➕</div>
    </div>

    <!-- 2. САЙДБАР ЛИЧНЫХ СООБЩЕНИЙ -->
    <div id="sidebar">
        <div class="search-btn-wrapper">
            <button class="search-btn" onclick="document.getElementById('friends-search-input').focus()">
                <span>Найти или начать беседу</span>
            </button>
        </div>

        <div class="sidebar-menu">
            <div class="nav-item active" id="nav-friends" onclick="showFriendsTab()">
                <span>👥</span> Друзья
            </div>
            <div class="nav-item"><span>🚀</span> Nitro</div>
            <div class="nav-item"><span>🛒</span> Магазин</div>
        </div>

        <div class="dms-header">
            <span>ЛИЧНЫЕ СООБЩЕНИЯ</span>
            <span class="plus" title="Создать ЛС" onclick="document.getElementById('friends-search-input').focus()">+</span>
        </div>

        <div class="dms-list" id="dms-list"></div>

        <!-- Нижняя панель профиля -->
        <div class="current-user-bar">
            <div class="current-user-info" onclick="openProfile()">
                <div class="avatar-wrap">
                    <div class="avatar-img" id="my-avatar-mini">?</div>
                    <div class="status-dot online"></div>
                </div>
                <div style="min-width:0; overflow:hidden;">
                    <div id="my-name-bar" style="font-weight:600; font-size:0.85rem; color:#fff; white-space:nowrap; text-overflow:ellipsis; overflow:hidden;">Загрузка...</div>
                    <div style="font-size:0.72rem; color:var(--text-muted);">В сети</div>
                </div>
            </div>
            <div class="user-panel-btns">
                <button class="panel-icon-btn" id="btn-mute" title="Заглушить микрофон" onclick="toggleMuteMic()">🎙️</button>
                <button class="panel-icon-btn" title="Заглушить звук" onclick="this.classList.toggle('muted')">🎧</button>
                <button class="panel-icon-btn" title="Настройки" onclick="openProfile()">⚙️</button>
            </div>
        </div>
    </div>

    <!-- 3. ЦЕНТРАЛЬНЫЙ ЭКРАН -->
    <div id="main-view">
        <!-- Шапка -->
        <div class="top-navbar">
            <div class="navbar-left" id="top-nav-left">
                <div class="navbar-title">👥 Друзья</div>
                <div style="width:1px; height:16px; background:rgba(255,255,255,0.1);"></div>
                <button class="tab-btn active" onclick="filterFriends('online')">В сети</button>
                <button class="tab-btn" onclick="filterFriends('all')">Все</button>
                <button class="tab-btn" onclick="filterFriends('blocked')">Заблокированные</button>
                <button class="tab-btn add-friend" onclick="document.getElementById('friends-search-input').focus()">Добавить в друзья</button>
            </div>

            <div class="navbar-right" id="chat-actions" style="display:none;">
                <button class="top-action-btn" title="Начать голосовой звонок 📞" onclick="startCall()">📞</button>
                <button class="top-action-btn" title="Закрепленные сообщения">📌</button>
            </div>
        </div>

        <!-- Вкладка Друзей (Discord Home) -->
        <div id="friends-view">
            <div class="friends-column">
                <div class="friends-search-box">
                    <input type="text" id="friends-search-input" placeholder="Поиск" oninput="renderFriendsList(this.value)">
                    <span style="color:var(--text-muted);">🔍</span>
                </div>
                <div id="friends-count-label" style="font-size:0.75rem; font-weight:700; color:var(--text-muted); margin-bottom:12px;">В СЕТИ — 0</div>
                <div id="friends-list-items" style="display:flex; flex-direction:column; gap:2px;"></div>
            </div>

            <!-- 4. Активные контакты -->
            <div id="activity-sidebar">
                <h3 style="color:#fff; font-size:1.15rem; font-weight:800;">Активные контакты</h3>
                
                <div class="activity-card">
                    <div class="avatar-wrap large">
                        <div class="avatar-img" style="background:#22c55e;">L</div>
                    </div>
                    <div>
                        <div style="font-weight:bold; color:#fff; font-size:0.92rem;">Leo</div>
                        <div style="font-size:0.8rem; color:var(--text-muted); margin-top:2px;">🎮 Играет в Roblox</div>
                    </div>
                </div>

                <div class="activity-card">
                    <div class="avatar-wrap large">
                        <div class="avatar-img" style="background:#3b82f6;">X</div>
                    </div>
                    <div>
                        <div style="font-weight:bold; color:#fff; font-size:0.92rem;">xiaofeng356</div>
                        <div style="font-size:0.8rem; color:var(--text-muted); margin-top:2px;">💻 Пишет на C++</div>
                    </div>
                </div>
            </div>
        </div>

        <!-- Чат Discord -->
        <div id="chat-view">
            <!-- Оверлей Звонка -->
            <div id="call-overlay">
                <div style="text-align:center;">
                    <div class="call-user-avatar" id="call-peer-avatar" style="margin:0 auto 14px auto; display:flex; align-items:center; justify-content:center; font-size:2.5rem; font-weight:bold; color:#fff;">?</div>
                    <h2 id="call-peer-name" style="color:#fff; margin-bottom:4px;">Собеседник</h2>
                    <div id="call-timer" style="color:var(--green); font-weight:600; font-size:0.9rem;">Идет вызов...</div>
                </div>
                <audio id="remoteAudio" autoplay></audio>
                <div class="call-controls">
                    <button class="call-btn mute" id="call-mute-btn" onclick="toggleCallMute()">🎙️</button>
                    <button class="call-btn end" onclick="endCall()">📞</button>
                </div>
            </div>

            <div class="messages-box" id="messages"></div>

            <div class="chat-input-container">
                <div class="chat-input-bar">
                    <button class="top-action-btn" onclick="document.getElementById('media-file-input').click()">➕</button>
                    <input type="text" id="msg-input" placeholder="Написать сообщение..." onkeydown="if(event.key==='Enter') sendMsg()">
                    <button class="top-action-btn" onclick="sendMsg()">➤</button>
                </div>
            </div>
        </div>
    </div>
</div>

<input type="file" id="media-file-input" style="display:none;" onchange="handleFileUpload(this.files[0])">

<script>
    let user = JSON.parse(localStorage.getItem("messenger_user") || "null");
    let currentTarget = "";
    let isRegister = false;
    let ws = null;
    let allUsers = [];
    let currentFilter = "online";
    let onlineUsers = [];
    let currentMessages = [];

    // WebRTC переменные
    let pc = null;
    let localStream = null;
    let callTimerInterval = null;
    let callSeconds = 0;
    let incomingCaller = null;

    const DEV_USERS = ["milesconxxwow", "miles"];
    function checkIsDev(uname) {
        return uname && DEV_USERS.includes(uname.toLowerCase().replace("@", ""));
    }

    window.onload = () => {
        if (user && user.username) {
            document.getElementById("auth-modal").style.display = "none";
            startApp();
        }
    };

    function toggleAuth() {
        isRegister = !isRegister;
        document.getElementById("auth-name").style.display = isRegister ? "block" : "none";
        document.getElementById("auth-username").style.display = isRegister ? "block" : "none";
        document.getElementById("auth-title").innerText = isRegister ? "Создать учетную запись" : "С возвращением!";
        document.getElementById("auth-btn").innerText = isRegister ? "Продолжить" : "Вход";
        document.getElementById("auth-toggle").innerText = isRegister ? "Уже есть аккаунт? Войти" : "Нужна учетная запись? Зарегистрироваться";
    }

    async function submitAuth() {
        if (isRegister) {
            const email = document.getElementById("auth-login").value;
            const display_name = document.getElementById("auth-name").value;
            const username = document.getElementById("auth-username").value;
            const password = document.getElementById("auth-pwd").value;

            const res = await fetch("/api/register", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({ email, display_name, username, password })
            });
            const data = await res.json();
            if (data.status === "ok") {
                user = data;
                localStorage.setItem("messenger_user", JSON.stringify(user));
                location.reload();
            } else alert(data.message);
        } else {
            const login = document.getElementById("auth-login").value;
            const password = document.getElementById("auth-pwd").value;

            const res = await fetch("/api/login", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({ login, password })
            });
            const data = await res.json();
            if (data.status === "ok") {
                user = data;
                localStorage.setItem("messenger_user", JSON.stringify(user));
                location.reload();
            } else alert(data.message);
        }
    }

    function logout() {
        localStorage.removeItem("messenger_user");
        location.reload();
    }

    async function startApp() {
        document.getElementById("my-name-bar").innerText = user.display_name;
        renderAvatarEl(document.getElementById("my-avatar-mini"), user.display_name, user.avatar_url);

        const protocol = location.protocol === "https:" ? "wss:" : "ws:";
        ws = new WebSocket(`${protocol}//${location.host}/ws/${encodeURIComponent(user.username)}`);

        ws.onmessage = async (e) => {
            const data = JSON.parse(e.data);
            if (data.type === "online_list") {
                onlineUsers = data.users;
                renderDMsList();
                renderFriendsList();
            } else if (data.type === "msg") {
                if (currentTarget === data.sender_username || currentTarget === data.target) {
                    currentMessages.push(data);
                    renderMessages();
                }
            } else if (data.type === "call_offer") {
                handleIncomingCallOffer(data);
            } else if (data.type === "call_answer") {
                if (pc) await pc.setRemoteDescription(new RTCSessionDescription(data.sdp));
            } else if (data.type === "ice_candidate") {
                if (pc && data.candidate) await pc.addIceCandidate(new RTCIceCandidate(data.candidate));
            } else if (data.type === "call_ended") {
                cleanupCall();
            }
        };

        await loadChatsAndChannels();
    }

    async function loadChatsAndChannels() {
        const res = await fetch(`/api/chats?username=${encodeURIComponent(user.username)}`);
        const data = await res.json();
        allUsers = data.chats || [];
        renderDMsList();
        renderFriendsList();
    }

    function renderDMsList() {
        const dmsBox = document.getElementById("dms-list");
        dmsBox.innerHTML = "";
        allUsers.forEach(u => {
            const isOnline = onlineUsers.includes(u.key);
            const div = document.createElement("div");
            div.className = `dm-item ${currentTarget === u.key ? 'active' : ''}`;
            div.onclick = () => openChat(u.key, u.name, u.avatar);

            div.innerHTML = `
                <div class="avatar-wrap">
                    <div class="avatar-img">${u.avatar ? `<img src="${u.avatar}" style="width:100%;height:100%;border-radius:50%;object-fit:cover;">` : u.name[0].toUpperCase()}</div>
                    <div class="status-dot ${isOnline ? 'online' : ''}"></div>
                </div>
                <div style="flex:1; min-width:0; overflow:hidden;">
                    <div style="font-weight:600; font-size:0.9rem; color:#fff; white-space:nowrap; text-overflow:ellipsis; overflow:hidden;">
                        ${u.name} ${u.is_dev ? '<span class="dev-badge">DEV</span>' : ''}
                    </div>
                    <div style="font-size:0.75rem; color:var(--text-muted); white-space:nowrap; text-overflow:ellipsis; overflow:hidden;">
                        ${u.activity || (isOnline ? 'В сети' : 'Не в сети')}
                    </div>
                </div>
            `;
            dmsBox.appendChild(div);
        });
    }

    function renderFriendsList(searchVal = "") {
        const list = document.getElementById("friends-list-items");
        list.innerHTML = "";
        
        let filtered = allUsers.filter(u => {
            if (searchVal) return u.name.toLowerCase().includes(searchVal.toLowerCase()) || u.tag.toLowerCase().includes(searchVal.toLowerCase());
            if (currentFilter === "online") return onlineUsers.includes(u.key);
            if (currentFilter === "blocked") return u.is_blocked;
            return true;
        });

        document.getElementById("friends-count-label").innerText = `${currentFilter.toUpperCase()} — ${filtered.length}`;

        filtered.forEach(f => {
            const isOnline = onlineUsers.includes(f.key);
            const row = document.createElement("div");
            row.className = "friend-row";
            row.innerHTML = `
                <div class="friend-meta">
                    <div class="avatar-wrap large">
                        <div class="avatar-img">${f.avatar ? `<img src="${f.avatar}" style="width:100%;height:100%;border-radius:50%;object-fit:cover;">` : f.name[0].toUpperCase()}</div>
                        <div class="status-dot ${isOnline ? 'online' : ''}"></div>
                    </div>
                    <div>
                        <div style="font-weight:600; color:#fff; font-size:0.95rem;">
                            ${f.name} ${f.is_dev ? '<span class="dev-badge">DEV</span>' : ''}
                            <span style="font-size:0.78rem; color:var(--text-muted); font-weight:normal;">${f.tag}</span>
                        </div>
                        <div style="font-size:0.8rem; color:var(--text-muted); margin-top:2px;">${f.activity}</div>
                    </div>
                </div>
                <div class="friend-actions">
                    <button class="circle-action-btn" title="Написать сообщение" onclick="openChat('${f.key}', '${f.name}', '${f.avatar}')">💬</button>
                    <button class="circle-action-btn" title="Позвонить" onclick="openChat('${f.key}', '${f.name}', '${f.avatar}'); startCall();">📞</button>
                </div>
            `;
            list.appendChild(row);
        });
    }

    function filterFriends(type) {
        currentFilter = type;
        document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
        event.target.classList.add("active");
        renderFriendsList();
    }

    function showFriendsTab() {
        currentTarget = "";
        document.getElementById("friends-view").style.display = "flex";
        document.getElementById("chat-view").style.display = "none";
        document.getElementById("chat-actions").style.display = "none";
        document.getElementById("top-nav-left").innerHTML = `
            <div class="navbar-title">👥 Друзья</div>
            <div style="width:1px; height:16px; background:rgba(255,255,255,0.1);"></div>
            <button class="tab-btn active" onclick="filterFriends('online')">В сети</button>
            <button class="tab-btn" onclick="filterFriends('all')">Все</button>
            <button class="tab-btn" onclick="filterFriends('blocked')">Заблокированные</button>
            <button class="tab-btn add-friend" onclick="document.getElementById('friends-search-input').focus()">Добавить в друзья</button>
        `;
        renderDMsList();
    }

    async function openChat(key, name, avatar) {
        currentTarget = key;
        document.getElementById("friends-view").style.display = "none";
        document.getElementById("chat-view").style.display = "flex";
        document.getElementById("chat-actions").style.display = "flex";

        const isDev = checkIsDev(key);
        document.getElementById("top-nav-left").innerHTML = `
            <div class="navbar-title">
                <span style="color:var(--text-muted);">@</span> ${name}
                ${isDev ? '<span class="dev-badge">DEV</span>' : ''}
            </div>
        `;

        renderDMsList();
        const res = await fetch(`/api/history?user=${encodeURIComponent(user.username)}&target=${encodeURIComponent(key)}`);
        currentMessages = await res.json();
        renderMessages();
    }

    function renderMessages() {
        const box = document.getElementById("messages");
        box.innerHTML = "";
        currentMessages.forEach(m => {
            const row = document.createElement("div");
            row.className = "discord-msg";
            row.innerHTML = `
                <div class="avatar-wrap large">
                    <div class="avatar-img">${m.avatar ? `<img src="${m.avatar}" style="width:100%;height:100%;border-radius:50%;object-fit:cover;">` : m.sender_name[0].toUpperCase()}</div>
                </div>
                <div class="msg-content-wrap">
                    <div class="msg-author-row">
                        <span class="msg-author">${m.sender_name}</span>
                        ${m.is_dev ? '<span class="dev-badge">DEV</span>' : ''}
                        <span class="msg-time">${m.time}</span>
                    </div>
                    <div class="msg-body-text">${escapeHtml(m.text)}</div>
                </div>
            `;
            box.appendChild(row);
        });
        box.scrollTop = box.scrollHeight;
    }

    function sendMsg() {
        const input = document.getElementById("msg-input");
        const text = input.value.trim();
        if (!text || !ws || !currentTarget) return;

        const timeStr = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        const payload = {
            sender_username: user.username,
            sender_name: user.display_name,
            target: currentTarget,
            text: text,
            avatar: user.avatar_url || "",
            time: timeStr
        };

        currentMessages.push(payload);
        renderMessages();
        ws.send(JSON.stringify(payload));
        input.value = "";
    }

    /* 📞 WEBRTC ГОЛОСОВЫЕ ЗВОНКИ 1-НА-1 */
    async function startCall() {
        if (!currentTarget) return;
        document.getElementById("call-overlay").style.display = "flex";
        document.getElementById("call-peer-name").innerText = currentTarget;
        document.getElementById("call-timer").innerText = "Подключение...";

        localStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
        pc = new RTCPeerConnection({ iceServers: [{ urls: "stun:stun.l.google.com:19302" }] });

        localStream.getTracks().forEach(track => pc.addTrack(track, localStream));
        pc.ontrack = e => {
            document.getElementById("remoteAudio").srcObject = e.streams[0];
            startCallTimer();
        };
        pc.onicecandidate = e => {
            if (e.candidate) ws.send(JSON.stringify({ type: "ice_candidate", target: currentTarget, candidate: e.candidate }));
        };

        const offer = await pc.createOffer();
        await pc.setLocalDescription(offer);

        ws.send(JSON.stringify({
            type: "call_offer",
            target: currentTarget,
            caller_name: user.display_name,
            caller_avatar: user.avatar_url,
            sdp: offer
        }));
    }

    function handleIncomingCallOffer(data) {
        incomingCaller = data;
        document.getElementById("caller-name").innerText = data.caller_name;
        renderAvatarEl(document.getElementById("caller-avatar"), data.caller_name, data.caller_avatar);
        document.getElementById("incoming-call-modal").style.display = "flex";
    }

    async function acceptIncomingCall() {
        document.getElementById("incoming-call-modal").style.display = "none";
        document.getElementById("call-overlay").style.display = "flex";
        document.getElementById("call-peer-name").innerText = incomingCaller.sender;

        localStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
        pc = new RTCPeerConnection({ iceServers: [{ urls: "stun:stun.l.google.com:19302" }] });

        localStream.getTracks().forEach(track => pc.addTrack(track, localStream));
        pc.ontrack = e => {
            document.getElementById("remoteAudio").srcObject = e.streams[0];
            startCallTimer();
        };
        pc.onicecandidate = e => {
            if (e.candidate) ws.send(JSON.stringify({ type: "ice_candidate", target: incomingCaller.sender, candidate: e.candidate }));
        };

        await pc.setRemoteDescription(new RTCSessionDescription(incomingCaller.sdp));
        const answer = await pc.createAnswer();
        await pc.setLocalDescription(answer);

        ws.send(JSON.stringify({
            type: "call_answer",
            target: incomingCaller.sender,
            sdp: answer
        }));
    }

    function declineIncomingCall() {
        document.getElementById("incoming-call-modal").style.display = "none";
        if (incomingCaller) {
            ws.send(JSON.stringify({ type: "call_ended", target: incomingCaller.sender }));
            incomingCaller = null;
        }
    }

    function endCall() {
        if (currentTarget) ws.send(JSON.stringify({ type: "call_ended", target: currentTarget }));
        cleanupCall();
    }

    function cleanupCall() {
        if (pc) { pc.close(); pc = null; }
        if (localStream) { localStream.getTracks().forEach(t => t.stop()); localStream = null; }
        clearInterval(callTimerInterval);
        callSeconds = 0;
        document.getElementById("call-overlay").style.display = "none";
        document.getElementById("incoming-call-modal").style.display = "none";
    }

    function startCallTimer() {
        clearInterval(callTimerInterval);
        callSeconds = 0;
        callTimerInterval = setInterval(() => {
            callSeconds++;
            const m = String(Math.floor(callSeconds / 60)).padStart(2, '0');
            const s = String(callSeconds % 60).padStart(2, '0');
            document.getElementById("call-timer").innerText = `${m}:${s}`;
        }, 1000);
    }

    function toggleCallMute() {
        if (localStream) {
            const track = localStream.getAudioTracks()[0];
            track.enabled = !track.enabled;
            document.getElementById("call-mute-btn").style.background = track.enabled ? "var(--bg-sidebar)" : "var(--red)";
        }
    }

    function toggleMuteMic() {
        const btn = document.getElementById("btn-mute");
        btn.classList.toggle("muted");
    }

    function renderAvatarEl(el, name, avatarUrl) {
        if (avatarUrl) {
            el.innerHTML = `<img src="${avatarUrl}" style="width:100%; height:100%; border-radius:50%; object-fit:cover;">`;
        } else {
            el.innerHTML = (name || "?")[0].toUpperCase();
        }
    }

    function openProfile() {
        document.getElementById("profile-name").innerText = user.display_name;
        document.getElementById("profile-tag").innerText = "@" + user.username;
        renderAvatarEl(document.getElementById("profile-avatar-preview"), user.display_name, user.avatar_url);
        document.getElementById("profile-modal").style.display = "flex";
    }

    async function uploadUserAvatar() {
        const file = document.getElementById("profile-file").files[0];
        if (!file) return;
        const form = new FormData();
        form.append("username", user.username);
        form.append("file", file);
        const res = await fetch("/api/upload_avatar", { method: "POST", body: form });
        const data = await res.json();
        if (data.status === "ok") {
            user.avatar_url = data.avatar_url;
            localStorage.setItem("messenger_user", JSON.stringify(user));
            location.reload();
        }
    }

    function escapeHtml(str) {
        return (str || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }
</script>
</body>
</html>
"""

@app.get("/")
async def get_client():
    return HTMLResponse(HTML_TEMPLATE)

@app.websocket("/ws/{username}")
async def ws_endpoint(websocket: WebSocket, username: str):
    await manager.connect(username, websocket)
    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            msg_type = data.get("type")
            target = data.get("target")

            # Сигналинг для WebRTC-звонков
            if msg_type in ["call_offer", "call_answer", "ice_candidate", "call_ended"]:
                data["sender"] = username
                await manager.send_to_user(data, target)
                continue

            # Сообщения чата
            text = data.get("text", "")
            time_str = datetime.now().strftime("%H:%M")

            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    """INSERT INTO messages (sender_username, sender_name, target, text, timestamp, avatar_url) 
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (username, data.get("sender_name", username), target, text, time_str, data.get("avatar", ""))
                )
                await db.commit()

            msg_out = {
                "type": "msg",
                "sender_username": username,
                "sender_name": data.get("sender_name", username),
                "target": target,
                "text": text,
                "time": time_str,
                "avatar": data.get("avatar", ""),
                "is_dev": is_dev(username)
            }

            await manager.send_to_user(msg_out, recipient_username=target)

    except WebSocketDisconnect:
        await manager.disconnect(username)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=80)
