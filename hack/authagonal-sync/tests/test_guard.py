"""The guard's own test: gh by PATH, gh by absolute path and a git push to github.com each fail without reaching GitHub."""
import os
import shutil
import subprocess
import tempfile
import unittest

from . import (BIN, CANARY_GH, CANARY_GH_LOG, GH_CONFIG_DIR, GIT_CONFIG_GLOBAL, NONEXISTENT, REAL_GH, canary_calls)

ID = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60, **kw)


class GuardTest(unittest.TestCase):
    def setUp(self):
        # the guard's deliberate calls go to their own log, so the suite-wide canary log stays a record of accidents
        self.log = tempfile.NamedTemporaryFile(prefix="deliberate-", delete=False)
        self.log.close()
        self.addCleanup(os.unlink, self.log.name)
        self.env = dict(os.environ, CANARY_GH_LOG=self.log.name)
        self.before = canary_calls()

    def tearDown(self):
        self.assertEqual(canary_calls(), self.before, "a call reached the suite-wide canary log")

    def called(self):
        with open(self.log.name) as f:
            return f.read()

    def test_environment_is_guarded(self):
        self.assertEqual(os.environ["GH_CONFIG_DIR"], GH_CONFIG_DIR)
        self.assertEqual(os.listdir(GH_CONFIG_DIR), [])
        self.assertEqual(os.environ["GIT_CONFIG_GLOBAL"], GIT_CONFIG_GLOBAL)
        self.assertEqual(os.environ["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(os.environ["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(os.environ["GIT_SSH_COMMAND"], "false")
        for k in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "SSH_AUTH_SOCK"):
            self.assertNotIn(k, os.environ)
        self.assertEqual(os.environ["PATH"].split(os.pathsep)[0], BIN)
        self.assertEqual(shutil.which("gh"), CANARY_GH)

    def test_gh_by_path_is_refused(self):
        r = run(["gh", "auth", "status"], env=self.env)
        self.assertEqual(r.returncode, 99)
        self.assertIn("gh canary: refused", r.stderr)
        self.assertIn("called with auth status", self.called())

    def test_gh_by_absolute_path_is_refused(self):
        r = run([CANARY_GH, "api", "user"], env=self.env)
        self.assertEqual(r.returncode, 99)
        self.assertIn("called with api user", self.called())
        if REAL_GH is None:
            self.skipTest("no real gh binary on the original PATH")
        # the real gh, with the guard's empty config and no token, is not logged in: both commands are answered locally
        for args in (["auth", "status"], ["auth", "token"]):
            r = run([REAL_GH, *args], env=self.env)
            self.assertNotEqual(r.returncode, 0, f"{args}: {r.stdout}{r.stderr}")
            self.assertNotIn("Logged in to", r.stdout + r.stderr)
            self.assertNotRegex(r.stdout.strip(), r"^gh[opsu]_|^github_pat_")

    def test_git_push_to_github_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            for c in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "x"]):
                self.assertEqual(run(["git", *c], cwd=d, env={**self.env, **ID}).returncode, 0)
            for url in ("https://github.com/authagonal/ingress-nginx", "git@github.com:authagonal/ingress-nginx.git",
                        "ssh://git@github.com/authagonal/ingress-nginx.git"):
                got = run(["git", "ls-remote", "--get-url", url], cwd=d, env=self.env).stdout.strip()
                self.assertTrue(got.startswith(NONEXISTENT), f"{url} resolves to {got}")
            r = run(["git", "push", "https://github.com/authagonal/ingress-nginx", "HEAD:refs/heads/guard-test"], cwd=d, env=self.env)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn(NONEXISTENT, r.stderr)
            self.assertNotIn("github.com", r.stderr.replace(NONEXISTENT, ""))


if __name__ == "__main__":
    unittest.main()
