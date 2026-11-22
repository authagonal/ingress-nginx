"""bump: D-5. One commit as the App on the bump branch from develop's head, the PR into develop with its refs, cosign commands, run URL and
Drawboard commit links, and the wait for a terraform.yml run in progress."""
import re
import subprocess

from .support import CLOUD_REPO, MAIN_TF, SHA_A, SHA_B, SHA_C, T, WorldCase, git

CTL = "ghcr.io/authagonal/ingress-nginx/controller:v1.0.1-%s@" + SHA_C
CERTGEN_PINNED = "ghcr.io/authagonal/ingress-nginx/kube-webhook-certgen:v1.6.9-4b54b827e2fa-go1.26.2@" + SHA_B
CERTGEN_NEW = "ghcr.io/authagonal/ingress-nginx/kube-webhook-certgen:v1.7.0-0123456789ab-go1.26.2@sha256:" + "d" * 64
BASE_REF = "ghcr.io/authagonal/ingress-nginx/nginx:v2.2.8-fedcba987654@sha256:" + "e" * 64
BRANCH = "authagonal-ingress-nginx/drawboard-%s"
RUN_URL = "https://github.com/authagonal/ingress-nginx/actions/runs/4242"
LINK = re.compile(r"https://github\.com/DrawboardLtd/ingress-nginx/commit/([0-9a-f]{7,40})")


class BumpTest(WorldCase):
    def prepare(self):
        """Drawboard has two commits the pinned controller (built from its base) lacks; the fork has them merged."""
        self.d1 = self.w.drawboard_commit("one.txt", "1\n", "first drawboard fix")
        self.d2 = self.w.drawboard_commit("two.txt", "2\n", "second drawboard fix")
        self.ok(self.w.bring_in())
        self.merge = self.w.head(self.w.fork, "drawboard/main")
        self.branch = BRANCH % self.d2[:12]
        self.develop = self.w.head(self.w.cloud, "develop")

    def bump(self, controller=None, certgen=None, extra=(), **env):
        args = ["bump", "--controller", CTL % self.merge[:12] if controller is None else controller, "--commit", self.merge, "--drawboard", self.d2,
                "--branch", self.branch, "--run-url", RUN_URL, *extra]
        if certgen:
            args += ["--certgen", certgen]
        return self.w.sync(*args, **env)

    def diff_lines(self):
        d = git(self.w.cloud, "diff", "-U0", "--no-color", "develop", self.branch)
        return [ln for ln in d.splitlines() if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]

    def commits_on_branch(self):
        return git(self.w.cloud, "log", "--format=%H", f"develop..{self.branch}").split()

    # ---- one commit, only the ref lines
    def test_certgen_changed_edits_both_ref_lines_in_one_commit(self):
        self.prepare()
        r = self.ok(self.bump(certgen=CERTGEN_NEW))
        (commit,) = self.commits_on_branch()
        self.assertEqual(git(self.w.cloud, "rev-parse", f"{commit}^"), self.develop)
        self.assertEqual(git(self.w.cloud, "diff", "--name-only", "develop", self.branch), "infra/platform/main.tf")
        lines = self.diff_lines()
        self.assertEqual(len(lines), 4, lines)
        self.assertEqual(sorted(re.match(r"[+-]\s*(\w+)", ln).group(1) for ln in lines), ["ingress_nginx_certgen_ref"] * 2 + ["ingress_nginx_controller_ref"] * 2)
        new = git(self.w.cloud, "show", f"{self.branch}:infra/platform/main.tf")
        self.assertIn(f'= "{CTL % self.merge[:12]}"', new)
        self.assertIn(f'= "{CERTGEN_NEW}"', new)
        self.assertEqual(git(self.w.cloud, "log", "-1", "--format=%ae", commit), "4242+authagonal-ingress-nginx-sync[bot]@users.noreply.github.com")
        self.assertEqual(r.outputs["pr_url"], self.w.pulls(CLOUD_REPO)[0]["html_url"])

    def test_certgen_unchanged_leaves_the_certgen_line_alone(self):
        self.prepare()
        self.ok(self.bump(certgen=CERTGEN_PINNED))
        self.assertEqual(len(self.commits_on_branch()), 1)
        lines = self.diff_lines()
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(all("ingress_nginx_controller_ref" in ln for ln in lines), lines)
        body = self.w.pulls(CLOUD_REPO)[0]["body"]
        self.assertNotIn(CERTGEN_PINNED, body.split("## Verify")[0].split("## New refs")[1].replace("unchanged", ""), "certgen is not listed as a new ref")
        self.assertIn("already pins", body)

    def test_certgen_unchanged_when_the_run_gives_none(self):
        self.prepare()
        self.ok(self.bump())
        self.assertEqual(len(self.diff_lines()), 2)

    def test_pr_opens_into_develop_from_the_branch_with_the_refs_cosign_and_run_url(self):
        self.prepare()
        self.ok(self.bump(certgen=CERTGEN_NEW, extra=["--base", BASE_REF]))
        (pr,) = self.w.pulls(CLOUD_REPO)
        self.assertEqual((pr["head"]["ref"], pr["base"]["ref"]), (self.branch, "develop"))
        body = pr["body"]
        for ref in (CTL % self.merge[:12], CERTGEN_NEW, BASE_REF):
            self.assertIn(ref, body)
            repo, digest = ref.split("@")[0].rsplit(":", 1)[0], ref.split("@")[1]
            self.assertIn(f"cosign verify {repo}@{digest}", body)
        self.assertEqual(body.count("--certificate-oidc-issuer https://token.actions.githubusercontent.com"), 3)
        self.assertIn(r"--certificate-identity-regexp '^https://github\.com/authagonal/ingress-nginx/\.github/workflows/authagonal-images\.yml@refs/'", body)
        self.assertIn(RUN_URL, body)

    def test_run_url_defaults_to_the_actions_environment(self):
        self.prepare()
        args = ["bump", "--controller", CTL % self.merge[:12], "--commit", self.merge, "--drawboard", self.d2, "--branch", self.branch]
        self.ok(self.w.sync(*args, GITHUB_SERVER_URL="https://github.com", GITHUB_REPOSITORY="authagonal/ingress-nginx", GITHUB_RUN_ID="31337"))
        self.assertIn("https://github.com/authagonal/ingress-nginx/actions/runs/31337", self.w.pulls(CLOUD_REPO)[0]["body"])

    # ---- the Drawboard commits
    def test_commits_listed_are_those_in_d_that_the_pinned_build_lacks(self):
        self.prepare()
        self.ok(self.bump())
        body = self.w.pulls(CLOUD_REPO)[0]["body"]
        want = git(self.w.fork, "rev-list", f"{self.w.base}..{self.d2}").split()
        self.assertEqual(sorted(LINK.findall(body)), sorted(want))
        self.assertEqual(sorted(want), sorted([self.d1, self.d2]))
        self.assertIn(f"https://github.com/DrawboardLtd/ingress-nginx/commit/{self.d1} first drawboard fix", body)
        self.assertNotIn("None:", body)

    def test_a_long_range_keeps_the_pr_body_under_githubs_limit_and_links_the_whole_range(self):
        self.prepare()
        n = 800  # about 96 characters of link plus a 60-character subject each: far past 65536 together
        stream = "".join(f"commit refs/heads/{T}\ncommitter T <t@example.com> {1700000000 + i} +0000\ndata {len(m := f'drawboard change {i:04d} ' + 'x' * 40)}\n{m}\n"
                         f"{'from ' + self.d2 + chr(10) if i == 0 else ''}\n" for i in range(n))
        subprocess.run(["git", "fast-import", "--quiet", "--force"], cwd=self.w.drawboard, input=stream, text=True, check=True)
        d = git(self.w.drawboard, "rev-parse", T)
        self.merge = d
        self.ok(self.w.sync("bump", "--controller", CTL % d[:12], "--commit", d, "--drawboard", d, "--branch", BRANCH % d[:12], "--run-url", RUN_URL))
        (pr,) = self.w.pulls(CLOUD_REPO)
        self.assertLess(len(pr["body"]), 65536)
        links = LINK.findall(pr["body"])
        self.assertGreater(len(links), 300, "a long range still lists the newest commits")
        self.assertEqual(links[0], d)
        self.assertRegex(pr["body"], rf"and {n + 2 - len(links)} older ones: https://github\.com/DrawboardLtd/ingress-nginx/compare/")

    def test_commits_none_when_the_pinned_build_has_every_drawboard_commit(self):
        self.prepare()
        self.w.set_pin(self.merge[:12])  # develop already pins a controller built from the merge that has d1 and d2
        self.ok(self.bump(controller="ghcr.io/authagonal/ingress-nginx/controller:v1.0.2-%s@%s" % (self.merge[:12], SHA_B)))
        body = self.w.pulls(CLOUD_REPO)[0]["body"]
        self.assertEqual(LINK.findall(body), [])
        self.assertRegex(body, r"(?i)\bnone\b")

    def test_commits_listed_says_so_when_the_pinned_commit_is_unknown(self):
        self.prepare()
        self.w.set_pin("0123456789ab")
        self.ok(self.bump())
        body = self.w.pulls(CLOUD_REPO)[0]["body"]
        self.assertEqual(LINK.findall(body), [])
        self.assertIn("Could not work out", body)

    # ---- nothing to do, and a branch left by a failed run
    def test_nothing_to_bump_opens_no_pr_when_develop_pins_the_refs_already(self):
        self.prepare()
        self.w.set_pin(self.merge[:12])
        r = self.ok(self.bump(controller="ghcr.io/authagonal/ingress-nginx/controller:v1.0.0-%s@%s" % (self.merge[:12], SHA_A)))
        self.assertEqual(self.w.pulls(CLOUD_REPO), [])
        self.assertEqual(r.outputs["pr_url"], "")
        self.assertEqual(git(self.w.cloud, "branch", "--list", self.branch), "")

    def test_a_branch_left_by_a_failed_run_is_replaced_not_develop(self):
        self.prepare()
        self.w.write(self.w.cwork, "stale.txt", "left behind\n")
        self.w.commit(self.w.cwork, "stale")
        git(self.w.cwork, "push", "--quiet", "--force", "origin", f"HEAD:refs/heads/{self.branch}")
        git(self.w.cwork, "reset", "--quiet", "--hard", self.develop)
        self.ok(self.bump())
        self.assertEqual(len(self.commits_on_branch()), 1)
        self.assertEqual(self.w.head(self.w.cloud, "develop"), self.develop)

    # ---- D-5's wait
    def test_terraform_wait_polls_until_no_run_is_in_progress(self):
        self.prepare()
        self.w.gh_state({"terraform_busy_polls": 2})
        r = self.ok(self.bump())
        polls = [c for c in self.w.gh_state()["calls"] if "terraform.yml" in (c.get("target") or "")]
        self.assertEqual(len(polls), 3)
        self.assertIn("branch=develop", polls[0]["target"])
        self.assertIn("waiting", r.stdout)
        self.assertEqual(len(self.w.pulls(CLOUD_REPO)), 1)

    def test_terraform_wait_gives_up_and_opens_the_pr_anyway(self):
        self.prepare()
        self.w.gh_state({"terraform_busy_forever": True})
        r = self.ok(self.bump(SYNC_TF_WAIT_SECONDS="0.1", SYNC_TF_POLL_SECONDS="0.02"))
        self.assertIn("opening the PR anyway", r.stdout)
        self.assertEqual(len(self.w.pulls(CLOUD_REPO)), 1)

    def test_terraform_wait_comes_before_develops_head_is_read(self):
        self.prepare()
        self.w.gh_state({"terraform_busy_polls": 1})
        r = self.ok(self.bump())
        self.assertIn("waiting", r.stdout)
        self.assertEqual(git(self.w.cloud, "rev-parse", f"{self.branch}^"), self.develop)

    # ---- refusals
    def test_an_unpublished_controller_is_refused(self):
        self.prepare()
        r = self.bump(controller="")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.w.pulls(CLOUD_REPO), [])


if __name__ == "__main__":
    import unittest
    unittest.main()
