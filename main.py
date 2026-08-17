import json
import os
import shutil
import uuid
import hashlib
import aiosqlite
import re
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from fastapi.responses import HTMLResponse
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
    return str(username).lower().strip().lstrip("@") in DEV_USERNAMES

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()

def normalize_text(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"[@#\-_.,!?/\\]+", "", str(text).lower()).strip()

def calculate_score(query: str, target: str) -> int:
    if not query or not target:
        return 0
    q = str(query).lower().strip()
    t = str(target).lower().strip()
    if q == t:
        return 100
    if t.startswith(q):
        return 80
    if q in t:
        return 50
    if normalize_text(q) in normalize_text(t):
        return 30
    return 0

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL COLLATE NOCASE,
                username TEXT UNIQUE NOT NULL COLLATE NOCASE,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                avatar_url TEXT DEFAULT '',
                custom_status TEXT DEFAULT 'online',
                last_seen TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_tag TEXT UNIQUE NOT NULL COLLATE NOCASE,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                creator_username TEXT NOT NULL COLLATE NOCASE,
                avatar_url TEXT DEFAULT '',
                pinned_msg_id INTEGER DEFAULT 0,
                members TEXT DEFAULT '[]',
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_username TEXT NOT NULL COLLATE NOCASE,
                sender_name TEXT NOT NULL,
                target TEXT NOT NULL COLLATE NOCASE,
                text TEXT DEFAULT '',
                msg_type TEXT DEFAULT 'text',
                file_url TEXT DEFAULT '',
                file_name TEXT DEFAULT '',
                reply_to_id INTEGER DEFAULT 0,
                reply_to_text TEXT DEFAULT '',
                reply_to_sender TEXT DEFAULT '',
                forward_from TEXT DEFAULT '',
                reactions TEXT DEFAULT '{}',
                is_read INTEGER DEFAULT 0,
                is_edited INTEGER DEFAULT 0,
                is_deleted INTEGER DEFAULT 0,
                timestamp TEXT NOT NULL,
                avatar_url TEXT DEFAULT ''
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS blocks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                blocker TEXT NOT NULL COLLATE NOCASE,
                blocked TEXT NOT NULL COLLATE NOCASE,
                UNIQUE(blocker, blocked)
            )
        """)

        migrations = [
            ("users", "custom_status", "TEXT DEFAULT 'online'"),
            ("users", "last_seen", "TEXT DEFAULT ''"),
            ("channels", "members", "TEXT DEFAULT '[]'"),
            ("messages", "forward_from", "TEXT DEFAULT ''"),
            ("messages", "is_read", "INTEGER DEFAULT 0"),
            ("messages", "is_edited", "INTEGER DEFAULT 0"),
            ("messages", "is_deleted", "INTEGER DEFAULT 0"),
            ("messages", "reactions", "TEXT DEFAULT '{}'")
        ]
        for tbl, col, ctype in migrations:
            try:
                await db.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {ctype}")
            except Exception:
                pass

        await db.commit()

class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, WebSocket] = {}

    async def connect(self, username: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[username.lower()] = websocket
        await self.broadcast_online()

    async def disconnect(self, username: str):
        uname = username.lower()
        if uname in self.active_connections:
            del self.active_connections[uname]
            time_now = datetime.now().strftime("%H:%M")
            try:
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("UPDATE users SET last_seen = ? WHERE LOWER(username) = ?", (time_now, uname))
                    await db.commit()
            except Exception:
                pass
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
        r_uname = recipient_username.lower().strip().lstrip("@")
        if r_uname in self.active_connections:
            try:
                await self.active_connections[r_uname].send_text(json.dumps(message))
            except Exception:
                pass

    async def broadcast_channel(self, message: dict):
        for conn in list(self.active_connections.values()):
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

class StatusModel(BaseModel):
    username: str
    status: str

class UpdateProfileModel(BaseModel):
    username: str
    display_name: str
    email: str
    password: str = ""

class ChannelMemberModel(BaseModel):
    channel_tag: str
    username: str
    requester: str = ""

@app.post("/api/register")
async def register(data: RegisterModel):
    email = data.email.strip().lower()
    uname = data.username.strip().lstrip("@").lower()
    name = data.display_name.strip() or uname
    pwd = data.password.strip()

    if not email or not uname or not pwd:
        return {"status": "error", "message": "Заполните все обязательные поля"}
    
    if not re.match(r"^[a-zA-Z0-9_]+$", uname):
        return {"status": "error", "message": "Юзернейм может содержать только латинские буквы, цифры и _"}

    if len(uname) < 3:
        return {"status": "error", "message": "Юзернейм должен быть от 3 символов"}
    if len(pwd) < 4:
        return {"status": "error", "message": "Пароль должен быть от 4 символов"}

    async with aiosqlite.connect(DB_PATH) as db:
        cur_u = await db.execute("SELECT id FROM users WHERE LOWER(username) = LOWER(?)", (uname,))
        if await cur_u.fetchone():
            return {"status": "error", "message": f"Юзернейм @{uname} уже занят! Выберите другой."}

        cur_e = await db.execute("SELECT id FROM users WHERE LOWER(email) = LOWER(?)", (email,))
        if await cur_e.fetchone():
            return {"status": "error", "message": "Пользователь с такой почтой уже существует"}

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
            "custom_status": "online",
            "is_dev": is_dev(uname)
        }

@app.post("/api/login")
async def login(data: LoginModel):
    login_val = data.login.strip().lstrip("@").lower()
    pwd_hash = hash_password(data.password.strip())

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """SELECT username, display_name, email, avatar_url, custom_status 
               FROM users 
               WHERE (LOWER(email) = LOWER(?) OR LOWER(username) = LOWER(?)) AND password_hash = ?""",
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
                "custom_status": user[4] or "online",
                "is_dev": is_dev(user[0])
            }
        return {"status": "error", "message": "Неверный логин или пароль"}

@app.post("/api/update_profile")
async def update_profile(data: UpdateProfileModel):
    uname = data.username.strip().lower()
    new_name = data.display_name.strip()
    new_email = data.email.strip().lower()
    pwd = data.password.strip()

    if not new_name or not new_email:
        return {"status": "error", "message": "Имя и email не могут быть пустыми"}

    async with aiosqlite.connect(DB_PATH) as db:
        cur_u = await db.execute("SELECT email, password_hash FROM users WHERE LOWER(username) = LOWER(?)", (uname,))
        user_row = await cur_u.fetchone()
        if not user_row:
            return {"status": "error", "message": "Пользователь не найден"}

        old_email = user_row[0].lower()
        pwd_hash = user_row[1]

        if new_email != old_email:
            if not pwd:
                return {"status": "error", "message": "Для смены Email введите текущий пароль"}
            if hash_password(pwd) != pwd_hash:
                return {"status": "error", "message": "Неверный пароль от аккаунта"}

            cur_e = await db.execute("SELECT id FROM users WHERE LOWER(email) = LOWER(?) AND LOWER(username) != LOWER(?)", (new_email, uname))
            if await cur_e.fetchone():
                return {"status": "error", "message": "Этот Email уже занят другим аккаунтом"}

        await db.execute("UPDATE users SET display_name = ?, email = ? WHERE LOWER(username) = LOWER(?)", (new_name, new_email, uname))
        await db.execute("UPDATE messages SET sender_name = ? WHERE LOWER(sender_username) = LOWER(?)", (new_name, uname))
        await db.commit()

        return {"status": "ok", "display_name": new_name, "email": new_email}

@app.post("/api/upload_file")
async def upload_file_endpoint(file: UploadFile = File(...)):
    ext = file.filename.split(".")[-1].lower() if "." in file.filename else "bin"
    unique_name = f"file_{uuid.uuid4().hex[:12]}.{ext}"
    file_path = os.path.join(UPLOAD_DIR, unique_name)

    with open(file_path, "wb") as buf:
        shutil.copyfileobj(file.file, buf)

    is_img = ext in ["jpg", "jpeg", "png", "gif", "webp", "bmp", "svg"]
    is_vid = ext in ["mp4", "webm", "mov", "avi", "mkv"]
    is_audio = ext in ["webm", "ogg", "mp3", "wav", "m4a", "aac"]

    if is_img: file_type = "image"
    elif is_vid: file_type = "video"
    elif is_audio: file_type = "voice"
    else: file_type = "document"

    return {
        "status": "ok",
        "url": f"/uploads/{unique_name}",
        "file_name": file.filename,
        "msg_type": file_type
    }

@app.post("/api/upload_avatar")
async def upload_avatar(username: str = Form(...), file: UploadFile = File(...)):
    uname = username.strip().lower()
    ext = file.filename.split(".")[-1].lower() if "." in file.filename else "jpg"
    filename = f"avatar_{uname}_{uuid.uuid4().hex[:8]}.{ext}"
    file_path = os.path.join(UPLOAD_DIR, filename)

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    avatar_url = f"/uploads/{filename}"

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET avatar_url = ? WHERE LOWER(username) = LOWER(?)", (avatar_url, uname))
        await db.commit()

    return {"status": "ok", "avatar_url": avatar_url}

@app.post("/api/update_status")
async def update_status(data: StatusModel):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET custom_status = ? WHERE LOWER(username) = LOWER(?)", (data.status, data.username.lower()))
        await db.commit()
    return {"status": "ok"}

@app.post("/api/create_channel")
async def create_channel(tag: str = Form(...), name: str = Form(...), desc: str = Form(""), creator: str = Form(...), file: UploadFile = File(None)):
    clean_tag = tag.strip().lstrip("#").lower()
    clean_name = name.strip()
    creator_uname = creator.strip().lower()
    if not clean_tag or not clean_name:
        return {"status": "error", "message": "Укажите название и тег"}

    avatar_url = ""
    if file and file.filename:
        ext = file.filename.split(".")[-1].lower()
        filename = f"chan_{clean_tag}_{uuid.uuid4().hex[:8]}.{ext}"
        path = os.path.join(UPLOAD_DIR, filename)
        with open(path, "wb") as buf:
            shutil.copyfileobj(file.file, buf)
        avatar_url = f"/uploads/{filename}"

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id FROM channels WHERE LOWER(channel_tag) = LOWER(?)", (clean_tag,))
        if await cur.fetchone():
            return {"status": "error", "message": "Канал с таким тегом уже существует"}

        initial_members = json.dumps([creator_uname])
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        await db.execute(
            """INSERT INTO channels (channel_tag, name, description, creator_username, avatar_url, members, created_at) 
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (clean_tag, clean_name, desc, creator_uname, avatar_url, initial_members, created_at)
        )
        await db.commit()

    return {"status": "ok", "channel_tag": clean_tag, "name": clean_name, "avatar_url": avatar_url}

@app.post("/api/update_channel")
async def update_channel(tag: str = Form(...), name: str = Form(...), desc: str = Form(""), requester: str = Form(...), file: UploadFile = File(None)):
    clean_tag = tag.strip().lstrip("#").lower()
    req_uname = requester.strip().lower()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT creator_username, avatar_url FROM channels WHERE LOWER(channel_tag) = LOWER(?)", (clean_tag,))
        row = await cur.fetchone()
        if not row:
            return {"status": "error", "message": "Канал не найден"}
        if row[0].lower() != req_uname and not is_dev(req_uname):
            return {"status": "error", "message": "Только создатель может менять канал"}

        avatar_url = row[1]
        if file and file.filename:
            ext = file.filename.split(".")[-1].lower()
            filename = f"chan_{clean_tag}_{uuid.uuid4().hex[:8]}.{ext}"
            path = os.path.join(UPLOAD_DIR, filename)
            with open(path, "wb") as buf:
                shutil.copyfileobj(file.file, buf)
            avatar_url = f"/uploads/{filename}"

        await db.execute(
            "UPDATE channels SET name = ?, description = ?, avatar_url = ? WHERE LOWER(channel_tag) = LOWER(?)",
            (name.strip(), desc.strip(), avatar_url, clean_tag)
        )
        await db.commit()

    return {"status": "ok", "name": name.strip(), "description": desc.strip(), "avatar_url": avatar_url}

@app.post("/api/delete_channel")
async def delete_channel(data: ChannelMemberModel):
    clean_tag = data.channel_tag.strip().lstrip("#").lower()
    req_uname = data.username.strip().lower()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT creator_username FROM channels WHERE LOWER(channel_tag) = LOWER(?)", (clean_tag,))
        row = await cur.fetchone()
        if not row:
            return {"status": "error", "message": "Канал не найден"}
        if row[0].lower() != req_uname and not is_dev(req_uname):
            return {"status": "error", "message": "Только создатель может удалить канал"}

        await db.execute("DELETE FROM channels WHERE LOWER(channel_tag) = LOWER(?)", (clean_tag,))
        await db.execute("DELETE FROM messages WHERE LOWER(target) = LOWER(?)", (f"#{clean_tag}",))
        await db.commit()
    return {"status": "ok"}

@app.post("/api/join_channel")
async def join_channel(data: ChannelMemberModel):
    clean_tag = data.channel_tag.strip().lstrip("#").lower()
    uname = data.username.strip().lower()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT members FROM channels WHERE LOWER(channel_tag) = LOWER(?)", (clean_tag,))
        row = await cur.fetchone()
        if not row:
            return {"status": "error", "message": "Канал не найден"}
        
        members = json.loads(row[0] or "[]")
        if uname not in members:
            members.append(uname)
            await db.execute("UPDATE channels SET members = ? WHERE LOWER(channel_tag) = LOWER(?)", (json.dumps(members), clean_tag))
            await db.commit()
    return {"status": "ok"}

@app.post("/api/leave_channel")
async def leave_channel(data: ChannelMemberModel):
    clean_tag = data.channel_tag.strip().lstrip("#").lower()
    uname = data.username.strip().lower()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT members, creator_username FROM channels WHERE LOWER(channel_tag) = LOWER(?)", (clean_tag,))
        row = await cur.fetchone()
        if not row:
            return {"status": "error", "message": "Канал не найден"}
        
        # Создатель не может покинуть свой канал, он может только удалить его
        if row[1].lower() == uname:
            return {"status": "error", "message": "Создатель не может покинуть канал. Вы можете удалить его в настройках."}

        members = json.loads(row[0] or "[]")
        if uname in members:
            members.remove(uname)
            await db.execute("UPDATE channels SET members = ? WHERE LOWER(channel_tag) = LOWER(?)", (json.dumps(members), clean_tag))
            await db.commit()
    return {"status": "ok"}

@app.post("/api/add_channel_member")
async def add_channel_member(data: ChannelMemberModel):
    clean_tag = data.channel_tag.strip().lstrip("#").lower()
    target_user = data.username.strip().lstrip("@").lower()
    req_uname = data.requester.strip().lower()

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT creator_username, members FROM channels WHERE LOWER(channel_tag) = LOWER(?)", (clean_tag,))
        row = await cur.fetchone()
        if not row:
            return {"status": "error", "message": "Канал не найден"}
        if row[0].lower() != req_uname and not is_dev(req_uname):
            return {"status": "error", "message": "Только владелец может добавлять участников"}

        cur_u = await db.execute("SELECT id FROM users WHERE LOWER(username) = LOWER(?)", (target_user,))
        if not await cur_u.fetchone():
            return {"status": "error", "message": "Пользователь не найден"}

        members = json.loads(row[1] or "[]")
        if target_user not in members:
            members.append(target_user)
            await db.execute("UPDATE channels SET members = ? WHERE LOWER(channel_tag) = LOWER(?)", (json.dumps(members), clean_tag))
            await db.commit()

    return {"status": "ok", "message": f"@{target_user} добавлен в канал"}

@app.get("/api/user_info")
async def get_user_info(username: str, current_user: str):
    target_uname = username.strip().lstrip("@").lower()
    cur_uname = current_user.strip().lower()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT username, display_name, email, avatar_url, created_at, custom_status, last_seen FROM users WHERE LOWER(username) = LOWER(?)",
            (target_uname,)
        )
        row = await cur.fetchone()
        if not row:
            return {"status": "not_found"}
        
        cur_blk = await db.execute("SELECT id FROM blocks WHERE LOWER(blocker) = LOWER(?) AND LOWER(blocked) = LOWER(?)", (cur_uname, target_uname))
        is_blocked = bool(await cur_blk.fetchone())

        return {
            "status": "ok",
            "username": row[0],
            "display_name": row[1],
            "email": row[2],
            "avatar_url": row[3] or "",
            "created_at": row[4],
            "custom_status": row[5] or "online",
            "last_seen": row[6] or "",
            "is_dev": is_dev(row[0]),
            "is_blocked": is_blocked
        }

@app.post("/api/toggle_block")
async def toggle_block(data: BlockModel):
    blkr = data.blocker.strip().lower()
    blkd = data.blocked.strip().lower()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id FROM blocks WHERE LOWER(blocker) = LOWER(?) AND LOWER(blocked) = LOWER(?)", (blkr, blkd))
        row = await cur.fetchone()
        if row:
            await db.execute("DELETE FROM blocks WHERE LOWER(blocker) = LOWER(?) AND LOWER(blocked) = LOWER(?)", (blkr, blkd))
            await db.commit()
            return {"status": "ok", "blocked": False}
        else:
            await db.execute("INSERT INTO blocks (blocker, blocked) VALUES (?, ?)", (blkr, blkd))
            await db.commit()
            return {"status": "ok", "blocked": True}

@app.get("/api/chats")
async def get_user_chats(username: str):
    uname = username.strip().lower()
    async with aiosqlite.connect(DB_PATH) as db:
        chats = []
        cur_blk = await db.execute("SELECT blocked FROM blocks WHERE LOWER(blocker) = LOWER(?)", (uname,))
        blocked_list = [r[0].lower() for r in await cur_blk.fetchall()]

        cur_ch = await db.execute("SELECT channel_tag, name, avatar_url, pinned_msg_id, description, creator_username, members FROM channels")
        for ch in await cur_ch.fetchall():
            tag_key = f"#{ch[0]}"
            creator = ch[5]
            members = [m.lower() for m in json.loads(ch[6] or "[]")]
            
            if uname not in members and creator.lower() != uname and not is_dev(uname):
                continue

            cur_last = await db.execute(
                "SELECT text, msg_type, timestamp FROM messages WHERE LOWER(target) = LOWER(?) AND is_deleted = 0 ORDER BY id DESC LIMIT 1",
                (tag_key,)
            )
            last = await cur_last.fetchone()
            last_text = "Канал создан"
            if last:
                if last[1] == "voice": last_text = "🎙️ Голосовое"
                elif last[1] == "image": last_text = "📷 Фотография"
                elif last[1] == "video": last_text = "🎬 Видео"
                else: last_text = last[0] or "📎 Файл"

            chats.append({
                "key": tag_key,
                "name": ch[1],
                "tag": tag_key,
                "type": "channel",
                "avatar": ch[2] or "",
                "last_msg": last_text,
                "time": last[2] if last else "",
                "pinned_id": ch[3] or 0,
                "desc": ch[4] or "",
                "creator": creator,
                "is_member": True,
                "is_dev": False,
                "is_blocked": False
            })

        cur_peers = await db.execute("""
            SELECT DISTINCT 
                CASE WHEN LOWER(sender_username) = LOWER(?) THEN target ELSE sender_username END AS peer
            FROM messages 
            WHERE (LOWER(sender_username) = LOWER(?) OR LOWER(target) = LOWER(?)) 
              AND target NOT LIKE '#%'
        """, (uname, uname, uname))
        
        peers = [r[0].lower() for r in await cur_peers.fetchall() if r[0].lower() != uname and not r[0].startswith("#")]

        for p in peers:
            cur_u = await db.execute("SELECT username, display_name, avatar_url, custom_status, last_seen FROM users WHERE LOWER(username) = LOWER(?)", (p,))
            u_data = await cur_u.fetchone()
            if not u_data:
                continue
            real_uname = u_data[0]
            name = u_data[1]
            avatar = u_data[2] or ""
            status = u_data[3] or "online"
            last_seen = u_data[4] or ""
            
            cur_last = await db.execute("""
                SELECT text, msg_type, timestamp, is_read, sender_username FROM messages 
                WHERE ((LOWER(sender_username) = LOWER(?) AND LOWER(target) = LOWER(?)) OR (LOWER(sender_username) = LOWER(?) AND LOWER(target) = LOWER(?))) 
                  AND is_deleted = 0
                ORDER BY id DESC LIMIT 1
            """, (uname, p, p, uname))
            last = await cur_last.fetchone()
            last_text = ""
            last_read = 0
            last_sender = ""
            if last:
                last_read = last[3] or 0
                last_sender = last[4] or ""
                if last[1] == "voice": last_text = "🎙️ Голосовое"
                elif last[1] == "image": last_text = "📷 Фотография"
                elif last[1] == "video": last_text = "🎬 Видео"
                else: last_text = last[0] or "📎 Файл"

            chats.append({
                "key": real_uname,
                "name": name,
                "tag": f"@{real_uname}",
                "type": "user",
                "avatar": avatar,
                "custom_status": status,
                "last_seen": last_seen,
                "last_msg": last_text,
                "last_read": last_read,
                "last_sender": last_sender,
                "time": last[2] if last else "",
                "pinned_id": 0,
                "is_dev": is_dev(real_uname),
                "is_blocked": p in blocked_list
            })

        return {"chats": chats}

@app.get("/api/search")
async def search(q: str = "", current_user: str = "", offset: int = 0, limit: int = 15):
    raw_query = str(q).strip()
    clean_q = raw_query.lstrip('@').lstrip('#').lower()
    cur_uname = current_user.strip().lower()
    if not clean_q:
        return {"results": [], "has_more": False}

    results = []
    async with aiosqlite.connect(DB_PATH) as db:
        cur_u = await db.execute("SELECT username, display_name, email, avatar_url FROM users")
        for r in await cur_u.fetchall():
            uname, dname, email, avatar = r[0], r[1], r[2], r[3]
            if cur_uname and uname.lower() == cur_uname:
                continue
            s1 = calculate_score(clean_q, uname)
            s2 = calculate_score(clean_q, dname)
            s3 = calculate_score(clean_q, email)
            final_score = max(s1, s2, s3)
            if final_score > 0:
                results.append({
                    "score": final_score,
                    "type": "user",
                    "key": uname,
                    "tag": f"@{uname}",
                    "name": dname,
                    "extra": email,
                    "avatar": avatar or "",
                    "is_dev": is_dev(uname)
                })

        cur_c = await db.execute("SELECT channel_tag, name, avatar_url, description, creator_username, members FROM channels")
        for r in await cur_c.fetchall():
            tag, name, avatar, desc, creator, members_json = r[0], r[1], r[2], r[3] or "", r[4], r[5]
            s1 = calculate_score(clean_q, tag)
            s2 = calculate_score(clean_q, name)
            s3 = calculate_score(clean_q, desc) // 2
            final_score = max(s1, s2, s3)
            if final_score > 0:
                members = [m.lower() for m in json.loads(members_json or "[]")]
                is_sub = cur_uname in members or creator.lower() == cur_uname
                results.append({
                    "score": final_score,
                    "type": "channel",
                    "key": f"#{tag}",
                    "tag": f"#{tag}",
                    "name": name,
                    "extra": desc or "Канал",
                    "avatar": avatar or "",
                    "creator": creator,
                    "is_member": is_sub,
                    "is_dev": False
                })

        if cur_uname:
            cur_m = await db.execute("""
                SELECT id, sender_username, sender_name, target, text, timestamp 
                FROM messages 
                WHERE is_deleted = 0 
                  AND msg_type = 'text' 
                  AND (LOWER(sender_username) = LOWER(?) OR LOWER(target) = LOWER(?) OR target LIKE '#%')
                ORDER BY id DESC LIMIT 150
            """, (cur_uname, cur_uname))
            for m in await cur_m.fetchall():
                mid, s_user, s_name, target, text, m_time = m[0], m[1], m[2], m[3], m[4] or "", m[5]
                if clean_q in text.lower():
                    dialog_key = target if target.startswith("#") else (s_user if s_user.lower() != cur_uname else target)
                    results.append({
                        "score": 35,
                        "type": "message",
                        "key": dialog_key,
                        "tag": f"В чате {dialog_key}",
                        "name": f"{s_name}: {text}",
                        "extra": m_time,
                        "avatar": "",
                        "is_dev": False
                    })

    results.sort(key=lambda x: x["score"], reverse=True)
    paged_results = results[offset : offset + limit]
    has_more = (offset + limit) < len(results)
    return {"results": paged_results, "has_more": has_more, "total": len(results)}

@app.get("/api/history")
async def get_history(user: str, target: str):
    u_req = user.strip().lower()
    t_req = target.strip()
    async with aiosqlite.connect(DB_PATH) as db:
        if t_req.startswith("#"):
            clean_tag = t_req.lstrip("#").lower()
            cur_c = await db.execute("SELECT creator_username, members, description FROM channels WHERE LOWER(channel_tag) = LOWER(?)", (clean_tag,))
            c_data = await cur_c.fetchone()
            
            creator = c_data[0] if c_data else ""
            members = [m.lower() for m in json.loads(c_data[1] or "[]")] if c_data else []
            desc = c_data[2] if c_data else ""
            is_member = u_req in members or creator.lower() == u_req or is_dev(u_req)

            cur = await db.execute(
                """SELECT id, sender_username, sender_name, target, text, msg_type, file_url, file_name, 
                          reply_to_id, reply_to_text, reply_to_sender, forward_from, reactions, is_read, is_edited, is_deleted, timestamp, avatar_url 
                   FROM messages WHERE LOWER(target) = LOWER(?) AND is_deleted = 0 ORDER BY id ASC LIMIT 250""",
                (t_req,)
            )
            rows = await cur.fetchall()
            return {
                "type": "channel",
                "creator": creator,
                "is_member": is_member,
                "description": desc,
                "messages": [{
                    "id": r[0],
                    "sender_username": r[1],
                    "sender_name": r[2],
                    "target": r[3],
                    "text": r[4] or "",
                    "msg_type": r[5] or "text",
                    "file_url": r[6] or "",
                    "file_name": r[7] or "",
                    "reply_to_id": r[8] or 0,
                    "reply_to_text": r[9] or "",
                    "reply_to_sender": r[10] or "",
                    "forward_from": r[11] or "",
                    "reactions": json.loads(r[12] or "{}"),
                    "is_read": bool(r[13]),
                    "is_edited": bool(r[14]),
                    "time": r[16],
                    "avatar": r[17] or "",
                    "is_dev": is_dev(r[1])
                } for r in rows]
            }
        else:
            t_uname = t_req.lower()
            await db.execute("UPDATE messages SET is_read = 1 WHERE LOWER(sender_username) = LOWER(?) AND LOWER(target) = LOWER(?)", (t_uname, u_req))
            await db.commit()

            cur = await db.execute(
                """SELECT id, sender_username, sender_name, target, text, msg_type, file_url, file_name, 
                          reply_to_id, reply_to_text, reply_to_sender, forward_from, reactions, is_read, is_edited, is_deleted, timestamp, avatar_url 
                   FROM messages 
                   WHERE ((LOWER(sender_username) = LOWER(?) AND LOWER(target) = LOWER(?)) 
                       OR (LOWER(sender_username) = LOWER(?) AND LOWER(target) = LOWER(?))) 
                     AND is_deleted = 0
                   ORDER BY id ASC LIMIT 250""",
                (u_req, t_uname, t_uname, u_req)
            )
            rows = await cur.fetchall()
            return {
                "type": "user",
                "messages": [{
                    "id": r[0],
                    "sender_username": r[1],
                    "sender_name": r[2],
                    "target": r[3],
                    "text": r[4] or "",
                    "msg_type": r[5] or "text",
                    "file_url": r[6] or "",
                    "file_name": r[7] or "",
                    "reply_to_id": r[8] or 0,
                    "reply_to_text": r[9] or "",
                    "reply_to_sender": r[10] or "",
                    "forward_from": r[11] or "",
                    "reactions": json.loads(r[12] or "{}"),
                    "is_read": bool(r[13]),
                    "is_edited": bool(r[14]),
                    "time": r[16],
                    "avatar": r[17] or "",
                    "is_dev": is_dev(r[1])
                } for r in rows]
            }

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru" data-theme="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Cipher Messenger</title>
    <style>
        :root[data-theme="dark"] {
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
            --card-bg: #121520;
            --card-border: #1e2332;
        }

        :root[data-theme="light"] {
            --bg-main: #f0f2f5;
            --bg-sidebar: #ffffff;
            --bg-sidebar-hover: #f1f5f9;
            --bg-chat: #f8fafc;
            --bg-header: #ffffff;
            --bg-input: #e2e8f0;
            --bubble-in: #ffffff;
            --bubble-out: #2563eb;
            --text-main: #0f172a;
            --text-sub: #64748b;
            --badge-blue: #2563eb;
            --online-green: #16a34a;
            --border-color: #e2e8f0;
            --card-bg: #ffffff;
            --card-border: #cbd5e1;
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
            transition: background-color 0.2s, color 0.2s;
        }

        .dev-badge {
            background: linear-gradient(135deg, #8b5cf6, #ec4899);
            color: #fff !important;
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

        mark.hl {
            background: rgba(234, 179, 8, 0.35);
            color: #fef08a;
            border-radius: 3px;
            padding: 0 2px;
        }

        a.mention {
            color: var(--badge-blue);
            text-decoration: none;
            font-weight: 600;
            cursor: pointer;
        }
        a.mention:hover { text-decoration: underline; }

        .modal-overlay {
            position: fixed;
            inset: 0;
            background: rgba(0, 0, 0, 0.75);
            backdrop-filter: blur(8px);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 1000;
        }
        .card-modal {
            background: var(--card-bg);
            padding: 28px 24px;
            border-radius: 20px;
            width: 90%;
            max-width: 420px;
            border: 1px solid var(--card-border);
            box-shadow: 0 25px 50px rgba(0,0,0,0.5);
        }
        .brand-logo {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            font-size: 1.6rem;
            font-weight: 800;
            letter-spacing: 1px;
            color: var(--text-main);
            margin-bottom: 4px;
        }
        .brand-logo span { color: var(--badge-blue); }
        .card-modal h2 { font-size: 1.3rem; font-weight: 700; margin-bottom: 4px; text-align: center; color: var(--text-main); }
        .card-modal p.subtitle { font-size: 0.85rem; color: var(--text-sub); margin-bottom: 18px; text-align: center; }
        
        .card-modal input, .card-modal textarea, .card-modal select {
            width: 100%;
            padding: 12px 14px;
            margin-bottom: 12px;
            background: var(--bg-input);
            border: 1px solid var(--card-border);
            color: var(--text-main);
            border-radius: 12px;
            outline: none;
            font-size: 0.95rem;
        }
        .card-modal input:focus { border-color: var(--badge-blue); }

        .auth-error {
            background: rgba(239, 68, 68, 0.15);
            border: 1px solid #ef4444;
            color: #ef4444;
            padding: 10px;
            border-radius: 10px;
            font-size: 0.85rem;
            margin-bottom: 12px;
            display: none;
            text-align: center;
            font-weight: 600;
        }
        
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
        .btn-primary:hover { opacity: 0.9; }
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
        .user-profile-badge:hover { background: var(--bg-sidebar-hover); }
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
            border: 1px solid var(--border-color);
            color: var(--text-main);
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
        .icon-btn:hover { opacity: 0.8; }

        .search-container {
            padding: 8px 16px 14px 16px;
            position: relative;
            border-bottom: 1px solid var(--border-color);
        }
        .search-input {
            width: 100%;
            padding: 11px 14px 11px 36px;
            background: var(--bg-input);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            color: var(--text-main);
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
        .chat-item.active { background: var(--bg-sidebar-hover); }

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
        .online-dot.dnd { background: #ef4444; display: block; }

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
            color: var(--text-main);
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
            justify-content: space-between;
            padding: 0 20px;
            border-bottom: 1px solid var(--border-color);
            flex-shrink: 0;
        }
        .back-btn {
            display: none;
            background: none;
            border: none;
            color: var(--text-main);
            font-size: 1.4rem;
            margin-right: 14px;
            cursor: pointer;
        }

        .pinned-banner {
            background: var(--bg-sidebar-hover);
            padding: 8px 18px;
            display: none;
            align-items: center;
            justify-content: space-between;
            border-bottom: 1px solid var(--border-color);
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
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
        .msg-row.theirs .bubble {
            background: var(--bubble-in);
            color: var(--text-main);
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
            background: rgba(0, 0, 0, 0.15);
            border-left: 3px solid var(--badge-blue);
            padding: 4px 8px;
            border-radius: 4px;
            margin-bottom: 6px;
            font-size: 0.78rem;
        }
        .bubble-fwd {
            font-size: 0.75rem;
            color: #93c5fd;
            margin-bottom: 4px;
            font-style: italic;
        }
        
        .bubble-img {
            max-width: 100%;
            max-height: 320px;
            border-radius: 10px;
            margin-top: 4px;
            display: block;
            object-fit: cover;
        }

        .bubble-video {
            max-width: 100%;
            max-height: 320px;
            border-radius: 10px;
            margin-top: 4px;
            display: block;
            outline: none;
            background: #000;
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
            background: rgba(0,0,0,0.15);
            padding: 8px 12px;
            border-radius: 8px;
            text-decoration: none;
            color: inherit;
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
        .bubble-time { font-size: 0.68rem; opacity: 0.7; }
        .read-status { font-size: 0.75rem; color: #60a5fa; font-weight: bold; }

        .reactions-row {
            display: flex;
            flex-wrap: wrap;
            gap: 4px;
            margin-top: 4px;
        }
        .reaction-chip {
            background: rgba(0, 0, 0, 0.15);
            border-radius: 12px;
            padding: 2px 7px;
            font-size: 0.75rem;
            display: flex;
            align-items: center;
            gap: 4px;
            cursor: pointer;
            position: relative;
        }

        .action-banner {
            background: var(--bg-sidebar-hover);
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
            border: 1px solid var(--border-color);
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
            color: var(--text-main);
            padding: 12px 0;
            font-size: 0.95rem;
            outline: none;
        }

        .channel-restricted-bar {
            padding: 14px 18px;
            background: var(--bg-header);
            border-top: 1px solid var(--border-color);
            text-align: center;
            color: var(--text-sub);
            font-size: 0.9rem;
            display: none;
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
        .bar-btn:hover { color: var(--text-main); }
        .bar-btn.recording { color: #ef4444; animation: pulse 1s infinite; }

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

        .header-sub-btn {
            padding: 6px 12px;
            border-radius: 8px;
            font-size: 0.8rem;
            font-weight: 600;
            border: none;
            cursor: pointer;
            transition: 0.2s;
        }
        .btn-join { background: var(--badge-blue); color: #fff; }
        .btn-leave { background: rgba(239, 68, 68, 0.15); color: #ef4444; border: 1px solid #ef4444; }

        .msg-menu {
            position: fixed;
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 14px;
            padding: 6px;
            display: none;
            flex-direction: column;
            z-index: 2000;
            box-shadow: 0 10px 30px rgba(0,0,0,0.5);
            min-width: 180px;
        }
        .msg-menu-item {
            padding: 9px 14px;
            font-size: 0.85rem;
            color: var(--text-main);
            border-radius: 8px;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .msg-menu-item:hover { background: var(--bg-sidebar-hover); }
        .msg-menu-emojis {
            display: flex;
            gap: 6px;
            padding: 6px 10px;
            border-bottom: 1px solid var(--border-color);
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
        <span onclick="sendReaction('🎉')">🎉</span>
        <span onclick="sendReaction('😢')">😢</span>
    </div>
    <div class="msg-menu-item" onclick="startReply()">💬 Ответить</div>
    <div class="msg-menu-item" onclick="startForward()">↪️ Переслать</div>
    <div class="msg-menu-item" onclick="pinMessage()">📌 Закрепить</div>
    <div class="msg-menu-item" id="menu-edit-btn" onclick="startEdit()">✏️ Изменить</div>
    <div class="msg-menu-item" id="menu-del-btn" style="color: #ef4444;" onclick="deleteMessage()">🗑️ Удалить</div>
</div>

<div id="auth-modal" class="modal-overlay">
    <div class="card-modal">
        <div class="brand-logo">⚡ CIPHER<span>.</span></div>
        <p class="subtitle" id="auth-sub">Secure Cloud Network</p>
        
        <div id="auth-error-box" class="auth-error"></div>

        <input type="text" id="auth-login" placeholder="Email или @юзернейм">
        <input type="text" id="auth-name" placeholder="Отображаемое имя (например, Miles)" style="display:none;">
        <input type="text" id="auth-username" placeholder="Уникальный @юзернейм" style="display:none;">
        <input type="password" id="auth-pwd" placeholder="Пароль">
        
        <button class="btn-primary" onclick="submitAuth()" id="auth-btn">Войти в Cipher</button>
        <p class="subtitle" style="margin-top: 15px; cursor: pointer; color: var(--badge-blue);" id="auth-toggle" onclick="toggleAuth()">Нет аккаунта? Создать аккаунт</p>
    </div>
</div>

<div id="channel-modal" class="modal-overlay" style="display: none;">
    <div class="card-modal">
        <h2>Создать канал</h2>
        <p class="subtitle">Публичное пространство для публикаций</p>
        
        <input type="text" id="chan-name" placeholder="Название канала">
        <input type="text" id="chan-tag" placeholder="Уникальный тег (например, news, dev)">
        <textarea id="chan-desc" rows="2" placeholder="Описание канала..."></textarea>
        <label style="font-size: 0.8rem; color: var(--text-sub); display: block; margin-bottom: 6px;">Аватарка:</label>
        <input type="file" id="chan-file" accept="image/*">

        <button class="btn-primary" onclick="createChannelSubmit()">Создать канал</button>
        <button class="btn-cancel" onclick="document.getElementById('channel-modal').style.display='none'">Отмена</button>
    </div>
</div>

<div id="edit-channel-modal" class="modal-overlay" style="display: none;">
    <div class="card-modal">
        <h2>Настройки канала</h2>
        <p class="subtitle">Изменение информации, участников и удаление</p>
        
        <input type="text" id="edit-chan-name" placeholder="Название канала">
        <textarea id="edit-chan-desc" rows="2" placeholder="Описание канала..."></textarea>
        <label style="font-size: 0.8rem; color: var(--text-sub); display: block; margin-bottom: 6px;">Новая аватарка канала:</label>
        <input type="file" id="edit-chan-file" accept="image/*">

        <div style="margin: 12px 0; border-top: 1px solid var(--border-color); padding-top: 12px;">
            <label style="font-size: 0.8rem; color: var(--text-sub); display: block; margin-bottom: 6px;">Добавить участника (@юзернейм):</label>
            <div style="display:flex; gap:6px;">
                <input type="text" id="add-member-input" placeholder="@username" style="margin-bottom:0;">
                <button class="btn-primary" style="width:auto; padding:0 14px; margin-top:0;" onclick="addMemberSubmit()">+</button>
            </div>
        </div>

        <button class="btn-primary" onclick="saveChannelEdit()">Сохранить изменения</button>
        <button class="btn-primary" style="background:#ef4444; margin-top:8px;" onclick="deleteChannel()">🗑️ Удалить канал навсегда</button>
        <button class="btn-cancel" onclick="document.getElementById('edit-channel-modal').style.display='none'">Закрыть</button>
    </div>
</div>

<div id="profile-modal" class="modal-overlay" style="display: none;">
    <div class="card-modal" style="text-align: center;">
        <h2>Мой профиль</h2>
        <div id="profile-avatar-preview" style="width: 80px; height: 80px; border-radius: 50%; margin: 15px auto; display:flex; align-items:center; justify-content:center; font-size:2rem; font-weight:bold; color:#fff; overflow:hidden;"></div>
        
        <div style="text-align: left; margin-bottom: 12px;">
            <label style="font-size: 0.8rem; color: var(--text-sub); display: block; margin-bottom:4px;">Отображаемое имя (Ник):</label>
            <input type="text" id="profile-name-input" placeholder="Ваш ник">

            <label style="font-size: 0.8rem; color: var(--text-sub); display: block; margin-bottom:4px;">Email почта:</label>
            <input type="email" id="profile-email-input" placeholder="Ваш email">

            <div id="pwd-confirm-box" style="display:none;">
                <label style="font-size: 0.8rem; color: #ef4444; display: block; margin-bottom:4px;">Введите текущий пароль для подтверждения смены Email:</label>
                <input type="password" id="profile-pwd-input" placeholder="Ваш текущий пароль">
            </div>

            <label style="font-size: 0.8rem; color: var(--text-sub); display: block; margin-bottom:4px;">Юзернейм:</label>
            <div style="display:flex; align-items:center; gap:8px; margin-bottom: 12px;">
                <p id="profile-tag" style="color: var(--badge-blue); font-size: 0.95rem; font-weight: bold;"></p>
                <span id="profile-dev-badge" class="dev-badge" style="display:none;">🛠️ DEV</span>
            </div>
        </div>
        
        <label style="font-size: 0.8rem; color: var(--text-sub); display: block; text-align:left; margin-bottom:4px;">Личный статус:</label>
        <select id="user-status-select" onchange="changeMyStatus(this.value)">
            <option value="online">🟢 В сети</option>
            <option value="dnd">⛔ Не беспокоить</option>
            <option value="offline">⚪ Невидимка / Не в сети</option>
        </select>

        <label style="font-size: 0.8rem; color: var(--text-sub); display: block; text-align:left; margin-bottom:4px;">Сменить аватарку:</label>
        <input type="file" id="profile-file" accept="image/*">
        
        <button class="btn-primary" onclick="saveProfileChanges()">Сохранить изменения профиля</button>
        <button class="btn-cancel" onclick="logout()" style="color: #ef4444; margin-top: 8px;">Выйти из Cipher 🚪</button>
        <button class="btn-cancel" onclick="document.getElementById('profile-modal').style.display='none'">Закрыть</button>
    </div>
</div>

<div id="user-info-modal" class="modal-overlay" style="display: none;">
    <div class="card-modal" style="text-align: center;">
        <h2>Профиль пользователя</h2>
        <div id="info-avatar" style="width: 80px; height: 80px; border-radius: 50%; margin: 15px auto; display:flex; align-items:center; justify-content:center; font-size:2rem; font-weight:bold; color:#fff; overflow:hidden;"></div>
        <div style="display:flex; align-items:center; justify-content:center; gap:8px; margin-bottom:4px;">
            <h3 id="info-name" style="color:var(--text-main);"></h3>
            <span id="info-dev-badge" class="dev-badge" style="display:none;">🛠️ DEV</span>
        </div>
        <p id="info-tag" style="color: var(--badge-blue); font-size: 0.9rem; margin-bottom: 6px;"></p>
        <p id="info-status-text" style="font-size: 0.85rem; color: var(--online-green); margin-bottom: 4px;">🟢 В сети</p>
        <p id="info-date" style="color: var(--text-sub); font-size: 0.8rem; margin-bottom: 16px;">Регистрация: ...</p>
        
        <button class="btn-primary" id="info-block-btn" style="background:#ef4444;" onclick="toggleBlockContact()">🚫 Заблокировать</button>
        <button class="btn-cancel" onclick="document.getElementById('user-info-modal').style.display='none'">Закрыть</button>
    </div>
</div>

<div id="forward-modal" class="modal-overlay" style="display: none;">
    <div class="card-modal">
        <h2>Переслать сообщение</h2>
        <p class="subtitle">Выберите чат для отправки</p>
        <div id="forward-chats-list" style="max-height:220px; overflow-y:auto; margin-bottom:14px;"></div>
        <button class="btn-cancel" onclick="document.getElementById('forward-modal').style.display='none'">Отмена</button>
    </div>
</div>

<input type="file" id="media-file-input" style="display: none;" accept="image/*,video/*,audio/*,application/*" onchange="handleFileUpload(this.files[0])">

<div id="app-container">
    <div id="sidebar">
        <div class="sidebar-header">
            <div class="user-profile-badge" onclick="openProfile()">
                <div class="avatar-small" id="my-avatar-mini">?</div>
                <div>
                    <div style="display:flex; align-items:center; gap:6px;">
                        <span id="my-display-name" style="font-weight:600; font-size:0.92rem; color:var(--text-main);">Загрузка...</span>
                        <span id="my-dev-badge" class="dev-badge" style="display:none;">DEV</span>
                    </div>
                    <div id="my-tag" style="font-size:0.75rem; color:var(--text-sub);">@...</div>
                </div>
            </div>
            <div class="sidebar-actions">
                <button class="icon-btn" title="Сменить тему" onclick="toggleTheme()">🌓</button>
                <button class="icon-btn" title="Создать канал" onclick="document.getElementById('channel-modal').style.display='flex'">➕</button>
            </div>
        </div>

        <div class="search-container">
            <span class="search-icon">🔍</span>
            <input type="text" class="search-input" id="search-input" placeholder="Поиск @юзеров, каналов, сообщений..." oninput="handleSearch(this.value)">
        </div>

        <div class="chat-list" id="chat-list"></div>
    </div>

    <div id="chat-area">
        <div id="chat-placeholder" class="empty-placeholder">
            <div style="font-size: 3rem;">⚡</div>
            <h3 style="color:var(--text-main);">Добро пожаловать в Cipher</h3>
            <p>Выберите диалог или найдите контакт через поиск 🔍</p>
        </div>

        <div id="chat-content" style="display:none; flex-direction:column; height:100%;">
            <div class="chat-header">
                <div style="display:flex; align-items:center;">
                    <button class="back-btn" onclick="document.body.classList.remove('in-chat')">←</button>
                    <div class="avatar-small" id="header-avatar" style="width:42px; height:42px; font-size:1.1rem; margin-right:12px; cursor:pointer;" onclick="openPeerInfo()">?</div>
                    <div style="cursor:pointer;" onclick="openPeerInfo()">
                        <div style="display:flex; align-items:center; gap:6px;">
                            <span id="header-title" style="font-weight:600; font-size:1rem; color:var(--text-main);">Выберите чат</span>
                            <span id="header-dev-badge" class="dev-badge" style="display:none;">DEV</span>
                        </div>
                        <div id="header-sub" style="font-size:0.78rem; color:var(--badge-blue);">Cipher Network</div>
                    </div>
                </div>

                <div style="display:flex; align-items:center; gap:8px;">
                    <button class="header-sub-btn" id="channel-action-btn" style="display:none;" onclick="toggleChannelSubscription()"></button>
                    <button class="icon-btn" id="channel-settings-btn" title="Настройки канала" style="display:none;" onclick="openChannelSettings()">⚙️</button>
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

            <div class="input-bar" id="chat-input-bar">
                <button class="bar-btn" title="Прикрепить фото, видео или файл" onclick="document.getElementById('media-file-input').click()">📎</button>
                <div class="input-wrapper">
                    <input type="text" id="msg-input" placeholder="Сообщение..." oninput="handleTyping()" onkeydown="if(event.key==='Enter') sendMsg()">
                </div>
                <button class="bar-btn" id="voice-btn" title="Голосовое сообщение" onclick="toggleVoiceRecord()">🎙️</button>
                <button class="send-btn" onclick="sendMsg()">➤</button>
            </div>

            <div class="channel-restricted-bar" id="channel-restricted-bar">
                📢 Только создатель канала может публиковать посты
            </div>
        </div>
    </div>
</div>

<script>
    let user = JSON.parse(localStorage.getItem("messenger_user") || "null");
    let currentTarget = "";
    let currentChannelData = null;
    let currentTheme = localStorage.getItem("messenger_theme") || "dark";
    let isRegister = false;
    let ws = null;
    let chatsList = [];
    let onlineUsers = [];
    let currentMessages = [];
    let selectedMsg = null;
    let replyMsg = null;
    let editMsg = null;
    let forwardMsg = null;
    let viewingPeerInfo = null;

    let searchQuery = "";
    let searchOffset = 0;
    let searchResultsList = [];
    let searchHasMore = false;

    let mediaRecorder = null;
    let audioChunks = [];
    let isRecording = false;
    let typingTimeout = null;

    document.documentElement.setAttribute("data-theme", currentTheme);

    function toggleTheme() {
        currentTheme = currentTheme === "dark" ? "light" : "dark";
        document.documentElement.setAttribute("data-theme", currentTheme);
        localStorage.setItem("messenger_theme", currentTheme);
    }

    function playNotificationSound() {
        try {
            const ctx = new (window.AudioContext || window.webkitAudioContext)();
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();
            osc.type = "sine";
            osc.frequency.setValueAtTime(587.33, ctx.currentTime);
            osc.frequency.exponentialRampToValueAtTime(880, ctx.currentTime + 0.1);
            gain.gain.setValueAtTime(0.2, ctx.currentTime);
            gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.25);
            osc.connect(gain);
            gain.connect(ctx.destination);
            osc.start();
            osc.stop(ctx.currentTime + 0.25);
        } catch(e) {}
    }

    if ("Notification" in window && Notification.permission !== "granted" && Notification.permission !== "denied") {
        Notification.requestPermission();
    }

    function showBrowserNotification(title, text) {
        if ("Notification" in window && Notification.permission === "granted" && document.hidden) {
            new Notification(title, { body: text, icon: "/icon.svg" });
        }
    }

    const DEV_USERS = ["milesconxxwow", "miles"];
    function checkIsDev(uname) {
        return uname && DEV_USERS.includes(String(uname).toLowerCase().replace("@", ""));
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
        const val = String(str || "user");
        for (let i = 0; i < val.length; i++) hash += val.charCodeAt(i);
        return gradients[hash % gradients.length];
    }

    function formatMentionsAndTags(text) {
        if (!text) return "";
        let escaped = escapeHtml(text);
        escaped = escaped.replace(/@([a-zA-Z0-9_]+)/g, '<a class="mention" onclick="openPeerInfo(\\'$1\\')">@$1</a>');
        escaped = escaped.replace(/#([a-zA-Z0-9_]+)/g, '<a class="mention" onclick="handleSearch(\\'#$1\\')">#$1</a>');
        return escaped;
    }

    function highlightText(text, query) {
        if (!text) return "";
        if (!query) return escapeHtml(text);
        const cleanQ = String(query).replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&').replace(/^[@#]+/, "");
        if (!cleanQ) return escapeHtml(text);
        const regex = new RegExp(`(${cleanQ})`, "gi");
        return escapeHtml(text).replace(regex, `<mark class="hl">$1</mark>`);
    }

    function renderAvatarEl(el, name, avatarUrl) {
        if (!el) return;
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
        document.getElementById("auth-error-box").style.display = "none";
    }

    function showAuthError(msg) {
        const box = document.getElementById("auth-error-box");
        box.innerText = msg;
        box.style.display = "block";
    }

    async function submitAuth() {
        document.getElementById("auth-error-box").style.display = "none";
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
                localStorage.removeItem("messenger_target");
                user = data;
                localStorage.setItem("messenger_user", JSON.stringify(user));
                location.reload();
            } else {
                showAuthError(data.message);
            }
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
                localStorage.removeItem("messenger_target");
                user = data;
                localStorage.setItem("messenger_user", JSON.stringify(user));
                location.reload();
            } else {
                showAuthError(data.message);
            }
        }
    }

    function logout() {
        localStorage.clear();
        location.reload();
    }

    async function fetchUserChats() {
        try {
            const res = await fetch(`/api/chats?username=${encodeURIComponent(user.username)}`);
            const data = await res.json();
            chatsList = data.chats || [];
            if (!document.getElementById("search-input").value.trim()) {
                renderSidebar();
            }
        } catch(e) {}
    }

    function startApp() {
        document.getElementById("my-display-name").innerText = user.display_name;
        document.getElementById("my-tag").innerText = "@" + user.username;
        renderAvatarEl(document.getElementById("my-avatar-mini"), user.display_name, user.avatar_url);

        if (checkIsDev(user.username)) {
            const myDevBadge = document.getElementById("my-dev-badge");
            if (myDevBadge) myDevBadge.style.display = "inline-flex";
        }

        const protocol = location.protocol === "https:" ? "wss:" : "ws:";
        ws = new WebSocket(`${protocol}//${location.host}/ws/${encodeURIComponent(user.username)}`);

        ws.onmessage = (e) => {
            const data = JSON.parse(e.data);
            if (data.type === "online_list") {
                onlineUsers = data.users || [];
                if (!document.getElementById("search-input").value.trim()) renderSidebar();
            } else if (data.type === "typing" && data.sender.toLowerCase() === currentTarget.toLowerCase()) {
                document.getElementById("header-sub").innerText = "печатает...";
                clearTimeout(typingTimeout);
                typingTimeout = setTimeout(() => {
                    updateHeaderSubtitle();
                }, 2000);
            } else if (data.type === "msg") {
                const isGroup = data.target.startsWith("#");
                const senderMatch = data.sender_username.toLowerCase() === currentTarget.toLowerCase();
                const targetMatch = data.target.toLowerCase() === currentTarget.toLowerCase();
                const isMine = data.sender_username.toLowerCase() === user.username.toLowerCase();

                if (!isMine) {
                    playNotificationSound();
                    showBrowserNotification(data.sender_name, data.text || "Медиафайл");
                }

                if (isGroup ? targetMatch : (senderMatch || (isMine && targetMatch))) {
                    currentMessages.push(data);
                    renderAllMessages();
                    if (!isMine && !isGroup) {
                        ws.send(JSON.stringify({ action: "read", target: data.sender_username }));
                    }
                }
                fetchUserChats();
            } else if (data.type === "read_receipt") {
                if (currentTarget.toLowerCase() === data.reader.toLowerCase()) {
                    currentMessages.forEach(m => { 
                        if (m.sender_username.toLowerCase() === user.username.toLowerCase()) m.is_read = true; 
                    });
                    renderAllMessages();
                }
                fetchUserChats();
            } else if (data.type === "edit_msg" || data.type === "delete_msg" || data.type === "reaction" || data.type === "pin") {
                loadHistory();
            }
        };

        fetchUserChats().then(() => {
            if (currentTarget) selectChat(currentTarget);
            else if (chatsList.length > 0) selectChat(chatsList[0].key);
        });
    }

    function openProfile() {
        const nameInput = document.getElementById("profile-name-input");
        const emailInput = document.getElementById("profile-email-input");
        const tagEl = document.getElementById("profile-tag");
        const statusSelect = document.getElementById("user-status-select");
        const pwdBox = document.getElementById("pwd-confirm-box");

        if (nameInput) nameInput.value = user.display_name || "";
        if (emailInput) {
            emailInput.value = user.email || "";
            emailInput.oninput = () => {
                pwdBox.style.display = (emailInput.value.trim().toLowerCase() !== (user.email || '').toLowerCase()) ? "block" : "none";
            };
        }
        if (pwdBox) pwdBox.style.display = "none";
        if (tagEl) tagEl.innerText = "@" + user.username;

        renderAvatarEl(document.getElementById("profile-avatar-preview"), user.display_name, user.avatar_url);
        
        const devBadge = document.getElementById("profile-dev-badge");
        if (devBadge) {
            devBadge.style.display = checkIsDev(user.username) ? "inline-flex" : "none";
        }
        if (statusSelect) {
            statusSelect.value = user.custom_status || "online";
        }

        document.getElementById("profile-modal").style.display = "flex";
    }

    async function saveProfileChanges() {
        const newName = document.getElementById("profile-name-input").value.trim();
        const newEmail = document.getElementById("profile-email-input").value.trim().toLowerCase();
        const pwd = document.getElementById("profile-pwd-input") ? document.getElementById("profile-pwd-input").value.trim() : "";
        const fileInput = document.getElementById("profile-file");

        if (!newName || !newEmail) {
            return alert("Заполните ник и email");
        }

        const res = await fetch("/api/update_profile", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ username: user.username, display_name: newName, email: newEmail, password: pwd })
        });
        const data = await res.json();
        if (data.status !== "ok") {
            return alert(data.message);
        }

        user.display_name = newName;
        user.email = newEmail;

        if (fileInput.files[0]) {
            const form = new FormData();
            form.append("username", user.username);
            form.append("file", fileInput.files[0]);
            const resAv = await fetch("/api/upload_avatar", { method: "POST", body: form });
            const dataAv = await resAv.json();
            if (dataAv.status === "ok") {
                user.avatar_url = dataAv.avatar_url;
            }
        }

        localStorage.setItem("messenger_user", JSON.stringify(user));
        location.reload();
    }

    async function changeMyStatus(st) {
        user.custom_status = st;
        localStorage.setItem("messenger_user", JSON.stringify(user));
        await fetch("/api/update_status", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ username: user.username, status: st })
        });
    }

    async function openPeerInfo(targetUsername = null) {
        const target = targetUsername || currentTarget;
        if (!target) return;

        if (target.startsWith("#")) {
            openChannelSettings();
            return;
        }

        const res = await fetch(`/api/user_info?username=${encodeURIComponent(target)}&current_user=${encodeURIComponent(user.username)}`);
        const data = await res.json();
        if (data.status === "ok") {
            viewingPeerInfo = data;
            document.getElementById("info-name").innerText = data.display_name;
            document.getElementById("info-tag").innerText = "@" + data.username;
            document.getElementById("info-date").innerText = "Регистрация: " + data.created_at;
            renderAvatarEl(document.getElementById("info-avatar"), data.display_name, data.avatar_url);
            
            const isOnline = onlineUsers.includes(data.username.toLowerCase());
            const stEl = document.getElementById("info-status-text");
            if (data.custom_status === "dnd") {
                stEl.innerText = "⛔ Не беспокоить";
                stEl.style.color = "#ef4444";
            } else if (isOnline && data.custom_status !== "offline") {
                stEl.innerText = "🟢 В сети";
                stEl.style.color = "var(--online-green)";
            } else {
                stEl.innerText = data.last_seen ? `Был(а) в сети в ${data.last_seen}` : "Был(а) недавно";
                stEl.style.color = "var(--text-sub)";
            }

            document.getElementById("info-dev-badge").style.display = data.is_dev ? "inline-flex" : "none";
            const btn = document.getElementById("info-block-btn");
            btn.innerText = data.is_blocked ? "Разблокировать" : "🚫 Заблокировать";
            btn.style.background = data.is_blocked ? "#22c55e" : "#ef4444";

            document.getElementById("user-info-modal").style.display = "flex";
        }
    }

    function openChannelSettings() {
        const chat = chatsList.find(c => c.key.toLowerCase() === currentTarget.toLowerCase());
        const isOwner = (currentChannelData && currentChannelData.creator.toLowerCase() === user.username.toLowerCase()) || is_dev(user.username);
        if (!isOwner) return;

        document.getElementById("edit-chan-name").value = chat ? chat.name : "";
        document.getElementById("edit-chan-desc").value = chat ? chat.desc : "";
        document.getElementById("edit-channel-modal").style.display = "flex";
    }

    async function addMemberSubmit() {
        const input = document.getElementById("add-member-input");
        const val = input.value.trim();
        if (!val) return;

        const res = await fetch("/api/add_channel_member", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ channel_tag: currentTarget, username: val, requester: user.username })
        });
        const data = await res.json();
        if (data.status === "ok") {
            alert(data.message);
            input.value = "";
        } else {
            alert(data.message);
        }
    }

    async function deleteChannel() {
        if (!confirm("Вы уверены, что хотите удалить этот канал навсегда? Это действие необратимо.")) return;
        const res = await fetch("/api/delete_channel", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ channel_tag: currentTarget, username: user.username })
        });
        const data = await res.json();
        if (data.status === "ok") {
            document.getElementById("edit-channel-modal").style.display = "none";
            localStorage.removeItem("messenger_target");
            currentTarget = "";
            document.getElementById("chat-content").style.display = "none";
            document.getElementById("chat-placeholder").style.display = "flex";
            await fetchUserChats();
        } else {
            alert(data.message);
        }
    }

    async function toggleChannelSubscription() {
        if (!currentTarget.startsWith("#") || !currentChannelData) return;
        const endpoint = currentChannelData.is_member ? "/api/leave_channel" : "/api/join_channel";
        const res = await fetch(endpoint, {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ channel_tag: currentTarget, username: user.username })
        });
        const data = await res.json();
        if (data.status === "ok") {
            await fetchUserChats();
            selectChat(currentTarget);
        } else {
            alert(data.message);
        }
    }

    async function saveChannelEdit() {
        const name = document.getElementById("edit-chan-name").value;
        const desc = document.getElementById("edit-chan-desc").value;
        const file = document.getElementById("edit-chan-file").files[0];

        const form = new FormData();
        form.append("tag", currentTarget.replace("#", ""));
        form.append("name", name);
        form.append("desc", desc);
        form.append("requester", user.username);
        if (file) form.append("file", file);

        const res = await fetch("/api/update_channel", { method: "POST", body: form });
        const data = await res.json();
        if (data.status === "ok") {
            document.getElementById("edit-channel-modal").style.display = "none";
            await fetchUserChats();
            selectChat(currentTarget);
        } else alert(data.message);
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
        searchQuery = (val || "").trim();
        searchOffset = 0;
        searchResultsList = [];

        if (!searchQuery) {
            renderSidebar();
            return;
        }
        searchTimeout = setTimeout(async () => {
            await fetchSearchResults();
        }, 150);
    }

    async function fetchSearchResults(isLoadMore = false) {
        try {
            const res = await fetch(`/api/search?q=${encodeURIComponent(searchQuery)}&current_user=${encodeURIComponent(user.username)}&offset=${searchOffset}&limit=12`);
            const data = await res.json();
            
            if (isLoadMore) {
                searchResultsList = searchResultsList.concat(data.results || []);
            } else {
                searchResultsList = data.results || [];
            }
            searchHasMore = data.has_more;
            renderSearchResults();
        } catch(e) {}
    }

    function loadMoreSearch() {
        searchOffset += 12;
        fetchSearchResults(true);
    }

    function renderSearchResults() {
        const list = document.getElementById("chat-list");
        list.innerHTML = `<div style="padding:10px 18px; font-size:0.75rem; color:var(--text-sub); display:flex; justify-content:space-between;">
            <span>РЕЗУЛЬТАТЫ ПОИСКА</span>
            <span>${searchResultsList.length} НАЙДЕНО</span>
        </div>`;
        
        if (searchResultsList.length === 0) {
            list.innerHTML += `<div style="padding:24px; text-align:center; color:var(--text-sub); font-size:0.85rem;">Ничего не найдено</div>`;
            return;
        }

        searchResultsList.forEach(item => {
            const div = document.createElement("div");
            div.className = "chat-item";
            const isUserDev = item.is_dev;
            
            div.onclick = () => {
                selectChat(item.key);
                document.getElementById("search-input").value = "";
            };

            const isMsg = item.type === "message";
            const iconBadge = isMsg ? "💬 " : (item.type === "channel" ? "📢 " : "");

            div.innerHTML = `
                <div class="avatar-wrap">
                    <div class="avatar-img" style="background:${getGradient(item.name)}">${item.avatar ? `<img src="${item.avatar}" style="width:100%;height:100%;border-radius:50%;object-fit:cover;">` : (isMsg ? '💬' : item.name[0].toUpperCase())}</div>
                </div>
                <div class="chat-details">
                    <div class="chat-name">
                        <span>${iconBadge}${highlightText(item.name, searchQuery)}</span>
                        ${isUserDev ? '<span class="dev-badge">DEV</span>' : ''}
                        <span style="font-size:0.78rem; color:var(--badge-blue); font-weight:normal;">${highlightText(item.tag, searchQuery)}</span>
                    </div>
                    <div class="chat-preview">${highlightText(item.extra, searchQuery)}</div>
                </div>
            `;
            list.appendChild(div);
        });

        if (searchHasMore) {
            const loadMore = document.createElement("div");
            loadMore.className = "load-more-btn";
            loadMore.innerText = "⬇️ Показать ещё результаты";
            loadMore.onclick = loadMoreSearch;
            list.appendChild(loadMore);
        }
    }

    function renderSidebar() {
        const list = document.getElementById("chat-list");
        list.innerHTML = "";
        
        if (chatsList.length === 0) {
            list.innerHTML = `<div style="padding:30px 20px; text-align:center; color:var(--text-sub); font-size:0.85rem;">Нет активных чатов.<br>Создайте канал ➕ или найдите контакт через поиск 🔍</div>`;
            return;
        }

        chatsList.forEach(chat => {
            const isOnline = onlineUsers.includes(chat.key.toLowerCase());
            const isPeerDev = checkIsDev(chat.key);
            const isDnd = chat.custom_status === "dnd";
            const isInv = chat.custom_status === "offline";

            const div = document.createElement("div");
            div.className = `chat-item ${currentTarget && currentTarget.toLowerCase() === chat.key.toLowerCase() ? 'active' : ''}`;
            div.onclick = () => selectChat(chat.key);

            let checkIcon = "";
            if (chat.last_sender && chat.last_sender.toLowerCase() === user.username.toLowerCase()) {
                checkIcon = chat.last_read ? '<span class="read-status">✓✓ </span>' : '<span style="opacity:0.6;">✓ </span>';
            }

            div.innerHTML = `
                <div class="avatar-wrap">
                    <div class="avatar-img" style="background:${getGradient(chat.name)}">${chat.avatar ? `<img src="${chat.avatar}" style="width:100%;height:100%;border-radius:50%;object-fit:cover;">` : chat.name[0].toUpperCase()}</div>
                    <div class="online-dot ${isDnd ? 'dnd' : (isOnline && !isInv ? 'visible' : '')}"></div>
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
                        <div class="chat-preview">${checkIcon}${chat.last_msg || (chat.key.startsWith('#') ? 'Канал' : 'Диалог')}</div>
                    </div>
                </div>
            `;
            list.appendChild(div);
        });
    }

    function updateHeaderSubtitle() {
        const sub = document.getElementById("header-sub");
        if (!currentTarget) return;
        if (currentTarget.startsWith("#")) {
            const chat = chatsList.find(c => c.key.toLowerCase() === currentTarget.toLowerCase());
            sub.innerText = (currentChannelData && currentChannelData.description) ? currentChannelData.description : (chat ? chat.desc || 'Канал' : 'Канал');
            return;
        }
        const isOnline = onlineUsers.includes(currentTarget.toLowerCase());
        const chat = chatsList.find(c => c.key.toLowerCase() === currentTarget.toLowerCase());
        if (chat && chat.custom_status === "dnd") {
            sub.innerText = "⛔ Не беспокоить";
            sub.style.color = "#ef4444";
        } else if (isOnline && (!chat || chat.custom_status !== "offline")) {
            sub.innerText = "в сети";
            sub.style.color = "var(--online-green)";
        } else {
            sub.innerText = chat && chat.last_seen ? `был(а) в ${chat.last_seen}` : "был(а) недавно";
            sub.style.color = "var(--text-sub)";
        }
    }

    async function selectChat(key) {
        if (!key) return;
        currentTarget = key;

        document.getElementById("chat-placeholder").style.display = "none";
        document.getElementById("chat-content").style.display = "flex";
        
        let chat = chatsList.find(c => c.key.toLowerCase() === key.toLowerCase());
        const name = chat ? chat.name : key;
        const avatar = chat ? chat.avatar : "";
        const isPeerDev = checkIsDev(key);

        document.getElementById("header-title").innerText = name;
        document.getElementById("header-dev-badge").style.display = isPeerDev ? "inline-flex" : "none";
        renderAvatarEl(document.getElementById("header-avatar"), name, avatar);

        document.body.classList.add("in-chat");
        if (!document.getElementById("search-input").value.trim()) renderSidebar();
        
        if (ws && ws.readyState === WebSocket.OPEN && !key.startsWith("#")) {
            ws.send(JSON.stringify({ action: "read", target: key }));
        }
        await loadHistory();
    }

    async function loadHistory() {
        if (!currentTarget) return;
        try {
            const res = await fetch(`/api/history?user=${encodeURIComponent(user.username)}&target=${encodeURIComponent(currentTarget)}`);
            const data = await res.json();
            
            currentMessages = data.messages || [];
            const isChannel = currentTarget.startsWith("#");

            const inputBar = document.getElementById("chat-input-bar");
            const restrictedBar = document.getElementById("channel-restricted-bar");
            const subBtn = document.getElementById("channel-action-btn");
            const settingsBtn = document.getElementById("channel-settings-btn");

            if (isChannel) {
                currentChannelData = data;
                const isOwner = (data.creator.toLowerCase() === user.username.toLowerCase()) || is_dev(user.username);
                
                settingsBtn.style.display = isOwner ? "flex" : "none";
                subBtn.style.display = "block";
                subBtn.className = `header-sub-btn ${data.is_member ? 'btn-leave' : 'btn-join'}`;
                subBtn.innerText = data.is_member ? "Покинуть" : "Подписаться";

                if (isOwner) {
                    inputBar.style.display = "flex";
                    restrictedBar.style.display = "none";
                } else {
                    inputBar.style.display = data.is_member ? "none" : "none";
                    restrictedBar.style.display = data.is_member ? "block" : "none";
                    if (!data.is_member) restrictedBar.innerText = "📢 Вы не подписаны на этот канал";
                    else restrictedBar.innerText = "📢 Только создатель канала может публиковать посты";
                }
            } else {
                currentChannelData = null;
                settingsBtn.style.display = "none";
                subBtn.style.display = "none";
                inputBar.style.display = "flex";
                restrictedBar.style.display = "none";
            }

            updateHeaderSubtitle();
            renderAllMessages();
        } catch(e) {}
    }

    function renderAllMessages() {
        const box = document.getElementById("messages");
        box.innerHTML = "";
        currentMessages.forEach(m => {
            const isMine = m.sender_username.toLowerCase() === user.username.toLowerCase();
            const row = document.createElement("div");
            row.className = `msg-row ${isMine ? 'mine' : 'theirs'}`;

            let contentHtml = "";
            if (m.forward_from) {
                contentHtml += `<div class="bubble-fwd">↪️ Переслано от @${escapeHtml(m.forward_from)}</div>`;
            }
            if (m.reply_to_text) {
                contentHtml += `<div class="bubble-reply"><span class="reply-user">@${escapeHtml(m.reply_to_sender)}</span>: ${escapeHtml(m.reply_to_text)}</div>`;
            }

            if (m.msg_type === "image") {
                contentHtml += `<img src="${m.file_url}" class="bubble-img" onclick="window.open('${m.file_url}', '_blank')">`;
            } else if (m.msg_type === "video") {
                contentHtml += `<video controls src="${m.file_url}" class="bubble-video"></video>`;
            } else if (m.msg_type === "voice") {
                contentHtml += `<div class="audio-player"><audio controls src="${m.file_url}"></audio></div>`;
            } else if (m.msg_type === "document") {
                contentHtml += `<a href="${m.file_url}" download="${m.file_name}" class="file-attachment">📄 ${escapeHtml(m.file_name || 'Скачать файл')}</a>`;
            }

            if (m.text) {
                contentHtml += `<div>${formatMentionsAndTags(m.text)}</div>`;
            }

            let reactionsHtml = "";
            if (m.reactions && Object.keys(m.reactions).length > 0) {
                reactionsHtml = `<div class="reactions-row">`;
                for (const [emo, data] of Object.entries(m.reactions)) {
                    const count = typeof data === 'object' ? data.count : data;
                    const usersList = typeof data === 'object' && data.users ? data.users.join(', ') : '';
                    reactionsHtml += `<div class="reaction-chip" title="Поставили: ${usersList}" onclick="event.stopPropagation(); toggleReaction(${m.id}, '${emo}')">${emo} ${count}</div>`;
                }
                reactionsHtml += `</div>`;
            }

            const isMsgDev = checkIsDev(m.sender_username);
            let checkIcon = "";
            if (isMine && !currentTarget.startsWith("#")) {
                checkIcon = m.is_read ? '<span class="read-status">✓✓</span>' : '<span style="opacity:0.6;">✓</span>';
            }

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
                        ${checkIcon}
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
        menu.style.left = `${Math.min(e.clientX, window.innerWidth - 190)}px`;
        menu.style.top = `${Math.min(e.clientY, window.innerHeight - 220)}px`;
        menu.style.display = "flex";

        document.getElementById("menu-edit-btn").style.display = isMine && selectedMsg.msg_type === "text" ? "flex" : "none";
        document.getElementById("menu-del-btn").style.display = isMine ? "flex" : "none";
        e.stopPropagation();
    }

    function sendReaction(emoji) {
        if (!selectedMsg || !ws || ws.readyState !== WebSocket.OPEN) return;
        ws.send(JSON.stringify({
            action: "reaction",
            msg_id: selectedMsg.id,
            emoji: emoji,
            target: currentTarget
        }));
        document.getElementById("msg-menu").style.display = "none";
    }

    function toggleReaction(msgId, emoji) {
        if (!ws || ws.readyState !== WebSocket.OPEN) return;
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
        forwardMsg = null;
        document.getElementById("action-title").innerText = `Ответ пользователю @${replyMsg.sender_username}`;
        document.getElementById("action-text").innerText = replyMsg.text || (replyMsg.msg_type === 'image' ? '📷 Фото' : (replyMsg.msg_type === 'video' ? '🎬 Видео' : '🎙️ Голосовое'));
        document.getElementById("action-banner").style.display = "flex";
        document.getElementById("msg-input").focus();
    }

    function startForward() {
        forwardMsg = selectedMsg;
        const box = document.getElementById("forward-chats-list");
        box.innerHTML = "";
        chatsList.forEach(c => {
            const div = document.createElement("div");
            div.className = "chat-item";
            div.style.padding = "8px 10px";
            div.style.borderRadius = "8px";
            div.onclick = () => sendForwardTo(c.key);
            div.innerHTML = `
                <div class="avatar-wrap" style="margin-right:10px;">
                    <div class="avatar-img" style="width:36px; height:36px;">${c.name[0].toUpperCase()}</div>
                </div>
                <div><b>${c.name}</b> <span style="font-size:0.75rem; color:var(--badge-blue);">${c.tag}</span></div>
            `;
            box.appendChild(div);
        });
        document.getElementById("forward-modal").style.display = "flex";
    }

    function sendForwardTo(targetKey) {
        if (!forwardMsg || !ws || ws.readyState !== WebSocket.OPEN) return;
        const timeStr = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        const payload = {
            action: "send",
            sender_username: user.username,
            sender_name: user.display_name,
            target: targetKey,
            text: forwardMsg.text,
            msg_type: forwardMsg.msg_type,
            file_url: forwardMsg.file_url,
            file_name: forwardMsg.file_name,
            forward_from: forwardMsg.sender_username,
            avatar: user.avatar_url || "",
            time: timeStr
        };
        ws.send(JSON.stringify(payload));
        document.getElementById("forward-modal").style.display = "none";
        selectChat(targetKey);
    }

    function startEdit() {
        editMsg = selectedMsg;
        replyMsg = null;
        forwardMsg = null;
        document.getElementById("action-title").innerText = "Редактирование сообщения";
        document.getElementById("action-text").innerText = editMsg.text;
        document.getElementById("action-banner").style.display = "flex";
        document.getElementById("msg-input").value = editMsg.text;
        document.getElementById("msg-input").focus();
    }

    function cancelAction() {
        replyMsg = null;
        editMsg = null;
        forwardMsg = null;
        document.getElementById("action-banner").style.display = "none";
        document.getElementById("msg-input").value = "";
    }

    function deleteMessage() {
        if (!selectedMsg || !ws || ws.readyState !== WebSocket.OPEN) return;
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
        if (ws && ws.readyState === WebSocket.OPEN && currentTarget && !currentTarget.startsWith("#")) {
            ws.send(JSON.stringify({ action: "typing", target: currentTarget }));
        }
    }

    async function handleFileUpload(file) {
        if (!file || !currentTarget) return;
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
                reply_to_id: replyMsg ? replyMsg.id : 0,
                reply_to_text: replyMsg ? (replyMsg.text || "Медиа") : "",
                reply_to_sender: replyMsg ? replyMsg.sender_username : "",
                avatar: user.avatar_url || "",
                time: timeStr
            };
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify(payload));
            }
            cancelAction();
        }
        document.getElementById("media-file-input").value = "";
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
                btn.title = "Нажмите для отправки";
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
        if (!text || !ws || ws.readyState !== WebSocket.OPEN || !currentTarget) return;

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
            file_url: "",
            file_name: "",
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
        return String(str).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
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
    uname = username.strip().lower()
    await manager.connect(uname, websocket)
    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            action = data.get("action", "send")
            target = data.get("target", "").strip()

            if action == "typing":
                await manager.send_to_user({"type": "typing", "sender": uname}, target)
                continue

            elif action == "read":
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("UPDATE messages SET is_read = 1 WHERE LOWER(sender_username) = LOWER(?) AND LOWER(target) = LOWER(?)", (target.lower(), uname))
                    await db.commit()
                await manager.send_to_user({"type": "read_receipt", "reader": uname}, target)
                continue

            elif action == "reaction":
                msg_id = data.get("msg_id")
                emoji = data.get("emoji")
                async with aiosqlite.connect(DB_PATH) as db:
                    cur = await db.execute("SELECT reactions FROM messages WHERE id = ?", (msg_id,))
                    row = await cur.fetchone()
                    if row:
                        reacts = json.loads(row[0] or "{}")
                        if emoji not in reacts:
                            reacts[emoji] = {"count": 1, "users": [uname]}
                        else:
                            if isinstance(reacts[emoji], int):
                                reacts[emoji] = {"count": reacts[emoji] + 1, "users": [uname]}
                            else:
                                if uname in reacts[emoji]["users"]:
                                    reacts[emoji]["users"].remove(uname)
                                    reacts[emoji]["count"] = max(0, reacts[emoji]["count"] - 1)
                                    if reacts[emoji]["count"] == 0:
                                        del reacts[emoji]
                                else:
                                    reacts[emoji]["users"].append(uname)
                                    reacts[emoji]["count"] += 1

                        await db.execute("UPDATE messages SET reactions = ? WHERE id = ?", (json.dumps(reacts), msg_id))
                        await db.commit()
                
                payload = {"type": "reaction", "msg_id": msg_id, "target": target}
                if target.startswith("#"):
                    await manager.broadcast_channel(payload)
                else:
                    await manager.send_to_user(payload, target)
                    await manager.send_to_user(payload, uname)
                continue

            elif action == "edit":
                msg_id = data.get("msg_id")
                new_text = data.get("text")
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("UPDATE messages SET text = ?, is_edited = 1 WHERE id = ? AND LOWER(sender_username) = LOWER(?)", (new_text, msg_id, uname))
                    await db.commit()
                
                payload = {"type": "edit_msg", "msg_id": msg_id, "text": new_text, "target": target}
                if target.startswith("#"):
                    await manager.broadcast_channel(payload)
                else:
                    await manager.send_to_user(payload, target)
                    await manager.send_to_user(payload, uname)
                continue

            elif action == "delete":
                msg_id = data.get("msg_id")
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("UPDATE messages SET is_deleted = 1 WHERE id = ? AND LOWER(sender_username) = LOWER(?)", (msg_id, uname))
                    await db.commit()
                
                payload = {"type": "delete_msg", "msg_id": msg_id, "target": target}
                if target.startswith("#"):
                    await manager.broadcast_channel(payload)
                else:
                    await manager.send_to_user(payload, target)
                    await manager.send_to_user(payload, uname)
                continue

            # Отправка нового сообщения
            text = data.get("text", "")
            msg_type = data.get("msg_type", "text")
            file_url = data.get("file_url", "")
            file_name = data.get("file_name", "")
            reply_id = data.get("reply_to_id", 0)
            reply_text = data.get("reply_to_text", "")
            reply_sender = data.get("reply_to_sender", "")
            forward_from = data.get("forward_from", "")
            time_str = datetime.now().strftime("%H:%M")

            if target.startswith("#"):
                clean_tag = target.lstrip("#").lower()
                async with aiosqlite.connect(DB_PATH) as db:
                    cur_c = await db.execute("SELECT creator_username FROM channels WHERE LOWER(channel_tag) = LOWER(?)", (clean_tag,))
                    c_row = await cur_c.fetchone()
                    if not c_row or (c_row[0].lower() != uname and not is_dev(uname)):
                        continue

            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute(
                    """INSERT INTO messages (sender_username, sender_name, target, text, msg_type, file_url, file_name, 
                                             reply_to_id, reply_to_text, reply_to_sender, forward_from, timestamp, avatar_url) 
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (uname, data.get("sender_name", uname), target, text, msg_type, file_url, file_name, 
                     reply_id, reply_text, reply_sender, forward_from, time_str, data.get("avatar", ""))
                )
                await db.commit()
                msg_id = cur.lastrowid

            msg_out = {
                "type": "msg",
                "id": msg_id,
                "sender_username": uname,
                "sender_name": data.get("sender_name", uname),
                "target": target,
                "text": text,
                "msg_type": msg_type,
                "file_url": file_url,
                "file_name": file_name,
                "reply_to_id": reply_id,
                "reply_to_text": reply_text,
                "reply_to_sender": reply_sender,
                "forward_from": forward_from,
                "reactions": {},
                "is_read": False,
                "is_edited": False,
                "time": time_str,
                "avatar": data.get("avatar", ""),
                "is_dev": is_dev(uname)
            }

            if target.startswith("#"):
                await manager.broadcast_channel(msg_out)
            else:
                async with aiosqlite.connect(DB_PATH) as db:
                    cur_block = await db.execute("SELECT id FROM blocks WHERE LOWER(blocker) = LOWER(?) AND LOWER(blocked) = LOWER(?)", (target.lower(), uname))
                    is_target_blocked = bool(await cur_block.fetchone())
                
                if not is_target_blocked:
                    await manager.send_to_user(msg_out, recipient_username=target)
                await manager.send_to_user(msg_out, recipient_username=uname)

    except WebSocketDisconnect:
        await manager.disconnect(uname)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=80)
