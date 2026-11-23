"""The suite's guard. It runs on import, before any test, in the test process's own environment, so every child process inherits it.

The suite runs on a person's machine, where gh and git hold real credentials for github.com. Nothing here may reach GitHub, so:

- GH_CONFIG_DIR is an empty temporary directory, and GH_TOKEN, GITHUB_TOKEN and GH_ENTERPRISE_TOKEN are unset: gh has no login.
- GIT_CONFIG_GLOBAL is a temporary file rewriting https://github.com/, git@github.com: and ssh://git@github.com/ (insteadOf) to a path that does
  not exist; GIT_CONFIG_NOSYSTEM=1, GIT_TERMINAL_PROMPT=0, GIT_SSH_COMMAND=false and no SSH_AUTH_SOCK: git has no route, no prompt and no key.
- A canary gh is first on PATH. It records its call in CANARY_GH_LOG and exits non-zero. A test that needs a gh puts its own stand-in on PATH
  ahead of this one; the canary is only reached when the code under test reaches for a gh it was not given.

GuardedTestCase fails a test whose run called the canary.
"""
import atexit
import os
import shutil
import stat
import tempfile
import unittest

NONEXISTENT = "/nonexistent/authagonal-sync-guard/"

# The real gh, found before the guard puts anything on PATH: the first gh on the original PATH that is a binary. A script named gh is a
# stand-in someone else put there (the loop's own canary is one), not the real thing, and running it would only call that canary.
def _find_real_gh(path):
    for d in path.split(os.pathsep):
        cand = os.path.join(d, "gh")
        if d and os.path.isfile(cand) and os.access(cand, os.X_OK):
            with open(cand, "rb") as f:
                if f.read(2) != b"#!":
                    return os.path.abspath(cand)
    return None


REAL_GH = _find_real_gh(os.environ.get("PATH", ""))

_ROOT = tempfile.mkdtemp(prefix="authagonal-sync-guard-")
atexit.register(shutil.rmtree, _ROOT, ignore_errors=True)
ROOT = _ROOT
BIN = os.path.join(_ROOT, "bin")
CANARY_GH = os.path.join(BIN, "gh")
CANARY_GH_LOG = os.path.join(_ROOT, "gh-canary-called.log")
GH_CONFIG_DIR = os.path.join(_ROOT, "ghcfg")
GIT_CONFIG_GLOBAL = os.path.join(_ROOT, "gitconfig")
HOME = os.path.join(_ROOT, "home")

for _d in (BIN, GH_CONFIG_DIR, HOME):
    os.makedirs(_d)
with open(CANARY_GH, "w") as _f:
    _f.write('#!/bin/sh\n'
             'echo "gh canary: called with $*" >> "${CANARY_GH_LOG:-%s}"\n'
             'echo "gh canary: refused" >&2\n'
             'exit 99\n' % CANARY_GH_LOG)
os.chmod(CANARY_GH, os.stat(CANARY_GH).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
with open(GIT_CONFIG_GLOBAL, "w") as _f:
    _f.write('[url "%s"]\n\tinsteadOf = https://github.com/\n\tinsteadOf = git@github.com:\n\tinsteadOf = ssh://git@github.com/\n' % NONEXISTENT)

for _k in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "SSH_AUTH_SOCK"):
    os.environ.pop(_k, None)
os.environ.update(
    GH_CONFIG_DIR=GH_CONFIG_DIR,
    GIT_CONFIG_GLOBAL=GIT_CONFIG_GLOBAL,
    GIT_CONFIG_NOSYSTEM="1",
    GIT_CONFIG_SYSTEM="/dev/null",
    GIT_TERMINAL_PROMPT="0",
    GIT_SSH_COMMAND="false",
    GIT_ASKPASS="/bin/false",
    SSH_ASKPASS="/bin/false",
    HOME=HOME,
    XDG_CONFIG_HOME=os.path.join(HOME, ".config"),
    CANARY_GH_LOG=CANARY_GH_LOG,
    PATH=BIN + os.pathsep + os.environ.get("PATH", ""),
)


def canary_calls():
    try:
        with open(CANARY_GH_LOG) as f:
            return f.read()
    except FileNotFoundError:
        return ""


class GuardedTestCase(unittest.TestCase):
    """A test that must never reach the canary gh."""

    def tearDown(self):
        self.assertEqual(canary_calls(), "", "the canary gh was called: a test reached a gh it was not given")
