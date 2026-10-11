# -*- coding: utf-8 -*-
"""python -m unittest discover tests —— 只用标准库，模型用假的，数据写在临时目录里。"""
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import config  # noqa: E402
import llm  # noqa: E402
import promises  # noqa: E402
import server  # noqa: E402

ZONE = timezone(timedelta(hours=8))


def at(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=ZONE)


class TempData(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        promises.DATA_DIR = Path(self.tmp.name)
        self._load, self._chat, self._json = config.load, llm.chat, llm.chat_json
        config.load = lambda: {**config.DEFAULTS, "api_key": "test", "utc_offset_hours": 8}
        promises.initialise()

    def tearDown(self):
        config.load, llm.chat, llm.chat_json = self._load, self._chat, self._json
        self.tmp.cleanup()


class GateTests(unittest.TestCase):
    def test_keyword_gate(self):
        self.assertTrue(promises.worth_checking("十分钟后叫我", "好"))
        self.assertTrue(promises.worth_checking("", "四点半提醒你吃药"))
        self.assertTrue(promises.worth_checking("wake me up in 10 minutes", "okay"))
        self.assertFalse(promises.worth_checking("今天好累", "抱抱"))
        self.assertFalse(promises.worth_checking("待会儿吃什么", "你想吃什么"))


class DecisionTests(TempData):
    def test_create_replace_cancel_duplicate(self):
        now = at("2026-10-11 14:00").astimezone(timezone.utc)
        self.assertEqual(promises.apply_decision("s", {"action": "create", "due_local": "2026-10-11 14:10", "task": "叫她起床"}, now), "created")
        self.assertEqual(promises.apply_decision("s", {"action": "create", "due_local": "2026-10-11 14:11"}, now), "duplicate")
        item = promises.pending("s")[0]
        self.assertEqual((item["due_local"], item["task"]), ("14:10", "叫她起床"))
        self.assertEqual(promises.apply_decision("s", {"action": "replace", "target_id": item["id"], "due_local": "2026-10-11 14:30", "task": "叫她起床"}, now), "replaced")
        self.assertEqual(promises.pending("s")[0]["due_local"], "14:30")
        self.assertEqual(promises.apply_decision("s", {"action": "cancel", "target_id": item["id"]}, now), "cancelled")
        self.assertEqual(promises.pending("s"), [])

    def test_bad_times(self):
        now = at("2026-10-11 14:00").astimezone(timezone.utc)
        for due in ("2026-10-11 13:00", "2026-10-13 09:00", "明天", None):
            self.assertEqual(promises.apply_decision("s", {"action": "create", "due_local": due}, now), "bad_time")

    def test_capture_uses_the_model_only_when_the_gate_passes(self):
        calls = []
        llm.chat_json = lambda messages, max_tokens=300: calls.append(messages) or {"action": "none"}
        self.assertEqual(promises.capture("s", "今天好累", "抱抱"), "skipped")
        self.assertEqual(promises.capture("s", "十分钟后叫我", "好，十分钟后叫你"), "none")
        self.assertEqual(len(calls), 1)
        self.assertIn("十分钟后叫你", calls[0][1]["content"])

    def test_context_lists_pending(self):
        self.assertEqual(promises.context("s"), "")
        now = promises.now_utc()
        due = (now + timedelta(minutes=20)).astimezone(ZONE).strftime("%Y-%m-%d %H:%M")
        promises.apply_decision("s", {"action": "create", "due_local": due, "task": "提醒她吃药"}, now)
        self.assertIn("提醒她吃药", promises.context("s"))


class FireTests(TempData):
    def _due_now(self, said=""):
        with promises.db() as connection:
            connection.execute(
                "INSERT INTO promises (session_id, due_at, task, said, created_at) VALUES ('s', ?, '叫她起床', ?, ?)",
                ((promises.now_utc() - timedelta(seconds=5)).isoformat(), said, promises.now_utc().isoformat()),
            )

    def test_fires_once_and_stores_the_message(self):
        prompts = []
        llm.chat = lambda messages, **kw: prompts.append(messages) or "醒醒，说好的十分钟到了。"
        self._due_now(said="她：十分钟后叫我\n他：好")
        self.assertEqual(promises.run_due(), 1)
        self.assertEqual(promises.run_due(), 0)
        fired = promises.messages("s", only_promises=True)
        self.assertEqual(fired[0]["content"], "醒醒，说好的十分钟到了。")
        self.assertIn("十分钟后叫我", prompts[0][1]["content"])  # no chat log → uses the exchange it was promised in

    def test_retries_then_fails(self):
        def broken(messages, **kw):
            raise llm.LLMError("down")
        llm.chat = broken
        self._due_now()
        for _ in range(promises.MAX_ATTEMPTS):
            promises.run_due()
        with promises.db() as connection:
            self.assertEqual(connection.execute("SELECT status FROM promises").fetchone()[0], "failed")

    def test_long_overdue_is_missed(self):
        with promises.db() as connection:
            connection.execute(
                "INSERT INTO promises (session_id, due_at, created_at) VALUES ('s', ?, ?)",
                ((promises.now_utc() - timedelta(hours=2)).isoformat(), promises.now_utc().isoformat()),
            )
        self.assertEqual(promises.run_due(), 0)
        with promises.db() as connection:
            self.assertEqual(connection.execute("SELECT status FROM promises").fetchone()[0], "missed")


class HttpTests(TempData):
    def setUp(self):
        super().setUp()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def post(self, path, body):
        request = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read())

    def test_capture_and_cancel_over_http(self):
        due = (promises.now_utc() + timedelta(minutes=10)).astimezone(ZONE).strftime("%Y-%m-%d %H:%M")
        llm.chat_json = lambda messages, max_tokens=300: {"action": "create", "due_local": due, "task": "叫她起床"}
        result = self.post("/promises/capture", {"session_id": "me", "user_text": "十分钟后叫我", "reply": "好，十分钟后叫你"})
        self.assertEqual(result["outcome"], "created")
        with urllib.request.urlopen(self.base + "/promises/context?session_id=me") as response:
            self.assertIn("叫她起床", response.read().decode("utf-8"))
        self.assertTrue(self.post("/promises/cancel", {"id": result["items"][0]["id"], "session_id": "me"})["ok"])

    def test_demo_chat(self):
        llm.chat = lambda messages, **kw: "好，十分钟后叫你。"
        llm.chat_json = lambda messages, max_tokens=300: {"action": "none"}
        self.assertEqual(self.post("/api/chat", {"text": "十分钟后叫我"})["reply"], "好，十分钟后叫你。")
        with urllib.request.urlopen(self.base + "/api/messages?session_id=default") as response:
            self.assertEqual([m["role"] for m in json.loads(response.read())["items"]], ["user", "assistant"])


if __name__ == "__main__":
    unittest.main()
