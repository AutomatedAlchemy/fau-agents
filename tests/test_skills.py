#!/usr/bin/env python3
"""Tests for skill discovery, {{CLI}} rendering, roots and staging."""

import os
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401
from fau_agents import skills


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        tool = self.root / "xrdlab"
        (tool / ".venv" / "bin").mkdir(parents=True)
        (tool / ".venv" / "bin" / "python3").write_text("#!/bin/sh\n")
        (tool / "SKILL.md").write_text(
            "---\nname: xrdlab\ndescription: things\n---\n\nRun it: {{CLI}} inventory\n")
        plain = self.root / "NHR" / "tools" / "plainly"
        plain.mkdir(parents=True)
        (plain / "SKILL.md").write_text("---\nname: plainly\n---\n{{CLI}}\n")

    def test_discovers_frontmatter_names_sorted(self):
        names = [name for name, _s, _b in skills.discover([self.root])]
        self.assertEqual(names, ["plainly", "xrdlab"])

    def test_cli_placeholder_is_baked_to_tool_venv(self):
        rendered = {name: body for name, _s, body in skills.discover([self.root])}
        self.assertNotIn("{{CLI}}", rendered["xrdlab"])
        self.assertIn(".venv/bin/python3", rendered["xrdlab"])
        self.assertIn("xrdlab/main.py", rendered["xrdlab"])

    def test_tool_without_venv_falls_back_to_a_real_interpreter(self):
        rendered = {name: body for name, _s, body in skills.discover([self.root])}
        cli = rendered["plainly"].strip().splitlines()[-1]
        interp, script = shlex.split(cli)
        self.assertTrue(Path(interp).name.startswith("python"), cli)
        self.assertTrue(script.endswith("plainly/main.py"), cli)

    def test_one_tool_repo_script_name(self):
        tool = self.root / "manim-kit"
        tool.mkdir()
        (tool / "manim_kit.py").write_text("")
        (tool / "SKILL.md").write_text("---\nname: manim-kit\n---\n{{CLI}}\n")
        rendered = {name: body for name, _s, body in skills.discover([self.root])}
        self.assertIn("manim-kit/manim_kit.py", rendered["manim-kit"])

    def test_name_falls_back_to_directory(self):
        tool = self.root / "nameless"
        tool.mkdir()
        (tool / "SKILL.md").write_text("# no frontmatter here\n")
        names = [name for name, _s, _b in skills.discover([self.root])]
        self.assertIn("nameless", names)

    def test_hidden_and_vendor_dirs_are_skipped(self):
        for junk in (".claude/skills/shadow", "node_modules/shadow2",
                     ".staged/claude/fauclaude/skills/shadow3"):
            d = self.root / junk
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text("---\nname: %s\n---\n" % d.name)
        names = [n for n, _s, _b in skills.discover([self.root])]
        for shadow in ("shadow", "shadow2", "shadow3"):
            self.assertNotIn(shadow, names)

    def test_flat_layout_and_shell_entry_point(self):
        tool = self.root / "flat"
        tool.mkdir()
        (tool / "SKILL.md").write_text("---\nname: flat\n---\n{{CLI}} go\n")
        (tool / "run.sh").write_text("#!/bin/sh\n")
        (tool / "run.sh").chmod(0o755)
        body = {n: b for n, _s, b in skills.discover([self.root])}["flat"]
        self.assertIn(str(tool / "run.sh") + " go", body)


class RootTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.a = self.tmp / "a"
        self.b = self.tmp / "b"
        for root, body in ((self.a, "from-a"), (self.b, "from-b")):
            tool = root / "dupe"
            tool.mkdir(parents=True)
            (tool / "SKILL.md").write_text(f"---\nname: dupe\n---\n{body}\n")

    def test_env_root_takes_a_path_style_list(self):
        joined = os.pathsep.join([str(self.a), "/nonexistent", str(self.b)])
        with mock.patch.dict(os.environ, {"CLAUDE_FAU_SKILL_ROOT": joined}):
            self.assertEqual(skills.skill_roots(), [self.a.resolve(), self.b.resolve()])

    def test_first_root_wins_on_a_name_collision(self):
        found = skills.discover(skills.skill_roots(root=[self.a, self.b]))
        self.assertEqual(len(found), 1)
        self.assertIn("from-a", found[0][2])

    def test_profile_dir_comes_before_extra_roots(self):
        config = self.tmp / "config"
        (config / "ww3claude" / "skills").mkdir(parents=True)
        with mock.patch.object(skills, "XDG_CONFIG", config), \
                mock.patch.dict(os.environ, {"CLAUDE_FAU_SKILL_ROOT": ""}):
            roots = skills.skill_roots("ww3claude", extra=[self.b])
        self.assertEqual(roots[:2], [(config / "ww3claude" / "skills").resolve(),
                                     self.b.resolve()])

    def test_profiles_read_separate_dirs(self):
        self.assertNotEqual(skills.profile_dirs("fauclaude"), skills.profile_dirs("ww3claude"))
        self.assertEqual(skills.profile_dirs("ww3claude")[0].parts[-2:], ("ww3claude", "skills"))


class StageTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dest = Path(self._tmp.name) / "staged"

    def test_stage_writes_one_dir_per_skill_and_drops_stale_ones(self):
        skills.stage(self.dest, [("gone", None, "x")])
        names = skills.stage(self.dest, [("kept", None, "body")])
        self.assertEqual(names, ["kept"])
        self.assertEqual((self.dest / "kept" / "SKILL.md").read_text(), "body")
        self.assertFalse((self.dest / "gone").exists())

    def test_staging_is_separate_per_agent_and_profile(self):
        dirs = {skills.staged_dir(a, p) for a in ("claude", "opencode")
                for p in ("fauclaude", "ww3claude")}
        self.assertEqual(len(dirs), 4)
        for d in dirs:
            self.assertTrue(d.relative_to(skills.REPO_DIR).parts[0].startswith("."))


if __name__ == "__main__":
    unittest.main()
