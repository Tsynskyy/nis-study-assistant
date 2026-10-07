from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import io
import unittest
from unittest.mock import Mock, patch

from lms import cli
from lms.auth import AuthError


class TBankLoginTests(unittest.TestCase):
    def test_placeholder_email_fails_before_password_prompt(self) -> None:
        args = argparse.Namespace(email="ВАША_ПОЧТА", timeout=30, store_password=True)
        with patch.object(cli, "TBankAuthSession"), patch.object(cli, "prompt_tbank_password") as prompt:
            with self.assertRaisesRegex(AuthError, "настоящий email"):
                cli.cmd_tbank_login(args)
        prompt.assert_not_called()

    def test_reuses_email_from_existing_session(self) -> None:
        args = argparse.Namespace(email=None, timeout=30, store_password=True)
        auth = Mock(email="student@example.com")
        with (
            patch.object(cli, "TBankAuthSession", return_value=auth),
            patch.object(cli, "prompt_tbank_password", return_value="test-password"),
            patch.object(cli, "store_tbank_password") as store,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cli.cmd_tbank_login(args), 0)

        auth.load.assert_called_once_with()
        auth.login.assert_called_once_with("student@example.com", "test-password")
        store.assert_called_once_with("student@example.com", "test-password")


if __name__ == "__main__":
    unittest.main()
