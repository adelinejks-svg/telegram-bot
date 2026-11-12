import os, json, time, sqlite3, logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests


logging.basicConfig(level=logging.INFO)
TZ=ZoneInfo("Asia/Taipei")
TOKEN=os.environ["TELEGRAM_BOT_TOKEN"]
OWNER=int(os.environ.get("TELEGRAM_OWNER_ID","0"))
GEMINI_KEY=os.environ.get("GEMINI_API_KEY","").strip()
# This model has a free tier. Use a no-billing AI Studio project to avoid charges.
MODEL=os.environ.get("GEMINI_MODEL","gemini-2.5-flash-lite")
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
        c.execute("""CREATE TABLE IF NOT EXISTS monthly (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, day INTEGER NOT NULL,
            hour INTEGER NOT NULL, minute INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1, sent_month TEXT
        )""")
        # User explicitly requested these recurring reminders on 2026-11-12.
        # Stable keys mean restarts and deployments never duplicate them.
        c.execute("INSERT OR IGNORE INTO monthly(id,title,day,hour,minute) VALUES(10001,'繳停車費、ETC、查詢罰單、繳電話費',15,11,0)")
        c.execute("INSERT OR IGNORE INTO monthly(id,title,day,hour,minute) VALUES(10002,'購買 eSIM',26,11,0)")
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
def monthly_agenda():
    with db() as c:
        rows=c.execute("SELECT * FROM monthly WHERE enabled=1 ORDER BY day,hour,minute,id").fetchall()
    return "\n".join(f"📅 每月{r['day']}號 {r['hour']:02d}:{r['minute']:02d} #{r['id']} {r['title']}" for r in rows) or "目前沒有每月固定提醒。"
def record(role,content):
    with db() as c:
        c.execute("INSERT INTO history(role,content) VALUES(?,?)",(role,content[:1200]))
        c.execute("DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT 12)")
def think(text):
    if not GEMINI_KEY:
        raise RuntimeError("GEMINI_KEY_NOT_CONFIGURED")
    system=f"""你是繁體中文私人小秘書。台灣現在時間為 {stamp(now())}。使用者待辦：\n{agenda()}\n每月固定提醒：\n{monthly_agenda()}\n
回覆唯一 JSON：action(add/list/done/chat/monthly),reply,title,due,important,id,day,hour,minute。
明確要求「每月」「每個月」固定提醒時用 monthly，day=1..28 或 29..31，hour=0..23，minute=0..59。
若包含多個月度事項，可合併為一個提醒標題；不確定日期時間就用 chat 追問。
已存在的每月提醒不要重複建立；若使用者問有哪些固定提醒，請用 list。
只有使用者明確要設定提醒且有具體日期及時間才用 add；due 必須 ISO 8601 加 +08:00。缺時間就用 chat 追問，絕不猜時間。
done 只接受使用者指定的任務編號，否則追問。一般問題用 chat。
reply 不可假稱已成功新增或完成；程式會確認。請簡潔、有幫助地回答。"""
    with db() as c: rows=c.execute("SELECT role,content FROM history ORDER BY id DESC LIMIT 10").fetchall()
    # Convert saved OpenAI-format chat history to Gemini's role/parts format.
    contents=[]
    for row in reversed(rows):
        role="model" if row["role"]=="assistant" else "user"
        contents.append({"role":role,"parts":[{"text":row["content"]}]})
    contents.append({"role":"user","parts":[{"text":text}]})
    url=f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    payload={
        "systemInstruction":{"parts":[{"text":system}]},
        "contents":contents,
        "generationConfig":{"responseMimeType":"application/json","temperature":0.2,"maxOutputTokens":900}
    }
    # Temporary 503/502/504 responses are recoverable: retry without changing model or billing tier.
    for attempt in range(3):
        response=http.post(url,headers={"x-goog-api-key":GEMINI_KEY,"Content-Type":"application/json"},json=payload,timeout=40)
        if response.status_code not in (502,503,504):
            break
        if attempt<2:
            time.sleep(2*(attempt+1))
    if response.status_code in (502,503,504):
        raise RuntimeError("GEMINI_TEMP_UNAVAILABLE")
    if response.status_code==429:
        raise RuntimeError("GEMINI_FREE_LIMIT")
    if response.status_code in (401,403):
        raise RuntimeError("GEMINI_KEY_INVALID")
    response.raise_for_status()
    data=response.json()
    candidates=data.get("candidates") or []
    if not candidates:
        raise RuntimeError("GEMINI_NO_RESPONSE")
    parts=candidates[0].get("content",{}).get("parts",[])
    answer="".join(part.get("text","") for part in parts)
    return json.loads(answer)
def message(cid,txt):
    if txt=="/start":
        send(cid,"你好！我是你的 AI 私人小秘書 💙\n直接問我問題，或說「明天下午三點提醒我打給廠商」。\n/list 查看待辦；/help 看指令。");return
    if txt=="/help":
        send(cid,"/list 查行程\n/monthly 查每月提醒\n/monthlyadd 15 11 繳停車費  新增每月15號11點提醒\n/monthlydelete 10001  刪除每月提醒\n/done 3 完成編號3的事項\n/id 查 Telegram ID\n重要單次提醒每30分鐘催辦直到完成。");return
    if txt=="/monthly":
        send(cid,"📅 每月固定提醒\n"+monthly_agenda());return
    if txt.startswith("/monthlyadd "):
        try:
            parts=txt.split(maxsplit=3)
            day,hour,title=int(parts[1]),int(parts[2]),parts[3].strip()[:200]
            if not (1<=day<=28 and 0<=hour<=23 and title):
                raise ValueError("invalid monthly reminder")
            with db() as c:
                existing=c.execute("SELECT id FROM monthly WHERE title=? AND day=? AND hour=? AND minute=0 AND enabled=1",(title,day,hour)).fetchone()
                if existing:reply=f"已有相同提醒 #{existing['id']}，沒有重複新增。"
                else:
                    cur=c.execute("INSERT INTO monthly(title,day,hour) VALUES(?,?,?)",(title,day,hour))
                    reply=f"✅ 已設定每月{day}號 {hour:02d}:00 提醒：{title}（#{cur.lastrowid}）"
            send(cid,reply)
        except Exception:
            send(cid,"格式：/monthlyadd 15 11 記得繳停車費（支援每月1至28號）")
        return
    if txt.startswith("/monthlydelete "):
        try:
            ident=int(txt.split()[1])
            with db() as c:
                r=c.execute("UPDATE monthly SET enabled=0 WHERE id=? AND enabled=1",(ident,))
            send(cid,"✅ 已取消每月提醒。" if r.rowcount else "找不到該每月提醒。")
        except Exception:
            send(cid,"格式：/monthlydelete 10001")
        return
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
        elif action=="monthly":
            day=int(result.get("day") or 0)
            hour=int(result.get("hour") if result.get("hour") is not None else -1)
            minute=int(result.get("minute") or 0)
            title=str(result.get("title") or "").strip()[:200]
            if not (1<=day<=28 and 0<=hour<=23 and 0<=minute<=59 and title):
                reply="請告訴我每月幾號、幾點，以及要提醒的事情。（目前支援1至28號）"
            else:
                with db() as c:
                    existing=c.execute("SELECT id FROM monthly WHERE title=? AND day=? AND hour=? AND minute=? AND enabled=1",(title,day,hour,minute)).fetchone()
                    if existing:
                        reply=f"這個每月提醒已經存在（#{existing['id']}），不會重複新增。"
                    else:
                        cur=c.execute("INSERT INTO monthly(title,day,hour,minute) VALUES(?,?,?,?)",(title,day,hour,minute))
                        reply=f"✅ 已設定每月{day}號 {hour:02d}:{minute:02d}（台灣時間）提醒：{title}（#{cur.lastrowid}）"
        elif action=="list":reply="📋 未完成事項\n"+agenda()+"\n\n"+monthly_agenda()
        elif action=="done":
            n=int(result.get("id") or 0)
            with db() as c:res=c.execute("UPDATE tasks SET done=1 WHERE id=? AND done=0",(n,))
            reply="✅ 已完成。" if res.rowcount else "找不到該待辦。"
        else:reply=str(result.get("reply") or "可以再說詳細一點嗎？")
        send(cid,reply)
        record("user",txt);record("assistant",reply)
    except Exception as exc:
        logging.exception("AI error")
        if str(exc)=="GEMINI_KEY_NOT_CONFIGURED":
            send(cid,"我已經改成 Gemini，但還沒設定 Gemini API 金鑰。請先到 Railway 安全設定 GEMINI_API_KEY。")
        elif str(exc)=="GEMINI_FREE_LIMIT":
            send(cid,"Gemini 免費使用額度暫時用完了。請稍後再問，不需要付費升級；已設定提醒仍會嘗試照常發送。")
        elif str(exc)=="GEMINI_TEMP_UNAVAILABLE":
            send(cid,"Gemini 現在暫時忙碌（503），我已自動重試仍未成功。已設定的提醒不受影響，請稍後再傳一次。")
        elif str(exc)=="GEMINI_KEY_INVALID":
            send(cid,"Gemini API 金鑰無效或沒有權限，請檢查金鑰設定。")
        else:
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
    # Monthly reminders run independently from Gemini API.
    # Do not mark as sent until Telegram confirms delivery.
    with db() as c:
        monthlies=c.execute("SELECT * FROM monthly WHERE enabled=1 ORDER BY id").fetchall()
    current_month=t.strftime("%Y-%m")
    for r in monthlies:
        if r["sent_month"]==current_month:
            continue
        if (t.day,t.hour,t.minute)<(r["day"],r["hour"],r["minute"]):
            continue
        # Avoid a newly deployed service sending old monthly reminders days late.
        if t.day!=r["day"] or t.hour!=r["hour"]:
            continue
        try:
            send(OWNER,f"📅 每月固定提醒\n{r['title']}\n每月{r['day']}號 {r['hour']:02d}:{r['minute']:02d}（台灣時間）")
            with db() as c:
                c.execute("UPDATE monthly SET sent_month=? WHERE id=?",(current_month,r["id"]))
        except Exception:
            logging.exception("Monthly reminder failed")
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
