#!/usr/bin/env python3
"""Testes da status line. Rodar com: python3 -m unittest -v test_cli"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import cli

CLI_PATH = Path(__file__).resolve().parent / "cli.py"

FULL = {
    "cwd": "/x",
    "model": {"display_name": "Opus 5.5"},
    "context_window": {"used_percentage": 63.4, "context_window_size": 200000},
    "rate_limits": {
        "five_hour": {"used_percentage": 41, "resets_at": int(time.time()) + 7980},
        "seven_day": {"used_percentage": 88, "resets_at": int(time.time()) + 3 * 86400},
    },
    "cost": {"total_cost_usd": 1.234, "total_duration_ms": 754000,
             "total_lines_added": 156, "total_lines_removed": 23},
}


def plain(text):
    return cli.ANSI_RE.sub("", text)


class StatuslineTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name)
        for patcher in (
            mock.patch.object(cli, "CACHE_DIR", self.cache),
            mock.patch.object(cli, "git_branch", return_value=None),
            mock.patch.dict(os.environ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop("NO_COLOR", None)

    def assertNoErrors(self):
        log = self.cache / "error.log"
        self.assertFalse(log.exists(), log.read_text() if log.exists() else "")

    def test_full_payload(self):
        out = plain(cli.build(FULL))
        self.assertEqual(out.count("\n"), 1)
        for expected in ("◆ Opus 5.5", "63%/200k", "5h", "41%", "↻2h12", "7d", "88%", "$1.23", "+156 -23"):
            self.assertIn(expected, out)
        self.assertNoErrors()

    def test_all_null(self):
        out = plain(cli.build({"cwd": "/x", "workspace": None, "model": None, "cost": None,
                               "context_window": {"used_percentage": None}, "rate_limits": None}))
        self.assertEqual(out.split("\n")[1], "ctx ░░░░░░░░░░ --")
        self.assertNoErrors()

    def test_wrong_types(self):
        out = plain(cli.build({"cwd": 42, "workspace": "abc", "model": {"display_name": ["x"]},
                               "context_window": 5, "rate_limits": [1], "cost": "caro"}))
        self.assertEqual(out.count("\n"), 1)
        self.assertNoErrors()

    def test_pathological_numbers(self):
        cases = {float("nan"): "--", float("inf"): "--", -float("inf"): "--", True: "--",
                 "50": "--", 10**400: "--", -5: "0%", 250: "100%"}
        for value, expected in cases.items():
            with self.subTest(value=value):
                out = plain(cli.build({"context_window": {"used_percentage": value},
                                       "rate_limits": {"five_hour": {"used_percentage": value}}}))
                self.assertIn(f"ctx", out)
                self.assertTrue(out.split("\n")[1].split(" │ ")[0].endswith(expected), out)
        self.assertNoErrors()

    def test_json_nan_literal(self):
        data = json.loads('{"context_window": {"used_percentage": NaN, "context_window_size": Infinity}}')
        self.assertIn("ctx ░░░░░░░░░░ --", plain(cli.build(data)))
        self.assertNoErrors()

    def test_reset_in(self):
        now = time.time()
        self.assertEqual(cli.reset_in(now + 7980), "2h12")
        self.assertEqual(cli.reset_in((now + 7980) * 1000), "2h12")
        self.assertEqual(cli.reset_in(now + 3 * 86400 + 30), "3d0h")
        self.assertIsNotNone(cli.reset_in("2099-01-01T00:00:00Z"))
        for bad in ("lixo", {"a": 1}, [1], True, 10**400, float("nan"), float("inf"), now - 10, None):
            with self.subTest(bad=bad):
                self.assertIsNone(cli.reset_in(bad))

    def test_escape_injection(self):
        out = cli.build({"cwd": "/tmp/a\nb\x1b[31mRED\x9b",
                         "model": {"display_name": "Op\nus\x1b]0;titulo\x07\r"}})
        self.assertEqual(out.count("\n"), 1)
        self.assertNotIn("\x1b", plain(out))
        self.assertNotIn("\x9b", out)
        self.assertNoErrors()

    def test_long_strings_are_capped(self):
        out = plain(cli.build({"cwd": "/" + "d" * 500, "model": {"display_name": "M" * 500}}))
        self.assertLess(len(out.split("\n")[0]), 150)

    def test_context_fallback(self):
        data = {"context_window": {"used_percentage": None, "context_window_size": 200000,
                                   "current_usage": {"input_tokens": 50000, "cache_read_input_tokens": 50000}}}
        self.assertIn("50%", plain(cli.build(data)))
        data["context_window"]["context_window_size"] = 0
        self.assertIn("--", plain(cli.build(data)))
        self.assertNoErrors()

    def test_failing_segment_is_isolated(self):
        with mock.patch.object(cli, "seg_model", side_effect=RuntimeError("boom")):
            out = plain(cli.build(FULL))
        self.assertNotIn("Opus", out)
        self.assertIn("63%", out)
        self.assertIn("41%", out)
        self.assertIn("boom", (self.cache / "error.log").read_text())

    def test_fits_width(self):
        for width in (100, 60, 40, 25):
            with self.subTest(width=width):
                out = plain(cli.build({**FULL, "cwd": "/x"}, width=width))
                line1, line2 = out.split("\n")
                self.assertLessEqual(len(line2), width, line2)
                self.assertTrue(line2.startswith("ctx"))
        # Sem largura conhecida, nada é cortado
        self.assertIn("$1.23", plain(cli.build(FULL, width=None)))

    def test_width_drop_order(self):
        out = plain(cli.build(FULL, width=60)).split("\n")[1]
        self.assertNotIn("$", out)   # extras saem primeiro
        self.assertNotIn("7d", out)  # depois o limite semanal
        self.assertIn("5h", out)

    def test_terminal_width_from_env(self):
        os.environ["COLUMNS"] = "77"
        self.assertEqual(cli.terminal_width(), 77)

    def test_no_color(self):
        os.environ["NO_COLOR"] = "1"
        self.assertNotIn("\x1b", cli.build(FULL))

    def test_log_rotation(self):
        log = self.cache / "error.log"
        log.write_text("x" * (cli.LOG_MAX_BYTES + 1))
        try:
            raise RuntimeError("novo")
        except RuntimeError:
            cli.log_error("teste")
        self.assertTrue((self.cache / "error.log.1").exists())
        self.assertIn("novo", log.read_text())


class MainProcessTest(unittest.TestCase):
    """Roda o script de verdade, como o Claude Code faz."""

    def run_cli(self, payload, **env):
        with tempfile.TemporaryDirectory() as cache:
            full_env = {**os.environ, "XDG_CACHE_HOME": cache, "COLUMNS": "200", **env}
            full_env.pop("NO_COLOR", None)
            result = subprocess.run([sys.executable, str(CLI_PATH)], input=payload,
                                    capture_output=True, env=full_env, timeout=10)
            debug = Path(cache, "claude-statusline", "last.json")
            return result, debug.read_bytes() if debug.exists() else None

    def test_ascii_locale(self):
        result, _ = self.run_cli(json.dumps(FULL).encode(), PYTHONIOENCODING="ascii", LC_ALL="C", LANG="C")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("█", result.stdout.decode("utf-8"))

    def test_invalid_payloads(self):
        for payload in (b"xx", b"[1,2]", b"\xff\xfe{", b"[" * 100000):
            with self.subTest(payload=payload[:10]):
                result, _ = self.run_cli(payload)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(b"statusline:", result.stdout)
                self.assertEqual(result.stderr, b"")

    def test_empty_input(self):
        result, _ = self.run_cli(b"")
        self.assertEqual((result.returncode, result.stdout), (0, b""))

    def test_debug_dump(self):
        payload = json.dumps(FULL).encode()
        _, dumped = self.run_cli(payload, STATUSLINE_DEBUG="1")
        self.assertEqual(dumped, payload)


@unittest.skipUnless(shutil.which("git"), "git não instalado")
class GitTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = tmp.name

    def git(self, *args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                        "-c", "init.defaultBranch=main", "-C", self.repo, *args],
                       check=True, capture_output=True)

    def test_branch_dirty_and_detached(self):
        self.git("init", "-q")
        self.assertEqual(cli.git_branch(self.repo), "main")  # repo sem commits
        Path(self.repo, "a").write_text("1")
        self.git("add", "a")
        self.git("commit", "-qm", "init")
        self.assertEqual(cli.git_branch(self.repo), "main")
        Path(self.repo, "a").write_text("2")
        self.assertEqual(cli.git_branch(self.repo), "main*")
        self.assertFalse(Path(self.repo, ".git", "index.lock").exists())
        self.git("stash", "-q")
        self.git("checkout", "-q", "--detach")
        self.assertRegex(cli.git_branch(self.repo), r"^[0-9a-f]{7}$")

    def test_not_a_repo(self):
        self.assertIsNone(cli.git_branch(self.repo))
        self.assertIsNone(cli.git_branch("/caminho/que/nao/existe"))
        self.assertIsNone(cli.git_branch("/tmp\0x"))


if __name__ == "__main__":
    unittest.main()
