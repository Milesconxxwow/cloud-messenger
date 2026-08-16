import json
import os
import aiosqlite
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

app = FastAPI(title="Modern Cloud Messenger")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = "messenger.db"

# Инициализация базы данных
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

# Менеджер активных WebSocket подключений
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

# История сообщений для конкретного чата
@app.get("/api/history")
async def get_history(user1: str, user2: str):
    async with aiosqlite.connect(DB_PATH) as db:
        if user2 == "Общий чат":
            cursor = await db.execute(
                "SELECT sender, target, text, timestamp FROM messages WHERE target = 'Общий чат' ORDER BY id ASC LIMIT 100"
            )
        else:
            cursor = await db.execute(
                """SELECT sender, target, text, timestamp FROM messages 
                   WHERE (sender = ? AND target = ?) OR (sender = ? AND target = ?) 
                   ORDER BY id ASC LIMIT 100""",
                (user1, user2, user2, user1)
            )
        rows = await cursor.fetchall()
        return [{"sender": r[0], "target": r[1], "text": r[2], "time": r[3]} for r in rows]

# WebSocket хаб
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

            # Сохранение в базу
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