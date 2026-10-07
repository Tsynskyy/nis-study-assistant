from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import requests

from lms.auth import HSEAuthSession, AuthError
from lms.legacy_auth import LegacyLMSAuthSession
from lms.locking import output_lock
from lms.passport import export_passport
from lms.refresh import refresh
from lms.sync import SyncOptions, sync_all, write_json




class FakeClient:
    def __init__(self, courses):
        self.catalog = courses
        self.session = Mock()
        self.clones = []
        self.calls = []
        self.fail = False

    def clone(self):
        self.clones.append(threading.get_ident())
        return self

    def courses(self, _): return self.catalog
    def course_state(self, cid):
        self.calls.append((cid, "state"))
        if self.fail:
            raise ValueError("not JSON")
        return {}
    def course_page(self, cid):
        self.calls.append((cid, "page"))
        return {"sections": []}
    def course_events(self, cid):
        self.calls.append((cid, "events"))
        return []
    def grades(self, cid):
        self.calls.append((cid, "grades"))
        return {"available": True, "tables": []}
    def notifications(self): return {"data": {"notifications": [], "unreadcount": 0}}
    def conversations(self): return []


def course(cid=1, current=False):
    now = datetime.now(timezone.utc)
    return {"id": cid, "fullname": f"Course {cid}",
            "startdate": int((now - timedelta(days=800)).timestamp()),
            "enddate": int((now + timedelta(days=30) if current else now - timedelta(days=400)).timestamp())}


class FastSyncTests(unittest.TestCase):
    def test_fast_preserves_catalog_freshness_and_reuses_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient([course(1), course(2, True), course(3)])
            first = sync_all(client, "", SyncOptions(root, workers=1))
            self.assertEqual(len(client.clones), 1)
            original = first["course_results"][0]["resources"]["grades"]["checked_at"]
            client.calls.clear()
            report = sync_all(client, "", SyncOptions(root, workers=1, mode="fast", watch_course_ids={3}))
            self.assertEqual(report["course_count"], 3)
            self.assertEqual(report["resources_cached"], 4)
            self.assertEqual(report["resources_fetched"], 8)
            self.assertFalse(any(cid == 1 for cid, _ in client.calls))
            cached = report["course_results"][0]["resources"]["grades"]
            self.assertEqual(cached["checked_at"], original)
            self.assertEqual(cached["attempts"], 0)
            output = root / "passport"
            result = export_passport(root, output)
            self.assertEqual(result["cached_resource_count"], 4)
            self.assertIn("Время снимка не означает", (output / "LMS.md").read_text(encoding="utf-8"))
            self.assertEqual(len(json.loads((root / "courses/index.json").read_text(encoding="utf-8"))), 3)

    def test_missing_corrupt_expired_and_failed_resources_are_fetched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); client = FakeClient([course()])
            report = sync_all(client, "", SyncOptions(root))
            resources = report["course_results"][0]["resources"]
            resources["state"]["checked_at"] = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
            resources["grades"]["status"] = "error"
            write_json(root / "snapshot.json", report)
            (root / "courses/1/page.json").unlink()
            (root / "courses/1/events.json").write_text("corrupt")
            result = sync_all(client, "", SyncOptions(root, mode="fast"))
            self.assertEqual(result["resources_fetched"], 4)

    def test_new_changed_and_upcoming_courses_are_never_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); client = FakeClient([course(1), course(2)])
            sync_all(client, "", SyncOptions(root))
            client.catalog[0]["fullname"] = "Changed"
            write_json(root / "courses/2/events.json", [{"timestart": datetime.now(timezone.utc).timestamp() + 1000}])
            client.catalog.append(course(3))
            result = sync_all(client, "", SyncOptions(root, mode="fast"))
            self.assertEqual(result["resources_fetched"], 12)

    def test_partial_errors_stop_passport_and_are_retried_next_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); client = FakeClient([course()])
            sync_all(client, "", SyncOptions(root))
            client.fail = True
            report = sync_all(client, "", SyncOptions(root))
            self.assertTrue(report["course_results"][0]["errors"])
            with self.assertRaisesRegex(ValueError, "частичные ошибки"):
                export_passport(root, root / "passport")
            client.fail = False
            report = sync_all(client, "", SyncOptions(root, mode="fast"))
            self.assertEqual(report["resources_fetched"], 1)
            self.assertEqual(report["resources_cached"], 3)

    def test_timeout_retries_record_attempt_count_without_url_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient([course()]); counter = iter([True, False])
            def state(cid):
                if next(counter): raise requests.Timeout("secret=do-not-log")
                return {}
            client.course_state = state
            with patch.object(threading.Event, "wait", return_value=False):
                report = sync_all(client, "", SyncOptions(Path(directory)))
            resource = report["course_results"][0]["resources"]["state"]
            self.assertEqual(resource["attempts"], 2)
            self.assertNotIn("do-not-log", json.dumps(report))

    def test_cancel_stops_remaining_courses_and_marks_snapshot_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); event = threading.Event()
            client = FakeClient([course(i) for i in range(1, 31)])
            def progress(_): event.set()
            with self.assertRaisesRegex(RuntimeError, "отменен"):
                sync_all(client, "", SyncOptions(root, workers=1, progress=progress, cancel_event=event))
            self.assertEqual(len(client.calls), 4)
            self.assertEqual(json.loads((root / "snapshot.json").read_text(encoding="utf-8"))["status"], "running")
            with self.assertRaises(ValueError): export_passport(root, root / "passport")

    def test_fast_disallows_partial_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                sync_all(FakeClient([]), "", SyncOptions(Path(directory), mode="fast", course_ids={1}))


class RecoveryTests(unittest.TestCase):
    def test_expired_cookies_cleared_before_login_for_both_systems(self):
        for cls in (HSEAuthSession, LegacyLMSAuthSession):
            session = cls()
            session.session.cookies.set("old", "expired")
            session.load_cookies = Mock()
            session.check = Mock(return_value=None)
            def login(*_):
                self.assertEqual(len(session.session.cookies), 0)
                raise AuthError("OTP required")
            session.login = Mock(side_effect=login)
            with self.assertRaisesRegex(AuthError, "OTP"):
                session.ensure_authenticated("user", lambda: "password")
            self.assertEqual(session.login.call_count, 1)

    def test_active_session_keeps_cookies_and_never_requests_password(self):
        for cls in (HSEAuthSession, LegacyLMSAuthSession):
            session = cls(); session.session.cookies.set("good", "value")
            session.load_cookies = Mock(); session.check = Mock(return_value="ok")
            password = Mock()
            self.assertEqual(session.ensure_authenticated("user", password), ("ok", False))
            password.assert_not_called()
            self.assertEqual(session.session.cookies.get("good"), "value")

    def test_second_writer_refused_and_lock_released(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with output_lock(root):
                with self.assertRaises(ValueError):
                    with output_lock(root): pass
            with output_lock(root): pass


class RefreshTransactionTests(unittest.TestCase):
    def test_publish_failure_rolls_back_installed_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); output = root / "passport"; output.mkdir()
            (output / "LMS.md").write_text("unchanged")
            before = {p.name: p.read_bytes() for p in output.iterdir()}
            raw = root / "raw"
            sync_all(FakeClient([course()]), "", SyncOptions(raw))
            replace = Path.replace
            installs = []
            def fail_third(path, target):
                target = Path(target)
                if output in target.parents and ".backup" not in path.parts:
                    installs.append(target)
                    if len(installs) == 3: raise OSError("test filesystem failure")
                return replace(path, target)
            with patch.object(Path, "replace", fail_third):
                with self.assertRaises(OSError):
                    refresh(raw, output, {"ok": lambda _: 0}, progress=lambda _: None)
            self.assertEqual({p.name: p.read_bytes() for p in output.rglob("*") if p.is_file()}, before)

    def test_failed_source_preserves_existing_files_and_runs_other_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); output = root / "passport"; output.mkdir()
            (output / "LMS.md").write_text("unchanged")
            other = Mock(return_value=0)
            with self.assertRaisesRegex(ValueError, "Не все источники"):
                refresh(root / "raw", output, {"bad": lambda _: 2, "other": other}, progress=lambda _: None)
            other.assert_called_once()
            self.assertEqual((output / "LMS.md").read_text(encoding="utf-8"), "unchanged")

    def test_success_builds_summary_and_preserves_curated_prose(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); output = root / "passport"; output.mkdir()
            (output / "AUTUMN-2026.md").write_text("curated")
            raw = root / "raw"
            sync_all(FakeClient([course()]), "", SyncOptions(raw))
            report = refresh(raw, output, {"test": lambda _: 0}, progress=lambda _: None)
            self.assertEqual(report["status"], "complete")
            self.assertEqual((output / "AUTUMN-2026.md").read_text(encoding="utf-8"), "curated")
            self.assertTrue((output / "LMS.md").is_file())
            self.assertTrue((output / "data/lms_deadlines.csv").is_file())


if __name__ == "__main__":
    unittest.main()
