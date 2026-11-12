import os, json, time, sqlite3, logging, re
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
        c.execute("INSERT OR IGNORE INTO state(k,v) VALUES('market_time','09:00')")
        c.execute("INSERT OR IGNORE INTO state(k,v) VALUES('market_enabled','1')")
        c.execute("""CREATE TABLE IF NOT EXISTS monthly (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, day INTEGER NOT NULL,
            hour INTEGER NOT NULL, minute INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1, sent_month TEXT
        )""")
        # User explicitly requested these recurring reminders on 2026-11-12.
        # Stable keys mean restarts and deployments never duplicate them.
        c.execute("INSERT OR IGNORE INTO monthly(id,title,day,hour,minute) VALUES(10001,'繳停車費、ETC、查詢罰單、繳電話費',15,11,0)")
        c.execute("INSERT OR IGNORE INTO monthly(id,title,day,hour,minute) VALUES(10002,'購買 eSIM',26,11,0)")
        c.execute("""CREATE TABLE IF NOT EXISTS monthly_occurrences(
            monthly_id INTEGER NOT NULL, period TEXT NOT NULL,
            sent_at TEXT, done_at TEXT, snooze_until TEXT,
            PRIMARY KEY(monthly_id,period)
        )""")
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
def monthly_status():
    month=now().strftime("%Y-%m")
    with db() as c:
        rules=c.execute("SELECT * FROM monthly WHERE enabled=1 ORDER BY day,id").fetchall()
        result=[]
        for r in rules:
            occ=c.execute("SELECT done_at FROM monthly_occurrences WHERE monthly_id=? AND period=?",(r["id"],month)).fetchone()
            done="✅ 本月已完成" if occ and occ["done_at"] else "🔔 未完成會每30分鐘提醒"
            result.append(f"每月{r['day']}號 {r['hour']:02d}:{r['minute']:02d} #{r['id']} {r['title']}｜{done}")
    return "\n".join(result) or "沒有每月固定提醒。"

def add_monthly(item):
    try:
        day=int(item.get("day") or 0)
        hour=int(item.get("hour") if item.get("hour") is not None else -1)
        minute=int(item.get("minute") or 0)
        title=str(item.get("title") or "").strip()[:200]
        if not (1<=day<=28 and 0<=hour<=23 and 0<=minute<=59 and title):
            return "請提供每月幾號、幾點與提醒事項。"
        with db() as c:
            rows=c.execute("SELECT * FROM monthly WHERE day=? AND hour=? AND minute=? AND enabled=1",(day,hour,minute)).fetchall()
            for r in rows:
                if title==r["title"] or title in r["title"] or r["title"] in title:
                    return f"✅ 已設定過 #{r['id']}：{r['title']}，不會重複建立。"
            rid=c.execute("INSERT INTO monthly(title,day,hour,minute) VALUES(?,?,?,?)",(title,day,hour,minute)).lastrowid
        return f"✅ 新增每月提醒 #{rid}：每月{day}號 {hour:02d}:{minute:02d} {title}"
    except (TypeError,ValueError):
        return "請提供確定的日期與時間。"


def market_time():
    with db() as c:
        r=c.execute("SELECT v FROM state WHERE k='market_time'").fetchone()
    return r["v"] if r else "09:00"

def daily_market_report():
    """Deterministic market commentary; no chargeable AI call or invented quotes."""
    lines=[f"📈 每日市場晨報｜{now():%Y/%m/%d %H:%M}（台灣）"]
    values={}
    sources=[]
    try:
        r=http.get("https://api.frankfurter.dev/v2/rate/USD/TWD",timeout=12)
        r.raise_for_status()
        d=r.json()
        value=float(d["rate"])
        if value<=0:raise ValueError("invalid USD/TWD")
        values["USD/TWD"]=value
        lines.append(f"💵 美元兌台幣：1 USD = NT$ {value:.3f}（參考中間價；資料日 {d.get('date','未提供')}）")
        sources.append("Frankfurter")
    except Exception:
        logging.exception("USD/TWD market price fetch failed")
        lines.append("💵 美元兌台幣：暫時無法取得（不顯示推測價格）")
    for symbol,label in (("XAU","🥇 黃金"),("BTC","₿ 比特幣")):
        try:
            r=http.get("https://api.gold-api.com/price/"+symbol,timeout=12)
            r.raise_for_status()
            d=r.json()
            value=float(d["price"])
            if value<=0:raise ValueError("invalid price")
            values[symbol]=value
            unit="每金衡盎司" if symbol=="XAU" else "每枚"
            updated=d.get("updatedAt","未提供")
            lines.append(f"{label}：US$ {value:,.2f}（{unit}；報價時間 {updated} UTC）")
            sources.append("Gold API")
        except Exception:
            logging.exception("Market price fetch failed: %s",symbol)
            lines.append(f"{label}：暫時無法取得（不顯示推測價格）")
    insights=[]
    with db() as c:
        for k,value in values.items():
            row=c.execute("SELECT v FROM state WHERE k=?",(f"market_last_{k}",)).fetchone()
            if row:
                try:
                    earlier=json.loads(row["v"])
                    old=float(earlier["price"])
                    old_date=earlier["date"]
                    if old>0 and old_date!=now().strftime("%Y-%m-%d"):
                        delta=(value/old-1)*100
                        trend="上升" if delta>0 else "下降" if delta<0 else "持平"
                        insights.append(f"{k}：較上次晨報{trend} {abs(delta):.2f}%（比較基準 {old_date}，非24小時漲跌）")
                except (KeyError,ValueError,TypeError,ZeroDivisionError):
                    pass
            c.execute("INSERT INTO state(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                      (f"market_last_{k}",json.dumps({"price":value,"date":now().strftime("%Y-%m-%d")})))
    lines.append("\n🔎 簡短分析")
    if insights:
        lines.extend("• "+insight for insight in insights)
        lines.append("• 美元、黃金與比特幣風險性質不同，單日變化不代表長期趨勢。")
    else:
        lines.append("• 首次建立比較基準；下一個交易日／下一次晨報起顯示漲跌百分比。")
        lines.append("• 黃金以美元／金衡盎司計價；比特幣波動通常較大，注意風險。")
    lines.append("\n📌 匯率為參考價，不等於銀行現鈔買賣價。不同市場更新時段不同，休市可能沿用前次報價。")
    lines.append("資料來源："+("、".join(dict.fromkeys(sources)) if sources else "來源暫時無回應")+"；不是投資建議。")
    return "\n".join(lines)

def record(role,content):
    with db() as c:
        c.execute("INSERT INTO history(role,content) VALUES(?,?)",(role,content[:1200]))
        c.execute("DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT 12)")
def think(text):
    if not GEMINI_KEY:
        raise RuntimeError("GEMINI_KEY_NOT_CONFIGURED")
    system=f"""你是繁體中文私人小秘書。台灣現在時間為 {stamp(now())}。使用者待辦：\n{agenda()}\n每月固定提醒：\n{monthly_agenda()}\n
回覆唯一 JSON 物件：action(add/list/done/chat/monthly),reply,title,due,important,id,day,hour,minute。
如一次要求新增多個每月事項，請輸出 {{"actions":[{{"action":"monthly","day":15,"hour":11,"minute":0,"title":"..."}},{{"action":"monthly","day":26,"hour":11,"minute":0,"title":"..."}}]}}，不能輸出最外層 JSON 陣列。
討論現有提醒的時間、是否持續催辦、說「有說11點」都不是新增提醒，請用 chat 回答，不要追問已知道的時間。
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
    parsed=json.loads(answer)
    if isinstance(parsed,list):
        parsed={"actions":parsed}
    if not isinstance(parsed,dict):
        raise RuntimeError("GEMINI_UNEXPECTED_RESPONSE")
    return parsed
def message(cid,txt):
    if txt=="/start":
        send(cid,"你好！我是你的 AI 私人小秘書 💙\n直接問我問題，或說「明天下午三點提醒我打給廠商」。\n/list 查看待辦；/help 看指令。");return
    if txt=="/help":
        send(cid,"/list 查行程\n/monthly 查每月提醒\n/test 即時推播測試\n/testremind 1分鐘後推播測試\n/monthlyadd 15 11 繳停車費  新增每月15號11點提醒\n/monthlydelete 10001  刪除每月提醒\n/done 3 完成編號3的事項\n/id 查 Telegram ID\n重要單次提醒每30分鐘催辦直到完成。");return
    if txt=="/market":
        send(cid,daily_market_report());return
    if txt in ("/marketoff","/marketon"):
        enabled="0" if txt=="/marketoff" else "1"
        with db() as c:
            c.execute("INSERT INTO state(k,v) VALUES('market_enabled',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",(enabled,))
        send(cid,"✅ 每天市場晨報已"+("暫停。" if enabled=="0" else "啟用，時間 "+market_time()+"。"));return
    m=re.fullmatch(r"/markettime\s+([01]?\d|2[0-3]):([0-5]\d)",txt)
    if m:
        newtime=f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"
        with db() as c:
            c.execute("INSERT INTO state(k,v) VALUES('market_time',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",(newtime,))
        send(cid,"✅ 市場晨報已改為每天 "+newtime+"（台灣時間）");return
    if ("美金" in txt or "美元" in txt or "黃金" in txt or "比特幣" in txt) and ("每天" in txt or "每日" in txt or "早上" in txt or "時間改" in txt or "改成" in txt) and ("點" in txt or ":" in txt):
        match=re.search(r"(?:上午|早上|每天|每日)?\s*(\d{1,2})\s*[點:]\s*(\d{1,2})?",txt)
        if match:
            hr=int(match.group(1));minute=int(match.group(2) or 0)
            if 0<=hr<=23 and 0<=minute<=59:
                nt=f"{hr:02d}:{minute:02d}"
                with db() as c:
                    c.execute("INSERT INTO state(k,v) VALUES('market_time',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",(nt,))
                    c.execute("INSERT INTO state(k,v) VALUES('market_enabled','1') ON CONFLICT(k) DO UPDATE SET v=excluded.v")
                send(cid,"✅ 美金、黃金、比特幣的每日價格與簡短分析，已設定每天 "+nt+"（台灣時間）。");return
    if txt=="/test":
        send(cid,"✅ Telegram 推播測試成功！這則訊息不需要 Gemini，也不會新增待辦。");return
    if txt=="/testremind":
        due=now()+timedelta(minutes=1)
        with db() as c:
            n=c.execute("INSERT INTO tasks(title,due,important) VALUES(?,?,0)",
                        ("🔔 一分鐘後提醒測試（只發送一次）",stamp(due))).lastrowid
        send(cid,f"✅ 測試提醒已排定，約一分鐘後推播。編號 #{n}");return
    if txt in ("每日市場","每日市場分析","美金黃金比特幣","每日價格","市場晨報","價格分析"):
        send(cid,daily_market_report());return
    if txt=="/monthly":
        send(cid,"📅 每月固定提醒\n"+monthly_status());return
    if any(p in txt for p in ("沒有完成","沒完成","持續提醒","一直提醒","繼續提醒")):
        send(cid,"好的，已設定每月固定提醒在11:00開始，未完成每30分鐘催辦，直到按「✅ 本月完成」；次月仍照常提醒。\n"+monthly_status());return
    if txt in ("有說11點啊","有說11點","11點啊","11點","上午11點","大概11點"):
        send(cid,"對，早就設定上午11點了，不用再說一次。\n"+monthly_status());return
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
        result=think(txt)
        if isinstance(result.get("actions"),list):
            replies=[add_monthly(item) for item in result["actions"][:10]
                     if isinstance(item,dict) and item.get("action")=="monthly"]
            reply="\n".join(replies) or str(result.get("reply") or "可以再說詳細一點嗎？")
            send(cid,reply)
            record("user",txt);record("assistant",reply)
            return
        action=result.get("action","chat")
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
            reply=add_monthly(result)
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
        if action in ("mdone","mlater"):
            period=now().strftime("%Y-%m")
            with db() as c:
                r=c.execute("SELECT id FROM monthly WHERE id=? AND enabled=1",(task,)).fetchone()
                if not r:
                    msg="找不到這筆每月提醒"
                elif action=="mdone":
                    c.execute("""INSERT INTO monthly_occurrences(monthly_id,period,done_at)
                        VALUES(?,?,?) ON CONFLICT(monthly_id,period) DO UPDATE SET done_at=excluded.done_at""",
                        (task,period,stamp(now())))
                    msg="✅ 本月完成，這個月停止催辦，下個月仍會提醒。"
                else:
                    c.execute("""INSERT INTO monthly_occurrences(monthly_id,period,snooze_until)
                        VALUES(?,?,?) ON CONFLICT(monthly_id,period) DO UPDATE SET snooze_until=excluded.snooze_until""",
                        (task,period,stamp(now()+timedelta(minutes=30))))
                    msg="⏰ 30分鐘後繼續提醒"
            api("answerCallbackQuery",{"callback_query_id":q["id"],"text":msg})
            send(OWNER,msg)
            return
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
    # One market report per Taiwan calendar date; retry on next polling loop if send fails.
    with db() as c:
        en=c.execute("SELECT v FROM state WHERE k='market_enabled'").fetchone()
        last=c.execute("SELECT v FROM state WHERE k='market_sent'").fetchone()
    at=market_time()
    if (not en or en["v"]=="1") and (not last or last["v"]!=t.strftime("%Y-%m-%d")) and t.strftime("%H:%M")>=at:
        try:
            send(OWNER,daily_market_report())
            with db() as c:
                c.execute("INSERT INTO state(k,v) VALUES('market_sent',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",(t.strftime("%Y-%m-%d"),))
        except Exception:
            logging.exception("Market report send failed")
    # Persist a separate occurrence for each calendar month, independent of Gemini.
    period=t.strftime("%Y-%m")
    with db() as c:
        rules=c.execute("SELECT * FROM monthly WHERE enabled=1 ORDER BY id").fetchall()
        for r in rules:
            due=t.replace(day=r["day"],hour=r["hour"],minute=r["minute"],second=0,microsecond=0)
            if t>=due:
                c.execute("INSERT OR IGNORE INTO monthly_occurrences(monthly_id,period) VALUES(?,?)",(r["id"],period))
        occurrences=c.execute("""SELECT o.*,m.title,m.day,m.hour,m.minute FROM monthly_occurrences o
            JOIN monthly m ON m.id=o.monthly_id
            WHERE m.enabled=1 AND o.done_at IS NULL
            ORDER BY o.period,o.monthly_id""").fetchall()
    for occ in occurrences:
        due=datetime.fromisoformat(f"{occ['period']}-{occ['day']:02d}T{occ['hour']:02d}:{occ['minute']:02d}:00+08:00")
        if t<due:continue
        last=datetime.fromisoformat(occ["sent_at"]) if occ["sent_at"] else None
        snooze=datetime.fromisoformat(occ["snooze_until"]) if occ["snooze_until"] else None
        if (last and t-last<timedelta(minutes=30)) or (snooze and t<snooze):
            continue
        try:
            send(OWNER,f"🔔 每月固定提醒（{occ['period']}）\n{occ['title']}\n沒完成就每30分鐘繼續提醒。",[
                [{"text":"✅ 本月完成","callback_data":f"mdone:{occ['monthly_id']}"},
                 {"text":"⏰ 30分鐘後","callback_data":f"mlater:{occ['monthly_id']}"}]])
            with db() as c:
                c.execute("UPDATE monthly_occurrences SET sent_at=? WHERE monthly_id=? AND period=?",
                          (stamp(t),occ["monthly_id"],occ["period"]))
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
