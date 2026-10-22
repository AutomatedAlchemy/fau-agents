#!/usr/bin/env python3
"""Tests for the gateway module: where the key comes from, which models count,
the prompt that asks for a missing key."""

import io
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401
from fau_agents import gateway


class KeyResolutionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_environment_wins(self):
        with mock.patch.dict(os.environ, {"LLMAPI_KEY": "sk-env"}):
            self.assertEqual(gateway.resolve_key(), "sk-env")

    def test_home_file_comes_before_faullm_dir(self):
        home_file = self.tmp / ".faullm_env"
        home_file.write_text("LLMAPI_KEY=sk-home\n")
        faullm = self.tmp / "faullm"
        faullm.mkdir()
        (faullm / ".env").write_text("LLMAPI_KEY=sk-faullm\n")
        with mock.patch.dict(os.environ, {"LLMAPI_KEY": "", "FAULLM_DIR": str(faullm)}), \
                mock.patch.object(gateway, "HOME", self.tmp):
            self.assertEqual(gateway.resolve_key(), "sk-home")

    def test_falls_back_to_faullm_dir(self):
        faullm = self.tmp / "faullm"
        faullm.mkdir()
        (faullm / ".env").write_text('LLMAPI_KEY="sk-file"\n')
        with mock.patch.dict(os.environ, {"LLMAPI_KEY": "", "FAULLM_DIR": str(faullm)}), \
                mock.patch.object(gateway, "HOME", self.tmp):
            self.assertEqual(gateway.resolve_key(), "sk-file")

    def test_installer_layout_sibling_is_found(self):
        """The WW3 installer clones NHR/ next to this repo."""
        env_file = self.tmp / "NHR" / "tools" / "faullm" / ".env"
        env_file.parent.mkdir(parents=True)
        env_file.write_text("LLMAPI_KEY=sk-sibling\n")
        with mock.patch.dict(os.environ, {"LLMAPI_KEY": ""}), \
                mock.patch.object(gateway, "HOME", self.tmp / "home"), \
                mock.patch.object(gateway, "REPO_DIR", self.tmp / "fau-agents"):
            os.environ.pop("FAULLM_DIR", None)
            self.assertEqual(gateway.resolve_key(), "sk-sibling")

    def test_export_prefix_and_quotes_are_stripped(self):
        (self.tmp / ".env").write_text("export LLMAPI_KEY = 'sk-quoted'\n")
        self.assertEqual(gateway.key_from_file(self.tmp / ".env"), "sk-quoted")

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(gateway.key_from_file(self.tmp / "nope.env"), "")


class ModelFilterTest(unittest.TestCase):
    def test_embedding_and_ocr_models_are_dropped(self):
        listing = [{"id": "chat-a"}, {"id": "emb", "mode": "embedding"},
                   {"id": "ocr", "mode": "ocr"}, {"id": "chat-b", "mode": "chat"}]
        self.assertEqual([m["id"] for m in gateway.chat_models(listing)],
                         ["chat-a", "chat-b"])

    def test_listing_failure_is_reported_not_raised(self):
        with mock.patch.object(gateway, "fetch_models", side_effect=OSError("down")):
            self.assertEqual(gateway.print_models("k", out=io.StringIO()), [])

    def test_base_url_carries_no_v1_suffix(self):
        """Claude Code appends its own /v1/messages."""
        self.assertFalse(gateway.BASE_URL.endswith("/v1"))
        self.assertTrue(gateway.OPENAI_URL.endswith("/v1"))

    def test_ssl_context_trusts_the_gateway_root(self):
        """A fresh Windows store lacks it; the context must carry it itself."""
        names = [dict(rdn[0] for rdn in cert["subject"]).get("commonName")
                 for cert in gateway.ssl_context().get_ca_certs()]
        self.assertIn("HARICA TLS ECC Root CA 2021", names)


class SaveKeyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / ".faullm_env"

    def test_new_file_is_private(self):
        gateway.save_key("sk-new", self.path)
        self.assertEqual(self.path.read_text(), "LLMAPI_KEY=sk-new\n")
        if os.name == "posix":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_only_the_key_line_changes(self):
        self.path.write_text("# mine\nexport LLMAPI_KEY=sk-old\nOTHER=1\n")
        self.path.chmod(0o644)
        gateway.save_key("sk-new", self.path)
        self.assertEqual(self.path.read_text(),
                         "# mine\nexport LLMAPI_KEY=sk-new\nOTHER=1\n")
        self.assertEqual(gateway.key_from_file(self.path), "sk-new")
        if os.name == "posix":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)


class CheckKeyTest(unittest.TestCase):
    def check(self, outcome):
        kwargs = {"side_effect": outcome} if isinstance(outcome, Exception) \
            else {"return_value": outcome}
        with mock.patch.object(gateway, "fetch_models", **kwargs):
            return gateway.check_key("sk-x")

    def http_error(self, code):
        return urllib.error.HTTPError(gateway.OPENAI_URL, code, "x", {}, None)

    def test_401_means_refused(self):
        self.assertIs(self.check(self.http_error(401)), False)

    def test_unreachable_or_server_error_is_unknown(self):
        self.assertIsNone(self.check(urllib.error.URLError("no route")))
        self.assertIsNone(self.check(self.http_error(502)))

    def test_listing_means_accepted(self):
        self.assertIs(self.check([]), True)


class AskForKeyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.err = io.StringIO()
        self.stdin = mock.Mock()
        self.stdin.isatty.return_value = True
        patches = (mock.patch.object(gateway, "HOME", self.home),
                   mock.patch.object(gateway.sys, "stdin", self.stdin),
                   mock.patch.object(gateway.sys, "stderr", self.err),
                   mock.patch.object(gateway.webbrowser, "open"))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_without_a_terminal_it_only_prints_the_message(self):
        self.stdin.isatty.return_value = False
        with mock.patch.object(gateway.getpass, "getpass") as ask:
            self.assertEqual(gateway.ask_for_key("ww3claude"), "")
        ask.assert_not_called()
        self.assertIn(gateway.KEY_FORM_URL, self.err.getvalue())

    def test_a_refused_key_is_asked_again_and_the_good_one_saved(self):
        with mock.patch.object(gateway.getpass, "getpass", side_effect=["sk-bad", "sk-good"]), \
                mock.patch.object(gateway, "check_key", side_effect=[False, True]):
            self.assertEqual(gateway.ask_for_key("ww3claude"), "sk-good")
        self.assertEqual(gateway.key_from_file(self.home / ".faullm_env"), "sk-good")
        self.assertIn("refused", self.err.getvalue())
        self.assertNotIn("sk-good", self.err.getvalue())

    def test_enter_alone_sends_the_user_to_the_form(self):
        with mock.patch.object(gateway.getpass, "getpass", return_value=""), \
                mock.patch.object(gateway, "can_open_browser", return_value=True):
            self.assertEqual(gateway.ask_for_key("ww3claude"), "")
        gateway.webbrowser.open.assert_called_once_with(gateway.KEY_FORM_URL)
        self.assertIn(gateway.KEY_FORM_URL, self.err.getvalue())
        self.assertFalse((self.home / ".faullm_env").exists())

    def test_no_browser_without_a_display(self):
        with mock.patch.dict(os.environ, {"DISPLAY": "", "WAYLAND_DISPLAY": ""}), \
                mock.patch.object(gateway.os, "name", "posix"), \
                mock.patch.object(gateway.sys, "platform", "linux"):
            self.assertFalse(gateway.can_open_browser())

    def test_ctrl_c_gives_up_quietly(self):
        with mock.patch.object(gateway.getpass, "getpass", side_effect=KeyboardInterrupt):
            self.assertEqual(gateway.ask_for_key("ww3claude"), "")


if __name__ == "__main__":
    unittest.main()
