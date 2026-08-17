import json
import os
import hashlib
import aiosqlite
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

app = FastAPI(title="Dark Messenger")

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
                nickname TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        # Таблица сообщений
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender TEXT NOT NULL,
                target TEXT NOT NULL,
                text TEXT NOT NULL,
                timestamp TEXT NOT NULL
            )
        """)
        await db.commit()

class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, WebSocket] = {}

    async def connect(self, nickname: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[nickname] = websocket
        await self.broadcast_users()

    async def disconnect(self, nickname: str):
        if nickname in self.active_connections:
            del self.active_connections[nickname]
        await self.broadcast_users()

    async def broadcast_users(self):
        users = list(self.active_connections.keys())
        payload = json.dumps({"type": "users", "list": users})
        for conn in self.active_connections.values():
            try:
                await conn.send_text(payload)
            except Exception:
                pass

    async def send_personal_message(self, message: dict, recipient: str):
        if recipient in self.active_connections:
            await self.active_connections[recipient].send_text(json.dumps(message))

    async def broadcast(self, message: dict, sender: str):
        for name, conn in self.active_connections.items():
            if name != sender:
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
    nickname: str
    password: str

class LoginModel(BaseModel):
    email: str
    password: str

@app.post("/api/register")
async def register(data: RegisterModel):
    email = data.email.strip().lower()
    nickname = data.nickname.strip()
    password = data.password.strip()

    if not email or not nickname or not password:
        return {"status": "error", "message": "Заполните все поля!"}
    if len(password) < 4:
        return {"status": "error", "message": "Пароль должен быть от 4 символов"}

    async with aiosqlite.connect(DB_PATH) as db:
        # Проверяем уникальность email
        cursor = await db.execute("SELECT id FROM users WHERE email = ?", (email,))
        if await cursor.fetchone():
            return {"status": "error", "message": "Почта уже зарегистрирована"}

        # Проверяем уникальность никнейма
        cursor = await db.execute("SELECT id FROM users WHERE nickname = ?", (nickname,))
        if await cursor.fetchone():
            return {"status": "error", "message": "Никнейм уже занят"}

        pwd_hash = hash_password(password)
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        await db.execute(
            "INSERT INTO users (email, nickname, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (email, nickname, pwd_hash, created_at)
        )
        await db.commit()
        return {"status": "ok", "nickname": nickname, "email": email}

@app.post("/api/login")
async def login(data: LoginModel):
    email = data.email.strip().lower()
    password = data.password.strip()
    pwd_hash = hash_password(password)

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT nickname, email FROM users WHERE email = ? AND password_hash = ?",
            (email, pwd_hash)
        )
        user = await cursor.fetchone()
        if user:
            return {"status": "ok", "nickname": user[0], "email": user[1]}
        return {"status": "error", "message": "Неверная почта или пароль"}

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Чаты</title>
    <style>
        :root {
            --bg-main: #0e1015;
            --bg-sidebar: #13151b;
            --bg-sidebar-hover: #1b1e27;
            --bg-chat: #0e1015;
            --bg-header: #13151b;
            --bg-input: #181b22;
            --bubble-in: #1f222c;
            --bubble-out: #2b77f7;
            --text-main: #ffffff;
            --text-sub: #7a8293;
            --badge-blue: #2b77f7;
            --online-green: #22c55e;
            --border-color: #1a1d26;
            --danger-red: #ef4444;
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            -webkit-tap-highlight-color: transparent;
        }

        body, html {
            height: 100%;
            background-color: var(--bg-main);
            color: var(--text-main);
            overflow: hidden;
        }

        /* Модалка входа / регистрации */
        #auth-modal {
            position: fixed;
            inset: 0;
            background: rgba(8, 10, 14, 0.94);
            backdrop-filter: blur(10px);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 999;
        }
        .auth-card {
            background: #161821;
            padding: 30px 26px;
            border-radius: 18px;
            width: 90%;
            max-width: 380px;
            border: 1px solid #232734;
            box-shadow: 0 20px 40px rgba(0,0,0,0.6);
        }
        .auth-card h2 { font-size: 1.4rem; font-weight: 700; margin-bottom: 6px; text-align: center; }
        .auth-card p.subtitle { font-size: 0.85rem; color: var(--text-sub); margin-bottom: 20px; text-align: center; }
        
        .auth-card input {
            width: 100%;
            padding: 13px 15px;
            margin-bottom: 12px;
            background: var(--bg-input);
            border: 1px solid #262b3a;
            color: #fff;
            border-radius: 12px;
            outline: none;
            font-size: 0.95rem;
            transition: 0.2s;
        }
        .auth-card input:focus { border-color: var(--badge-blue); }
        
        .auth-card button.submit-btn {
            width: 100%;
            padding: 13px;
            background: var(--badge-blue);
            color: white;
            border: none;
            border-radius: 12px;
            font-weight: 600;
            font-size: 0.98rem;
            cursor: pointer;
            transition: 0.2s;
            margin-top: 6px;
        }
        .auth-card button.submit-btn:hover { opacity: 0.9; }

        .auth-toggle {
            display: flex;
            justify-content: center;
            gap: 6px;
            margin-top: 18px;
            font-size: 0.85rem;
            color: var(--text-sub);
        }
        .auth-toggle span {
            color: var(--badge-blue);
            cursor: pointer;
            font-weight: 600;
        }
        .auth-error {
            color: var(--danger-red);
            font-size: 0.82rem;
            margin-bottom: 12px;
            text-align: center;
            display: none;
        }

        /* Контейнер приложения */
        #app-container {
            display: flex;
            height: 100%;
            width: 100%;
        }

        /* Левая панель - Чаты */
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
            padding: 18px 20px 14px 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border-color);
        }
        .sidebar-header .title {
            font-size: 1.35rem;
            font-weight: 700;
            letter-spacing: -0.3px;
        }
        .user-pill {
            font-size: 0.78rem;
            color: var(--text-sub);
            background: var(--bg-input);
            padding: 4px 10px;
            border-radius: 20px;
            display: flex;
            align-items: center;
            gap: 6px;
            cursor: pointer;
        }
        .user-pill:hover { color: #fff; }

        .chat-list {
            flex: 1;
            overflow-y: auto;
        }

        .chat-item {
            display: flex;
            align-items: center;
            padding: 14px 18px;
            cursor: pointer;
            transition: background 0.15s;
        }
        .chat-item:hover { background: var(--bg-sidebar-hover); }
        .chat-item.active { background: #1b1e28; }

        .avatar-wrap {
            position: relative;
            margin-right: 14px;
            flex-shrink: 0;
        }
        .avatar {
            width: 48px;
            height: 48px;
            border-radius: 50%;
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
            bottom: 1px;
            right: 1px;
            width: 13px;
            height: 13px;
            border-radius: 50%;
            background: var(--online-green);
            border: 2.5px solid var(--bg-sidebar);
        }

        .chat-details {
            flex: 1;
            min-width: 0;
        }
        .chat-top-row {
            display: flex;
            justify-content: space-between;
            align-items: baseline;
            margin-bottom: 4px;
        }
        .chat-name {
            font-weight: 600;
            font-size: 0.98rem;
            color: #ffffff;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .chat-time {
            font-size: 0.75rem;
            color: var(--text-sub);
            margin-left: 8px;
            flex-shrink: 0;
        }
        .chat-bottom-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .chat-preview {
            font-size: 0.84rem;
            color: var(--text-sub);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            line-height: 1.3;
        }
        .unread-badge {
            background: var(--badge-blue);
            color: white;
            font-size: 0.72rem;
            font-weight: 700;
            padding: 2px 7px;
            border-radius: 10px;
            margin-left: 8px;
            flex-shrink: 0;
        }

        /* Правая часть - Чат */
        #chat-area {
            flex: 1;
            display: flex;
            flex-direction: column;
            background: var(--bg-chat);
            height: 100%;
            position: relative;
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
            font-size: 1.5rem;
            margin-right: 14px;
            cursor: pointer;
        }

        .header-avatar {
            width: 42px;
            height: 42px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: bold;
            margin-right: 12px;
            flex-shrink: 0;
        }

        .header-info .name {
            font-weight: 600;
            font-size: 1rem;
            color: #fff;
        }
        .header-info .action-link {
            font-size: 0.8rem;
            color: var(--badge-blue);
            margin-top: 1px;
        }

        /* Сообщения */
        .messages-container {
            flex: 1;
            padding: 24px 28px;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            gap: 8px;
        }

        .msg-row {
            display: flex;
            width: 100%;
        }
        .msg-row.mine { justify-content: flex-end; }

        .bubble {
            max-width: 65%;
            padding: 10px 14px 8px 14px;
            border-radius: 12px;
            font-size: 0.92rem;
            line-height: 1.45;
            word-break: break-word;
            position: relative;
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

        .bubble-footer {
            display: flex;
            justify-content: flex-end;
            align-items: center;
            margin-top: 4px;
        }
        .bubble-time {
            font-size: 0.68rem;
            color: rgba(255, 255, 255, 0.55);
            margin-left: 8px;
        }

        /* Панель ввода */
        .input-bar {
            padding: 14px 20px;
            background: var(--bg-header);
            border-top: 1px solid var(--border-color);
            display: flex;
            align-items: center;
            gap: 12px;
            flex-shrink: 0;
        }

        .input-wrapper {
            flex: 1;
            display: flex;
            align-items: center;
            background: var(--bg-input);
            border: 1px solid #232734;
            border-radius: 12px;
            padding: 0 14px;
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
        .input-wrapper input::placeholder { color: var(--text-sub); }

        .attach-btn {
            background: none;
            border: none;
            color: var(--text-sub);
            cursor: pointer;
            font-size: 1.25rem;
            padding: 4px;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: color 0.2s;
        }
        .attach-btn:hover { color: #fff; }

        .send-btn {
            background: var(--badge-blue);
            border: none;
            color: white;
            width: 44px;
            height: 44px;
            border-radius: 12px;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.1rem;
            flex-shrink: 0;
            transition: opacity 0.2s;
        }
        .send-btn:hover { opacity: 0.9; }

        /* Мобильный вид */
        @media (max-width: 768px) {
            #sidebar { width: 100%; border-right: none; }
            #chat-area { display: none; width: 100%; }
            body.in-chat #sidebar { display: none; }
            body.in-chat #chat-area { display: flex; }
            .back-btn { display: block; }
            .messages-container { padding: 16px 14px; }
            .bubble { max-width: 82%; }
        }
    </style>
</head>
<body>

<!-- Модалка авторизации -->
<div id="auth-modal">
    <div class="auth-card">
        <h2 id="auth-title">Вход</h2>
        <p class="subtitle" id="auth-subtitle">Войдите со своим email</p>
        
        <div class="auth-error" id="auth-error"></div>
        
        <input type="email" id="auth-email" placeholder="Ваш Email (например user@mail.ru)" autofocus>
        <input type="text" id="auth-nick" placeholder="Ваш Никнейм" style="display: none;">
        <input type="password" id="auth-pwd" placeholder="Пароль">
        
        <button class="submit-btn" id="auth-btn" onclick="submitAuth()">Войти</button>
        
        <div class="auth-toggle">
            <span id="toggle-text">Нет аккаунта? Зарегистрироваться</span>
        </div>
    </div>
</div>

<div id="app-container">
    <div id="sidebar">
        <div class="sidebar-header">
            <div class="title">Чаты</div>
            <div class="user-pill" onclick="logout()" title="Нажмите, чтобы выйти">
                <span id="my-nick-badge">Пользователь</span> 🚪
            </div>
        </div>
        <div class="chat-list" id="chat-list"></div>
    </div>

    <div id="chat-area">
        <div class="chat-header">
            <button class="back-btn" onclick="closeChatMobile()">←</button>
            <div class="header-avatar" id="header-avatar">?</div>
            <div class="header-info">
                <div class="name" id="header-title">Выберите диалог</div>
                <div class="action-link" id="header-sub">в сети</div>
            </div>
        </div>

        <div class="messages-container" id="messages"></div>

        <div class="input-bar">
            <div class="input-wrapper">
                <input type="text" id="msg-input" placeholder="Введите сообщение" onkeydown="if(event.key==='Enter') sendMsg()">
                <button class="attach-btn" title="Прикрепить" onclick="alert('Вложения будут в следующем апдейте!')">📎</button>
            </div>
            <button class="send-btn" onclick="sendMsg()">➤</button>
        </div>
    </div>
</div>

<script>
    let isRegisterMode = false;
    let myNick = localStorage.getItem("chat_nick") || "";
    let myEmail = localStorage.getItem("chat_email") || "";
    let currentChat = "Общий чат";
    let ws = null;
    let chatsData = { "Общий чат": [] };
    let unreadCounts = {};

    const avatarGradients = [
        "linear-gradient(135deg, #f59e0b, #d97706)",
        "linear-gradient(135deg, #3b82f6, #1d4ed8)",
        "linear-gradient(135deg, #10b981, #047857)",
        "linear-gradient(135deg, #8b5cf6, #6d28d9)",
        "linear-gradient(135deg, #ec4899, #be185d)"
    ];

    function getGradient(name) {
        let hash = 0;
        for (let i = 0; i < name.length; i++) hash += name.charCodeAt(i);
        return avatarGradients[hash % avatarGradients.length];
    }

    // Переключение Регистрация / Логин
    document.getElementById("toggle-text").addEventListener("click", () => {
        isRegisterMode = !isRegisterMode;
        document.getElementById("auth-error").style.display = "none";
        if (isRegisterMode) {
            document.getElementById("auth-title").innerText = "Регистрация";
            document.getElementById("auth-subtitle").innerText = "Создайте аккаунт в мессенджере";
            document.getElementById("auth-nick").style.display = "block";
            document.getElementById("auth-btn").innerText = "Зарегистрироваться";
            document.getElementById("toggle-text").innerText = "Уже есть аккаунт? Войти";
        } else {
            document.getElementById("auth-title").innerText = "Вход";
            document.getElementById("auth-subtitle").innerText = "Войдите со своим email";
            document.getElementById("auth-nick").style.display = "none";
            document.getElementById("auth-btn").innerText = "Войти";
            document.getElementById("toggle-text").innerText = "Нет аккаунта? Зарегистрироваться";
        }
    });

    window.onload = () => {
        if (myNick && myEmail) {
            document.getElementById("auth-modal").style.display = "none";
            initApp(myNick);
        }
    };

    async function submitAuth() {
        const email = document.getElementById("auth-email").value.trim();
        const pwd = document.getElementById("auth-pwd").value.trim();
        const nick = document.getElementById("auth-nick").value.trim();
        const errBox = document.getElementById("auth-error");

        errBox.style.display = "none";

        const url = isRegisterMode ? "/api/register" : "/api/login";
        const payload = isRegisterMode ? { email, nickname: nick, password: pwd } : { email, password: pwd };

        try {
            const res = await fetch(url, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            });
            const data = await res.json();

            if (data.status === "ok") {
                myNick = data.nickname;
                myEmail = data.email;
                localStorage.setItem("chat_nick", myNick);
                localStorage.setItem("chat_email", myEmail);
                document.getElementById("auth-modal").style.display = "none";
                initApp(myNick);
            } else {
                errBox.innerText = data.message;
                errBox.style.display = "block";
            }
        } catch (e) {
            errBox.innerText = "Ошибка связи с сервером";
            errBox.style.display = "block";
        }
    }

    function logout() {
        if (confirm("Выйти из аккаунта?")) {
            localStorage.removeItem("chat_nick");
            localStorage.removeItem("chat_email");
            location.reload();
        }
    }

    function initApp(nickname) {
        document.getElementById("my-nick-badge").innerText = nickname;

        const loc = window.location;
        const wsProtocol = loc.protocol === "https:" ? "wss:" : "ws:";
        ws = new WebSocket(`${wsProtocol}//${loc.host}/ws/${encodeURIComponent(nickname)}`);

        ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            if (data.type === "users") {
                data.list.forEach(u => {
                    if (u !== myNick && !chatsData[u]) {
                        chatsData[u] = [];
                        unreadCounts[u] = 0;
                    }
                });
                renderSidebar();
            } else if (data.type === "msg") {
                const targetChat = data.target === "Общий чат" ? "Общий чат" : data.sender;
                if (!chatsData[targetChat]) chatsData[targetChat] = [];
                chatsData[targetChat].push({ text: data.text, mine: false, sender: data.sender, time: data.time });
                
                if (currentChat === targetChat) {
                    renderMessages();
                } else {
                    unreadCounts[targetChat] = (unreadCounts[targetChat] || 0) + 1;
                }
                renderSidebar();
            }
        };

        selectChat(currentChat);
    }

    async function loadHistory(chatName) {
        try {
            const res = await fetch(`/api/history?user1=${encodeURIComponent(myNick)}&user2=${encodeURIComponent(chatName)}`);
            const history = await res.json();
            chatsData[chatName] = history.map(m => ({
                text: m.text,
                mine: m.sender === myNick,
                sender: m.sender,
                time: m.time
            }));
            renderMessages();
            renderSidebar();
        } catch(e) {}
    }

    function renderSidebar() {
        const list = document.getElementById("chat-list");
        list.innerHTML = "";
        
        for (const name in chatsData) {
            const messages = chatsData[name] || [];
            const lastMsg = messages.length > 0 ? messages[messages.length - 1] : null;
            const unread = unreadCounts[name] || 0;

            const item = document.createElement("div");
            item.className = `chat-item ${name === currentChat ? 'active' : ''}`;
            item.onclick = () => selectChat(name);
            
            const previewText = lastMsg ? (lastMsg.mine ? `Вы: ${lastMsg.text}` : lastMsg.text) : "Нет сообщений";
            const timeText = lastMsg ? lastMsg.time : "";

            item.innerHTML = `
                <div class="avatar-wrap">
                    <div class="avatar" style="background: ${getGradient(name)}">${name[0].toUpperCase()}</div>
                    <div class="online-dot"></div>
                </div>
                <div class="chat-details">
                    <div class="chat-top-row">
                        <div class="chat-name">${name}</div>
                        <div class="chat-time">${timeText}</div>
                    </div>
                    <div class="chat-bottom-row">
                        <div class="chat-preview">${previewText}</div>
                        ${unread > 0 ? `<div class="unread-badge">${unread}</div>` : ''}
                    </div>
                </div>
            `;
            list.appendChild(item);
        }
    }

    function selectChat(name) {
        currentChat = name;
        unreadCounts[name] = 0;
        
        document.getElementById("header-title").innerText = name;
        document.getElementById("header-sub").innerText = name === "Общий чат" ? "Общая комната" : "в сети";
        const hAvatar = document.getElementById("header-avatar");
        hAvatar.innerText = name[0].toUpperCase();
        hAvatar.style.background = getGradient(name);

        document.body.classList.add("in-chat");
        renderSidebar();
        loadHistory(name);
    }

    function closeChatMobile() {
        document.body.classList.remove("in-chat");
    }

    function renderMessages() {
        const box = document.getElementById("messages");
        box.innerHTML = "";
        const history = chatsData[currentChat] || [];
        
        history.forEach(m => {
            const row = document.createElement("div");
            row.className = `msg-row ${m.mine ? 'mine' : 'theirs'}`;
            
            row.innerHTML = `
                <div class="bubble">
                    ${!m.mine && currentChat === "Общий чат" ? `<div style="font-size:0.75rem; color:#60a5fa; font-weight:600; margin-bottom:3px;">${m.sender}</div>` : ''}
                    <div>${escapeHtml(m.text)}</div>
                    <div class="bubble-footer">
                        <span class="bubble-time">${m.time}</span>
                    </div>
                </div>
            `;
            box.appendChild(row);
        });
        box.scrollTop = box.scrollHeight;
    }

    function sendMsg() {
        const input = document.getElementById("msg-input");
        const text = input.value.trim();
        if (!text || !ws) return;

        const timeStr = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        chatsData[currentChat].push({ text: text, mine: true, sender: myNick, time: timeStr });
        renderMessages();
        renderSidebar();

        ws.send(JSON.stringify({ target: currentChat, text: text }));
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

@app.get("/api/history")
async def get_history(user1: str, user2: str):
    async with aiosqlite.connect(DB_PATH) as db:
        if user2 == "Общий чат":
            cursor = await db.execute(
                "SELECT sender, target, text, timestamp FROM messages WHERE target = 'Общий чат' ORDER BY id ASC LIMIT 200"
            )
        else:
            cursor = await db.execute(
                """SELECT sender, target, text, timestamp FROM messages 
                   WHERE (sender = ? AND target = ?) OR (sender = ? AND target = ?) 
                   ORDER BY id ASC LIMIT 200""",
                (user1, user2, user2, user1)
            )
        rows = await cursor.fetchall()
        return [{"sender": r[0], "target": r[1], "text": r[2], "time": r[3]} for r in rows]

@app.websocket("/ws/{nickname}")
async def websocket_endpoint(websocket: WebSocket, nickname: str):
    await manager.connect(nickname, websocket)
    try:
        while True:
            data_raw = await websocket.receive_text()
            data = json.loads(data_raw)
            target = data.get("target")
            text = data.get("text")
            time_str = datetime.now().strftime("%H:%M")

            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "INSERT INTO messages (sender, target, text, timestamp) VALUES (?, ?, ?, ?)",
                    (nickname, target, text, time_str)
                )
                await db.commit()

            msg_payload = {
                "type": "msg",
                "sender": nickname,
                "target": target,
                "text": text,
                "time": time_str
            }

            if target == "Общий чат":
                await manager.broadcast(msg_payload, sender=nickname)
            else:
                await manager.send_personal_message(msg_payload, recipient=target)

    except WebSocketDisconnect:
        await manager.disconnect(nickname)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 80))
    uvicorn.run(app, host="0.0.0.0", port=port)
