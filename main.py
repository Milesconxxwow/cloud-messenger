import json
import os
import shutil
import uuid
import hashlib
import aiosqlite
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

app = FastAPI(title="Cloud Messenger Pro")

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

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # Таблица пользователей
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                username TEXT UNIQUE NOT NULL,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                avatar_url TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)
        # Таблица каналов
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
        # Таблица сообщений
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_username TEXT NOT NULL,
                sender_name TEXT NOT NULL,
                target TEXT NOT NULL,
                text TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                avatar_url TEXT DEFAULT ''
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
        for conn in self.active_connections.values():
            try:
                await conn.send_text(payload)
            except Exception:
                pass

    async def send_to_user(self, message: dict, recipient_username: str):
        if recipient_username in self.active_connections:
            await self.active_connections[recipient_username].send_text(json.dumps(message))

    async def broadcast(self, message: dict, sender_username: str = None):
        for uname, conn in self.active_connections.items():
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
    login: str  # email или @username
    password: str

@app.post("/api/register")
async def register(data: RegisterModel):
    email = data.email.strip().lower()
    uname = data.username.strip().lstrip("@").lower()
    name = data.display_name.strip() or uname
    pwd = data.password.strip()

    if not email or not uname or not pwd:
        return {"status": "error", "message": "Заполните все обязательные поля"}
    if len(uname) < 3:
        return {"status": "error", "message": "Юзернейм должен быть от 3 символов"}
    if len(pwd) < 4:
        return {"status": "error", "message": "Пароль должен быть от 4 символов"}

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
            "avatar_url": ""
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
                "avatar_url": user[3] or ""
            }
        return {"status": "error", "message": "Неверный логин или пароль"}

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
    clean_tag = tag.strip().lstrip("@").lower()
    clean_name = name.strip()
    if not clean_tag or not clean_name:
        return {"status": "error", "message": "Укажите название и тег канала"}

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

@app.get("/api/search")
async def search(q: str):
    query = f"%{q.strip().lstrip('@').lower()}%"
    async with aiosqlite.connect(DB_PATH) as db:
        # Поиск пользователей
        cur_u = await db.execute(
            "SELECT username, display_name, avatar_url FROM users WHERE username LIKE ? OR display_name LIKE ? LIMIT 10",
            (query, query)
        )
        users = [{"type": "user", "tag": f"@{r[0]}", "name": r[1], "avatar": r[2]} for r in await cur_u.fetchall()]

        # Поиск каналов
        cur_c = await db.execute(
            "SELECT channel_tag, name, avatar_url, description FROM channels WHERE channel_tag LIKE ? OR name LIKE ? LIMIT 10",
            (query, query)
        )
        channels = [{"type": "channel", "tag": f"#{r[0]}", "name": r[1], "avatar": r[2], "desc": r[3]} for r in await cur_c.fetchall()]

        return {"results": users + channels}

@app.get("/api/history")
async def get_history(user: str, target: str):
    async with aiosqlite.connect(DB_PATH) as db:
        if target.startswith("#") or target == "Общий чат":
            cur = await db.execute(
                """SELECT sender_username, sender_name, target, text, timestamp, avatar_url 
                   FROM messages WHERE target = ? ORDER BY id ASC LIMIT 200""",
                (target,)
            )
        else:
            cur = await db.execute(
                """SELECT sender_username, sender_name, target, text, timestamp, avatar_url 
                   FROM messages 
                   WHERE (sender_username = ? AND target = ?) OR (sender_username = ? AND target = ?) 
                   ORDER BY id ASC LIMIT 200""",
                (user, target, target, user)
            )
        rows = await cur.fetchall()
        return [{
            "sender_username": r[0],
            "sender_name": r[1],
            "target": r[2],
            "text": r[3],
            "time": r[4],
            "avatar": r[5]
        } for r in rows]

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Cloud Messenger Pro</title>
    <style>
        :root {
            --bg-main: #0e1015;
            --bg-sidebar: #13151b;
            --bg-sidebar-hover: #1c1f28;
            --bg-chat: #0e1015;
            --bg-header: #13151b;
            --bg-input: #181b22;
            --bubble-in: #1f222c;
            --bubble-out: #2b77f7;
            --text-main: #ffffff;
            --text-sub: #7a8293;
            --badge-blue: #2b77f7;
            --online-green: #22c55e;
            --border-color: #1c202a;
            --danger-red: #ef4444;
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            -webkit-tap-highlight-color: transparent;
        }

        body, html {
            height: 100%;
            background-color: var(--bg-main);
            color: var(--text-main);
            overflow: hidden;
        }

        /* Модальные окна */
        .modal-overlay {
            position: fixed;
            inset: 0;
            background: rgba(8, 10, 14, 0.92);
            backdrop-filter: blur(8px);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 1000;
        }
        .card-modal {
            background: #161821;
            padding: 28px 24px;
            border-radius: 18px;
            width: 90%;
            max-width: 400px;
            border: 1px solid #232734;
            box-shadow: 0 20px 40px rgba(0,0,0,0.6);
        }
        .card-modal h2 { font-size: 1.35rem; font-weight: 700; margin-bottom: 6px; text-align: center; }
        .card-modal p.subtitle { font-size: 0.85rem; color: var(--text-sub); margin-bottom: 18px; text-align: center; }
        
        .card-modal input, .card-modal textarea {
            width: 100%;
            padding: 12px 14px;
            margin-bottom: 12px;
            background: var(--bg-input);
            border: 1px solid #262b3a;
            color: #fff;
            border-radius: 12px;
            outline: none;
            font-size: 0.95rem;
        }
        .card-modal input:focus { border-color: var(--badge-blue); }
        
        .btn-primary {
            width: 100%;
            padding: 13px;
            background: var(--badge-blue);
            color: white;
            border: none;
            border-radius: 12px;
            font-weight: 600;
            font-size: 0.95rem;
            cursor: pointer;
            margin-top: 6px;
        }
        .btn-cancel {
            background: transparent;
            color: var(--text-sub);
            border: none;
            width: 100%;
            padding: 10px;
            cursor: pointer;
            font-size: 0.9rem;
            margin-top: 6px;
        }

        /* Контейнер */
        #app-container {
            display: flex;
            height: 100%;
            width: 100%;
        }

        /* Боковая панель */
        #sidebar {
            width: 380px;
            background: var(--bg-sidebar);
            border-right: 1px solid var(--border-color);
            display: flex;
            flex-direction: column;
            height: 100%;
            flex-shrink: 0;
        }

        .sidebar-header {
            padding: 16px 18px 12px 18px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .user-profile-badge {
            display: flex;
            align-items: center;
            gap: 10px;
            cursor: pointer;
            padding: 4px 8px;
            border-radius: 10px;
            transition: background 0.2s;
        }
        .user-profile-badge:hover { background: #1c202a; }
        .avatar-small {
            width: 36px;
            height: 36px;
            border-radius: 50%;
            object-fit: cover;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: bold;
            font-size: 0.9rem;
            color: #fff;
            flex-shrink: 0;
        }

        .sidebar-actions {
            display: flex;
            gap: 8px;
        }
        .icon-btn {
            background: var(--bg-input);
            border: 1px solid #232734;
            color: #fff;
            width: 36px;
            height: 36px;
            border-radius: 10px;
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            font-size: 1.1rem;
            transition: 0.2s;
        }
        .icon-btn:hover { background: #262b3a; }

        /* Поиск */
        .search-container {
            padding: 8px 16px 14px 16px;
            position: relative;
            border-bottom: 1px solid var(--border-color);
        }
        .search-input {
            width: 100%;
            padding: 10px 14px 10px 36px;
            background: var(--bg-input);
            border: 1px solid #232734;
            border-radius: 12px;
            color: #fff;
            outline: none;
            font-size: 0.9rem;
        }
        .search-icon {
            position: absolute;
            left: 28px;
            top: 19px;
            font-size: 0.85rem;
            color: var(--text-sub);
        }

        /* Список чатов */
        .chat-list {
            flex: 1;
            overflow-y: auto;
        }
        .chat-item {
            display: flex;
            align-items: center;
            padding: 12px 18px;
            cursor: pointer;
            transition: background 0.15s;
        }
        .chat-item:hover { background: var(--bg-sidebar-hover); }
        .chat-item.active { background: #1c202c; }

        .avatar-wrap {
            position: relative;
            margin-right: 14px;
            flex-shrink: 0;
        }
        .avatar-img {
            width: 48px;
            height: 48px;
            border-radius: 50%;
            object-fit: cover;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 700;
            font-size: 1.15rem;
            color: #fff;
            user-select: none;
        }
        .online-dot {
            position: absolute;
            bottom: 2px;
            right: 2px;
            width: 12px;
            height: 12px;
            border-radius: 50%;
            background: var(--online-green);
            border: 2px solid var(--bg-sidebar);
            display: none;
        }
        .online-dot.visible { display: block; }

        .chat-details {
            flex: 1;
            min-width: 0;
        }
        .chat-top-row {
            display: flex;
            justify-content: space-between;
            align-items: baseline;
            margin-bottom: 3px;
        }
        .chat-name {
            font-weight: 600;
            font-size: 0.96rem;
            color: #ffffff;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .chat-time {
            font-size: 0.72rem;
            color: var(--text-sub);
            margin-left: 6px;
        }
        .chat-bottom-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .chat-preview {
            font-size: 0.82rem;
            color: var(--text-sub);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        /* Область переписки */
        #chat-area {
            flex: 1;
            display: flex;
            flex-direction: column;
            background: var(--bg-chat);
            height: 100%;
        }

        .chat-header {
            height: 68px;
            background: var(--bg-header);
            display: flex;
            align-items: center;
            padding: 0 20px;
            border-bottom: 1px solid var(--border-color);
            flex-shrink: 0;
        }
        .back-btn {
            display: none;
            background: none;
            border: none;
            color: #fff;
            font-size: 1.4rem;
            margin-right: 14px;
            cursor: pointer;
        }

        .messages-container {
            flex: 1;
            padding: 24px;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            gap: 10px;
        }
        .msg-row {
            display: flex;
            align-items: flex-end;
            gap: 8px;
            width: 100%;
        }
        .msg-row.mine { justify-content: flex-end; }

        .msg-avatar {
            width: 28px;
            height: 28px;
            border-radius: 50%;
            object-fit: cover;
            flex-shrink: 0;
        }

        .bubble {
            max-width: 65%;
            padding: 10px 14px;
            border-radius: 14px;
            font-size: 0.92rem;
            line-height: 1.45;
            word-break: break-word;
        }
        .msg-row.theirs .bubble {
            background: var(--bubble-in);
            color: #f1f2f6;
            border-bottom-left-radius: 4px;
        }
        .msg-row.mine .bubble {
            background: var(--bubble-out);
            color: #ffffff;
            border-bottom-right-radius: 4px;
        }

        .bubble-header {
            font-size: 0.76rem;
            font-weight: 600;
            color: #60a5fa;
            margin-bottom: 3px;
        }
        .bubble-footer {
            display: flex;
            justify-content: flex-end;
            margin-top: 4px;
        }
        .bubble-time {
            font-size: 0.68rem;
            color: rgba(255, 255, 255, 0.55);
        }

        .input-bar {
            padding: 14px 20px;
            background: var(--bg-header);
            border-top: 1px solid var(--border-color);
            display: flex;
            align-items: center;
            gap: 12px;
        }
        .input-wrapper {
            flex: 1;
            background: var(--bg-input);
            border: 1px solid #232734;
            border-radius: 12px;
            padding: 0 14px;
            display: flex;
            align-items: center;
        }
        .input-wrapper input {
            flex: 1;
            background: transparent;
            border: none;
            color: #fff;
            padding: 14px 0;
            font-size: 0.95rem;
            outline: none;
        }
        .send-btn {
            background: var(--badge-blue);
            border: none;
            color: white;
            width: 44px;
            height: 44px;
            border-radius: 12px;
            cursor: pointer;
            font-size: 1.1rem;
        }

        @media (max-width: 768px) {
            #sidebar { width: 100%; }
            #chat-area { display: none; }
            body.in-chat #sidebar { display: none; }
            body.in-chat #chat-area { display: flex; }
            .back-btn { display: block; }
            .bubble { max-width: 82%; }
        }
    </style>
</head>
<body>

<!-- Модалка Авторизации -->
<div id="auth-modal" class="modal-overlay">
    <div class="card-modal">
        <h2 id="auth-title">Вход</h2>
        <p class="subtitle" id="auth-sub">Войдите в свой аккаунт</p>
        
        <input type="text" id="auth-login" placeholder="Email или @юзернейм">
        <input type="text" id="auth-name" placeholder="Отображаемое имя (например, Дима)" style="display:none;">
        <input type="text" id="auth-username" placeholder="Уникальный @юзернейм" style="display:none;">
        <input type="password" id="auth-pwd" placeholder="Пароль">
        
        <button class="btn-primary" onclick="submitAuth()" id="auth-btn">Войти</button>
        <p class="subtitle" style="margin-top: 15px; cursor: pointer; color: var(--badge-blue);" id="auth-toggle" onclick="toggleAuth()">Нет аккаунта? Создать</p>
    </div>
</div>

<!-- Модалка Создания Канала -->
<div id="channel-modal" class="modal-overlay" style="display: none;">
    <div class="card-modal">
        <h2>Создать канал</h2>
        <p class="subtitle">Публичное пространство для постов и общения</p>
        
        <input type="text" id="chan-name" placeholder="Название канала">
        <input type="text" id="chan-tag" placeholder="Тег (например, news, dev)">
        <textarea id="chan-desc" rows="2" placeholder="Описание канала..."></textarea>
        <label style="font-size: 0.8rem; color: var(--text-sub); display: block; margin-bottom: 6px;">Аватарка канала:</label>
        <input type="file" id="chan-file" accept="image/*">

        <button class="btn-primary" onclick="createChannelSubmit()">Создать</button>
        <button class="btn-cancel" onclick="document.getElementById('channel-modal').style.display='none'">Отмена</button>
    </div>
</div>

<!-- Модалка Профиля / Смены Аватарки -->
<div id="profile-modal" class="modal-overlay" style="display: none;">
    <div class="card-modal" style="text-align: center;">
        <h2>Мой профиль</h2>
        <div id="profile-avatar-preview" style="width: 80px; height: 80px; border-radius: 50%; margin: 15px auto; display:flex; align-items:center; justify-content:center; font-size:2rem; font-weight:bold; color:#fff; overflow:hidden;"></div>
        <h3 id="profile-name" style="margin-bottom: 2px;"></h3>
        <p id="profile-tag" style="color: var(--badge-blue); font-size: 0.9rem; margin-bottom: 15px;"></p>
        
        <label style="font-size: 0.85rem; color: var(--text-sub); display: block; margin-bottom: 8px;">Сменить аватарку:</label>
        <input type="file" id="profile-file" accept="image/*">
        
        <button class="btn-primary" onclick="uploadUserAvatar()">Сохранить фото</button>
        <button class="btn-cancel" onclick="logout()" style="color: var(--danger-red); margin-top: 8px;">Выйти из аккаунта 🚪</button>
        <button class="btn-cancel" onclick="document.getElementById('profile-modal').style.display='none'">Закрыть</button>
    </div>
</div>

<div id="app-container">
    <div id="sidebar">
        <div class="sidebar-header">
            <div class="user-profile-badge" onclick="openProfile()">
                <div class="avatar-small" id="my-avatar-mini">?</div>
                <div>
                    <div id="my-display-name" style="font-weight:600; font-size:0.92rem;">Загрузка...</div>
                    <div id="my-tag" style="font-size:0.75rem; color:var(--text-sub);">@...</div>
                </div>
            </div>
            <div class="sidebar-actions">
                <button class="icon-btn" title="Создать канал" onclick="document.getElementById('channel-modal').style.display='flex'">➕</button>
            </div>
        </div>

        <div class="search-container">
            <span class="search-icon">🔍</span>
            <input type="text" class="search-input" id="search-input" placeholder="Поиск @юзеров и #каналов..." oninput="handleSearch(this.value)">
        </div>

        <div class="chat-list" id="chat-list"></div>
    </div>

    <div id="chat-area">
        <div class="chat-header">
            <button class="back-btn" onclick="document.body.classList.remove('in-chat')">←</button>
            <div class="avatar-small" id="header-avatar" style="width:42px; height:42px; font-size:1.1rem; margin-right:12px;">?</div>
            <div>
                <div id="header-title" style="font-weight:600; font-size:1rem;">Выберите чат</div>
                <div id="header-sub" style="font-size:0.78rem; color:var(--badge-blue);">онлайн</div>
            </div>
        </div>

        <div class="messages-container" id="messages"></div>

        <div class="input-bar">
            <div class="input-wrapper">
                <input type="text" id="msg-input" placeholder="Напишите сообщение..." onkeydown="if(event.key==='Enter') sendMsg()">
            </div>
            <button class="send-btn" onclick="sendMsg()">➤</button>
        </div>
    </div>
</div>

<script>
    let user = JSON.parse(localStorage.getItem("messenger_user") || "null");
    let isRegister = false;
    let currentTarget = "Общий чат";
    let ws = null;
    let activeChats = ["Общий чат"];
    let chatsMeta = { "Общий чат": { name: "Общий чат", tag: "Общий чат", avatar: "", type: "channel" } };
    let onlineUsers = [];

    const gradients = [
        "linear-gradient(135deg, #f59e0b, #d97706)",
        "linear-gradient(135deg, #3b82f6, #1d4ed8)",
        "linear-gradient(135deg, #10b981, #047857)",
        "linear-gradient(135deg, #8b5cf6, #6d28d9)",
        "linear-gradient(135deg, #ec4899, #be185d)"
    ];

    function getGradient(str) {
        let hash = 0;
        for (let i = 0; i < str.length; i++) hash += str.charCodeAt(i);
        return gradients[hash % gradients.length];
    }

    function renderAvatarEl(el, name, avatarUrl) {
        if (avatarUrl) {
            el.innerHTML = `<img src="${avatarUrl}" class="msg-avatar" style="width:100%; height:100%; border-radius:50%; object-fit:cover;">`;
            el.style.background = "transparent";
        } else {
            el.innerHTML = (name || "?")[0].toUpperCase();
            el.style.background = getGradient(name || "default");
        }
    }

    window.onload = () => {
        if (user) {
            document.getElementById("auth-modal").style.display = "none";
            startApp();
        }
    };

    function toggleAuth() {
        isRegister = !isRegister;
        document.getElementById("auth-name").style.display = isRegister ? "block" : "none";
        document.getElementById("auth-username").style.display = isRegister ? "block" : "none";
        document.getElementById("auth-title").innerText = isRegister ? "Регистрация" : "Вход";
        document.getElementById("auth-btn").innerText = isRegister ? "Создать аккаунт" : "Войти";
        document.getElementById("auth-toggle").innerText = isRegister ? "Уже есть аккаунт? Войти" : "Нет аккаунта? Создать";
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

    function startApp() {
        document.getElementById("my-display-name").innerText = user.display_name;
        document.getElementById("my-tag").innerText = "@" + user.username;
        renderAvatarEl(document.getElementById("my-avatar-mini"), user.display_name, user.avatar_url);

        const protocol = location.protocol === "https:" ? "wss:" : "ws:";
        ws = new WebSocket(`${protocol}//${location.host}/ws/${encodeURIComponent(user.username)}`);

        ws.onmessage = (e) => {
            const data = JSON.parse(e.data);
            if (data.type === "online_list") {
                onlineUsers = data.users;
                renderSidebar();
            } else if (data.type === "msg") {
                const targetKey = data.target.startsWith("#") || data.target === "Общий чат" ? data.target : data.sender_username;
                if (!activeChats.includes(targetKey)) {
                    activeChats.push(targetKey);
                    chatsMeta[targetKey] = { name: data.sender_name, tag: targetKey, avatar: data.avatar, type: "user" };
                }
                if (currentTarget === targetKey || (targetKey === data.sender_username && currentTarget === data.sender_username)) {
                    appendMessage(data, false);
                }
                renderSidebar();
            }
        };

        selectChat("Общий чат");
    }

    function openProfile() {
        document.getElementById("profile-name").innerText = user.display_name;
        document.getElementById("profile-tag").innerText = "@" + user.username;
        renderAvatarEl(document.getElementById("profile-avatar-preview"), user.display_name, user.avatar_url);
        document.getElementById("profile-modal").style.display = "flex";
    }

    async function uploadUserAvatar() {
        const fileInput = document.getElementById("profile-file");
        if (!fileInput.files[0]) return alert("Выберите файл изображения");

        const form = new FormData();
        form.append("username", user.username);
        form.append("file", fileInput.files[0]);

        const res = await fetch("/api/upload_avatar", { method: "POST", body: form });
        const data = await res.json();
        if (data.status === "ok") {
            user.avatar_url = data.avatar_url;
            localStorage.setItem("messenger_user", JSON.stringify(user));
            location.reload();
        }
    }

    async function createChannelSubmit() {
        const name = document.getElementById("chan-name").value;
        const tag = document.getElementById("chan-tag").value;
        const desc = document.getElementById("chan-desc").value;
        const fileInput = document.getElementById("chan-file");

        const form = new FormData();
        form.append("name", name);
        form.append("tag", tag);
        form.append("desc", desc);
        form.append("creator", user.username);
        if (fileInput.files[0]) form.append("file", fileInput.files[0]);

        const res = await fetch("/api/create_channel", { method: "POST", body: form });
        const data = await res.json();
        if (data.status === "ok") {
            document.getElementById("channel-modal").style.display = "none";
            const fullTag = "#" + data.channel_tag;
            activeChats.unshift(fullTag);
            chatsMeta[fullTag] = { name: data.name, tag: fullTag, avatar: data.avatar_url, type: "channel" };
            selectChat(fullTag);
        } else alert(data.message);
    }

    let searchTimeout = null;
    function handleSearch(val) {
        clearTimeout(searchTimeout);
        if (!val.trim()) {
            renderSidebar();
            return;
        }
        searchTimeout = setTimeout(async () => {
            const res = await fetch(`/api/search?q=${encodeURIComponent(val)}`);
            const data = await res.json();
            renderSearchResults(data.results);
        }, 250);
    }

    function renderSearchResults(results) {
        const list = document.getElementById("chat-list");
        list.innerHTML = `<div style="padding:10px 18px; font-size:0.75rem; color:var(--text-sub);">РЕЗУЛЬТАТЫ ПОИСКА</div>`;
        results.forEach(item => {
            const div = document.createElement("div");
            div.className = "chat-item";
            div.onclick = () => {
                const targetKey = item.type === "user" ? item.tag.replace("@", "") : item.tag;
                if (!activeChats.includes(targetKey)) {
                    activeChats.unshift(targetKey);
                    chatsMeta[targetKey] = { name: item.name, tag: item.tag, avatar: item.avatar, type: item.type };
                }
                selectChat(targetKey);
                document.getElementById("search-input").value = "";
            };
            div.innerHTML = `
                <div class="avatar-wrap">
                    <div class="avatar-img" style="background:${getGradient(item.name)}">${item.avatar ? `<img src="${item.avatar}" style="width:100%;height:100%;border-radius:50%;object-fit:cover;">` : item.name[0].toUpperCase()}</div>
                </div>
                <div class="chat-details">
                    <div class="chat-name">${item.name} <span style="font-size:0.8rem; color:var(--badge-blue); font-weight:normal;">${item.tag}</span></div>
                    <div class="chat-preview">${item.desc || (item.type === 'user' ? 'Пользователь' : 'Канал')}</div>
                </div>
            `;
            list.appendChild(div);
        });
    }

    function renderSidebar() {
        const list = document.getElementById("chat-list");
        list.innerHTML = "";
        activeChats.forEach(key => {
            const meta = chatsMeta[key] || { name: key, tag: key, avatar: "" };
            const isOnline = onlineUsers.includes(key);

            const div = document.createElement("div");
            div.className = `chat-item ${currentTarget === key ? 'active' : ''}`;
            div.onclick = () => selectChat(key);

            div.innerHTML = `
                <div class="avatar-wrap">
                    <div class="avatar-img" style="background:${getGradient(meta.name)}">${meta.avatar ? `<img src="${meta.avatar}" style="width:100%;height:100%;border-radius:50%;object-fit:cover;">` : meta.name[0].toUpperCase()}</div>
                    <div class="online-dot ${isOnline ? 'visible' : ''}"></div>
                </div>
                <div class="chat-details">
                    <div class="chat-top-row">
                        <div class="chat-name">${meta.name}</div>
                    </div>
                    <div class="chat-preview">${key.startsWith('#') ? 'Канал' : (key === 'Общий чат' ? 'Публичный чат' : '@' + key)}</div>
                </div>
            `;
            list.appendChild(div);
        });
    }

    async function selectChat(key) {
        currentTarget = key;
        const meta = chatsMeta[key] || { name: key, tag: key, avatar: "" };
        
        document.getElementById("header-title").innerText = meta.name;
        document.getElementById("header-sub").innerText = key.startsWith("#") ? "Канал" : (key === "Общий чат" ? "Общая комната" : "@" + key);
        renderAvatarEl(document.getElementById("header-avatar"), meta.name, meta.avatar);

        document.body.classList.add("in-chat");
        renderSidebar();

        const res = await fetch(`/api/history?user=${encodeURIComponent(user.username)}&target=${encodeURIComponent(key)}`);
        const history = await res.json();
        
        const box = document.getElementById("messages");
        box.innerHTML = "";
        history.forEach(m => appendMessage(m, m.sender_username === user.username));
    }

    function appendMessage(m, isMine) {
        const box = document.getElementById("messages");
        const row = document.createElement("div");
        row.className = `msg-row ${isMine ? 'mine' : 'theirs'}`;

        row.innerHTML = `
            ${!isMine && m.avatar ? `<img src="${m.avatar}" class="msg-avatar">` : ''}
            <div class="bubble">
                ${!isMine ? `<div class="bubble-header">${m.sender_name} <span style="font-weight:normal; opacity:0.7;">@${m.sender_username}</span></div>` : ''}
                <div>${escapeHtml(m.text)}</div>
                <div class="bubble-footer"><span class="bubble-time">${m.time}</span></div>
            </div>
        `;
        box.appendChild(row);
        box.scrollTop = box.scrollHeight;
    }

    function sendMsg() {
        const input = document.getElementById("msg-input");
        const text = input.value.trim();
        if (!text || !ws) return;

        const timeStr = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        const payload = {
            sender_username: user.username,
            sender_name: user.display_name,
            target: currentTarget,
            text: text,
            avatar: user.avatar_url || "",
            time: timeStr
        };

        appendMessage(payload, true);
        ws.send(JSON.stringify(payload));
        input.value = "";
    }

    function escapeHtml(str) {
        return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
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
            target = data.get("target")
            text = data.get("text")
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
                "avatar": data.get("avatar", "")
            }

            if target == "Общий чат" or target.startswith("#"):
                await manager.broadcast(msg_out, sender_username=username)
            else:
                await manager.send_to_user(msg_out, recipient_username=target)

    except WebSocketDisconnect:
        await manager.disconnect(username)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=80)
