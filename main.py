import json
import os
import shutil
import uuid
import hashlib
import aiosqlite
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

app = FastAPI(title="Cipher Messenger Pro")

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
                pinned_msg_id INTEGER DEFAULT 0,
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

@app.get("/api/user_info")
async def get_user_info(username: str, current_user: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT username, display_name, email, avatar_url, created_at FROM users WHERE username = ?",
            (username.strip().lstrip("@"),)
        )
        row = await cur.fetchone()
        if not row:
            return {"status": "not_found"}
        
        cur_blk = await db.execute(
            "SELECT id FROM blocks WHERE blocker = ? AND blocked = ?",
            (current_user, row[0])
        )
        is_blocked = bool(await cur_blk.fetchone())

        return {
            "status": "ok",
            "username": row[0],
            "display_name": row[1],
            "email": row[2],
            "avatar_url": row[3] or "",
            "created_at": row[4],
            "is_dev": is_dev(row[0]),
            "is_blocked": is_blocked
        }

@app.post("/api/toggle_block")
async def toggle_block(data: BlockModel):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id FROM blocks WHERE blocker = ? AND blocked = ?",
            (data.blocker, data.blocked)
        )
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

        cur_blk = await db.execute("SELECT blocked FROM blocks WHERE blocker = ?", (username,))
        blocked_list = [r[0] for r in await cur_blk.fetchall()]

        # Каналы
        cur_ch = await db.execute("SELECT channel_tag, name, avatar_url, pinned_msg_id FROM channels")
        for ch in await cur_ch.fetchall():
            tag_key = f"#{ch[0]}"
            cur_last = await db.execute(
                "SELECT text, msg_type, timestamp FROM messages WHERE target = ? AND is_deleted = 0 ORDER BY id DESC LIMIT 1",
                (tag_key,)
            )
            last = await cur_last.fetchone()
            last_text = "Канал создан"
            if last:
                last_text = "🎙️ Голосовое" if last[1] == "voice" else ("📷 Фотография" if last[1] == "image" else (last[0] or "📎 Файл"))

            chats.append({
                "key": tag_key,
                "name": ch[1],
                "tag": tag_key,
                "type": "channel",
                "avatar": ch[2] or "",
                "last_msg": last_text,
                "time": last[2] if last else "",
                "pinned_id": ch[3] or 0,
                "is_dev": False,
                "is_blocked": False
            })

        # Приватные диалоги
        cur_peers = await db.execute("""
            SELECT DISTINCT 
                CASE WHEN sender_username = ? THEN target ELSE sender_username END AS peer
            FROM messages 
            WHERE (sender_username = ? OR target = ?) 
              AND target NOT LIKE '#%'
        """, (username, username, username))
        
        peers = [r[0] for r in await cur_peers.fetchall() if r[0] != username and not r[0].startswith("#")]

        for p in peers:
            cur_u = await db.execute("SELECT display_name, avatar_url FROM users WHERE username = ?", (p,))
            u_data = await cur_u.fetchone()
            name = u_data[0] if u_data else p
            avatar = u_data[1] if u_data else ""
            
            cur_last = await db.execute("""
                SELECT text, msg_type, timestamp FROM messages 
                WHERE ((sender_username = ? AND target = ?) OR (sender_username = ? AND target = ?)) AND is_deleted = 0
                ORDER BY id DESC LIMIT 1
            """, (username, p, p, username))
            last = await cur_last.fetchone()
            last_text = ""
            if last:
                last_text = "🎙️ Голосовое" if last[1] == "voice" else ("📷 Фотография" if last[1] == "image" else (last[0] or "📎 Файл"))

            chats.append({
                "key": p,
                "name": name,
                "tag": f"@{p}",
                "type": "user",
                "avatar": avatar,
                "last_msg": last_text,
                "time": last[2] if last else "",
                "pinned_id": 0,
                "is_dev": is_dev(p),
                "is_blocked": p in blocked_list
            })

        return {"chats": chats}

@app.get("/api/search")
async def search(q: str):
    query = f"%{q.strip().lstrip('@').lstrip('#').lower()}%"
    async with aiosqlite.connect(DB_PATH) as db:
        cur_u = await db.execute(
            "SELECT username, display_name, avatar_url FROM users WHERE username LIKE ? OR display_name LIKE ? LIMIT 10",
            (query, query)
        )
        users = [{
            "type": "user", 
            "key": r[0], 
            "tag": f"@{r[0]}", 
            "name": r[1], 
            "avatar": r[2],
            "is_dev": is_dev(r[0])
        } for r in await cur_u.fetchall()]

        cur_c = await db.execute(
            "SELECT channel_tag, name, avatar_url, description FROM channels WHERE channel_tag LIKE ? OR name LIKE ? LIMIT 10",
            (query, query)
        )
        channels = [{
            "type": "channel", 
            "key": f"#{r[0]}", 
            "tag": f"#{r[0]}", 
            "name": r[1], 
            "avatar": r[2], 
            "desc": r[3],
            "is_dev": False
        } for r in await cur_c.fetchall()]

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
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <title>Cipher Messenger</title>
    <style>
        :root {
            --bg-main: #0b0d13;
            --bg-sidebar: #10131c;
            --bg-sidebar-hover: #171b26;
            --bg-chat: #0b0d13;
            --bg-header: #10131c;
            --bg-input: #151822;
            --bubble-in: #191d29;
            --bubble-out: #2563eb;
            --text-main: #ffffff;
            --text-sub: #717a8c;
            --badge-blue: #3b82f6;
            --online-green: #10b981;
            --border-color: #171b26;
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

        .dev-badge {
            background: linear-gradient(135deg, #8b5cf6, #ec4899);
            color: #fff;
            font-size: 0.65rem;
            font-weight: 800;
            padding: 2px 6px;
            border-radius: 6px;
            letter-spacing: 0.5px;
            display: inline-flex;
            align-items: center;
            box-shadow: 0 0 10px rgba(236, 72, 153, 0.35);
            text-transform: uppercase;
        }

        .modal-overlay {
            position: fixed;
            inset: 0;
            background: rgba(6, 8, 12, 0.94);
            backdrop-filter: blur(10px);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 1000;
        }
        .card-modal {
            background: #121520;
            padding: 30px 26px;
            border-radius: 20px;
            width: 90%;
            max-width: 400px;
            border: 1px solid #1e2332;
            box-shadow: 0 25px 50px rgba(0,0,0,0.7);
        }
        .brand-logo {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            font-size: 1.6rem;
            font-weight: 800;
            letter-spacing: 1px;
            color: #fff;
            margin-bottom: 4px;
        }
        .brand-logo span { color: var(--badge-blue); }
        .card-modal h2 { font-size: 1.3rem; font-weight: 700; margin-bottom: 4px; text-align: center; }
        .card-modal p.subtitle { font-size: 0.85rem; color: var(--text-sub); margin-bottom: 20px; text-align: center; }
        
        .card-modal input, .card-modal textarea {
            width: 100%;
            padding: 12px 14px;
            margin-bottom: 12px;
            background: var(--bg-input);
            border: 1px solid #202636;
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

        #app-container {
            display: flex;
            height: 100%;
            width: 100%;
        }

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
            border-radius: 12px;
            transition: background 0.2s;
        }
        .user-profile-badge:hover { background: #171b26; }
        .avatar-small {
            width: 38px;
            height: 38px;
            border-radius: 50%;
            object-fit: cover;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: bold;
            font-size: 0.92rem;
            color: #fff;
            flex-shrink: 0;
        }

        .sidebar-actions { display: flex; gap: 8px; }
        .icon-btn {
            background: var(--bg-input);
            border: 1px solid #1e2332;
            color: #fff;
            width: 38px;
            height: 38px;
            border-radius: 12px;
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            font-size: 1.1rem;
            transition: 0.2s;
        }
        .icon-btn:hover { background: #202636; }

        .search-container {
            padding: 8px 16px 14px 16px;
            position: relative;
            border-bottom: 1px solid var(--border-color);
        }
        .search-input {
            width: 100%;
            padding: 11px 14px 11px 36px;
            background: var(--bg-input);
            border: 1px solid #1e2332;
            border-radius: 12px;
            color: #fff;
            outline: none;
            font-size: 0.9rem;
        }
        .search-icon {
            position: absolute;
            left: 28px;
            top: 20px;
            font-size: 0.85rem;
            color: var(--text-sub);
        }

        .chat-list { flex: 1; overflow-y: auto; }
        .chat-item {
            display: flex;
            align-items: center;
            padding: 12px 18px;
            cursor: pointer;
            transition: background 0.15s;
        }
        .chat-item:hover { background: var(--bg-sidebar-hover); }
        .chat-item.active { background: #171b28; }

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

        .chat-details { flex: 1; min-width: 0; }
        .chat-top-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 3px;
        }
        .chat-name {
            font-weight: 600;
            font-size: 0.96rem;
            color: #ffffff;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .chat-time { font-size: 0.72rem; color: var(--text-sub); margin-left: 6px; }
        .chat-bottom-row { display: flex; justify-content: space-between; align-items: center; }
        .chat-preview {
            font-size: 0.82rem;
            color: var(--text-sub);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        #chat-area {
            flex: 1;
            display: flex;
            flex-direction: column;
            background: var(--bg-chat);
            height: 100%;
        }

        .empty-placeholder {
            flex: 1;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            color: var(--text-sub);
            gap: 12px;
            text-align: center;
            padding: 20px;
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

        .pinned-banner {
            background: #151924;
            padding: 8px 18px;
            display: none;
            align-items: center;
            justify-content: space-between;
            border-bottom: 1px solid #1e2434;
            font-size: 0.82rem;
            cursor: pointer;
        }
        .pinned-banner span.label { color: var(--badge-blue); font-weight: 600; margin-right: 8px; }

        .messages-container {
            flex: 1;
            padding: 20px;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            gap: 8px;
        }
        .msg-row {
            display: flex;
            align-items: flex-end;
            gap: 8px;
            width: 100%;
            position: relative;
        }
        .msg-row.mine { justify-content: flex-end; }

        .msg-avatar {
            width: 28px;
            height: 28px;
            border-radius: 50%;
            object-fit: cover;
            flex-shrink: 0;
            cursor: pointer;
        }

        .bubble {
            max-width: 65%;
            padding: 9px 13px;
            border-radius: 14px;
            font-size: 0.92rem;
            line-height: 1.4;
            word-break: break-word;
            position: relative;
            cursor: pointer;
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
            margin-bottom: 4px;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .bubble-reply {
            background: rgba(0, 0, 0, 0.25);
            border-left: 3px solid var(--badge-blue);
            padding: 4px 8px;
            border-radius: 4px;
            margin-bottom: 6px;
            font-size: 0.78rem;
        }
        .bubble-reply .reply-user { font-weight: 600; color: #93c5fd; }
        
        .bubble-img {
            max-width: 100%;
            border-radius: 8px;
            margin-top: 4px;
            display: block;
        }

        .audio-player {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-top: 4px;
        }
        .audio-player audio { height: 32px; width: 220px; outline: none; }

        .file-attachment {
            display: flex;
            align-items: center;
            gap: 8px;
            background: rgba(0,0,0,0.2);
            padding: 8px 12px;
            border-radius: 8px;
            text-decoration: none;
            color: #fff;
            font-size: 0.85rem;
            margin-top: 4px;
        }

        .bubble-footer {
            display: flex;
            justify-content: flex-end;
            align-items: center;
            gap: 4px;
            margin-top: 4px;
        }
        .bubble-time { font-size: 0.68rem; color: rgba(255, 255, 255, 0.6); }

        .reactions-row {
            display: flex;
            flex-wrap: wrap;
            gap: 4px;
            margin-top: 4px;
        }
        .reaction-chip {
            background: rgba(255, 255, 255, 0.12);
            border-radius: 12px;
            padding: 2px 7px;
            font-size: 0.75rem;
            display: flex;
            align-items: center;
            gap: 4px;
            cursor: pointer;
        }

        .action-banner {
            background: #141722;
            padding: 8px 18px;
            display: none;
            align-items: center;
            justify-content: space-between;
            border-top: 1px solid var(--border-color);
            font-size: 0.84rem;
        }
        .action-banner span.title { color: var(--badge-blue); font-weight: 600; }
        .action-close { cursor: pointer; color: var(--text-sub); font-size: 1.1rem; }

        .input-bar {
            padding: 10px 18px;
            background: var(--bg-header);
            border-top: 1px solid var(--border-color);
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .input-wrapper {
            flex: 1;
            background: var(--bg-input);
            border: 1px solid #1e2332;
            border-radius: 14px;
            padding: 0 14px;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .input-wrapper input {
            flex: 1;
            background: transparent;
            border: none;
            color: #fff;
            padding: 12px 0;
            font-size: 0.95rem;
            outline: none;
        }

        .bar-btn {
            background: transparent;
            border: none;
            color: var(--text-sub);
            cursor: pointer;
            font-size: 1.25rem;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: color 0.2s;
        }
        .bar-btn:hover { color: #fff; }
        .bar-btn.recording { color: var(--danger-red); animation: pulse 1s infinite; }

        .send-btn {
            background: var(--badge-blue);
            border: none;
            color: white;
            width: 42px;
            height: 42px;
            border-radius: 12px;
            cursor: pointer;
            font-size: 1.1rem;
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .msg-menu {
            position: fixed;
            background: #181c28;
            border: 1px solid #282f42;
            border-radius: 14px;
            padding: 6px;
            display: none;
            flex-direction: column;
            z-index: 2000;
            box-shadow: 0 10px 30px rgba(0,0,0,0.8);
            min-width: 170px;
        }
        .msg-menu-item {
            padding: 9px 14px;
            font-size: 0.85rem;
            color: #fff;
            border-radius: 8px;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .msg-menu-item:hover { background: #222838; }
        .msg-menu-emojis {
            display: flex;
            gap: 6px;
            padding: 6px 10px;
            border-bottom: 1px solid #282f42;
            font-size: 1.25rem;
            justify-content: space-between;
        }
        .msg-menu-emojis span { cursor: pointer; transition: transform 0.15s; }
        .msg-menu-emojis span:hover { transform: scale(1.3); }

        @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.4; } 100% { opacity: 1; } }

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

<div id="msg-menu" class="msg-menu">
    <div class="msg-menu-emojis">
        <span onclick="sendReaction('👍')">👍</span>
        <span onclick="sendReaction('❤️')">❤️</span>
        <span onclick="sendReaction('🔥')">🔥</span>
        <span onclick="sendReaction('😂')">😂</span>
        <span onclick="sendReaction('😮')">😮</span>
        <span onclick="sendReaction('👏')">👏</span>
    </div>
    <div class="msg-menu-item" onclick="startReply()">💬 Ответить</div>
    <div class="msg-menu-item" onclick="pinMessage()">📌 Закрепить</div>
    <div class="msg-menu-item" id="menu-edit-btn" onclick="startEdit()">✏️ Изменить</div>
    <div class="msg-menu-item" id="menu-del-btn" style="color: var(--danger-red);" onclick="deleteMessage()">🗑️ Удалить</div>
</div>

<!-- Модалка Авторизации -->
<div id="auth-modal" class="modal-overlay">
    <div class="card-modal">
        <div class="brand-logo">⚡ CIPHER<span>.</span></div>
        <p class="subtitle" id="auth-sub">Secure Cloud Network</p>
        
        <input type="text" id="auth-login" placeholder="Email или @юзернейм">
        <input type="text" id="auth-name" placeholder="Отображаемое имя (например, Miles)" style="display:none;">
        <input type="text" id="auth-username" placeholder="Уникальный @юзернейм" style="display:none;">
        <input type="password" id="auth-pwd" placeholder="Пароль">
        
        <button class="btn-primary" onclick="submitAuth()" id="auth-btn">Войти в Cipher</button>
        <p class="subtitle" style="margin-top: 15px; cursor: pointer; color: var(--badge-blue);" id="auth-toggle" onclick="toggleAuth()">Нет аккаунта? Создать аккаунт</p>
    </div>
</div>

<!-- Модалка Создания Канала -->
<div id="channel-modal" class="modal-overlay" style="display: none;">
    <div class="card-modal">
        <h2>Создать канал</h2>
        <p class="subtitle">Публичное пространство для постов и общения</p>
        
        <input type="text" id="chan-name" placeholder="Название канала">
        <input type="text" id="chan-tag" placeholder="Тег канала (например, dev, music)">
        <textarea id="chan-desc" rows="2" placeholder="Описание канала..."></textarea>
        <label style="font-size: 0.8rem; color: var(--text-sub); display: block; margin-bottom: 6px;">Аватарка канала:</label>
        <input type="file" id="chan-file" accept="image/*">

        <button class="btn-primary" onclick="createChannelSubmit()">Создать канал</button>
        <button class="btn-cancel" onclick="document.getElementById('channel-modal').style.display='none'">Отмена</button>
    </div>
</div>

<!-- Модалка Моего Профиля -->
<div id="profile-modal" class="modal-overlay" style="display: none;">
    <div class="card-modal" style="text-align: center;">
        <h2>Мой Cipher ID</h2>
        <div id="profile-avatar-preview" style="width: 80px; height: 80px; border-radius: 50%; margin: 15px auto; display:flex; align-items:center; justify-content:center; font-size:2rem; font-weight:bold; color:#fff; overflow:hidden;"></div>
        <div style="display:flex; align-items:center; justify-content:center; gap:8px; margin-bottom:4px;">
            <h3 id="profile-name"></h3>
            <span id="profile-dev-badge" class="dev-badge" style="display:none;">🛠️ DEV</span>
        </div>
        <p id="profile-tag" style="color: var(--badge-blue); font-size: 0.9rem; margin-bottom: 15px;"></p>
        
        <label style="font-size: 0.85rem; color: var(--text-sub); display: block; margin-bottom: 8px;">Сменить фото:</label>
        <input type="file" id="profile-file" accept="image/*">
        
        <button class="btn-primary" onclick="uploadUserAvatar()">Сохранить аватарку</button>
        <button class="btn-cancel" onclick="logout()" style="color: var(--danger-red); margin-top: 8px;">Выйти из Cipher 🚪</button>
        <button class="btn-cancel" onclick="document.getElementById('profile-modal').style.display='none'">Закрыть</button>
    </div>
</div>

<!-- Модалка Просмотра Чужого Профиля -->
<div id="user-info-modal" class="modal-overlay" style="display: none;">
    <div class="card-modal" style="text-align: center;">
        <h2>Профиль пользователя</h2>
        <div id="info-avatar" style="width: 80px; height: 80px; border-radius: 50%; margin: 15px auto; display:flex; align-items:center; justify-content:center; font-size:2rem; font-weight:bold; color:#fff; overflow:hidden;"></div>
        <div style="display:flex; align-items:center; justify-content:center; gap:8px; margin-bottom:4px;">
            <h3 id="info-name"></h3>
            <span id="info-dev-badge" class="dev-badge" style="display:none;">🛠️ DEV</span>
        </div>
        <p id="info-tag" style="color: var(--badge-blue); font-size: 0.9rem; margin-bottom: 6px;"></p>
        <p id="info-date" style="color: var(--text-sub); font-size: 0.8rem; margin-bottom: 16px;">В сети с: ...</p>
        
        <button class="btn-primary" id="info-block-btn" style="background:#ef4444;" onclick="toggleBlockContact()">🚫 Заблокировать</button>
        <button class="btn-cancel" onclick="document.getElementById('user-info-modal').style.display='none'">Закрыть</button>
    </div>
</div>

<input type="file" id="media-file-input" style="display: none;" onchange="handleFileUpload(this.files[0])">

<div id="app-container">
    <div id="sidebar">
        <div class="sidebar-header">
            <div class="user-profile-badge" onclick="openProfile()">
                <div class="avatar-small" id="my-avatar-mini">?</div>
                <div>
                    <div style="display:flex; align-items:center; gap:6px;">
                        <span id="my-display-name" style="font-weight:600; font-size:0.92rem;">Загрузка...</span>
                        <span id="my-dev-badge" class="dev-badge" style="display:none;">DEV</span>
                    </div>
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
        <div id="chat-placeholder" class="empty-placeholder">
            <div style="font-size: 3rem;">⚡</div>
            <h3 style="color:#fff;">Добро пожаловать в Cipher</h3>
            <p>Выберите диалог или найдите контакт через поиск 🔍</p>
        </div>

        <div id="chat-content" style="display:none; flex-direction:column; height:100%;">
            <div class="chat-header">
                <button class="back-btn" onclick="document.body.classList.remove('in-chat')">←</button>
                <div class="avatar-small" id="header-avatar" style="width:42px; height:42px; font-size:1.1rem; margin-right:12px; cursor:pointer;" onclick="openPeerInfo()">?</div>
                <div style="cursor:pointer;" onclick="openPeerInfo()">
                    <div style="display:flex; align-items:center; gap:6px;">
                        <span id="header-title" style="font-weight:600; font-size:1rem;">Выберите чат</span>
                        <span id="header-dev-badge" class="dev-badge" style="display:none;">DEV</span>
                    </div>
                    <div id="header-sub" style="font-size:0.78rem; color:var(--badge-blue);">Cipher Network</div>
                </div>
            </div>

            <div id="pinned-banner" class="pinned-banner">
                <div><span class="label">📌 Закреплено:</span><span id="pinned-text">Сообщение</span></div>
                <span style="color:var(--text-sub);" onclick="unpinMessage(event)">✕</span>
            </div>

            <div class="messages-container" id="messages"></div>

            <div id="action-banner" class="action-banner">
                <div>
                    <span class="title" id="action-title">Ответ на сообщение</span>
                    <div id="action-text" style="color:var(--text-sub); font-size:0.78rem; margin-top:2px;">Текст...</div>
                </div>
                <span class="action-close" onclick="cancelAction()">✕</span>
            </div>

            <div class="input-bar">
                <button class="bar-btn" title="Прикрепить фото/файл" onclick="document.getElementById('media-file-input').click()">📎</button>
                <div class="input-wrapper">
                    <input type="text" id="msg-input" placeholder="Сообщение..." oninput="handleTyping()" onkeydown="if(event.key==='Enter') sendMsg()">
                </div>
                <button class="bar-btn" id="voice-btn" title="Голосовое сообщение" onclick="toggleVoiceRecord()">🎙️</button>
                <button class="send-btn" onclick="sendMsg()">➤</button>
            </div>
        </div>
    </div>
</div>

<script>
    let user = JSON.parse(localStorage.getItem("messenger_user") || "null");
    let currentTarget = localStorage.getItem("messenger_target") || "";
    let isRegister = false;
    let ws = null;
    let chatsList = [];
    let onlineUsers = [];
    let currentMessages = [];
    let selectedMsg = null;
    let replyMsg = null;
    let editMsg = null;
    let viewingPeerInfo = null;

    let mediaRecorder = null;
    let audioChunks = [];
    let isRecording = false;
    let typingTimeout = null;

    const DEV_USERS = ["milesconxxwow", "miles"];
    function checkIsDev(uname) {
        return uname && DEV_USERS.includes(uname.toLowerCase().replace("@", ""));
    }

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
        if (user && user.username) {
            document.getElementById("auth-modal").style.display = "none";
            startApp();
        }
        document.addEventListener("click", () => {
            document.getElementById("msg-menu").style.display = "none";
        });
    };

    function toggleAuth() {
        isRegister = !isRegister;
        document.getElementById("auth-name").style.display = isRegister ? "block" : "none";
        document.getElementById("auth-username").style.display = isRegister ? "block" : "none";
        document.getElementById("auth-btn").innerText = isRegister ? "Зарегистрироваться в Cipher" : "Войти в Cipher";
        document.getElementById("auth-toggle").innerText = isRegister ? "Уже есть аккаунт? Войти" : "Нет аккаунта? Создать аккаунт";
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
        localStorage.removeItem("messenger_target");
        location.reload();
    }

    async function fetchUserChats() {
        try {
            const res = await fetch(`/api/chats?username=${encodeURIComponent(user.username)}`);
            const data = await res.json();
            chatsList = data.chats || [];
            renderSidebar();
        } catch(e) {}
    }

    function startApp() {
        document.getElementById("my-display-name").innerText = user.display_name;
        document.getElementById("my-tag").innerText = "@" + user.username;
        renderAvatarEl(document.getElementById("my-avatar-mini"), user.display_name, user.avatar_url);

        if (checkIsDev(user.username)) {
            document.getElementById("my-dev-badge").style.display = "inline-flex";
            document.getElementById("profile-dev-badge").style.display = "inline-flex";
        }

        const protocol = location.protocol === "https:" ? "wss:" : "ws:";
        ws = new WebSocket(`${protocol}//${location.host}/ws/${encodeURIComponent(user.username)}`);

        ws.onmessage = (e) => {
            const data = JSON.parse(e.data);
            if (data.type === "online_list") {
                onlineUsers = data.users;
                renderSidebar();
            } else if (data.type === "typing" && data.sender === currentTarget) {
                document.getElementById("header-sub").innerText = "печатает...";
                clearTimeout(typingTimeout);
                typingTimeout = setTimeout(() => {
                    document.getElementById("header-sub").innerText = currentTarget.startsWith("#") ? "Канал" : "@" + currentTarget;
                }, 2000);
            } else if (data.type === "msg") {
                const isGroup = data.target.startsWith("#");
                const chatKey = isGroup ? data.target : data.sender_username;
                
                if (currentTarget === chatKey || (isGroup && currentTarget === data.target)) {
                    currentMessages.push(data);
                    renderAllMessages();
                }
                fetchUserChats();
            } else if (data.type === "edit_msg" || data.type === "delete_msg" || data.type === "reaction" || data.type === "pin") {
                loadHistory();
            }
        };

        fetchUserChats().then(() => {
            if (currentTarget && currentTarget !== "Общий чат") selectChat(currentTarget);
            else if (chatsList.length > 0) selectChat(chatsList[0].key);
        });
    }

    function openProfile() {
        document.getElementById("profile-name").innerText = user.display_name;
        document.getElementById("profile-tag").innerText = "@" + user.username;
        renderAvatarEl(document.getElementById("profile-avatar-preview"), user.display_name, user.avatar_url);
        if (checkIsDev(user.username)) document.getElementById("profile-dev-badge").style.display = "inline-flex";
        document.getElementById("profile-modal").style.display = "flex";
    }

    async function openPeerInfo(targetUsername = null) {
        const target = targetUsername || currentTarget;
        if (!target || target.startsWith("#")) return;

        const res = await fetch(`/api/user_info?username=${encodeURIComponent(target)}&current_user=${encodeURIComponent(user.username)}`);
        const data = await res.json();
        if (data.status === "ok") {
            viewingPeerInfo = data;
            document.getElementById("info-name").innerText = data.display_name;
            document.getElementById("info-tag").innerText = "@" + data.username;
            document.getElementById("info-date").innerText = "Регистрация: " + data.created_at;
            renderAvatarEl(document.getElementById("info-avatar"), data.display_name, data.avatar_url);
            
            document.getElementById("info-dev-badge").style.display = data.is_dev ? "inline-flex" : "none";
            const btn = document.getElementById("info-block-btn");
            btn.innerText = data.is_blocked ? "Разблокировать" : "🚫 Заблокировать";
            btn.style.background = data.is_blocked ? "#22c55e" : "#ef4444";

            document.getElementById("user-info-modal").style.display = "flex";
        }
    }

    async function toggleBlockContact() {
        if (!viewingPeerInfo) return;
        const res = await fetch("/api/toggle_block", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ blocker: user.username, blocked: viewingPeerInfo.username })
        });
        const data = await res.json();
        if (data.status === "ok") {
            viewingPeerInfo.is_blocked = data.blocked;
            const btn = document.getElementById("info-block-btn");
            btn.innerText = data.blocked ? "Разблокировать" : "🚫 Заблокировать";
            btn.style.background = data.blocked ? "#22c55e" : "#ef4444";
            fetchUserChats();
        }
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
            await fetchUserChats();
            selectChat("#" + data.channel_tag);
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
        list.innerHTML = `<div style="padding:10px 18px; font-size:0.75rem; color:var(--text-sub);">РЕЗУЛЬТАТЫ ПОИСКА CIPHER</div>`;
        if (results.length === 0) {
            list.innerHTML += `<div style="padding:18px; text-align:center; color:var(--text-sub); font-size:0.85rem;">Ничего не найдено</div>`;
            return;
        }
        results.forEach(item => {
            const div = document.createElement("div");
            div.className = "chat-item";
            const isUserDev = checkIsDev(item.key);
            div.onclick = () => {
                const exists = chatsList.find(c => c.key === item.key);
                if (!exists) {
                    chatsList.unshift({
                        key: item.key,
                        name: item.name,
                        tag: item.tag,
                        type: item.type,
                        avatar: item.avatar,
                        last_msg: item.desc || "Новый диалог",
                        time: "",
                        is_dev: isUserDev,
                        is_blocked: false
                    });
                }
                selectChat(item.key);
                document.getElementById("search-input").value = "";
            };
            div.innerHTML = `
                <div class="avatar-wrap">
                    <div class="avatar-img" style="background:${getGradient(item.name)}">${item.avatar ? `<img src="${item.avatar}" style="width:100%;height:100%;border-radius:50%;object-fit:cover;">` : item.name[0].toUpperCase()}</div>
                </div>
                <div class="chat-details">
                    <div class="chat-name">
                        <span>${item.name}</span>
                        ${isUserDev ? '<span class="dev-badge">DEV</span>' : ''}
                        <span style="font-size:0.78rem; color:var(--badge-blue); font-weight:normal;">${item.tag}</span>
                    </div>
                    <div class="chat-preview">${item.desc || (item.type === 'user' ? 'Пользователь' : 'Канал')}</div>
                </div>
            `;
            list.appendChild(div);
        });
    }

    function renderSidebar() {
        const list = document.getElementById("chat-list");
        list.innerHTML = "";
        
        if (chatsList.length === 0) {
            list.innerHTML = `<div style="padding:30px 20px; text-align:center; color:var(--text-sub); font-size:0.85rem;">Нет чатов.<br>Создайте канал ➕ или найдите контакт через поиск 🔍</div>`;
            return;
        }

        chatsList.forEach(chat => {
            const isOnline = onlineUsers.includes(chat.key);
            const isPeerDev = checkIsDev(chat.key);
            const div = document.createElement("div");
            div.className = `chat-item ${currentTarget === chat.key ? 'active' : ''}`;
            div.onclick = () => selectChat(chat.key);

            div.innerHTML = `
                <div class="avatar-wrap">
                    <div class="avatar-img" style="background:${getGradient(chat.name)}">${chat.avatar ? `<img src="${chat.avatar}" style="width:100%;height:100%;border-radius:50%;object-fit:cover;">` : chat.name[0].toUpperCase()}</div>
                    <div class="online-dot ${isOnline ? 'visible' : ''}"></div>
                </div>
                <div class="chat-details">
                    <div class="chat-top-row">
                        <div class="chat-name">
                            <span>${chat.name}</span>
                            ${isPeerDev ? '<span class="dev-badge">DEV</span>' : ''}
                            ${chat.is_blocked ? '<span style="font-size:0.7rem; color:var(--danger-red);">[Блок]</span>' : ''}
                        </div>
                        <div class="chat-time">${chat.time || ''}</div>
                    </div>
                    <div class="chat-bottom-row">
                        <div class="chat-preview">${chat.last_msg || (chat.key.startsWith('#') ? 'Канал' : 'Диалог')}</div>
                    </div>
                </div>
            `;
            list.appendChild(div);
        });
    }

    async function selectChat(key) {
        if (!key) return;
        currentTarget = key;
        localStorage.setItem("messenger_target", key);

        document.getElementById("chat-placeholder").style.display = "none";
        document.getElementById("chat-content").style.display = "flex";
        
        let chat = chatsList.find(c => c.key === key);
        const name = chat ? chat.name : key;
        const tag = chat ? chat.tag : key;
        const avatar = chat ? chat.avatar : "";
        const isPeerDev = checkIsDev(key);

        document.getElementById("header-title").innerText = name;
        document.getElementById("header-sub").innerText = key.startsWith("#") ? "Канал" : tag;
        document.getElementById("header-dev-badge").style.display = isPeerDev ? "inline-flex" : "none";
        renderAvatarEl(document.getElementById("header-avatar"), name, avatar);

        document.body.classList.add("in-chat");
        renderSidebar();
        await loadHistory();
    }

    async function loadHistory() {
        if (!currentTarget) return;
        const res = await fetch(`/api/history?user=${encodeURIComponent(user.username)}&target=${encodeURIComponent(currentTarget)}`);
        currentMessages = await res.json();
        renderAllMessages();
    }

    function renderAllMessages() {
        const box = document.getElementById("messages");
        box.innerHTML = "";
        currentMessages.forEach(m => {
            const isMine = m.sender_username === user.username;
            const row = document.createElement("div");
            row.className = `msg-row ${isMine ? 'mine' : 'theirs'}`;

            let contentHtml = "";
            if (m.reply_to_text) {
                contentHtml += `<div class="bubble-reply"><span class="reply-user">@${escapeHtml(m.reply_to_sender)}</span>: ${escapeHtml(m.reply_to_text)}</div>`;
            }

            if (m.msg_type === "image") {
                contentHtml += `<img src="${m.file_url}" class="bubble-img">`;
            } else if (m.msg_type === "voice") {
                contentHtml += `
                    <div class="audio-player">
                        <audio controls src="${m.file_url}"></audio>
                    </div>`;
            } else if (m.msg_type === "document") {
                contentHtml += `<a href="${m.file_url}" download="${m.file_name}" class="file-attachment">📄 ${escapeHtml(m.file_name || 'Скачать файл')}</a>`;
            }

            if (m.text) {
                contentHtml += `<div>${escapeHtml(m.text)}</div>`;
            }

            let reactionsHtml = "";
            if (m.reactions && Object.keys(m.reactions).length > 0) {
                reactionsHtml = `<div class="reactions-row">`;
                for (const [emo, count] of Object.entries(m.reactions)) {
                    reactionsHtml += `<div class="reaction-chip" onclick="event.stopPropagation(); toggleReaction(${m.id}, '${emo}')">${emo} ${count}</div>`;
                }
                reactionsHtml += `</div>`;
            }

            const isMsgDev = checkIsDev(m.sender_username);

            row.innerHTML = `
                ${!isMine && m.avatar ? `<img src="${m.avatar}" class="msg-avatar" onclick="openPeerInfo('${m.sender_username}')">` : ''}
                <div class="bubble" oncontextmenu="event.preventDefault(); openMsgMenu(event, ${m.id}, ${isMine})">
                    ${!isMine ? `
                        <div class="bubble-header" onclick="openPeerInfo('${m.sender_username}')">
                            <span>${m.sender_name}</span>
                            ${isMsgDev ? '<span class="dev-badge">DEV</span>' : ''}
                            <span style="font-weight:normal; opacity:0.6; font-size:0.7rem;">@${m.sender_username}</span>
                        </div>` : ''}
                    ${contentHtml}
                    ${reactionsHtml}
                    <div class="bubble-footer">
                        ${m.is_edited ? '<span style="font-size:0.65rem; opacity:0.6;">(изм.)</span>' : ''}
                        <span class="bubble-time">${m.time}</span>
                    </div>
                </div>
            `;
            box.appendChild(row);
        });
        box.scrollTop = box.scrollHeight;
    }

    function openMsgMenu(e, id, isMine) {
        selectedMsg = currentMessages.find(m => m.id === id);
        if (!selectedMsg) return;

        const menu = document.getElementById("msg-menu");
        menu.style.left = `${Math.min(e.clientX, window.innerWidth - 180)}px`;
        menu.style.top = `${Math.min(e.clientY, window.innerHeight - 200)}px`;
        menu.style.display = "flex";

        document.getElementById("menu-edit-btn").style.display = isMine && selectedMsg.msg_type === "text" ? "flex" : "none";
        document.getElementById("menu-del-btn").style.display = isMine ? "flex" : "none";
        e.stopPropagation();
    }

    function sendReaction(emoji) {
        if (!selectedMsg || !ws) return;
        ws.send(JSON.stringify({
            action: "reaction",
            msg_id: selectedMsg.id,
            emoji: emoji,
            target: currentTarget
        }));
        document.getElementById("msg-menu").style.display = "none";
    }

    function toggleReaction(msgId, emoji) {
        if (!ws) return;
        ws.send(JSON.stringify({
            action: "reaction",
            msg_id: msgId,
            emoji: emoji,
            target: currentTarget
        }));
    }

    function startReply() {
        replyMsg = selectedMsg;
        editMsg = null;
        document.getElementById("action-title").innerText = `Ответ пользователю @${replyMsg.sender_username}`;
        document.getElementById("action-text").innerText = replyMsg.text || (replyMsg.msg_type === 'image' ? '📷 Фото' : '🎙️ Голосовое');
        document.getElementById("action-banner").style.display = "flex";
        document.getElementById("msg-input").focus();
    }

    function startEdit() {
        editMsg = selectedMsg;
        replyMsg = null;
        document.getElementById("action-title").innerText = "Редактирование сообщения";
        document.getElementById("action-text").innerText = editMsg.text;
        document.getElementById("action-banner").style.display = "flex";
        document.getElementById("msg-input").value = editMsg.text;
        document.getElementById("msg-input").focus();
    }

    function cancelAction() {
        replyMsg = null;
        editMsg = null;
        document.getElementById("action-banner").style.display = "none";
        document.getElementById("msg-input").value = "";
    }

    function deleteMessage() {
        if (!selectedMsg || !ws) return;
        if (confirm("Удалить это сообщение?")) {
            ws.send(JSON.stringify({
                action: "delete",
                msg_id: selectedMsg.id,
                target: currentTarget
            }));
        }
    }

    function pinMessage() {
        if (!selectedMsg) return;
        const banner = document.getElementById("pinned-banner");
        document.getElementById("pinned-text").innerText = selectedMsg.text || "Медиафайл";
        banner.style.display = "flex";
    }

    function unpinMessage(e) {
        e.stopPropagation();
        document.getElementById("pinned-banner").style.display = "none";
    }

    function handleTyping() {
        if (ws && currentTarget && !currentTarget.startsWith("#")) {
            ws.send(JSON.stringify({ action: "typing", target: currentTarget }));
        }
    }

    async function handleFileUpload(file) {
        if (!file) return;
        const form = new FormData();
        form.append("file", file);

        const res = await fetch("/api/upload_file", { method: "POST", body: form });
        const data = await res.json();
        if (data.status === "ok") {
            const timeStr = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            const payload = {
                action: "send",
                sender_username: user.username,
                sender_name: user.display_name,
                target: currentTarget,
                text: "",
                msg_type: data.msg_type,
                file_url: data.url,
                file_name: data.file_name,
                avatar: user.avatar_url || "",
                time: timeStr
            };
            ws.send(JSON.stringify(payload));
        }
    }

    async function toggleVoiceRecord() {
        const btn = document.getElementById("voice-btn");
        if (!isRecording) {
            try {
                const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
                mediaRecorder = new MediaRecorder(stream);
                audioChunks = [];

                mediaRecorder.ondataavailable = e => audioChunks.push(e.data);
                mediaRecorder.onstop = async () => {
                    const audioBlob = new Blob(audioChunks, { type: 'audio/webm' });
                    const file = new File([audioBlob], "voice_message.webm", { type: "audio/webm" });
                    await handleFileUpload(file);
                    stream.getTracks().forEach(t => t.stop());
                };

                mediaRecorder.start();
                isRecording = true;
                btn.classList.add("recording");
                btn.title = "Нажмите, чтобы отправить голосовое";
            } catch (err) {
                alert("Нет доступа к микрофону");
            }
        } else {
            mediaRecorder.stop();
            isRecording = false;
            btn.classList.remove("recording");
            btn.title = "Голосовое сообщение";
        }
    }

    function sendMsg() {
        const input = document.getElementById("msg-input");
        const text = input.value.trim();
        if (!text || !ws || !currentTarget) return;

        if (editMsg) {
            ws.send(JSON.stringify({
                action: "edit",
                msg_id: editMsg.id,
                text: text,
                target: currentTarget
            }));
            cancelAction();
            return;
        }

        const timeStr = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        const payload = {
            action: "send",
            sender_username: user.username,
            sender_name: user.display_name,
            target: currentTarget,
            text: text,
            msg_type: "text",
            reply_to_id: replyMsg ? replyMsg.id : 0,
            reply_to_text: replyMsg ? (replyMsg.text || "Медиа") : "",
            reply_to_sender: replyMsg ? replyMsg.sender_username : "",
            avatar: user.avatar_url || "",
            time: timeStr
        };

        ws.send(JSON.stringify(payload));
        input.value = "";
        cancelAction();
    }

    function escapeHtml(str) {
        if (!str) return "";
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
            action = data.get("action", "send")
            target = data.get("target")

            if action == "typing":
                await manager.send_to_user({"type": "typing", "sender": username}, target)
                continue

            elif action == "reaction":
                msg_id = data.get("msg_id")
                emoji = data.get("emoji")
                async with aiosqlite.connect(DB_PATH) as db:
                    cur = await db.execute("SELECT reactions FROM messages WHERE id = ?", (msg_id,))
                    row = await cur.fetchone()
                    if row:
                        reacts = json.loads(row[0] or "{}")
                        reacts[emoji] = reacts.get(emoji, 0) + 1
                        await db.execute("UPDATE messages SET reactions = ? WHERE id = ?", (json.dumps(reacts), msg_id))
                        await db.commit()
                
                payload = {"type": "reaction", "msg_id": msg_id, "target": target}
                if target.startswith("#"):
                    await manager.broadcast(payload)
                else:
                    await manager.send_to_user(payload, target)
                    await manager.send_to_user(payload, username)
                continue

            elif action == "edit":
                msg_id = data.get("msg_id")
                new_text = data.get("text")
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("UPDATE messages SET text = ?, is_edited = 1 WHERE id = ? AND sender_username = ?", (new_text, msg_id, username))
                    await db.commit()
                
                payload = {"type": "edit_msg", "msg_id": msg_id, "text": new_text, "target": target}
                if target.startswith("#"):
                    await manager.broadcast(payload)
                else:
                    await manager.send_to_user(payload, target)
                    await manager.send_to_user(payload, username)
                continue

            elif action == "delete":
                msg_id = data.get("msg_id")
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("UPDATE messages SET is_deleted = 1 WHERE id = ? AND sender_username = ?", (msg_id, username))
                    await db.commit()
                
                payload = {"type": "delete_msg", "msg_id": msg_id, "target": target}
                if target.startswith("#"):
                    await manager.broadcast(payload)
                else:
                    await manager.send_to_user(payload, target)
                    await manager.send_to_user(payload, username)
                continue

            # Отправка нового сообщения
            text = data.get("text", "")
            msg_type = data.get("msg_type", "text")
            file_url = data.get("file_url", "")
            file_name = data.get("file_name", "")
            reply_id = data.get("reply_to_id", 0)
            reply_text = data.get("reply_to_text", "")
            reply_sender = data.get("reply_to_sender", "")
            time_str = datetime.now().strftime("%H:%M")

            # Проверка блокировки получателем
            async with aiosqlite.connect(DB_PATH) as db:
                if not target.startswith("#"):
                    cur_block = await db.execute("SELECT id FROM blocks WHERE blocker = ? AND blocked = ?", (target, username))
                    if await cur_block.fetchone():
                        # Получатель заблокировал отправителя — отправляем только самому себе
                        pass

                cur = await db.execute(
                    """INSERT INTO messages (sender_username, sender_name, target, text, msg_type, file_url, file_name, 
                                             reply_to_id, reply_to_text, reply_to_sender, timestamp, avatar_url) 
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (username, data.get("sender_name", username), target, text, msg_type, file_url, file_name, 
                     reply_id, reply_text, reply_sender, time_str, data.get("avatar", ""))
                )
                await db.commit()
                msg_id = cur.lastrowid

            msg_out = {
                "type": "msg",
                "id": msg_id,
                "sender_username": username,
                "sender_name": data.get("sender_name", username),
                "target": target,
                "text": text,
                "msg_type": msg_type,
                "file_url": file_url,
                "file_name": file_name,
                "reply_to_id": reply_id,
                "reply_to_text": reply_text,
                "reply_to_sender": reply_sender,
                "reactions": {},
                "is_edited": False,
                "time": time_str,
                "avatar": data.get("avatar", ""),
                "is_dev": is_dev(username)
            }

            if target.startswith("#"):
                await manager.broadcast(msg_out)
            else:
                async with aiosqlite.connect(DB_PATH) as db:
                    cur_block = await db.execute("SELECT id FROM blocks WHERE blocker = ? AND blocked = ?", (target, username))
                    is_target_blocked = bool(await cur_block.fetchone())
                
                if not is_target_blocked:
                    await manager.send_to_user(msg_out, recipient_username=target)
                await manager.send_to_user(msg_out, recipient_username=username)

    except WebSocketDisconnect:
        await manager.disconnect(username)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=80)
