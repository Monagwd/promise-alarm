#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""说到做到 · Promise Alarm · 单文件小服务（只用 Python 标准库）。

    python3 server.py            # 然后浏览器打开 http://127.0.0.1:8768

它做两件事：
1. 自带一个演示聊天页（web/）：和小机聊天，它答应几点叫你，到点真的会来；
2. 给你自己的聊天前端 / 网关几条接口（见 README）：
   POST /promises/capture     每轮聊天结束后把这一轮交过来，识别有没有约定
   GET  /promises/context     每轮接在系统提示后面：还没到点的约定
   GET  /promises             还没到点的约定（给界面显示，可以取消）
   POST /promises/cancel      取消一个约定
   GET  /promises/fired       到点后小机写的消息（轮询拿；也可以配 webhook_url 让它推给你）
"""
import json
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import config  # noqa: E402
import llm  # noqa: E402
import promises  # noqa: E402

HOST, PORT = "127.0.0.1", 8768
PORT_TRIES = 12
MAX_JSON = 64 * 1024
CHAT_RULE = (
    "【约定】她让你几点叫她、提醒她，或者你想过一会儿回来找她时，可以直接答应具体的时间（比如“十分钟后叫你”“四点半提醒你吃药”），"
    "系统会在那个时间让你真的回来给她发消息。没有具体时间就别随口答应。"
)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT / "web"), **kw)

    def log_message(self, fmt, *args):  # 只打接口，静态文件和轮询不刷屏
        line = args[0] if args else ""
        if "/promises/" in line and "GET" not in line:
            super().log_message(fmt, *args)

    def _send(self, body: bytes, content_type: str, code: int = 200, cors: bool = False):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cors:  # 接口允许你自己别的端口上的前端来调
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200, cors: bool = False):
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", code, cors)

    def _error(self, message: str, code: int = 400, cors: bool = False):
        self._json({"error": message}, code, cors)

    def _same_origin(self) -> bool:
        """演示聊天的接口只给本页面用。"""
        origin = self.headers.get("Origin")
        return not origin or urlsplit(origin).netloc == self.headers.get("Host", "")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_JSON:
            raise ValueError("内容太大了。")
        data = json.loads(self.rfile.read(length) or b"{}") if length else {}
        if not isinstance(data, dict):
            raise ValueError("请求不是有效的 JSON。")
        return data

    def _query(self) -> dict:
        query = parse_qs(urlsplit(self.path).query)
        return {key: values[0] for key, values in query.items()}

    def do_OPTIONS(self):
        self._send(b"", "text/plain", 204, cors=True)

    def do_GET(self):
        path, query = urlsplit(self.path).path, self._query()
        session = str(query.get("session_id") or "default")[:80]
        after = int(query.get("after") or 0) if str(query.get("after") or "0").isdigit() else 0
        if path == "/promises":
            return self._json({"items": promises.pending(session)}, cors=True)
        if path == "/promises/context":
            return self._send(promises.context(session).encode("utf-8"), "text/plain; charset=utf-8", cors=True)
        if path == "/promises/fired":
            return self._json({"items": promises.messages(session, after, only_promises=True)}, cors=True)
        if path == "/api/status":
            settings = config.load()
            return self._json({"configured": llm.available(), "char_name": settings["char_name"], "model": settings["model"]})
        if path == "/api/messages":
            return self._json({"items": promises.messages(session, after)})
        return super().do_GET()

    def do_POST(self):
        path = urlsplit(self.path).path
        integration = path.startswith("/promises/")
        try:
            data = self._body()
            session = str(data.get("session_id") or "default")[:80]
            if path == "/promises/capture":
                outcome = promises.capture(session, str(data.get("user_text") or ""), str(data.get("reply") or ""))
                return self._json({"outcome": outcome, "items": promises.pending(session)}, cors=True)
            if path == "/promises/cancel":
                ok = promises.cancel(int(data.get("id") or 0))
                return self._json({"ok": ok, "items": promises.pending(session)}, 200 if ok else 404, cors=True)
            if not self._same_origin():
                return self._error("只能在自己的页面里聊天。", 403)
            if path == "/api/chat":
                return self._json(chat(session, str(data.get("text") or "").strip()[:2000]))
        except (ValueError, TypeError) as error:
            return self._error(str(error) or "请求不对。", 400, integration)
        except llm.LLMError as error:
            return self._error(str(error), 502, integration)
        return self._error("没有这个接口。", 404, integration)


def chat(session: str, text: str) -> dict:
    """演示聊天：存她的话 → 小机回复（带上还没到点的约定）→ 后台识别这一轮有没有新约定。"""
    if not text:
        raise ValueError("说点什么吧。")
    settings = config.load()
    history = promises.messages(session, limit=20)
    promises.add_message(session, "user", text)
    now = promises.local_text(promises.now_utc(), "%Y-%m-%d %H:%M")
    system = "\n\n".join(part for part in (settings["persona"], CHAT_RULE, f"现在是 {now}。", promises.context(session)) if part)
    reply = llm.chat(
        [{"role": "system", "content": system}]
        + [{"role": m["role"], "content": m["content"]} for m in history]
        + [{"role": "user", "content": text}]
    )
    reply = promises.clean_reply(reply) or "……"
    message_id = promises.add_message(session, "assistant", reply)

    def check():
        try:
            outcome = promises.capture(session, text, reply)
            if outcome not in {"skipped", "none"}:
                print(f"约定：{outcome}")
        except Exception as error:
            print(f"识别约定失败：{error}")

    threading.Thread(target=check, daemon=True).start()
    return {"id": message_id, "reply": reply}


def serve():
    promises.initialise()
    promises.start_loop()
    for port in range(PORT, PORT + PORT_TRIES):  # 端口被占就往后挪
        try:
            server = ThreadingHTTPServer((HOST, port), Handler)
        except OSError:
            continue
        print(f"说到做到开在 http://{HOST}:{port}  （关掉这个窗口就停了，约定也会停）")
        if not llm.available():
            print("还没有填 API Key：复制 config.example.json 为 config.json 填上（见 README）。")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return
    print(f"{PORT}–{PORT + PORT_TRIES - 1} 端口都被占用了，关掉一些程序再试。")
    sys.exit(1)


if __name__ == "__main__":
    serve()
