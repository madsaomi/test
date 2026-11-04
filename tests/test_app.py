"""Автотесты приложения: python -m unittest discover -s tests -v

Каждый запуск работает на временной базе; Telegram подменяется заглушкой,
реальных запросов наружу нет.
"""
import gzip
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

# Окружение задаем ДО импорта приложения: модули читают его при импорте
_TMP = tempfile.mkdtemp(prefix="slc-tests-")
os.environ.update({
    "DATABASE_PATH": os.path.join(_TMP, "test.db"),
    "BOT_TOKEN": "123:TESTTOKEN",
    "CHAT_ID": "111",
    "ADMIN_TOKEN": "admintoken",
    "BACKGROUND_JOBS": "0",
    "TRUSTED_PROXIES": "1",
})
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402
import database  # noqa: E402
import test_session  # noqa: E402

PHONE = "+998901234567"


class FakeResp:
    def __init__(self, ok=True, status=200, body=None):
        self.ok = ok
        self.status_code = status
        self.text = "ok" if ok else "error"
        self._body = body or {}

    def json(self):
        return self._body


class BaseCase(unittest.TestCase):
    """Чистая база, заглушка Telegram и синхронная отправка в каждом тесте"""

    def setUp(self):
        with database.get_db() as conn:
            for table in ("test_results", "test_sessions", "rate_hits", "report_markers"):
                conn.execute(f"DELETE FROM {table}")
        self.client = app.app.test_client()
        self.sent = []
        self.tg_ok = True

        def fake_post(url, **kw):
            self.sent.append((url.rsplit("/", 1)[1], kw))
            return FakeResp(self.tg_ok, 200 if self.tg_ok else 400)

        patches = [
            mock.patch.object(app.requests, "post", side_effect=fake_post),
            mock.patch.object(app, "send_to_telegram_async",
                              side_effect=lambda m, result_id=None, photo_bytes=None:
                              app._send_tg_worker(m, result_id, photo_bytes)),
            mock.patch.object(app.time, "sleep", lambda s: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    # --- помощники ---
    def start(self, ip="10.0.0.1"):
        r = self.client.post("/test/start", headers={"X-Forwarded-For": ip})
        self.assertEqual(r.status_code, 200)
        return r.get_json()["sid"]

    def session(self, sid):
        return test_session.load_session(sid)

    def shift_clock(self, sid, seconds, key="shown_at"):
        """Делает вид, что прошло seconds секунд (сдвигает метку в прошлое)"""
        d = self.session(sid)
        d[key] -= seconds
        test_session.save_session(sid, d, finished=d.get("finished", False))

    @staticmethod
    def correct_answer(q):
        return q["answer"][0] if isinstance(q["answer"], list) else q["answer"]

    def answer(self, sid, pos, answer):
        return self.client.post(f"/api/answer/{sid}", json={"pos": pos, "answer": answer})

    def finish(self, sid, correct=0, delay=5):
        """Отвечает на все вопросы: первые `correct` верно, остальные пусто"""
        for pos in range(1, 51):
            self.shift_clock(sid, delay)
            q = self.session(sid)["questions"][pos - 1]
            self.assertEqual(self.answer(sid, pos, self.correct_answer(q) if pos <= correct else "").status_code, 200)

    def submit(self, sid, **fields):
        body = {"sid": sid, "fio": "Тест Тестов", "phone": PHONE, "branch": "Офис"}
        body.update(fields)
        return self.client.post("/submit", json=body, headers={"X-Forwarded-For": "10.0.0.1"})

    def results(self):
        with database.get_db() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM test_results ORDER BY id")]


class TestQuestions(BaseCase):
    def test_start_hides_answer_key(self):
        r = self.client.post("/test/start").get_json()
        self.assertEqual(r["total"], 50)
        self.assertNotIn("answer", r["question"])

    def test_session_follows_level_plan(self):
        qs = self.session(self.start())["questions"]
        self.assertEqual(len(qs), 50)
        self.assertEqual([q["type"] for q in qs].count("input"), 5)
        self.assertTrue(all(q["type"] == "input" for q in qs[45:]))

    def test_correct_and_wrong_answers_scored_on_server(self):
        sid = self.start()
        q1 = self.session(sid)["questions"][0]
        self.shift_clock(sid, 5)
        self.answer(sid, 1, self.correct_answer(q1))
        wrong = next(o for o in self.session(sid)["questions"][1]["options"]
                     if o != self.session(sid)["questions"][1]["answer"])
        self.shift_clock(sid, 5)
        self.answer(sid, 2, wrong)
        self.assertEqual(self.session(sid)["correct"], {"1": 1, "2": 0})

    def test_out_of_order_and_boolean_position_rejected(self):
        sid = self.start()
        self.assertEqual(self.answer(sid, 2, "x").status_code, 409)
        r = self.client.post(f"/api/answer/{sid}", json={"pos": True, "answer": "x"})
        self.assertEqual(r.status_code, 409)

    def test_late_answer_not_counted(self):
        sid = self.start()
        q1 = self.session(sid)["questions"][0]
        # Фиксированные числа, а не ANSWER_GRACE_SECONDS: иначе тест не заметит,
        # если запас случайно раздуют и таймер перестанет работать
        self.shift_clock(sid, q1["time"] + 30)
        self.answer(sid, 1, self.correct_answer(q1))
        d = self.session(sid)
        self.assertEqual(d["correct"]["1"], 0)
        self.assertEqual(d["answers"]["1"], "")
        self.assertEqual(d["too_slow"], 1)
        self.assertEqual(d["late_answers"]["1"], self.correct_answer(q1))

    def test_answer_within_grace_counted(self):
        sid = self.start()
        q1 = self.session(sid)["questions"][0]
        self.shift_clock(sid, q1["time"] + 3)  # опоздание по сети в пределах запаса
        self.answer(sid, 1, self.correct_answer(q1))
        self.assertEqual(self.session(sid)["correct"]["1"], 1)
        self.assertEqual(self.session(sid)["too_slow"], 0)

    def test_resync_keeps_clock_and_returns_time_left(self):
        sid = self.start()
        self.shift_clock(sid, 12)
        before = self.session(sid)["shown_at"]
        q = self.client.get(f"/api/question/{sid}").get_json()["question"]
        self.assertIn(q["time_left"], (17, 18))
        self.assertAlmostEqual(self.session(sid)["shown_at"], before, places=2)


class TestSubmit(BaseCase):
    def test_full_flow_saves_result_and_sends_sheet(self):
        sid = self.start()
        self.finish(sid, correct=20)
        r = self.submit(sid, source="Instagram", work_study="Школа 17", phone2="931234567")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["score"], 20)
        self.assertEqual(r.get_json()["level"], "Elementary")
        row = self.results()[0]
        self.assertEqual((row["source"], row["phone2"], row["telegram_sent"]), ("Instagram", "+998931234567", 1))
        self.assertEqual(self.sent[0][0], "sendPhoto")

    def test_second_submit_rejected(self):
        sid = self.start()
        self.finish(sid)
        self.assertEqual(self.submit(sid).status_code, 200)
        self.assertEqual(self.submit(sid).status_code, 400)
        self.assertEqual(len(self.results()), 1)

    def test_unfinished_test_rejected(self):
        self.assertEqual(self.submit(self.start()).status_code, 400)

    def test_branch_whitelisted_and_unknown_source_dropped(self):
        for branch in ("", "Мусор <b>x</b>"):
            sid = self.start()
            self.finish(sid)
            self.assertEqual(self.submit(sid, branch=branch).status_code, 400)
        sid = self.start()
        self.finish(sid)
        self.assertEqual(self.submit(sid, source="что угодно").status_code, 200)
        self.assertIsNone(self.results()[0]["source"])

    def test_phone_and_name_validated(self):
        sid = self.start()
        self.finish(sid)
        self.assertEqual(self.submit(sid, phone="+99890123").status_code, 400)
        self.assertEqual(self.submit(sid, fio="Ая").status_code, 400)

    def test_honeypot_rejects(self):
        sid = self.start()
        self.finish(sid)
        self.assertEqual(self.submit(sid, hp="http://spam").status_code, 400)

    def test_duration_measured_on_server(self):
        sid = self.start()
        self.finish(sid)
        self.shift_clock(sid, 25 * 60, key="started_at")
        self.submit(sid, time="0 мин 1 сек")  # время клиента игнорируется
        self.assertTrue(self.results()[0]["time_spent"].startswith("25 мин"))


class TestTelegram(BaseCase):
    def test_failed_send_stays_unsent_and_hides_token(self):
        def network_error(url, **kw):
            raise app.requests.exceptions.ConnectionError(f"Max retries exceeded with url: {url}")
        sid = self.start()
        self.finish(sid)
        with mock.patch.object(app.requests, "post", side_effect=network_error):
            self.submit(sid)
        row = self.results()[0]
        self.assertEqual(row["telegram_sent"], 0)
        self.assertNotIn("TESTTOKEN", row["telegram_error"])
        self.assertIn("<BOT_TOKEN>", row["telegram_error"])

    def test_resend_waits_then_delivers_once(self):
        self.tg_ok = False
        sid = self.start()
        self.finish(sid)
        self.submit(sid)
        self.assertEqual(database.claim_unsent_results(), [])  # свежий — еще не трогаем
        with database.get_db() as conn:
            conn.execute("UPDATE test_results SET created_at = datetime('now', '-20 minutes')")
        self.tg_ok = True
        self.sent.clear()
        app._resend_unsent()
        self.assertEqual([k for k, _ in self.sent], ["sendPhoto"])
        self.assertIn("досылка", self.sent[0][1]["data"]["caption"])
        self.assertEqual(self.results()[0]["telegram_sent"], 1)
        self.sent.clear()
        app._resend_unsent()
        self.assertEqual(self.sent, [])


    def test_repeat_attempt_shown_to_admins(self):
        for _ in range(2):
            sid = self.start()
            self.finish(sid, correct=10)
            self.submit(sid)
        first, second = (kw["data"]["caption"] for _, kw in self.sent)
        self.assertNotIn("Повторная сдача", first)
        self.assertIn("попытка №2", second)
        self.assertIn("прошлая: 10/50", second)

    def test_429_waits_retry_after(self):
        answers = [FakeResp(False, 429, {"parameters": {"retry_after": 7}}), FakeResp(True)]
        sleeps = []
        with mock.patch.object(app.requests, "post", side_effect=lambda url, **kw: answers.pop(0)),                 mock.patch.object(app.time, "sleep", side_effect=sleeps.append):
            self.assertTrue(app._send_tg_worker("hi"))
        self.assertEqual(sleeps, [8])


class TestLimits(BaseCase):
    def test_start_limit_uses_real_ip_not_spoofed(self):
        for i in range(app.SESSION_START_LIMIT):
            r = self.client.post("/test/start", headers={"X-Forwarded-For": f"9.9.9.{i}, 7.7.7.7"})
            self.assertEqual(r.status_code, 200)
        r = self.client.post("/test/start", headers={"X-Forwarded-For": "1.1.1.1, 7.7.7.7"})
        self.assertEqual(r.status_code, 429)
        self.assertEqual(self.client.post("/test/start", headers={"X-Forwarded-For": "8.8.8.8"}).status_code, 200)

    def test_admin_lockout_after_wrong_tokens(self):
        self.assertEqual(self.client.get("/admin/results?key=ключ").status_code, 403)  # не 500
        for i in range(app.ADMIN_FAIL_LIMIT - 1):
            self.client.get(f"/admin/results?key=bad{i}")
        self.assertEqual(self.client.get("/admin/results", headers={"X-Admin-Token": "admintoken"}).status_code, 429)
        other = self.client.get("/admin/results", headers={"X-Admin-Token": "admintoken", "X-Forwarded-For": "5.5.5.5"})
        self.assertEqual(other.status_code, 200)


class TestHttp(BaseCase):
    def test_404_html_for_browser_json_for_api(self):
        html = {"Accept": "text/html"}
        self.assertIn("text/html", self.client.get("/nope", headers=html).content_type)
        self.assertIn("json", self.client.get("/api/question/xyz", headers=html).content_type)
        self.assertIn("json", self.client.get("/nope", headers={"Accept": "application/json"}).content_type)

    def test_security_headers(self):
        h = self.client.get("/").headers
        self.assertIn("frame-ancestors 'none'", h["Content-Security-Policy"])
        self.assertEqual(h["X-Frame-Options"], "DENY")
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")

    def test_form_rendered_from_option_lists(self):
        page = self.client.get("/").get_data(as_text=True)
        for value, _ in app.BRANCHES + app.SOURCES:
            self.assertIn(f'value="{value}"', page)


class TestBackup(BaseCase):
    def _make_result(self):
        sid = self.start()
        self.finish(sid)
        self.submit(sid)

    def test_backup_is_valid_db_without_sessions(self):
        self._make_result()
        self.start()  # незавершенная сессия не должна попасть в копию
        name, data, rows = app.build_backup()
        self.assertTrue(name.endswith(".db.gz"))
        path = os.path.join(_TMP, "restored.db")
        with open(path, "wb") as f:
            f.write(gzip.decompress(data))
        conn = sqlite3.connect(path)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM test_results").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM test_sessions").fetchone()[0], 0)
        finally:
            conn.close()
        self.assertEqual(rows, 1)

    def test_daily_backup_sent_once(self):
        self._make_result()
        self.sent.clear()
        with mock.patch.object(app, "BACKUP_HOUR", 0):
            app._maybe_send_backup()
            app._maybe_send_backup()
        self.assertEqual([k for k, _ in self.sent], ["sendDocument"])

    def test_failed_backup_retried_next_cycle(self):
        self.tg_ok = False
        with mock.patch.object(app, "BACKUP_HOUR", 0):
            app._maybe_send_backup()
            self.tg_ok = True
            self.sent.clear()
            app._maybe_send_backup()
        self.assertEqual([k for k, _ in self.sent], ["sendDocument"])

    def test_admin_backup_download(self):
        self._make_result()
        self.assertEqual(self.client.get("/admin/backup").status_code, 403)
        r = self.client.get("/admin/backup", headers={"X-Admin-Token": "admintoken"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(gzip.decompress(r.data)[:15], b"SQLite format 3")


class TestAdminPage(BaseCase):
    def login(self, token="admintoken"):
        return self.client.post("/admin/login", data={"token": token})

    def test_login_form_without_cookie(self):
        r = self.client.get("/admin")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Токен администратора", r.get_data(as_text=True))

    def test_wrong_token_rejected_and_counted(self):
        self.assertEqual(self.login("bad").status_code, 403)
        with database.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM rate_hits WHERE kind='admin_fail'").fetchone()[0], 1)

    def test_login_sets_signed_cookie_not_token(self):
        r = self.login()
        self.assertEqual(r.status_code, 303)
        cookie = r.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertNotIn("admintoken", cookie)

    def test_page_lists_results_escaped(self):
        sid = self.start()
        self.finish(sid, correct=12)
        self.submit(sid, fio="<b>Хакер</b> Тест")
        self.login()
        page = self.client.get("/admin").get_data(as_text=True)
        self.assertIn("&lt;b&gt;Хакер&lt;/b&gt;", page)
        self.assertNotIn("<b>Хакер</b>", page)
        self.assertIn("12<small>/50", page)

    def test_cookie_grants_api_and_sheet(self):
        sid = self.start()
        self.finish(sid)
        self.submit(sid)
        rid = self.results()[0]["id"]
        self.assertEqual(self.client.get(f"/admin/sheet/{rid}").status_code, 403)
        self.login()
        self.assertEqual(self.client.get("/admin/results").status_code, 200)
        r = self.client.get(f"/admin/sheet/{rid}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "image/jpeg")
        self.assertEqual(self.client.get("/admin/sheet/999999").status_code, 404)

    def test_sheet_has_no_none_for_empty_fields(self):
        sid = self.start()
        self.finish(sid)
        self.submit(sid)  # без второго номера, учебы и источника
        self.login()
        drawn = []
        real_text = app.sheet_generator.ImageDraw.ImageDraw.text
        def spy(draw, xy, text, *a, **kw):
            drawn.append(str(text))
            return real_text(draw, xy, text, *a, **kw)
        with mock.patch.object(app.sheet_generator.ImageDraw.ImageDraw, "text", spy):
            self.client.get(f"/admin/sheet/{self.results()[0]['id']}")
        self.assertTrue(drawn)
        self.assertNotIn("None", " ".join(drawn))

    def _result(self, phone=PHONE, branch="Офис", correct=10, late=0):
        sid = self.start()
        for pos in range(1, 51):
            q = self.session(sid)["questions"][pos - 1]
            self.shift_clock(sid, 60 if pos <= late else 5)
            self.answer(sid, pos, self.correct_answer(q) if pos <= correct else "")
        self.shift_clock(sid, 20 * 60, key="started_at")  # реалистичные 20 минут, без флага скорости
        self.submit(sid, phone=phone, branch=branch)
        return self.results()[-1]["id"]

    def test_filters_by_branch_and_flagged(self):
        self._result(phone="+998900000001", branch="Офис")
        self._result(phone="+998900000002", branch="Универ", late=2)
        self.login()
        page = lambda q: self.client.get("/admin" + q).get_data(as_text=True)
        self.assertIn("+998900000002", page("?period=all&branch=Универ"))
        self.assertNotIn("+998900000001", page("?period=all&branch=Универ"))
        flagged = page("?period=all&flagged=1")
        self.assertIn("+998900000002", flagged)
        self.assertNotIn("+998900000001", flagged)
        self.assertIn("2 ответа пришли после таймера", flagged)

    def test_attempts_numbered_per_phone(self):
        self._result()
        self._result()
        self.login()
        self.assertIn("Попытка 2 из 2", self.client.get("/admin?period=all").get_data(as_text=True))

    def test_student_page(self):
        first = self._result(correct=5)
        rid = self._result(correct=15, late=1)  # первый верный ответ опоздал — не засчитан
        self.login()
        page = self.client.get(f"/admin/result/{rid}").get_data(as_text=True)
        self.assertIn("14<small> из 50", page)
        self.assertIn("1 ответ пришел после таймера — не засчитан<", page)
        self.assertIn(f'href="/admin/result/{first}"', page)  # ссылка на прошлую попытку
        self.assertIn(f'src="/admin/sheet/{rid}"', page)
        self.assertEqual(self.client.get("/admin/result/999999").status_code, 404)

    def test_student_page_requires_login(self):
        rid = self._result()
        r = self.client.get(f"/admin/result/{rid}")
        self.assertEqual(r.status_code, 303)

    def test_export_csv_for_excel(self):
        sid = self.start()
        self.finish(sid)
        self.submit(sid, fio="=HYPERLINK(1) Тест")
        self.login()
        r = self.client.get("/admin/export.csv?period=all")
        self.assertEqual(r.status_code, 200)
        text = r.data.decode("utf-8")
        self.assertTrue(text.startswith("﻿ID;Дата;ФИО"))
        self.assertIn(";	=HYPERLINK(1) Тест;", text)   # формула обезврежена
        self.assertIn(";	+998901234567;", text)        # телефон — текстом, не числом

    def test_plural_forms(self):
        self.assertEqual([app.plural(n, "ответ", "ответа", "ответов") for n in (1, 2, 5, 11, 21, 22)],
                         ["ответ", "ответа", "ответов", "ответов", "ответ", "ответа"])

    def test_logout_clears_access(self):
        self.login()
        self.client.post("/admin/logout")
        self.assertIn("Токен администратора", self.client.get("/admin").get_data(as_text=True))
        self.assertEqual(self.client.get("/admin/results").status_code, 403)


class TestMigration(unittest.TestCase):
    def test_init_db_upgrades_old_schema(self):
        path = os.path.join(_TMP, "old.db")
        conn = sqlite3.connect(path)
        conn.execute("""CREATE TABLE test_results (id INTEGER PRIMARY KEY, created_at TIMESTAMP,
            fio TEXT, phone TEXT, branch TEXT, score INT, total INT, percentage REAL, level TEXT,
            time_spent TEXT, answers_json TEXT, telegram_sent INT DEFAULT 0, telegram_error TEXT)""")
        conn.commit()
        conn.close()
        with mock.patch.object(database, "DB_PATH", path):
            database.init_db()
            database.init_db()  # повторный запуск не падает
            with database.get_db() as c:
                cols = {r[1] for r in c.execute("PRAGMA table_info(test_results)")}
        self.assertTrue({"questions_json", "telegram_attempts", "source"} <= cols)


if __name__ == "__main__":
    unittest.main()
