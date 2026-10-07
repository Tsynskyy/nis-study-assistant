from __future__ import annotations

import hashlib
import ssl
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from lms.tbank import TBankAuthSession, TBankClient, TBankTLSAdapter, _uuid, sync_tbank
from lms.auth import AuthError


class Response:
    def __init__(self, body, status_code=200):
        self.body = body
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class TBankAuthTests(unittest.TestCase):
    def test_invalid_email_never_reaches_network(self):
        for email in ("ВАША_ПОЧТА", "ADDRESS", "a@", "a b@example.com", "a@@example.com"):
            auth = TBankAuthSession()
            auth.session = Mock()
            with self.subTest(email=email), self.assertRaises(AuthError):
                auth.login(email, "test-password")
            auth.session.get.assert_not_called()
            auth.session.post.assert_not_called()

    def test_action_400_has_useful_error_and_does_not_send_password(self):
        auth = TBankAuthSession()
        auth.session = Mock()
        auth.session.get.return_value = Response({})
        auth.session.post.return_value = Response({"error": "invalid_model"}, 400)
        with self.assertRaisesRegex(AuthError, "HTTP 400"):
            auth.login(" student@example.com ", "test-password")
        auth.session.post.assert_called_once()
        self.assertEqual(auth.session.post.call_args.kwargs["json"]["input"], "student@example.com")
        self.assertNotIn("password", auth.session.post.call_args.kwargs["json"])

    def test_extra_ca_is_scoped_to_exact_origin(self):
        session = TBankAuthSession().session
        self.assertIsInstance(session.get_adapter("https://edu.tbank.ru/sign-in"), TBankTLSAdapter)
        for url in ("https://edu.hse.ru/", "https://education.tbank.ru/",
                    "https://edu.tbank.ru.evil.example/", "http://edu.tbank.ru/"):
            with self.subTest(url=url):
                self.assertNotIsInstance(session.get_adapter(url), TBankTLSAdapter)
                with self.assertRaises(requests.exceptions.SSLError):
                    TBankTLSAdapter().cert_verify(Mock(), url, True, None)

    def test_bundled_ca_keeps_verification_enabled(self):
        connection = Mock()
        TBankTLSAdapter().cert_verify(connection, "https://edu.tbank.ru/sign-in", False, None)
        self.assertEqual(connection.cert_reqs, "CERT_REQUIRED")
        pem = Path(connection.ca_certs).read_text(encoding="utf-8")
        self.assertEqual(hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest(),
                         "d26d2d0231b7c39f92cc738512ba54103519e4405d68b5bd703e9788ca8ecf31")
        context = ssl.create_default_context(cafile=connection.ca_certs)
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_login_keeps_password_out_of_session_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "tbank-session.json"
            config = Path(directory) / "config" / "tbank.json"
            auth = TBankAuthSession()
            auth.session = Mock()
            auth.session.cookies = requests.cookies.RequestsCookieJar()
            auth.session.cookies.set("token", "private-token", domain="edu.tbank.ru", path="/")
            auth.session.cookies.set("refresh_token", "private-refresh", domain="edu.tbank.ru", path="/")
            auth.session.cookies.set("unrelated", "tracking", domain="other.example", path="/")
            auth.session.get.return_value = Response({})
            auth.session.post.side_effect = [
                Response({"action": "input-password"}), Response({"user": {"id": 1}}),
            ]
            with (
                patch("lms.tbank.session_path", return_value=path),
                patch("lms.tbank.config_path", return_value=config),
                patch.object(auth, "check", return_value=True),
            ):
                auth.login("student@example.com", "test-password")
            contents = path.read_text(encoding="utf-8")
            self.assertNotIn("test-password", contents)
            self.assertNotIn("tracking", contents)
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(json.loads(contents)["email"], "student@example.com")
            self.assertEqual(json.loads(config.read_text(encoding="utf-8"))["email"], "student@example.com")

    def test_load_ignores_foreign_cookies(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tbank-session.json"
            config = Path(directory) / "tbank-config.json"
            path.write_text(json.dumps({
                "version": 1, "email": "student@example.com", "cookies": [
                    {"name": "token", "value": "bad", "domain": "attacker.example"},
                    {"name": "token", "value": "good", "domain": "edu.tbank.ru"},
                ],
            }))
            auth = TBankAuthSession()
            with (
                patch("lms.tbank.session_path", return_value=path),
                patch("lms.tbank.config_path", return_value=config),
            ):
                self.assertTrue(auth.load())
            self.assertEqual(len(auth.session.cookies), 1)
            self.assertEqual(auth.session.cookies.get("token", domain="edu.tbank.ru", path="/"), "good")

    def test_client_accepts_only_internal_api_paths(self):
        client = TBankClient(TBankAuthSession())
        for value in ("https://evil.example/api/x", "//evil.example/api/x", "/api/../admin"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                client.get_json(value)
        with self.assertRaises(ValueError):
            _uuid("../../other")

    def test_redirected_session_is_not_considered_active(self):
        auth = TBankAuthSession()
        auth.session = Mock()
        auth.session.get.return_value = Response({}, status_code=302)
        self.assertFalse(auth.check())


class FakeClient:
    def passing_activities(self):
        return [{"activityId": "11111111-1111-1111-1111-111111111111",
                 "title": "Example course", "entityType": 1}]

    def completed_activities(self):
        return []

    def course_streams(self, course_id):
        return [{"streamId": "22222222-2222-2222-2222-222222222222"}]

    def course_info(self, stream_id):
        return {
            "title": "Example course", "userCourseStreamId": "enrolled",
            "modules": [{"title": "Week 1", "units": [
                {"id": "33333333-3333-3333-3333-333333333333", "type": "theory"},
                {"id": "44444444-4444-4444-4444-444444444444", "type": "exam"},
            ]}],
        }

    def course_progress(self, stream_id):
        return {"score": 3, "units": []}

    def unit(self, stream_id, unit_id):
        return {"id": unit_id, "description": "course material"}

    def practice(self, unit_id):
        return {"tasks": [{"description": "assignment"}], "info": {}}

    def practice_history(self, unit_id):
        return [{"score": 3}]


class TBankSyncTests(unittest.TestCase):
    def test_sync_saves_course_lessons_and_assignment(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "tbank-data"
            report = sync_tbank(FakeClient(), output)
            stream = report["course_results"][0]["streams"][0]
            self.assertEqual((stream["units"], stream["exams"], stream["errors"]), (2, 1, []))
            base = output / "courses" / "11111111-1111-1111-1111-111111111111" / "streams" / "22222222-2222-2222-2222-222222222222"
            self.assertEqual(json.loads((base / "progress.json").read_text(encoding="utf-8"))["score"], 3)
            self.assertEqual(
                json.loads((base / "units" / "44444444-4444-4444-4444-444444444444" / "practice.json").read_text(encoding="utf-8"))["tasks"][0]["description"],
                "assignment",
            )
            if os.name == "posix":
                self.assertEqual((base / "info.json").stat().st_mode & 0o777, 0o600)
                self.assertEqual(output.stat().st_mode & 0o777, 0o700)

    def test_unavailable_unit_does_not_hide_other_units(self):
        client = FakeClient()
        original = client.unit

        def unit(stream_id, unit_id):
            if unit_id.startswith("3333"):
                raise requests.Timeout()
            return original(stream_id, unit_id)

        client.unit = unit
        with tempfile.TemporaryDirectory() as directory:
            report = sync_tbank(client, Path(directory) / "tbank-data")
            stream = report["course_results"][0]["streams"][0]
            self.assertEqual((stream["units"], stream["exams"]), (1, 1))
            self.assertEqual(len(stream["errors"]), 1)


if __name__ == "__main__":
    unittest.main()
