"""Pi coding agent support (install --agent pi).

The extension contract is pinned here; the TS itself is validated against
the real pi package in the sandbox probe (see PR notes), not by Python.
"""

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _sandbox import SANDBOX, is_inside  # noqa: E402,F401


class TestPiPaths(unittest.TestCase):
    def test_global_paths(self):
        from foldcrumbs import install
        p = install.pi_paths(global_scope=True)
        self.assertEqual(p["extensions"],
                         Path.home() / ".pi" / "agent" / "extensions")
        self.assertEqual(p["agents"],
                         Path.home() / ".pi" / "agent" / "AGENTS.md")

    def test_project_paths(self):
        import os
        import tempfile
        from foldcrumbs import install
        d = Path(tempfile.mkdtemp(prefix="pi_paths_"))
        old = os.getcwd()
        os.chdir(d)
        try:
            p = install.pi_paths(global_scope=False)
            self.assertEqual(p["extensions"].resolve(),
                             (d / ".pi" / "extensions").resolve())
            self.assertEqual(p["agents"].resolve(),
                             (d / "AGENTS.md").resolve())
        finally:
            os.chdir(old)


class TestPiExtensionWrite(unittest.TestCase):
    def setUp(self):
        import tempfile
        base = Path(tempfile.mkdtemp(prefix="pi_ext_"))
        self.d = base / "ext"
        self.d.mkdir(parents=True, exist_ok=True)
        self.runtime = base / "runtime"

    def test_write_creates_ts_with_real_paths(self):
        from foldcrumbs import install
        path = install.write_pi_extension(self.d, runtime_root=self.runtime)
        self.assertEqual(path.name, "foldcrumbs.ts")
        body = path.read_text(encoding="utf-8")
        # the staged hook path must be absolute and exist
        self.assertIn(str(self.runtime), body)
        hook_line = [ln for ln in body.splitlines()
                     if ln.startswith("const HOOK")]
        self.assertEqual(len(hook_line), 1)
        hook_path = hook_line[0].split('"')[1]
        self.assertTrue(Path(hook_path).exists(), hook_path)
        # tools + events declared
        for needle in ('pi.registerTool', '"foldcrumbs_recall"',
                       '"foldcrumbs_remember"', 'pi.on("session_start"',
                       'pi.on("before_agent_start"',
                       '@earendil-works/pi-coding-agent', 'from "typebox"'):
            self.assertIn(needle, body)

    def test_rewrite_is_idempotent_single_file(self):
        from foldcrumbs import install
        install.write_pi_extension(self.d, runtime_root=self.runtime)
        install.write_pi_extension(self.d, runtime_root=self.runtime)
        ts = sorted(p.name for p in self.d.glob("*.ts"))
        self.assertEqual(ts, ["foldcrumbs.ts"])

    def test_remove_only_ours(self):
        from foldcrumbs import install
        install.write_pi_extension(self.d, runtime_root=self.runtime)
        other = self.d / "user-tool.ts"
        other.write_text("// mine", encoding="utf-8")
        self.assertTrue(install.remove_pi_extension(self.d))
        self.assertFalse((self.d / "foldcrumbs.ts").exists())
        self.assertTrue(other.exists())
        self.assertFalse(install.remove_pi_extension(self.d))

    def test_agents_md_block_appended_once(self):
        from foldcrumbs import install
        agents = SANDBOX / "pi_agents_test.md"
        agents.unlink(missing_ok=True)
        self.assertEqual(install.append_agents_md(agents), agents)
        body = agents.read_text(encoding="utf-8")
        self.assertIn("Memory (foldcrumbs)", body)
        # second call: no duplicate
        self.assertIsNone(install.append_agents_md(agents))
        self.assertEqual(
            agents.read_text(encoding="utf-8").count("Memory (foldcrumbs)"), 1)


class TestPiCliWiring(unittest.TestCase):
    def test_install_uninstall_accept_pi(self):
        from foldcrumbs import cli as cli_mod
        p = cli_mod.build_parser()
        args = p.parse_args(["install", "--agent", "pi"])
        self.assertEqual(args.agent, "pi")
        args2 = p.parse_args(["uninstall", "--agent", "pi"])
        self.assertEqual(args2.agent, "pi")

    def test_install_pi_end_to_end_local(self):
        # full CLI path with --local inside the sandbox: writes .pi/extensions
        # + AGENTS.md under cwd, touches nothing else
        import contextlib
        import io
        import os
        from foldcrumbs import cli as cli_mod
        proj = SANDBOX / "pi_e2e"
        proj.mkdir(parents=True, exist_ok=True)
        old = os.getcwd()
        os.chdir(proj)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = cli_mod.main(["install", "--agent", "pi", "--local",
                                   "--no-backend-prompt"])
            self.assertEqual(rc, 0)
            self.assertTrue((proj / ".pi" / "extensions"
                             / "foldcrumbs.ts").exists())
            self.assertIn("Memory (foldcrumbs)",
                          (proj / "AGENTS.md").read_text(encoding="utf-8"))
            out = buf.getvalue()
            self.assertIn("pi extension:", out)
            # uninstall removes the extension, leaves AGENTS.md
            buf2 = io.StringIO()
            with contextlib.redirect_stdout(buf2):
                rc = cli_mod.main(["uninstall", "--agent", "pi", "--local"])
            self.assertEqual(rc, 0)
            self.assertFalse((proj / ".pi" / "extensions"
                              / "foldcrumbs.ts").exists())
            self.assertIn("pi extension removed: True", buf2.getvalue())
        finally:
            os.chdir(old)


if __name__ == "__main__":
    unittest.main()
