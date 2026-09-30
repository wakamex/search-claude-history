import json
import os
import subprocess
import sys
import tempfile
import unittest
from importlib.metadata import version
from pathlib import Path


def record(role, text, timestamp):
    return {
        "type": role,
        "sessionId": "0123abcd-0000-0000-0000-000000000000",
        "timestamp": timestamp,
        "message": {"role": role, "content": text},
    }


class CliTests(unittest.TestCase):
    def run_cli(self, *args, config_dir=None):
        env = dict(os.environ, NO_COLOR="1")
        if config_dir is not None:
            env["CLAUDE_CONFIG_DIR"] = str(config_dir)
        return subprocess.run(
            args, capture_output=True, text=True, env=env, check=False
        )

    def test_entry_points_report_the_package_version(self):
        expected = version("search-claude-history")
        for args in (
            ("sch", "--version"),
            ("search-claude-history", "--version"),
            (sys.executable, "-m", "search_claude_history", "--version"),
        ):
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, result.stdout)

    def test_search_finds_matching_messages_in_synthetic_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            config_dir = Path(temporary)
            project = config_dir / "projects" / "-code-demo"
            project.mkdir(parents=True)
            session = project / "0123abcd-0000-0000-0000-000000000000.jsonl"
            session.write_text(
                "\n".join(
                    json.dumps(item)
                    for item in (
                        record("user", "where is the frobnicate helper", "2026-01-02T03:04:05Z"),
                        record("assistant", "it lives in utils", "2026-01-02T03:04:06Z"),
                    )
                )
                + "\n"
            )

            found = self.run_cli("sch", "frobnicate", config_dir=config_dir)
            missing = self.run_cli("sch", "no-such-text", config_dir=config_dir)

        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertIn("where is the frobnicate helper", found.stdout)
        self.assertIn("-code-demo", found.stdout)
        self.assertNotIn("it lives in utils", found.stdout)
        self.assertNotIn("where is the frobnicate helper", missing.stdout)


if __name__ == "__main__":
    unittest.main()
