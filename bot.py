import os
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
import anthropic
from tavily import TavilyClient

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")

conversation_history = {}
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
tavily = TavilyClient(api_key=TAVILY_API_KEY)

SYSTEM_PROMPT = "你是一個萬能的繁體中文助理，什麼問題都可以回答，請用繁體中文回答。如果需要查詢即時資訊，會提供搜尋結果給你參考。"

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_message = update.message.text

    # 搜尋即時資訊
    try:
        search_result = tavily.search(query=user_message, max_results=3)
        search_text = "\n".join([r["content"] for r in search_result["results"]])
        enhanced_message = f"用戶問題：{user_message}\n\n搜尋結果參考：\n{search_text}"
    except:
        enhanced_message = user_message

    if user_id not in conversation_history:
        conversation_history[user_id] = []

    conversation_history[user_id].append({
        "role": "user",
        "content": enhanced_message
    })

    if len(conversation_history[user_id]) > 20:
        conversation_history[user_id] = conversation_history[user_id][-20:]

    response = claude.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=conversation_history[user_id]
    )

    assistant_reply = response.content[0].text

    conversation_history[user_id].append({
        "role": "assistant",
        "content": assistant_reply
    })

    await update.message.reply_text(assistant_reply)

app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
print("Bot 啟動中...")
app.run_polling()