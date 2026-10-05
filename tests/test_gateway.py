#!/usr/bin/env python3
"""Tests for the gateway module: where the key comes from, which models count."""

import io
import os
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
