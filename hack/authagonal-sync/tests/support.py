"""Fixtures: local bare repositories standing for Drawboard, the fork and authagonal-cloud, a stand-in gh, and a way to run sync.py against them."""
import json
import os
import shutil
import subprocess
import sys
import tempfile

from . import CANARY_GH, GuardedTestCase  # noqa: F401  (importing the package runs the guard)

HERE = os.path.dirname(os.path.abspath(__file__))
SYNC = os.path.join(os.path.dirname(HERE), "sync.py")
IDENT = {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com", "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com"}
FORK_REPO = "authagonal/ingress-nginx"
CLOUD_REPO = "authagonal/authagonal-cloud"
T = "drawboard/main"  # the fork's branch the sync brings Drawboard into
SHA_A, SHA_B, SHA_C = "sha256:" + "a" * 64, "sha256:" + "b" * 64, "sha256:" + "c" * 64

MAIN_TF = '''locals {
  cluster = "dev"
}

locals {
  ingress_nginx_controller_ref = "ghcr.io/authagonal/ingress-nginx/controller:v1.0.0-%(suffix)s@%(ca)s"   # pinned by digest
  ingress_nginx_certgen_ref    = "ghcr.io/authagonal/ingress-nginx/kube-webhook-certgen:v1.6.9-4b54b827e2fa-go1.26.2@%(cg)s"
}

locals {
  ingress_nginx_controller_parts = regex("^([^/]+)/([^:]+):([^@]+)@(sha256:[0-9a-f]{64})$", local.ingress_nginx_controller_ref)
  ingress_nginx_certgen_parts    = regex("^([^/]+)/([^:]+):([^@]+)@(sha256:[0-9a-f]{64})$", local.ingress_nginx_certgen_ref)
}
'''


def git(cwd, *args, check=True, env=None):
    r = subprocess.run(["git", *args], cwd=cwd, env={**os.environ, **IDENT, **(env or {})}, capture_output=True, text=True, timeout=120)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} in {cwd}: {r.stderr}{r.stdout}")
    return r.stdout.strip()


class World:
    """root/drawboard.git, root/fork.git and root/cloud.git (bare), the clones the tests make commits in, and the stand-in gh."""

    def __init__(self, root):
        self.root = root
        self.drawboard, self.fork, self.cloud = (os.path.join(root, n + ".git") for n in ("drawboard", "fork", "cloud"))
        for r in (self.drawboard, self.fork, self.cloud):
            subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", r], check=True)
        self.dwork = self.clone(self.drawboard, "dwork")
        self.write(self.dwork, "a.txt", "one\ntwo\nthree\n")
        self.write(self.dwork, "README", "drawboard\n")
        self.commit(self.dwork, "base")
        git(self.dwork, "push", "--quiet", "origin", f"HEAD:refs/heads/{T}")
        self.base = git(self.dwork, "rev-parse", "HEAD")
        # the fork's branch: Drawboard's base, then a merge commit of the kind the sync makes, so there is a Drawboard-Head trailer
        git(self.dwork, "push", "--quiet", self.fork, f"HEAD:refs/heads/{T}")
        self.fwork = self.clone(self.fork, "fwork", branch=T)
        git(self.fwork, "commit", "--quiet", "--allow-empty", "-m", f"Merge Drawboard's ingress-nginx {self.base[:12]}\n\nDrawboard-Head: {self.base}")
        git(self.fwork, "push", "--quiet", "origin", f"HEAD:refs/heads/{T}")
        # authagonal-cloud: develop pins a controller built from Drawboard's base
        self.cwork = self.clone(self.cloud, "cwork")
        self.set_pin(self.base[:12])
        self.state = os.path.join(root, "gh-state.json")
        self.gh_bin = os.path.join(root, "ghbin")
        os.makedirs(self.gh_bin)
        with open(os.path.join(self.gh_bin, "gh"), "w") as f:
            f.write(f'#!/bin/sh\nexec "{sys.executable}" "{os.path.join(HERE, "fake_gh.py")}" "$@"\n')
        os.chmod(os.path.join(self.gh_bin, "gh"), 0o755)
        self.gh_state({})

    def set_pin(self, suffix, certgen=SHA_B):
        os.makedirs(os.path.join(self.cwork, "infra", "platform"), exist_ok=True)
        self.write(self.cwork, "infra/platform/main.tf", MAIN_TF % {"suffix": suffix, "ca": SHA_A, "cg": certgen})
        self.commit(self.cwork, "pin")
        git(self.cwork, "push", "--quiet", "--force", "origin", "HEAD:refs/heads/develop")

    @staticmethod
    def clone(url, name, branch=None):
        dest = os.path.join(os.path.dirname(url), name)
        subprocess.run(["git", "clone", "--quiet", *(["--branch", branch] if branch else []), url, dest], check=True,
                       capture_output=True, env={**os.environ, **IDENT})
        return dest

    @staticmethod
    def write(repo, rel, text):
        p = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)

    @staticmethod
    def commit(repo, msg):
        git(repo, "add", "-A")
        git(repo, "commit", "--quiet", "-m", msg)
        return git(repo, "rev-parse", "HEAD")

    def drawboard_commit(self, rel, text, msg="drawboard change"):
        """A commit on Drawboard's branch; returns its sha."""
        self.write(self.dwork, rel, text)
        sha = self.commit(self.dwork, msg)
        git(self.dwork, "push", "--quiet", "--force", "origin", f"HEAD:refs/heads/{T}")
        return sha

    def drawboard_rewrite(self, rel, text):
        """Drawboard force-pushes its branch to a history that has none of the old commits; returns the new head."""
        git(self.dwork, "checkout", "--quiet", "--orphan", "rewritten")
        self.write(self.dwork, rel, text)
        sha = self.commit(self.dwork, "rewritten history")
        git(self.dwork, "push", "--quiet", "--force", "origin", f"HEAD:refs/heads/{T}")
        return sha

    def fork_commit(self, rel, text, msg="authagonal change"):
        self.write(self.fwork, rel, text)
        sha = self.commit(self.fwork, msg)
        git(self.fwork, "push", "--quiet", "origin", f"HEAD:refs/heads/{T}")
        return sha

    def head(self, repo, ref):
        return git(repo, "rev-parse", ref)

    # ---- the stand-in gh
    def gh_state(self, update=None):
        st = {}
        if os.path.exists(self.state):
            with open(self.state) as f:
                st = json.load(f)
        if update is not None:
            st.update(update)
            with open(self.state, "w") as f:
                json.dump(st, f)
        return st

    def add_pr(self, repo, head, state="open"):
        st = self.gh_state()
        st.setdefault("pulls", {}).setdefault(repo, []).append(
            {"number": 1, "state": state, "head": {"ref": head}, "base": {"ref": "x"}, "title": "t", "body": "", "html_url": "u"})
        self.gh_state(st)

    def pulls(self, repo):
        return self.gh_state().get("pulls", {}).get(repo, [])

    # ---- sync.py
    def env(self, **extra):
        env = dict(os.environ)
        env["PATH"] = self.gh_bin + os.pathsep + env["PATH"]  # ahead of the guard's canary gh
        env.update(FAKE_GH_STATE=self.state, SYNC_FORK_URL=self.fork, SYNC_FORK_REPO=FORK_REPO, SYNC_DRAWBOARD_URL=self.drawboard,
                   SYNC_CLOUD_URL=self.cloud, SYNC_CLOUD_REPO=CLOUD_REPO, GH_TOKEN="fake-token-not-a-secret", GITHUB_RUN_ID="4242",
                   SYNC_TF_POLL_SECONDS="0.01", SYNC_TF_WAIT_SECONDS="5")
        for k in ("GITHUB_OUTPUT", "GITHUB_REF_NAME", "SYNC_FORCE", "GITHUB_REPOSITORY"):
            env.pop(k, None)
        env.update(extra)
        return env

    def sync(self, *args, **extra_env):
        out = os.path.join(self.root, "github-output")
        if os.path.exists(out):
            os.unlink(out)
        r = subprocess.run([sys.executable, SYNC, *args, "--github-output", out] if args[0] != "edit-main-tf" else [sys.executable, SYNC, *args],
                           env=self.env(**extra_env), capture_output=True, text=True, timeout=120, cwd=self.root)
        outputs = {}
        if os.path.exists(out):
            with open(out) as f:
                outputs = dict(ln.rstrip("\n").split("=", 1) for ln in f if "=" in ln)
        r.outputs = outputs
        return r

    def bring_in(self, *args, **env):
        return self.sync("bring-in", "--branch", T, *args, **env)


class WorldCase(GuardedTestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="authagonal-sync-test-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.w = World(self._tmp)

    def ok(self, r):
        self.assertEqual(r.returncode, 0, f"stdout: {r.stdout}\nstderr: {r.stderr}")
        return r
