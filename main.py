import json
import os
import aiosqlite
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

app = FastAPI(title="Cloud Messenger")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = "messenger.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
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

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Messenger</title>
    <style>
        :root {
            --bg-dark: #0f1115;
            --bg-chat: #16181d;
            --bg-header: #1c1f26;
            --bg-input: #232730;
            --bg-bubble-in: #2a2f3a;
            --bg-bubble-out: #2563eb;
            --accent: #3b82f6;
            --text-white: #f3f4f6;
            --text-muted: #9ca3af;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
        body, html { height: 100%; background: var(--bg-dark); color: var(--text-white); overflow: hidden; }

        #login-modal {
            position: fixed; inset: 0; background: rgba(0,0,0,0.85);
            display: flex; align-items: center; justify-content: center; z-index: 100;
        }
        .login-card {
            background: var(--bg-header); padding: 28px; border-radius: 16px;
            width: 90%; max-width: 320px; text-align: center; box-shadow: 0 10px 25px rgba(0,0,0,0.5);
        }
        .login-card h2 { margin-bottom: 8px; font-size: 1.4rem; }
        .login-card p { font-size: 0.85rem; color: var(--text-muted); margin-bottom: 20px; }
        .login-card input {
            width: 100%; padding: 14px; margin-bottom: 16px; background: var(--bg-input);
            border: 1px solid #374151; color: white; border-radius: 10px; outline: none; font-size: 1rem;
        }
        .login-card button {
            width: 100%; padding: 14px; background: var(--accent); color: white;
            border: none; border-radius: 10px; font-weight: bold; font-size: 1rem; cursor: pointer;
        }

        #app-container { display: flex; height: 100%; width: 100%; }

        #sidebar {
            width: 320px; background: var(--bg-dark); border-right: 1px solid #232730;
            display: flex; flex-direction: column; height: 100%;
        }
        .sidebar-header { padding: 20px 16px; font-size: 1.3rem; font-weight: bold; }
        .chat-list { flex: 1; overflow-y: auto; }
        .chat-item {
            display: flex; align-items: center; padding: 14px 16px;
            cursor: pointer; border-bottom: 1px solid #1a1c22;
        }
        .chat-item.active { background: #1c202a; border-left: 3px solid var(--accent); }
        .avatar {
            width: 44px; height: 44px; border-radius: 50%; background: var(--accent);
            display: flex; align-items: center; justify-content: center;
            font-weight: bold; font-size: 1.1rem; margin-right: 12px; flex-shrink: 0;
        }
        .chat-info { flex: 1; min-width: 0; }
        .chat-name { font-weight: 600; font-size: 1rem; }
        .chat-sub { font-size: 0.8rem; color: var(--text-muted); }

        #chat-area { flex: 1; display: flex; flex-direction: column; background: var(--bg-chat); height: 100%; }
        .chat-header {
            height: 64px; background: var(--bg-header); display: flex;
            align-items: center; padding: 0 16px; border-bottom: 1px solid #232730;
        }
        .back-btn {
            display: none; background: none; border: none; color: white;
            font-size: 1.5rem; margin-right: 14px; cursor: pointer;
        }
        .messages-container {
            flex: 1; padding: 16px; overflow-y: auto; display: flex; flex-direction: column; gap: 10px;
        }

        .bubble-row { display: flex; width: 100%; }
        .bubble-row.mine { justify-content: flex-end; }
        .bubble {
            max-width: 75%; padding: 10px 14px; border-radius: 14px;
            background: var(--bg-bubble-in); word-break: break-word; font-size: 0.95rem; line-height: 1.4;
        }
        .bubble-row.mine .bubble { background: var(--bg-bubble-out); }
        .sender-name { font-size: 0.75rem; color: #60a5fa; font-weight: bold; margin-bottom: 2px; }
        .msg-time { font-size: 0.65rem; color: rgba(255,255,255,0.6); text-align: right; margin-top: 4px; }

        .input-bar {
            padding: 12px 16px; background: var(--bg-header); display: flex; gap: 10px; align-items: center;
        }
        .input-bar input {
            flex: 1; padding: 12px 18px; background: var(--bg-input);
            border: 1px solid #374151; border-radius: 24px; color: white; outline: none; font-size: 1rem;
        }
        .input-bar button {
            width: 44px; height: 44px; border-radius: 50%; background: var(--accent);
            border: none; color: white; font-size: 1.2rem; cursor: pointer; display: flex;
            align-items: center; justify-content: center; flex-shrink: 0;
        }

        @media (max-width: 700px) {
            #sidebar { width: 100%; }
            #chat-area { display: none; width: 100%; }
            body.in-chat #sidebar { display: none; }
            body.in-chat #chat-area { display: flex; }
            .back-btn { display: block; }
        }
    </style>
</head>
<body>

<div id="login-modal">
    <div class="login-card">
        <h2>Вход в сеть</h2>
        <p>Введите ваш постоянный никнейм</p>
        <input type="text" id="nick-input" placeholder="Никнейм..." autofocus>
        <button onclick="login()">Подключиться</button>
    </div>
</div>

<div id="app-container">
    <div id="sidebar">
        <div class="sidebar-header">Чаты</div>
        <div class="chat-list" id="chat-list"></div>
    </div>

    <div id="chat-area">
        <div class="chat-header">
            <button class="back-btn" onclick="closeChatMobile()">←</button>
            <div>
                <div id="header-title" style="font-weight: bold; font-size: 1.1rem;">Общий чат</div>
                <div id="header-sub" style="font-size: 0.75rem; color: var(--text-muted);">Канал связи</div>
            </div>
        </div>
        <div class="messages-container" id="messages"></div>
        <div class="input-bar">
            <input type="text" id="msg-input" placeholder="Сообщение..." onkeydown="if(event.key==='Enter') sendMsg()">
            <button onclick="sendMsg()">➤</button>
        </div>
    </div>
</div>

<script>
    let myNick = localStorage.getItem("chat_nick") || "";
    let currentChat = "Общий чат";
    let ws = null;
    let chatsData = { "Общий чат": [] };

    window.onload = () => {
        if (myNick) {
            document.getElementById("nick-input").value = myNick;
            login();
        }
    };

    function login() {
        const inputVal = document.getElementById("nick-input").value.trim();
        if (!inputVal) return;
        myNick = inputVal;
        localStorage.setItem("chat_nick", myNick);
        document.getElementById("login-modal").style.display = "none";

        const loc = window.location;
        const wsProtocol = loc.protocol === "https:" ? "wss:" : "ws:";
        ws = new WebSocket(`${wsProtocol}//${loc.host}/ws/${encodeURIComponent(myNick)}`);

        ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            if (data.type === "users") {
                data.list.forEach(u => {
                    if (u !== myNick && !chatsData[u]) chatsData[u] = [];
                });
                renderSidebar();
            } else if (data.type === "msg") {
                const targetChat = data.target === "Общий чат" ? "Общий чат" : data.sender;
                if (!chatsData[targetChat]) chatsData[targetChat] = [];
                chatsData[targetChat].push({ text: data.text, mine: false, sender: data.sender, time: data.time });
                if (currentChat === targetChat) renderMessages();
            }
        };

        loadHistory(currentChat);
        renderSidebar();
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
        } catch(e) {}
    }

    function renderSidebar() {
        const list = document.getElementById("chat-list");
        list.innerHTML = "";
        for (const name in chatsData) {
            const item = document.createElement("div");
            item.className = `chat-item ${name === currentChat ? 'active' : ''}`;
            item.onclick = () => selectChat(name);
            item.innerHTML = `
                <div class="avatar">${name[0].toUpperCase()}</div>
                <div class="chat-info">
                    <div class="chat-name">${name}</div>
                    <div class="chat-sub">${name === "Общий чат" ? "Общая комната" : "Личный диалог"}</div>
                </div>
            `;
            list.appendChild(item);
        }
    }

    function selectChat(name) {
        currentChat = name;
        document.getElementById("header-title").innerText = name;
        document.getElementById("header-sub").innerText = name === "Общий чат" ? `Вы вошли как: ${myNick}` : "Личные сообщения";
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
            row.className = `bubble-row ${m.mine ? 'mine' : ''}`;
            row.innerHTML = `
                <div class="bubble">
                    ${!m.mine ? `<div class="sender-name">${m.sender}</div>` : ''}
                    <div>${m.text}</div>
                    <div class="msg-time">${m.time}</div>
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

        ws.send(JSON.stringify({ target: currentChat, text: text }));
        input.value = "";
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
                "SELECT sender, target, text, timestamp FROM messages WHERE target = 'Общий чат' ORDER BY id ASC LIMIT 150"
            )
        else:
            cursor = await db.execute(
                """SELECT sender, target, text, timestamp FROM messages 
                   WHERE (sender = ? AND target = ?) OR (sender = ? AND target = ?) 
                   ORDER BY id ASC LIMIT 150""",
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
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
