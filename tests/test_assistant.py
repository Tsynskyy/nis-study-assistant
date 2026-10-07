import contextlib
import io
import json
import os
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import date
from pathlib import Path

os.environ["ASSISTANT_NO_LOG"] = "1"

from assistant.caldav import CalDAV, sync  # noqa: E402
from assistant.corpus import load_corpus, split_markdown  # noqa: E402
from assistant.integrations import digest, send_mail, to_ics, upcoming  # noqa: E402
from assistant.mcp import handle  # noqa: E402
from assistant.monitor import report  # noqa: E402
from assistant.pipeline import Assistant  # noqa: E402
from assistant.retrieval import RETRIEVERS, query_tokens, tokenize  # noqa: E402
from assistant.schedule import cron, load_env, powershell  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


class AssistantTest(unittest.TestCase):
    def setUp(self):
        for name in ("ASSISTANT_MODEL", "OLLAMA_URL", "LLM_MAX_TOKENS", "SMTP_HOST", "SMTP_PORT", "SMTP_SSL",
                     "SMTP_USER", "SMTP_PASSWORD", "DIGEST_TO", "CALDAV_PASSWORD"):
            os.environ.pop(name, None)
        os.environ["ASSISTANT_LLM"] = "off"
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        self.chunks = load_corpus(FIXTURES)

    def test_markdown_chunks_keep_headings(self):
        chunks = split_markdown("x.md", "# A\n\n## B\n\nтекст\n")
        self.assertEqual(chunks[0].title, "A / B")
        self.assertIn("текст", chunks[0].text)

    def test_tokenize_stems_and_drops_stopwords(self):
        self.assertEqual(tokenize("Оценка по экономике"), ["оценк", "эконо"])

    def test_query_expands_abbreviations_and_merges_stems(self):
        self.assertEqual(query_tokens("экзамен ОРГ"), ["экзам", "орг", "основ", "росси", "госуд"])
        self.assertEqual(tokenize("Итог и итоговая"), ["итого", "итого"])
        self.assertEqual(tokenize("Как считается оценивание"), ["оценк"])

    def test_every_retriever_finds_economics_formula(self):
        for name, cls in RETRIEVERS.items():
            hits = cls(self.chunks).search("формула оценки по экономике", 1)
            self.assertIn("0,29", hits[0][0].text, name)

    def test_deadline_router_returns_only_future(self):
        bot = Assistant(chunks=self.chunks, today=date(2026, 10, 6))
        ans = bot.ask("Какие ближайшие дедлайны?")
        self.assertEqual(ans.route, "deadlines")
        self.assertIn("Загрузка отчёта", ans.text)
        self.assertNotIn("Старое задание", ans.text)

    def test_out_of_scope_question_falls_back(self):
        bot = Assistant(chunks=self.chunks, today=date(2026, 10, 6))
        self.assertEqual(bot.ask("рецепт борща").route, "fallback")

    def test_missing_sources_raise(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(FileNotFoundError):
            load_corpus(Path(tmp))

    def test_report_flags_high_fallback_rate(self):
        recs = [{"ts": "2026-10-06T10:00:00", "question": "q", "route": "fallback", "retriever": "bm25",
                 "generator": "extractive", "sources": [], "top_score": None, "latency_ms": 1.0}]
        os.environ["HSE_PASSPORT_DIR"] = str(FIXTURES)
        rep = report(recs)
        self.assertEqual(rep["fallback_rate"], 1.0)
        self.assertTrue(any("fallback_rate" in a for a in rep["alerts"]))

    def serve_llm(self, content):
        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen["path"] = self.path
                seen["body"] = json.loads(self.rfile.read(int(self.headers["content-length"])))
                out = json.dumps({"message": {"role": "assistant", "content": content}, "done": True}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        os.environ.pop("ASSISTANT_LLM", None)
        os.environ["OLLAMA_URL"] = f"http://127.0.0.1:{server.server_port}"
        return seen

    def test_local_model_generates_answer(self):
        seen = self.serve_llm("105 баллов [AUTUMN-2026.md]")
        ans = Assistant(chunks=self.chunks, today=date(2026, 10, 6)).ask("Сколько баллов за семестр по безопасности?")
        self.assertEqual(ans.text, "105 баллов [AUTUMN-2026.md]")
        self.assertEqual(ans.generator, "ollama:gemma4:e4b")
        self.assertEqual(seen["path"], "/api/chat")
        self.assertEqual(seen["body"]["model"], "gemma4:e4b")
        self.assertFalse(seen["body"]["stream"])
        self.assertIn("105 баллов", seen["body"]["messages"][1]["content"])
        self.assertEqual(seen["body"]["options"]["num_predict"], 2000)

    def test_empty_llm_answer_falls_back_to_extractive(self):
        for content in ("", None, "  \n"):
            with self.subTest(content=content):
                self.serve_llm(content)
                ans = Assistant(chunks=self.chunks, today=date(2026, 10, 6)).ask("Сколько баллов за семестр по безопасности?")
                self.assertEqual(ans.generator, "extractive")
                self.assertIn("105", ans.text)

    def test_unreachable_llm_falls_back_to_extractive(self):
        os.environ.pop("ASSISTANT_LLM")
        os.environ.update(OLLAMA_URL="http://127.0.0.1:9", LLM_TIMEOUT="2")
        ans = Assistant(chunks=self.chunks, today=date(2026, 10, 6)).ask("Сколько баллов за семестр по безопасности?")
        self.assertEqual(ans.generator, "extractive")
        self.assertIn("105", ans.text)

    def test_deadlines_are_listed_without_llm(self):
        seen = self.serve_llm("список от модели")
        ans = Assistant(chunks=self.chunks, today=date(2026, 10, 6)).ask("Какие ближайшие дедлайны?")
        self.assertEqual(ans.generator, "extractive")
        self.assertIn("Загрузка отчёта", ans.text)
        self.assertEqual(seen, {})

    def test_llm_can_be_switched_off(self):
        seen = self.serve_llm("не должно вызываться")
        os.environ["ASSISTANT_LLM"] = "off"
        ans = Assistant(chunks=self.chunks, today=date(2026, 10, 6)).ask("Сколько баллов за семестр по безопасности?")
        self.assertEqual(ans.generator, "extractive")
        self.assertEqual(seen, {})

    def test_ics_has_only_future_deadlines_with_reminder(self):
        cal = to_ics(upcoming(self.chunks, date(2026, 10, 6)))
        self.assertEqual(cal.count("BEGIN:VEVENT"), 1)
        self.assertIn("DTEND:20261009T205900Z", cal)
        self.assertIn("TRIGGER:-P1D", cal)
        self.assertNotIn("Старое задание", cal)
        self.assertTrue(all(len(line.encode()) <= 75 for line in cal.split("\r\n")))

    def test_digest_lists_week_deadlines_and_alerts(self):
        subject, body = digest(self.chunks, date(2026, 10, 6), ["data_age_days = 9 > 7"])
        self.assertIn("дедлайнов 1", subject)
        self.assertIn("09.10 23:59 Загрузка отчёта", body)
        self.assertIn("data_age_days", body)
        _, empty = digest(self.chunks, date(2026, 10, 20), [])
        self.assertIn("- нет", empty)

    def test_digest_is_sent_over_smtp(self):
        got = {}

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.wfile.write(b"220 fake\r\n")
                data = False
                for raw in self.rfile:
                    line = raw.decode().rstrip("\r\n")
                    if data:
                        if line == ".":
                            data = False
                            self.wfile.write(b"250 ok\r\n")
                        else:
                            got.setdefault("body", []).append(line)
                        continue
                    cmd = line.split(" ")[0].upper()
                    if cmd == "EHLO":
                        self.wfile.write(b"250-fake\r\n250 AUTH PLAIN LOGIN\r\n")
                    elif cmd == "AUTH":
                        got["auth"] = line
                        self.wfile.write(b"235 ok\r\n")
                    elif cmd == "DATA":
                        data = True
                        self.wfile.write(b"354 go\r\n")
                    elif cmd == "QUIT":
                        self.wfile.write(b"221 bye\r\n")
                        return
                    else:
                        got.setdefault("cmds", []).append(line)
                        self.wfile.write(b"250 ok\r\n")

        server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        os.environ.update(SMTP_HOST="127.0.0.1", SMTP_PORT=str(server.server_address[1]), SMTP_SSL="0",
                          SMTP_USER="student@edu.hse.ru", SMTP_PASSWORD="app-password")
        self.assertEqual(send_mail("Тема", "Текст"), "student@edu.hse.ru")
        self.assertIn("AUTH PLAIN", got["auth"])
        self.assertIn("rcpt to:<student@edu.hse.ru>", [c.lower() for c in got["cmds"]])
        self.assertIn("Subject: =?utf-8?b?", "\n".join(got["body"]))

    def serve_caldav(self):
        calls = []
        pages = {
            "/": '<d:multistatus xmlns:d="DAV:"><d:response><d:href>/</d:href><d:propstat><d:prop>'
                 '<d:current-user-principal><d:href>/principals/me/</d:href></d:current-user-principal>'
                 '</d:prop></d:propstat></d:response></d:multistatus>',
            "/principals/me/": '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response>'
                               '<d:href>/principals/me/</d:href><d:propstat><d:prop><c:calendar-home-set>'
                               '<d:href>/calendars/me/</d:href></c:calendar-home-set></d:prop></d:propstat>'
                               '</d:response></d:multistatus>',
            "/calendars/me/": '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                              '<d:response><d:href>/calendars/me/</d:href><d:propstat><d:prop><d:resourcetype>'
                              '<d:collection/></d:resourcetype></d:prop></d:propstat></d:response>'
                              '<d:response><d:href>/calendars/me/todo/</d:href><d:propstat><d:prop>'
                              '<d:displayname>Дела</d:displayname><d:resourcetype><d:collection/><c:calendar/>'
                              '</d:resourcetype><c:supported-calendar-component-set><c:comp name="VTODO"/>'
                              '</c:supported-calendar-component-set></d:prop></d:propstat></d:response>'
                              '<d:response><d:href>/calendars/me/events/</d:href><d:propstat><d:prop>'
                              '<d:displayname>Мои события</d:displayname><d:resourcetype><d:collection/>'
                              '<c:calendar/></d:resourcetype><c:supported-calendar-component-set>'
                              '<c:comp name="VEVENT"/></c:supported-calendar-component-set></d:prop></d:propstat>'
                              '</d:response></d:multistatus>',
        }

        class Handler(BaseHTTPRequestHandler):
            def handle_any(self):
                size = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(size).decode() if size else ""
                calls.append((self.command, self.path, self.headers.get("Authorization"), body))
                out = pages.get(self.path, "").encode() if self.command == "PROPFIND" else b""
                self.send_response(207 if self.command == "PROPFIND" else 201)
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            do_PROPFIND = do_PUT = do_DELETE = handle_any

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return CalDAV(f"http://127.0.0.1:{server.server_port}/", "student@edu.hse.ru", "app-password"), calls

    def test_calendar_sync_sends_only_changes(self):
        client, calls = self.serve_caldav()
        state = Path(tempfile.mkdtemp()) / "state.json"
        self.assertEqual(sync(self.chunks, date(2026, 10, 6), client, state), (1, 0))
        puts = [c for c in calls if c[0] == "PUT"]
        self.assertEqual(len(puts), 1)
        self.assertTrue(puts[0][1].startswith("/calendars/me/events/"))
        self.assertIn("Загрузка отчёта", puts[0][3])
        self.assertTrue(puts[0][2].startswith("Basic "))
        calls.clear()
        self.assertEqual(sync(self.chunks, date(2026, 10, 6), client, state), (0, 0))
        self.assertEqual(calls, [])
        moved = upcoming(self.chunks, date(2026, 10, 6))[0]
        moved.meta["due"] = "2026-10-10T23:59+03:00"
        self.assertEqual(sync(self.chunks, date(2026, 10, 6), client, state), (1, 0))
        self.assertEqual([c[0] for c in calls], ["PUT"])
        calls.clear()
        rest = [c for c in self.chunks if c is not moved]
        self.assertEqual(sync(rest, date(2026, 10, 6), client, state), (0, 1))
        self.assertEqual([c[0] for c in calls], ["DELETE"])
        self.assertEqual(sync(self.chunks, date(2026, 10, 20), client, state), (0, 0))

    def test_schedule_runs_digest_at_nine_and_calendar_every_half_hour(self):
        script = powershell("install", r"C:\Python\pythonw.exe", Path(r"C:\nis"))
        self.assertIn("-Argument '-m assistant digest'", script)
        self.assertIn("-Daily -At 09:00", script)
        self.assertIn("-Argument '-m assistant sync-calendar'", script)
        self.assertIn("New-TimeSpan -Minutes 30", script)
        self.assertIn("-StartWhenAvailable", script)
        self.assertIn("-WorkingDirectory 'C:\\nis'", script)
        self.assertIn("Unregister-ScheduledTask -TaskName NisAssistantDigest", powershell("remove", "py"))
        self.assertIn("0 9 * * *", cron("/usr/bin/python3", Path("/nis")))

    def test_env_file_does_not_override_environment(self):
        env = Path(tempfile.mkdtemp()) / ".env"
        env.write_text("# комментарий\nSMTP_USER=a@ya.ru\nCALDAV_PASSWORD=\"secret\"\n", encoding="utf-8")
        os.environ["SMTP_USER"] = "b@ya.ru"
        load_env(env)
        self.assertEqual(os.environ["SMTP_USER"], "b@ya.ru")
        self.assertEqual(os.environ["CALDAV_PASSWORD"], "secret")

    def call_tool(self, name, **arguments):
        reply = handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                        "params": {"name": name, "arguments": arguments}})
        self.assertEqual(reply["id"], 7)
        self.assertFalse(reply["result"]["isError"], reply)
        return json.loads(reply["result"]["content"][0]["text"])

    def test_mcp_tools_answer_from_materials(self):
        os.environ.update(HSE_PASSPORT_DIR=str(FIXTURES), ASSISTANT_TODAY="2026-10-06")
        self.addCleanup(os.environ.pop, "ASSISTANT_TODAY", None)
        init = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-03-26")
        self.assertIn("tools", init["result"]["capabilities"])
        self.assertIsNone(handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        names = [t["name"] for t in handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]]
        self.assertEqual(names, ["search_materials", "upcoming_deadlines", "monitoring_report"])
        found = self.call_tool("search_materials", query="формула оценки по экономике", k=2)
        self.assertTrue(found["found"])
        self.assertIn("0,29", found["fragments"][0]["text"])
        self.assertEqual(self.call_tool("search_materials", query="рецепт борща"), {"found": False, "fragments": []})
        deadlines = self.call_tool("upcoming_deadlines")
        self.assertEqual([d["due"][:10] for d in deadlines], ["2026-10-09"])
        self.assertIn("alerts", self.call_tool("monitoring_report"))
        self.assertEqual(handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                 "params": {"name": "send_mail"}})["error"]["code"], -32602)
        bad = handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                      "params": {"name": "upcoming_deadlines", "arguments": {"weeks": 1}}})
        self.assertTrue(bad["result"]["isError"])

    def test_mcp_server_speaks_json_rpc_over_stdio(self):
        env = dict(os.environ, HSE_PASSPORT_DIR=str(FIXTURES), ASSISTANT_TODAY="2026-10-06", PYTHONIOENCODING="utf-8")
        lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                 {"jsonrpc": "2.0", "method": "notifications/initialized"},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "upcoming_deadlines", "arguments": {"days": 7}}},
                 {"jsonrpc": "2.0", "id": 3, "method": "resources/list"}]
        out = subprocess.run([sys.executable, "-m", "assistant", "mcp"], input="\n".join(json.dumps(m) for m in lines),
                             capture_output=True, text=True, encoding="utf-8", env=env,
                             cwd=Path(__file__).resolve().parents[1], timeout=60)
        replies = [json.loads(line) for line in out.stdout.splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2, 3], out.stderr)
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "nis-study-assistant")
        self.assertIn("Загрузка отчёта", replies[1]["result"]["content"][0]["text"])
        self.assertEqual(replies[2]["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
