import os, time, json, urllib.request, urllib.parse
from dotenv import load_dotenv
load_dotenv()
TOKEN=os.getenv("BOT_TOKEN","")
WEBAPP_URL=os.getenv("WEBAPP_URL","")
if not TOKEN or "PASTE_" in TOKEN: raise SystemExit("Укажите BOT_TOKEN в .env")
if not WEBAPP_URL.startswith("https://"): raise SystemExit("WEBAPP_URL должен быть публичным HTTPS-адресом")
API=f"https://api.telegram.org/bot{TOKEN}/"
offset=None

def call(method, payload):
    data=urllib.parse.urlencode(payload).encode()
    req=urllib.request.Request(API+method,data=data)
    with urllib.request.urlopen(req,timeout=40) as r: return json.loads(r.read())

def main():
    global offset
    print("NICOTIME bot is running. Stop with Ctrl+C.")
    while True:
        try:
            params={"timeout":30}
            if offset is not None: params["offset"]=offset
            updates=call("getUpdates",params).get("result",[])
            for u in updates:
                offset=u["update_id"]+1
                msg=u.get("message") or u.get("edited_message")
                if not msg: continue
                chat_id=msg["chat"]["id"]
                text=msg.get("text","")
                if text.startswith("/start"):
                    keyboard={"inline_keyboard":[[{"text":"Открыть NICOTIME","web_app":{"url":WEBAPP_URL}}]]}
                    call("sendMessage",{"chat_id":chat_id,"text":"NICOTIME — внутренняя система управления ассортиментом. Нажмите кнопку, чтобы открыть приложение.","reply_markup":json.dumps(keyboard,ensure_ascii=False)})
                elif text.startswith("/help"):
                    call("sendMessage",{"chat_id":chat_id,"text":"Для запуска приложения отправьте /start."})
                else:
                    call("sendMessage",{"chat_id":chat_id,"text":"Чтобы открыть NICOTIME, отправьте /start."})
        except KeyboardInterrupt: break
        except Exception as e:
            print("Bot polling error:",e); time.sleep(3)
if __name__=="__main__": main()
