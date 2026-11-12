"""Offline smoke tests: fail the image build if basic AI parsing breaks."""
import os, tempfile, json
os.environ["TELEGRAM_BOT_TOKEN"]="TEST_TOKEN"
os.environ["TELEGRAM_OWNER_ID"]="806815686"
os.environ["GEMINI_API_KEY"]="TEST_GEMINI_KEY"
os.environ["DB_PATH"]=tempfile.mktemp(prefix="secretary-test-",suffix=".db")
import secretary as bot
bot.init()
class FakeResponse:
    status_code=200
    def raise_for_status(self): pass
    def json(self): return {"candidates":[{"content":{"parts":[{"text":json.dumps([
        {"action":"monthly","day":15,"hour":11,"minute":0,"title":"繳費"},
        {"action":"monthly","day":26,"hour":11,"minute":0,"title":"eSIM"}],ensure_ascii=False)}]}}]}
def fake_post(url,**kwargs):
    assert "gemini" in url and kwargs["json"]["systemInstruction"]
    return FakeResponse()
bot.http.post=fake_post
result=bot.think("每月15號11點提醒繳費，每月26號11點買eSIM")
assert isinstance(result,dict),result
assert len(result["actions"])==2,result
assert "新增每月提醒" in bot.add_monthly(result["actions"][0])
assert "已設定過" in bot.add_monthly(result["actions"][0])
assert bot.monthly_status()
print("Secretary offline smoke tests passed")
