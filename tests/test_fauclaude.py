#!/usr/bin/env python3
"""Tests for fauclaude: plugin layout, the launch environment (where the auth
footgun lives), config isolation, the remembered capture choice."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _paths

main = _paths.load("fauclaude")


class PluginBuildTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "src"
        tool = self.root / "cifsearch"
        tool.mkdir(parents=True)
        (tool / "SKILL.md").write_text("---\nname: cifsearch\n---\nbody\n")
        self.dest = Path(self._tmp.name) / "plugin"

    def test_layout_is_a_valid_plugin_named_after_the_profile(self):
        dest, names = main.build_plugin("ww3claude", dest=self.dest, root=self.root)
        manifest = json.loads((dest / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["name"], "ww3claude")
        self.assertEqual(names, ["cifsearch"])
        self.assertTrue((dest / "skills" / "cifsearch" / "SKILL.md").is_file())

    def test_rebuild_drops_skills_that_disappeared(self):
        main.build_plugin(dest=self.dest, root=self.root)
        stale = self.dest / "skills" / "gone"
        stale.mkdir(parents=True)
        (stale / "SKILL.md").write_text("---\nname: gone\n---\n")
        main.build_plugin(dest=self.dest, root=self.root)
        self.assertFalse(stale.exists())

    def test_default_plugin_dir_is_per_profile_and_hidden(self):
        with mock.patch.dict(os.environ, {"CLAUDE_FAU_PLUGIN_DIR": ""}):
            fau, ww3 = main.plugin_dir("fauclaude"), main.plugin_dir("ww3claude")
        self.assertNotEqual(fau, ww3)
        self.assertIn(".staged", fau.parts)


class LaunchEnvTest(unittest.TestCase):
    def test_api_key_is_blanked_not_set_to_the_token(self):
        """Both set is the ambiguous-auth warning this launcher exists to avoid."""
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-oldvalue"}):
            env = main.launch_env("sk-fau")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "sk-fau")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "")

    def test_small_model_is_pinned_to_the_gateway(self):
        env = main.launch_env("k")
        self.assertEqual(env["ANTHROPIC_SMALL_FAST_MODEL"], main.gateway.DEFAULT_SMALL_MODEL)
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], main.gateway.DEFAULT_SMALL_MODEL)

    def test_parent_session_markers_are_scrubbed(self):
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "x"}):
            env = main.launch_env("k")
        self.assertNotIn("CLAUDECODE", env)
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", env)


class ConfigIsolationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.source = root / ".claude"
        self.source.mkdir()
        (self.source / "settings.json").write_text(json.dumps({
            "theme": "dark", "effortLevel": "xhigh", "model": "opus",
            "permissions": {"deny": ["Read(~/.ssh/**)"]},
            "autoMode": {"environment": ["repo-specific blurb"]},
        }))
        (root / ".claude.json").write_text(json.dumps(
            {"hasCompletedOnboarding": True, "installMethod": "native", "oauthAccount": {"x": 1}}))
        self.dest = root / ".claude-fau"

    def _seeded(self, name):
        return json.loads((main.ensure_config_dir(self.dest, self.source) / name).read_text())

    def test_model_and_effort_are_not_inherited(self):
        seeded = self._seeded("settings.json")
        self.assertNotIn("model", seeded)
        self.assertNotIn("effortLevel", seeded)
        self.assertEqual(seeded["theme"], "dark")

    def test_repo_specific_and_secret_shaped_keys_stay_behind(self):
        seeded = self._seeded("settings.json")
        self.assertNotIn("autoMode", seeded)
        self.assertNotIn("permissions", seeded)

    def test_onboarding_state_is_seeded_without_the_account(self):
        state = self._seeded(".claude.json")
        self.assertTrue(state["hasCompletedOnboarding"])
        self.assertNotIn("oauthAccount", state)

    def test_existing_profile_is_never_overwritten(self):
        main.ensure_config_dir(self.dest, self.source)
        settings = self.dest / "settings.json"
        settings.write_text(json.dumps({"theme": "light", "model": "gpt-oss-120b"}))
        main.ensure_config_dir(self.dest, self.source)
        self.assertEqual(json.loads(settings.read_text())["model"], "gpt-oss-120b")

    def test_missing_source_config_still_yields_a_usable_profile(self):
        dest = Path(self._tmp.name) / "fresh"
        main.ensure_config_dir(dest, Path(self._tmp.name) / "nonexistent")
        self.assertEqual(json.loads((dest / "settings.json").read_text()), {})
        self.assertTrue(json.loads((dest / ".claude.json").read_text())["hasCompletedOnboarding"])

    def test_launch_env_points_claude_at_the_isolated_dir(self):
        env = main.launch_env("k", main.gateway.BASE_URL, Path("/somewhere/.claude-fau"))
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/somewhere/.claude-fau")

    def test_shared_config_leaves_the_variable_alone(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            self.assertNotIn("CLAUDE_CONFIG_DIR", main.launch_env("k"))


class CaptureSettingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "cfg" / "fau-agents.json"

    def test_off_until_turned_on(self):
        self.assertFalse(main.capture_enabled(self.path))

    def test_choice_is_remembered_both_ways(self):
        main.set_capture(True, self.path)
        self.assertTrue(main.capture_enabled(self.path))
        main.set_capture(False, self.path)
        self.assertFalse(main.capture_enabled(self.path))

    def test_flags_persist_and_launch_reads_the_setting(self):
        with mock.patch.object(main, "SETTINGS_FILE", self.path), \
                mock.patch.object(main, "launch", return_value=0) as launch:
            main.main(["--capture"])
            self.assertTrue(launch.call_args.kwargs["record"])
            main.main([])
            self.assertTrue(launch.call_args.kwargs["record"])
            main.main(["--no-capture"])
            self.assertFalse(launch.call_args.kwargs["record"])

    def test_corrupt_settings_count_as_off(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("not json")
        self.assertFalse(main.capture_enabled(self.path))


class ArgvTest(unittest.TestCase):
    def test_plugin_dir_and_model_are_passed(self):
        argv = main.build_argv("some/model", Path("/p"), [])
        self.assertEqual(argv[:5], ["claude", "--model", "some/model", "--plugin-dir", "/p"])

    def test_unknown_flags_pass_through_to_claude(self):
        with mock.patch.object(main, "launch", return_value=0) as launch, \
                mock.patch.object(main, "capture_enabled", return_value=False):
            main.main(["--profile", "ww3claude", "--skill-root", "/x", "--resume",
                       "--", "-c"])
        args, kwargs = launch.call_args
        self.assertEqual(args[1], ["--resume", "-c"])
        self.assertEqual(kwargs["profile"], "ww3claude")
        self.assertEqual(kwargs["extra"], ["/x"])

    def test_missing_key_fails_loudly_without_building(self):
        with mock.patch.object(main.gateway, "resolve_key", return_value=""), \
                mock.patch.object(main, "build_plugin") as build:
            self.assertEqual(main.launch("m", []), 1)
            build.assert_not_called()

    def test_advertise_metadata_declares_no_skill(self):
        """The payload is session-scoped; a skill_name would install it globally."""
        self.assertNotIn("skill_name", main.PARENT_METADATA)
        self.assertEqual(main.PARENT_METADATA["alias"], "fauclaude")


if __name__ == "__main__":
    unittest.main()
