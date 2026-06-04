import os
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
import anthropic
from tavily import TavilyClient
import psycopg2
from datetime import datetime

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
tavily = TavilyClient(api_key=TAVILY_API_KEY)

def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            content TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

def save_memory(user_id, content):
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("INSERT INTO memories (user_id, content) VALUES (%s, %s)", (user_id, content))
    conn.commit()
    cur.close()
    conn.close()

def get_memories(user_id):
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("SELECT content FROM memories WHERE user_id = %s ORDER BY created_at DESC LIMIT 20", (user_id,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [r[0] for r in rows]

conversation_history = {}

SYSTEM_PROMPT = "你是一個萬能的繁體中文助理，請用繁體中文回答。你可以記住用戶說的重要事項。"

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_message = update.message.text

    # 取得記憶
    memories = get_memories(user_id)
    memory_text = "\n".join(memories) if memories else "無"

    # 搜尋即時資訊
    try:
        search_result = tavily.search(query=user_message, max_results=3)
        search_text = "\n".join([r["content"] for r in search_result["results"]])
    except:
        search_text = "無搜尋結果"

    enhanced_message = f"用戶問題：{user_message}\n\n過去記憶：\n{memory_text}\n\n搜尋結果：\n{search_text}"

    if user_id not in conversation_history:
        conversation_history[user_id] = []

    conversation_history[user_id].append({
        "role": "user",
        "content": enhanced_message
    })

    if len(conversation_history[user_id]) > 10:
        conversatio