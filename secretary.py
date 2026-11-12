import os, json, time, sqlite3, logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
from openai import OpenAI

logging.basicConfig(level=logging.INFO)
TZ=ZoneInfo("Asia/Taipei")
TOKEN=os.environ["TELEGRAM_BOT_TOKEN"]
OWNER=int(os.environ.get("TELEGRAM_OWNER_ID","0"))
client=OpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=30)
MODEL=os.environ.get("OPENAI_MODEL","gpt-4.1-mini")
DB=os.environ.get("DB_PATH","/data/secretary.db")
os.makedirs(os.path.dirname(DB) or ".",exist_ok=True)
BASE="https://api.telegram.org/bot"+TOKEN
http=requests.Session()

def now(): return datetime.now(TZ)
def stamp(dt): return dt.astimezone(TZ).isoformat(timespec="seconds")
def db():
    c=sqlite3.connect(DB,timeout=10)
    c.row_factory=sqlite3.Row
    return c
def init():
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, title TEXT NOT NULL, due TEXT NOT NULL, important INTEGER DEFAULT 0, sent TEXT, done INTEGER DEFAULT 0)")
        c.execute("CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY, role TEXT, content TEXT)")
def api(method, data):
    r=http.post(BASE+"/"+method,json=data,timeout=35)
    r.raise_for_status()
    j=r.json()
    if not j.get("ok"): raise RuntimeError(j.get("description","Telegram error"))
    return j["result"]
def send(cid,msg,buttons=None):
    data={"chat_id":cid,"text":msg[:3900]}
    if buttons:data["reply_markup"]={"inline_keyboard":buttons}
    return api("sendMessage",data)
def pending():
    with db() as c:return c.execute("SELECT * FROM tasks WHERE done=0 ORDER BY due,id").fetchall()
def agenda():
    tasks=pending()
    return "\n".join(f"#{r['id']} {datetime.fromisoformat(r['due']).strftime('%m/%d %H:%M')} {r['title']}" for r in tasks[:30]) or "目前沒有未完成事項。"
def record(role,content):
    with db() as c:
        c.execute("INSERT INTO history(role,content) VALUES(?,?)",(role,content[:1200]))
        c.execute("DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT 12)")
def think(text):
    system=f"""你是繁體中文私人小秘書。台灣現在時間為 {stamp(now())}。使用者待辦：\n{agenda()}\n
回覆唯一 JSON：action(add/list/done/chat),reply,title,due,important,id。
只有使用者明確要設定提醒且有具體日期及時間才用 add；due 必須 ISO 8601 加 +08:00。缺時間就用 chat 追問，絕不猜時間。
done 只接受使用者指定的任務編號，否則追問。一般問題用 chat。
reply 不可假稱已成功新增或完成；程式會確認。請簡潔、有幫助地回答。"""
    with db() as c: rows=c.execute("SELECT role,content FROM history ORDER BY id DESC LIMIT 10").fetchall()
    msgs=[{"role":"system","content":system}]+[{"role":r["role"],"content":r["content"]} for r in reversed(rows)]+[{"role":"user","content":text}]
    ans=client.chat.completions.create(model=MODEL,messages=msgs,response_format={"type":"json_object"},temperature=0.2,max_tokens=500)
    return json.loads(ans.choices[0].message.content)
def message(cid,txt):
    if txt=="/start":
        send(cid,"你好！我是你的 AI 私人小秘書 💙\n直接問我問題，或說「明天下午三點提醒我打給廠商」。\n/list 查看待辦；/help 看指令。");return
    if txt=="/help":
        send(cid,"/list 查行程\n/done 3 完成編號3的事項\n/id 查看自己的 Telegram 數字 ID\n重要提醒每30分鐘重複傳送，直到按已完成。");return
    if txt=="/list":send(cid,"📋 待辦事項\n"+agenda());return
    if txt.startswith("/done "):
        try:
            n=int(txt.split()[1])
            with db() as c:res=c.execute("UPDATE tasks SET done=1 WHERE id=? AND done=0",(n,))
            send(cid,"✅ 已完成。" if res.rowcount else "找不到該事項。")
        except Exception:send(cid,"請輸入 /done 3")
        return
    try:
        result=think(txt);action=result.get("action","chat")
        if action=="add":
            due=datetime.fromisoformat(str(result["due"])).astimezone(TZ)
            title=str(result.get("title","")).strip()[:200]
            if due<=now() or not title:send(cid,"請提供未來的確切日期、時間和提醒內容。");return
            important=1 if result.get("important") else 0
            with db() as c:
                cur=c.execute("INSERT INTO tasks(title,due,important) VALUES(?,?,?)",(title,stamp(due),important))
                n=cur.lastrowid
            reply=f"✅ 已新增 #{n}\n{title}\n🕒 {due:%Y/%m/%d %H:%M}（台灣）"
            if important:reply+="\n❗重要提醒將每30分鐘再次通知，直到確認完成。"
        elif action=="list":reply="📋 待辦事項\n"+agenda()
        elif action=="done":
            n=int(result.get("id") or 0)
            with db() as c:res=c.execute("UPDATE tasks SET done=1 WHERE id=? AND done=0",(n,))
            reply="✅ 已完成。" if res.rowcount else "找不到該待辦。"
        else:reply=str(result.get("reply") or "可以再說詳細一點嗎？")
        send(cid,reply)
        record("user",txt);record("assistant",reply)
    except Exception:
        logging.exception("AI error")
        send(cid,"抱歉，AI 暫時無法回答，已排定的提醒仍會繼續嘗試發送。")
def callback(q):
    if not OWNER or q["from"]["id"]!=OWNER or q["message"]["chat"]["id"]!=OWNER:return
    action,_,raw=q.get("data","").partition(":")
    try:
        task=int(raw)
        with db() as c:
            if action=="done":
                res=c.execute("UPDATE tasks SET done=1 WHERE id=? AND done=0",(task,))
                msg="✅ 完成" if res.rowcount else "此事項已完成"
            elif action=="later":
                res=c.execute("UPDATE tasks SET due=?,sent=NULL WHERE id=? AND done=0",(stamp(now()+timedelta(minutes=10)),task))
                msg="⏰ 10分鐘後再提醒" if res.rowcount else "此事項已完成"
            else:msg="不支援的操作"
        api("answerCallbackQuery",{"callback_query_id":q["id"],"text":msg})
    except Exception:logging.exception("Callback error")
def scheduler():
    if not OWNER:return
    t=now()
    with db() as c:
        v=c.execute("SELECT v FROM state WHERE k='daily'").fetchone()
        if t.hour>=8 and (not v or v["v"]!=t.strftime("%Y-%m-%d")):
            send(OWNER,"🌞 今日待辦總覽\n"+agenda())
            c.execute("INSERT INTO state(k,v) VALUES('daily',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",(t.strftime("%Y-%m-%d"),))
    for r in pending():
        due=datetime.fromisoformat(r["due"])
        if due>t:continue
        last=datetime.fromisoformat(r["sent"]) if r["sent"] else None
        if last and (not r["important"] or t-last<timedelta(minutes=30)):continue
        try:
            send(OWNER,f"🔔 {'重要提醒' if r['important'] else '行程提醒'} #{r['id']}\n{r['title']}\n{due:%m/%d %H:%M}",[
                [{"text":"✅ 已完成","callback_data":f"done:{r['id']}"},{"text":"⏰ 10分鐘後","callback_data":f"later:{r['id']}"}]])
            with db() as c:c.execute("UPDATE tasks SET sent=? WHERE id=?",(stamp(t),r["id"]))
        except Exception:logging.exception("Reminder send error")
def main():
    init()
    logging.info("Secretary started. Owner configured: %s",bool(OWNER))
    offset=None
    while True:
        try:
            scheduler()
            payload={"timeout":12,"allowed_updates":["message","callback_query"]}
            if offset is not None:payload["offset"]=offset
            for upd in api("getUpdates",payload):
                offset=upd["update_id"]+1
                if "callback_query" in upd:
                    callback(upd["callback_query"]);continue
                msg=upd.get("message",{});chat=msg.get("chat",{})
                if chat.get("type")!="private":continue
                user=msg.get("from",{}).get("id");cid=chat.get("id")
                if not cid:continue
                txt=msg.get("text","").strip()
                if txt=="/id":send(cid,f"你的 Telegram ID 是：{user}")
                elif not OWNER or user!=OWNER:send(cid,"🔒 這是私人助理，尚未授權使用。")
                elif txt:message(cid,txt)
                else:send(cid,"目前支援文字訊息。")
        except Exception:
            logging.exception("Polling error")
            time.sleep(5)
if __name__=="__main__":main()
