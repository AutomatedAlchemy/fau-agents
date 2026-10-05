#!/usr/bin/env python3
"""Tests for fauopencode: the generated OpenCode config and the launch."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _paths

main = _paths.load("fauopencode")

LISTING = [
    {"id": "deepseek-ai/DeepSeek-V4-Flash-0731",
     "max_input_tokens": 1048576, "max_output_tokens": 1048576},
    {"id": "google/gemma-4-E4B-it", "max_input_tokens": 131072, "max_output_tokens": 131072},
    {"id": "Microsoft/Phi-4-mini-instruct", "max_input_tokens": 16384, "max_output_tokens": 16384},
    {"id": "Qwen/Qwen3-Embedding-4B", "mode": "embedding",
     "max_input_tokens": 32768, "max_output_tokens": 32768},
]
MODEL, SMALL = "deepseek-ai/DeepSeek-V4-Flash-0731", "google/gemma-4-E4B-it"


class ConfigTest(unittest.TestCase):
    def config(self, listing=LISTING, model=MODEL):
        return main.build_config(listing, model, SMALL, Path("/staged"))

    def test_only_the_gateway_provider_is_enabled(self):
        cfg = self.config()
        self.assertEqual(cfg["enabled_providers"], ["fau"])
        self.assertEqual(cfg["model"], f"fau/{MODEL}")
        self.assertEqual(cfg["small_model"], f"fau/{SMALL}")
        self.assertEqual(cfg["share"], "disabled")

    def test_key_is_named_not_embedded(self):
        options = self.config()["provider"]["fau"]["options"]
        self.assertEqual(options["apiKey"], "{env:LLMAPI_KEY}")
        self.assertTrue(options["baseURL"].endswith("/api/llmgw/v1"))

    def test_embedding_models_are_not_offered(self):
        models = self.config()["provider"]["fau"]["models"]
        self.assertNotIn("Qwen/Qwen3-Embedding-4B", models)
        self.assertIn("Microsoft/Phi-4-mini-instruct", models)

    def test_output_limit_leaves_room_for_the_prompt(self):
        models = self.config()["provider"]["fau"]["models"]
        big = models[MODEL]["limit"]
        self.assertEqual(big["context"], 1048576)
        self.assertEqual(big["output"], main.OUTPUT_CAP)
        small = models["Microsoft/Phi-4-mini-instruct"]["limit"]
        self.assertLessEqual(small["output"], small["context"])

    def test_failed_listing_still_offers_the_chosen_models(self):
        models = self.config(listing=[])["provider"]["fau"]["models"]
        self.assertEqual(set(models), {MODEL, SMALL})
        self.assertEqual(models[MODEL]["limit"], main.FALLBACK_LIMIT)

    def test_skills_point_at_the_staging_dir(self):
        self.assertEqual(self.config()["skills"], {"paths": ["/staged"]})

    def test_config_is_valid_json_for_the_environment(self):
        env = main.launch_env("sk-x", self.config())
        self.assertEqual(env["LLMAPI_KEY"], "sk-x")
        self.assertEqual(json.loads(env["OPENCODE_CONFIG_CONTENT"])["model"], f"fau/{MODEL}")
        self.assertNotIn("sk-x", env["OPENCODE_CONFIG_CONTENT"])


class LaunchTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_missing_key_fails_without_staging(self):
        with mock.patch.object(main.gateway, "resolve_key", return_value=""), \
                mock.patch.object(main, "stage_skills") as stage:
            self.assertEqual(main.launch(MODEL, SMALL, []), 1)
            stage.assert_not_called()

    def test_passthrough_reaches_opencode(self):
        with mock.patch.object(main, "launch", return_value=0) as launch:
            main.main(["--profile", "ww3claude", "run", "--format", "json", "hi"])
        args, kwargs = launch.call_args
        self.assertEqual(args[2], ["run", "--format", "json", "hi"])
        self.assertEqual(kwargs["profile"], "ww3claude")

    def test_exec_gets_key_and_config(self):
        staged = Path(self._tmp.name) / "staged"
        with mock.patch.object(main.gateway, "resolve_key", return_value="sk-y"), \
                mock.patch.object(main, "find_opencode", return_value="/bin/opencode"), \
                mock.patch.object(main.gateway, "fetch_models", return_value=LISTING), \
                mock.patch.object(main, "stage_skills", return_value=(staged, [])), \
                mock.patch.object(main.os, "execve") as execve, \
                mock.patch.object(main.os, "name", "posix"), \
                mock.patch("sys.stderr"):
            main.launch(MODEL, SMALL, ["run", "hi"])
        exe, argv, env = execve.call_args.args
        self.assertEqual(argv, ["/bin/opencode", "run", "hi"])
        self.assertEqual(env["LLMAPI_KEY"], "sk-y")
        self.assertIn(str(staged), env["OPENCODE_CONFIG_CONTENT"])

    def test_advertise_metadata(self):
        self.assertEqual(main.PARENT_METADATA["alias"], "fauopencode")
        self.assertNotIn("skill_name", main.PARENT_METADATA)


if __name__ == "__main__":
    unittest.main()
