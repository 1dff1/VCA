import sqlite3
import hashlib
import uuid
import json
import logging
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация приложения
app = FastAPI(title="Voice Call App")

# CORS для разработки
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Инициализация базы данных
def init_db():
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# Модели
class UserCreate(BaseModel):
    username: str
    password: str

class UserLogin(BaseModel):
    username: str
    password: str

# Хэширование пароля
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

# Хранилище сессий
active_connections: Dict[str, WebSocket] = {}
user_sessions: Dict[str, int] = {}  # session_id -> user_id
user_names: Dict[int, str] = {}     # user_id -> username

# Маршруты API
@app.post("/register")
async def register(user: UserCreate):
    # Валидация
    if len(user.username) < 3:
        raise HTTPException(status_code=400, detail="Имя должно быть не менее 3 символов")
    if len(user.username) > 20:
        raise HTTPException(status_code=400, detail="Имя должно быть не более 20 символов")
    if len(user.password) < 4:
        raise HTTPException(status_code=400, detail="Пароль должен быть не менее 4 символов")
    
    # Проверка существования пользователя
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE username = ?", (user.username,))
    if cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Пользователь с таким именем уже существует")
    
    # Создание пользователя
    cursor.execute(
        "INSERT INTO users (username, password_hash) VALUES (?, ?)",
        (user.username, hash_password(user.password))
    )
    conn.commit()
    conn.close()
    
    return {"message": "Регистрация успешна"}

@app.post("/login")
async def login(user: UserLogin):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, username, password_hash FROM users WHERE username = ?",
        (user.username,)
    )
    result = cursor.fetchone()
    conn.close()
    
    if not result or result[2] != hash_password(user.password):
        raise HTTPException(status_code=401, detail="Неверное имя или пароль")
    
    # Создание сессии
    session_id = str(uuid.uuid4())
    user_id = result[0]
    username = result[1]
    
    user_sessions[session_id] = user_id
    user_names[user_id] = username
    
    return {"session_id": session_id, "username": username, "user_id": user_id}

# Статические файлы
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def get():
    with open("static/index.html") as f:
        return HTMLResponse(f.read())

# WebSocket
@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    # Проверка сессии
    if session_id not in user_sessions:
        await websocket.close(code=4001)
        return
    
    user_id = user_sessions[session_id]
    username = user_names.get(user_id, f"User_{user_id}")
    
    await websocket.accept()
    active_connections[session_id] = websocket
    
    logger.info(f"Пользователь {username} (ID: {user_id}) подключился")
    await broadcast_online_users()
    
    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            msg_type = message.get("type")
            
            if msg_type == "call_user":
                target_user_id = message["target_user_id"]
                target_session = find_user_session(target_user_id)
                
                if target_session:
                    await active_connections[target_session].send_text(
                        json.dumps({
                            "type": "incoming_call",
                            "from_user_id": user_id,
                            "from_username": username,
                            "offer": message["offer"]
                        })
                    )
                else:
                    await websocket.send_text(
                        json.dumps({
                            "type": "error",
                            "message": "Пользователь не найден или отключился"
                        })
                    )
            
            elif msg_type == "answer_call":
                target_user_id = message["target_user_id"]
                target_session = find_user_session(target_user_id)
                
                if target_session:
                    await active_connections[target_session].send_text(
                        json.dumps({
                            "type": "call_answered",
                            "answer": message["answer"]
                        })
                    )
            
            elif msg_type == "ice_candidate":
                target_user_id = message["target_user_id"]
                target_session = find_user_session(target_user_id)
                
                if target_session:
                    await active_connections[target_session].send_text(
                        json.dumps({
                            "type": "ice_candidate",
                            "candidate": message["candidate"]
                        })
                    )
            
            elif msg_type == "call_declined":
                target_user_id = message["target_user_id"]
                target_session = find_user_session(target_user_id)
                
                if target_session:
                    await active_connections[target_session].send_text(
                        json.dumps({
                            "type": "call_declined",
                            "by": user_id
                        })
                    )
            
            elif msg_type == "call_ended":
                target_user_id = message["target_user_id"]
                target_session = find_user_session(target_user_id)
                
                if target_session:
                    await active_connections[target_session].send_text(
                        json.dumps({
                            "type": "call_ended"
                        })
                    )
    
    except WebSocketDisconnect:
        logger.info(f"Пользователь {username} (ID: {user_id}) отключился")
        if session_id in active_connections:
            del active_connections[session_id]
        if session_id in user_sessions:
            del user_sessions[session_id]
        if user_id in user_names:
            del user_names[user_id]
        await broadcast_online_users()

# Вспомогательные функции
def find_user_session(user_id: int) -> str | None:
    for session_id, uid in user_sessions.items():
        if uid == user_id and session_id in active_connections:
            return session_id
    return None

async def broadcast_online_users():
    online_users = [
        {"user_id": uid, "username": uname}
        for uid, uname in user_names.items()
    ]
    
    for session_id, websocket in active_connections.items():
        try:
            user_id = user_sessions.get(session_id)
            await websocket.send_text(
                json.dumps({
                    "type": "online_users",
                    "users": online_users,
                    "current_user_id": user_id
                })
            )
        except:
            pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)