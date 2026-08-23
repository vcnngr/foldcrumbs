"""CLI version exposure — regression tests.

The installed binary had no way to report its own version: `foldcrumbs
version` was an invalid choice and `--version` was not defined. Both paths
must print the package version and exit 0.
"""

import argparse
import contextlib
import io
import unittest

from foldcrumbs import __version__, cli


class TestVersionExposure(unittest.TestCase):

    def test_subcommand_version_prints_package_version(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli._cmd_version(argparse.Namespace())
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), f"foldcrumbs {__version__}")

    def test_main_version_subcommand_via_main(self):
        """`foldcrumbs version` must route through main() cleanly."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["version"])
        self.assertEqual(rc, 0)
        self.assertIn(__version__, out.getvalue())

    def test_main_version_flag(self):
        """`foldcrumbs --version` exits via argparse action='version' (SystemExit 0)."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as ctx:
                cli.main(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn(__version__, out.getvalue())

    def test_version_listed_in_help(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit):
                cli.main(["--help"])
        self.assertIn("version", out.getvalue())


if __name__ == "__main__":
    unittest.main()
