import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from lms import auth


class AuthTests(unittest.TestCase):
    def test_private_json_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "state" / "value.json"
            auth._private_json(target, {"ok": True})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"ok": True})
            if os.name == "posix":
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)


    def test_authentication_detection(self) -> None:
        class Response:
            url = "https://edu.hse.ru/my/courses.php"
            text = '<script>window.M={"sesskey":"abc"}</script>'

        self.assertTrue(auth.HSEAuthSession._looks_authenticated(Response()))

    def test_macos_keychain_store_keeps_password_out_of_process_arguments(self) -> None:
        completed = Mock(returncode=0, stdout="", stderr="")

        def which(name: str) -> str | None:
            return "/usr/bin/security" if name == "security" else None

        with (
            patch.object(auth.sys, "platform", "darwin"),
            patch.object(auth.shutil, "which", side_effect=which),
            patch.object(auth.subprocess, "run", return_value=completed) as run,
        ):
            auth.store_password("student@edu.hse.ru", "p@ss word")

        command = run.call_args.args[0]
        self.assertEqual(command, ["/usr/bin/security", "-i"])
        self.assertNotIn("p@ss word", command)
        self.assertIn("7040737320776f7264", run.call_args.kwargs["input"])

    def test_macos_keychain_loads_password(self) -> None:
        completed = Mock(returncode=0, stdout="stored-password\n", stderr="")

        def which(name: str) -> str | None:
            return "/usr/bin/security" if name == "security" else None

        with (
            patch.object(auth.sys, "platform", "darwin"),
            patch.object(auth.shutil, "which", side_effect=which),
            patch.object(auth.subprocess, "run", return_value=completed) as run,
            patch.dict(auth.os.environ, {}, clear=True),
        ):
            self.assertEqual(auth.load_password("student@edu.hse.ru"), "stored-password")

        self.assertEqual(
            run.call_args.args[0],
            [
                "/usr/bin/security", "find-generic-password", "-a",
                "student@edu.hse.ru", "-s", auth.SERVICE_NAME, "-w",
            ],
        )

    def test_windows_credential_manager_dispatches_without_subprocess(self) -> None:
        with (
            patch.object(auth.sys, "platform", "win32"),
            patch.object(auth, "_windows_store_credential") as store,
            patch.object(auth, "_windows_load_credential", return_value="stored") as load,
            patch.object(auth, "_windows_clear_credential", return_value=True) as clear,
            patch.object(auth.subprocess, "run") as run,
            patch.dict(auth.os.environ, {}, clear=True),
        ):
            auth.store_password("student@edu.hse.ru", "p@ss word")
            self.assertEqual(auth.load_password("student@edu.hse.ru"), "stored")
            self.assertTrue(auth.clear_password("student@edu.hse.ru"))

        store.assert_called_once_with(
            auth.SERVICE_NAME, "student@edu.hse.ru", "p@ss word",
            "HSE Smart LMS (student@edu.hse.ru)",
        )
        load.assert_called_once_with(auth.SERVICE_NAME, "student@edu.hse.ru")
        clear.assert_called_once_with(auth.SERVICE_NAME, "student@edu.hse.ru")
        run.assert_not_called()

    def test_windows_credential_target_is_namespaced(self) -> None:
        self.assertEqual(
            auth._windows_credential_target("hse-edu-parser", "student@edu.hse.ru"),
            "hse-edu-parser:student@edu.hse.ru",
        )


if __name__ == "__main__":
    unittest.main()
