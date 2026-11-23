"""bring-in: D-2 (merging Drawboard in), D-3 (already handled) and D-6 (the person PR), against local bare repositories and a stand-in gh."""
import os
import stat

from . import canary_calls
from .support import CLOUD_REPO, FORK_REPO, SYNC, T, WorldCase, git
import importlib.util
import tempfile

CLOUD_PFX = "authagonal-ingress-nginx/drawboard-"
PERSON_PFX = "authagonal-sync/drawboard-"


class BringInTest(WorldCase):
    # ---- D-2: nothing to bring in
    def test_already_contained_makes_no_merge_and_builds_the_head(self):
        before = self.w.head(self.w.fork, T)
        r = self.ok(self.w.bring_in())
        self.assertEqual(self.w.head(self.w.fork, T), before)
        self.assertEqual(r.outputs["outcome"], "contained")
        self.assertEqual((r.outputs["build"], r.outputs["commit"], r.outputs["drawboard"]), ("true", before, self.w.base))
        self.assertEqual(r.outputs["bump_branch"], CLOUD_PFX + self.w.base[:12])
        self.assertEqual(self.w.pulls(FORK_REPO), [])

    def test_already_contained_when_t_is_ahead_of_drawboard(self):
        ahead = self.w.fork_commit("local.txt", "authagonal only\n")
        r = self.ok(self.w.bring_in())
        self.assertEqual(self.w.head(self.w.fork, T), ahead)
        self.assertEqual(r.outputs["outcome"], "contained")

    # ---- D-2: the merge
    def test_clean_merge_makes_a_merge_commit_of_d_into_t(self):
        self.w.fork_commit("local.txt", "authagonal only\n")
        before = self.w.head(self.w.fork, T)
        d = self.w.drawboard_commit("new.txt", "from drawboard\n")
        r = self.ok(self.w.bring_in())
        head = self.w.head(self.w.fork, T)
        self.assertEqual(git(self.w.fork, "rev-parse", f"{head}^1"), before)
        self.assertEqual(git(self.w.fork, "rev-parse", f"{head}^2"), d)
        self.assertEqual(git(self.w.fork, "show", f"{head}:new.txt"), "from drawboard")
        self.assertEqual(git(self.w.fork, "show", f"{head}:local.txt"), "authagonal only")
        self.assertEqual((r.outputs["outcome"], r.outputs["build"], r.outputs["commit"], r.outputs["drawboard"]), ("merged", "true", head, d))
        self.assertEqual(r.outputs["bump_branch"], CLOUD_PFX + d[:12])

    def test_trailer_names_the_drawboard_head_and_the_app_authors(self):
        d = self.w.drawboard_commit("new.txt", "from drawboard\n")
        self.ok(self.w.bring_in())
        head = self.w.head(self.w.fork, T)
        self.assertEqual(git(self.w.fork, "log", "-1", "--format=%(trailers:key=Drawboard-Head,valueonly)", head), d)
        self.assertEqual(git(self.w.fork, "log", "-1", "--format=%an <%ae>", head),
                         "authagonal-ingress-nginx-sync[bot] <4242+authagonal-ingress-nginx-sync[bot]@users.noreply.github.com>")

    def test_trailer_of_the_last_merge_is_what_a_rewrite_is_judged_by(self):
        d1 = self.w.drawboard_commit("new.txt", "from drawboard\n")
        self.ok(self.w.bring_in())
        self.assertEqual(self.w.head(self.w.fork, f"{T}^2"), d1)
        before = self.w.head(self.w.fork, T)
        d2 = self.w.drawboard_rewrite("new.txt", "different history\n")  # no longer contains d1, the trailer's head
        r = self.ok(self.w.bring_in())
        self.assertEqual(self.w.head(self.w.fork, T), before)
        self.assertEqual(r.outputs["outcome"], "rewritten-pr")
        self.assertIn(d1, self.w.pulls(FORK_REPO)[0]["body"])

    def _resolve_rewrite_by_a_person(self):
        """d1 merged; Drawboard rewrites to d2; the person merges the PR's head into T with a merge commit that has no trailer."""
        d1 = self.w.drawboard_commit("new.txt", "from drawboard\n")
        self.ok(self.w.bring_in())
        d2 = self.w.drawboard_rewrite("other.txt", "after the rewrite\n")
        self.assertEqual(self.ok(self.w.bring_in()).outputs["outcome"], "rewritten-pr")
        git(self.w.fwork, "pull", "--quiet", "--ff-only", "origin", T)
        git(self.w.fwork, "pull", "--quiet", "--no-rebase", "--allow-unrelated-histories", "--no-edit", self.w.drawboard, T)
        git(self.w.fwork, "push", "--quiet", "origin", f"HEAD:refs/heads/{T}")
        self.assertEqual(git(self.w.fwork, "log", "-1", "--format=%(trailers:key=Drawboard-Head,valueonly)"), "")
        self.assertEqual(self.ok(self.w.bring_in()).outputs["outcome"], "contained")
        return d1, d2

    def test_after_a_person_resolves_a_rewrite_the_next_drawboard_commit_is_merged(self):
        _, d2 = self._resolve_rewrite_by_a_person()
        d3 = self.w.drawboard_commit("third.txt", "on top of the rewrite\n")
        r = self.ok(self.w.bring_in())
        head = self.w.head(self.w.fork, T)
        self.assertEqual((r.outputs["outcome"], r.outputs["build"], r.outputs["drawboard"]), ("merged", "true", d3), r.stdout)
        self.assertEqual(git(self.w.fork, "rev-parse", f"{head}^2"), d3)
        self.assertEqual(r.outputs["bump_branch"], CLOUD_PFX + d3[:12])

    def test_a_second_rewrite_after_a_resolved_one_is_still_a_rewrite(self):
        self._resolve_rewrite_by_a_person()
        before = self.w.head(self.w.fork, T)
        git(self.w.dwork, "checkout", "--quiet", "--orphan", "again")
        self.w.write(self.w.dwork, "again.txt", "x\n")
        self.w.commit(self.w.dwork, "rewritten again")
        git(self.w.dwork, "push", "--quiet", "--force", "origin", f"HEAD:refs/heads/{T}")
        r = self.ok(self.w.bring_in())
        self.assertEqual((r.outputs["outcome"], self.w.head(self.w.fork, T)), ("rewritten-pr", before))

    def _person_merges_drawboard(self, *extra):
        git(self.w.fwork, "pull", "--quiet", "--ff-only", "origin", T)
        git(self.w.fwork, "pull", "--quiet", "--no-rebase", "--allow-unrelated-histories", "--no-edit", *extra, self.w.drawboard, T)
        git(self.w.fwork, "push", "--quiet", "origin", f"HEAD:refs/heads/{T}")

    def test_a_rewrite_of_a_head_a_person_merged_is_still_a_rewrite(self):
        # d1 merged with its trailer; Drawboard rewrites to r1, r2; a person merges r2; Drawboard keeps r1 and replaces r2 with r2b
        self.w.drawboard_commit("new.txt", "from drawboard\n")
        self.ok(self.w.bring_in())
        git(self.w.dwork, "checkout", "--quiet", "--orphan", "again")
        self.w.write(self.w.dwork, "r1.txt", "r1\n")
        r1 = self.w.commit(self.w.dwork, "r1")
        self.w.write(self.w.dwork, "r2.txt", "r2\n")
        r2 = self.w.commit(self.w.dwork, "r2")
        git(self.w.dwork, "push", "--quiet", "--force", "origin", f"HEAD:refs/heads/{T}")
        self.assertEqual(self.ok(self.w.bring_in()).outputs["outcome"], "rewritten-pr")
        self._person_merges_drawboard()
        self.assertEqual(self.ok(self.w.bring_in()).outputs["outcome"], "contained")
        git(self.w.dwork, "reset", "--quiet", "--hard", r1)
        self.w.write(self.w.dwork, "r2b.txt", "r2b\n")
        self.w.commit(self.w.dwork, "r2b")
        git(self.w.dwork, "push", "--quiet", "--force", "origin", f"HEAD:refs/heads/{T}")
        before = self.w.head(self.w.fork, T)
        r = self.ok(self.w.bring_in())
        self.assertEqual((r.outputs["outcome"], r.outputs["build"], self.w.head(self.w.fork, T)), ("rewritten-pr", "false", before), r.stdout)
        self.assertIn(r2, self.w.pulls(FORK_REPO)[-1]["body"])

    def test_a_rewrite_after_a_person_resolved_a_conflict_is_still_a_rewrite(self):
        self.w.fork_commit("a.txt", "one\nFORK\nthree\n")
        self.w.drawboard_commit("a.txt", "one\nDRAWBOARD\nthree\n", "dc")
        self.assertEqual(self.ok(self.w.bring_in()).outputs["outcome"], "conflict-pr")
        self._person_merges_drawboard("-X", "ours")
        self.assertEqual(self.ok(self.w.bring_in()).outputs["outcome"], "contained")
        git(self.w.dwork, "reset", "--quiet", "--hard", self.w.base)                      # back to the head T last had a trailer for, plus a replacement
        self.w.write(self.w.dwork, "replacement.txt", "x\n")
        self.w.commit(self.w.dwork, "replacement of dc")
        git(self.w.dwork, "push", "--quiet", "--force", "origin", f"HEAD:refs/heads/{T}")
        before = self.w.head(self.w.fork, T)
        r = self.ok(self.w.bring_in())
        self.assertEqual((r.outputs["outcome"], self.w.head(self.w.fork, T)), ("rewritten-pr", before), r.stdout)

    def test_a_pr_resolved_on_its_branch_then_merged_does_not_make_the_next_drawboard_commit_a_rewrite(self):
        self.w.fork_commit("a.txt", "one\nFORK\nthree\n")
        dc = self.w.drawboard_commit("a.txt", "one\nDRAWBOARD\nthree\n", "dc")
        self.assertEqual(self.ok(self.w.bring_in()).outputs["outcome"], "conflict-pr")
        branch = f"{PERSON_PFX}{dc[:12]}"
        person = self.w.clone(self.w.fork, "person", branch=T)
        git(person, "fetch", "--quiet", "origin", branch)
        git(person, "checkout", "--quiet", "-b", "pr", "FETCH_HEAD")
        git(person, "merge", "--quiet", "-X", "ours", "--no-edit", f"origin/{T}")        # "resolve conflicts": T merged into the PR branch
        git(person, "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}")
        git(person, "checkout", "--quiet", T)
        git(person, "merge", "--quiet", "--no-ff", "-m", f"Merge pull request #1 from authagonal/{branch}", "pr")
        git(person, "push", "--quiet", "origin", f"HEAD:refs/heads/{T}")
        self.assertEqual(self.ok(self.w.bring_in()).outputs["outcome"], "contained")
        d2 = self.w.drawboard_commit("next.txt", "on top of dc\n")
        r = self.ok(self.w.bring_in())
        self.assertEqual((r.outputs["outcome"], r.outputs["drawboard"]), ("merged", d2), r.stdout)

    def test_a_person_merge_of_something_else_is_not_a_drawboard_head(self):
        d = self.w.drawboard_commit("new.txt", "from drawboard\n")
        feature = self.w.clone(self.w.fork, "feature", branch=T)
        git(feature, "checkout", "--quiet", "-b", "feature")
        self.w.write(feature, "feature.txt", "f\n")
        self.w.commit(feature, "feature work")
        git(feature, "checkout", "--quiet", T)
        git(feature, "merge", "--quiet", "--no-ff", "-m", "Merge pull request #7 from authagonal/feature", "feature")
        git(feature, "push", "--quiet", "origin", f"HEAD:refs/heads/{T}")
        r = self.ok(self.w.bring_in())
        self.assertEqual((r.outputs["outcome"], r.outputs["drawboard"]), ("merged", d), r.stdout)

    def test_trailer_constant_is_the_head_the_fork_was_built_from(self):
        spec = importlib.util.spec_from_file_location("sync_under_test", SYNC)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.LAST_BROUGHT_IN, "8af499c7d8c1324a7e6895da0f5fc16d2c9643e6")
        with tempfile.TemporaryDirectory() as d:  # a history with no trailer at all
            git(d, "init", "-q", "-b", "main")
            git(d, "commit", "-q", "--allow-empty", "-m", "no trailer here")
            self.assertEqual(mod.last_brought_in(d, "HEAD"), mod.LAST_BROUGHT_IN)
            git(d, "commit", "-q", "--allow-empty", "-m", "merge\n\nDrawboard-Head: " + "1" * 40)
            git(d, "commit", "-q", "--allow-empty", "-m", "later, no trailer")
            self.assertEqual(mod.last_brought_in(d, "HEAD"), "1" * 40)

    def test_clean_merge_is_a_merge_commit_even_when_t_is_behind_d(self):
        git(self.w.dwork, "pull", "--quiet", "--no-rebase", self.w.fork, T)          # Drawboard contains T: a fast-forward is possible
        d = self.w.drawboard_commit("new.txt", "from drawboard\n")
        before = self.w.head(self.w.fork, T)
        self.ok(self.w.bring_in())
        head = self.w.head(self.w.fork, T)
        self.assertEqual((git(self.w.fork, "rev-parse", f"{head}^1"), git(self.w.fork, "rev-parse", f"{head}^2")), (before, d))

    def test_skip_actions_in_the_merge_commit_message(self):
        self.w.drawboard_commit("new.txt", "from drawboard\n")
        self.ok(self.w.bring_in())
        self.assertIn("[skip actions]", git(self.w.fork, "log", "-1", "--format=%B", T))

    def test_tags_pushed_are_drawboards_controller_tags_in_d_the_fork_lacks(self):
        git(self.w.dwork, "tag", "controller-v0.9.0", self.w.base)                       # in the fork's history, and a tag it lacks
        git(self.w.dwork, "tag", "controller-v1.0.0", self.w.base)                       # the fork has it, elsewhere
        git(self.w.fwork, "tag", "controller-v1.0.0", "HEAD")
        git(self.w.fwork, "push", "--quiet", "origin", "controller-v1.0.0")
        fork_v100 = self.w.head(self.w.fork, "controller-v1.0.0")
        d = self.w.drawboard_commit("new.txt", "from drawboard\n")
        git(self.w.dwork, "tag", "controller-v1.1.0", d)
        git(self.w.dwork, "tag", "-a", "-m", "annotated", "controller-v1.2.0", d)
        git(self.w.dwork, "tag", "not-a-controller-tag", d)
        git(self.w.dwork, "checkout", "--quiet", "-b", "side")                           # a controller tag outside D's history
        self.w.write(self.w.dwork, "side.txt", "s")
        side = self.w.commit(self.w.dwork, "side")
        git(self.w.dwork, "tag", "controller-v9.9.9", side)
        git(self.w.dwork, "push", "--quiet", "origin", "--tags")
        self.ok(self.w.bring_in())
        tags = set(git(self.w.fork, "tag", "--list").split())
        self.assertEqual(tags, {"controller-v0.9.0", "controller-v1.0.0", "controller-v1.1.0", "controller-v1.2.0"})
        self.assertEqual(git(self.w.fork, "rev-parse", "controller-v1.1.0^{commit}"), d)
        self.assertEqual(git(self.w.fork, "rev-parse", "controller-v1.2.0^{commit}"), d)
        self.assertEqual(self.w.head(self.w.fork, "controller-v1.0.0"), fork_v100, "a tag the fork already has is never moved")

    def test_contained_after_a_person_merge_still_pushes_the_controller_tag(self):
        self.w.drawboard_commit("new.txt", "from drawboard\n")
        self.ok(self.w.bring_in())
        d2 = self.w.drawboard_rewrite("other.txt", "after the rewrite\n")
        self.assertEqual(self.ok(self.w.bring_in()).outputs["outcome"], "rewritten-pr")
        git(self.w.dwork, "tag", "controller-v1.5.0", d2)
        git(self.w.dwork, "push", "--quiet", "origin", "controller-v1.5.0")
        git(self.w.fwork, "pull", "--quiet", "--ff-only", "origin", T)
        git(self.w.fwork, "pull", "--quiet", "--no-rebase", "--allow-unrelated-histories", "--no-edit", self.w.drawboard, T)
        git(self.w.fwork, "push", "--quiet", "origin", f"HEAD:refs/heads/{T}")
        r = self.ok(self.w.bring_in())
        self.assertEqual((r.outputs["outcome"], r.outputs["build"]), ("contained", "true"))
        self.assertEqual(git(self.w.fork, "rev-parse", "controller-v1.5.0^{commit}"), d2)

    def test_contained_after_drawboard_tags_the_merged_head_pushes_the_controller_tag(self):
        d = self.w.drawboard_commit("new.txt", "from drawboard\n")
        self.assertEqual(self.ok(self.w.bring_in()).outputs["outcome"], "merged")
        self.assertEqual(git(self.w.fork, "tag", "--list"), "")
        git(self.w.dwork, "tag", "controller-v1.6.0", d)
        git(self.w.dwork, "push", "--quiet", "origin", "controller-v1.6.0")
        before = self.w.head(self.w.fork, T)
        r = self.ok(self.w.bring_in("--force"))
        self.assertEqual((r.outputs["outcome"], r.outputs["build"], self.w.head(self.w.fork, T)), ("contained-forced", "true", before))
        self.assertEqual(git(self.w.fork, "rev-parse", "controller-v1.6.0^{commit}"), d)

    def test_a_contained_run_that_builds_nothing_pushes_no_tag(self):
        d = self.w.drawboard_commit("new.txt", "from drawboard\n")
        self.ok(self.w.bring_in())
        self.w.add_pr(CLOUD_REPO, CLOUD_PFX + d[:12], "open")
        git(self.w.dwork, "tag", "controller-v1.7.0", d)
        git(self.w.dwork, "push", "--quiet", "origin", "controller-v1.7.0")
        r = self.ok(self.w.bring_in())
        self.assertEqual(r.outputs["build"], "false")
        self.assertEqual(git(self.w.fork, "tag", "--list"), "")

    def test_fast_forward_only_when_t_moves_during_the_run_nothing_is_forced(self):
        d = self.w.drawboard_commit("new.txt", "from drawboard\n")
        git(self.w.dwork, "tag", "controller-v1.1.0", d)
        git(self.w.dwork, "push", "--quiet", "origin", "controller-v1.1.0")
        competitor = self.w.clone(self.w.fork, "competitor", branch=T)
        self.w.write(competitor, "other.txt", "someone else pushed\n")
        theirs = self.w.commit(competitor, "someone else's commit")
        # a post-merge hook in the sync's own clone (git copies it from the template directory) moves T after the merge and before the push,
        # so a plain push is not a fast-forward and a forced one would succeed
        tpl = os.path.join(self._tmp, "template", "hooks")
        os.makedirs(tpl)
        hook = os.path.join(tpl, "post-merge")
        with open(hook, "w") as f:
            f.write(f'#!/bin/sh\nunset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE\ngit -C "{competitor}" push --quiet "{self.w.fork}" HEAD:refs/heads/{T} 2>/dev/null\nexit 0\n')
        os.chmod(hook, os.stat(hook).st_mode | stat.S_IXUSR)
        r = self.w.bring_in(GIT_TEMPLATE_DIR=os.path.dirname(tpl))
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("refused", r.stderr)
        self.assertEqual(self.w.head(self.w.fork, T), theirs, "T keeps the commit that moved it; the sync did not force")
        self.assertEqual(git(self.w.fork, "tag", "--list"), "", "the push is atomic: no tag went without the branch")
        self.assertNotIn("build", r.outputs)

    # ---- D-6: the person PR
    def test_conflict_opens_a_person_pr_and_changes_nothing_on_t(self):
        self.w.fork_commit("a.txt", "one\nFORK\nthree\n")
        before = self.w.head(self.w.fork, T)
        d = self.w.drawboard_commit("a.txt", "one\nDRAWBOARD\nthree\n")
        r = self.ok(self.w.bring_in())
        self.assertEqual(self.w.head(self.w.fork, T), before)
        self.assertEqual(self.w.head(self.w.fork, f"refs/heads/{PERSON_PFX}{d[:12]}"), d)
        (pr,) = self.w.pulls(FORK_REPO)
        self.assertEqual((pr["head"]["ref"], pr["base"]["ref"]), (PERSON_PFX + d[:12], T))
        self.assertIn("conflict", pr["body"])
        self.assertIn("a.txt", pr["body"])
        self.assertEqual((r.outputs["outcome"], r.outputs["build"], r.outputs["bump_branch"]), ("conflict-pr", "false", ""))
        self.assertEqual(self.w.pulls(CLOUD_REPO), [])

    def test_rewritten_opens_a_person_pr_and_changes_nothing_on_t(self):
        before = self.w.head(self.w.fork, T)
        d = self.w.drawboard_rewrite("a.txt", "one\nrewritten\nthree\n")
        r = self.ok(self.w.bring_in())
        self.assertEqual(self.w.head(self.w.fork, T), before)
        self.assertEqual(self.w.head(self.w.fork, f"refs/heads/{PERSON_PFX}{d[:12]}"), d)
        (pr,) = self.w.pulls(FORK_REPO)
        self.assertIn("rewrote", pr["body"])
        self.assertEqual((r.outputs["outcome"], r.outputs["build"]), ("rewritten-pr", "false"))

    def test_rewritten_when_there_is_no_trailer_and_the_constant_is_not_in_the_history(self):
        git(self.w.fwork, "reset", "--quiet", "--hard", "HEAD~1")                       # drops the trailer commit
        git(self.w.fwork, "push", "--quiet", "--force", "origin", f"HEAD:refs/heads/{T}")
        self.w.drawboard_commit("new.txt", "from drawboard\n")
        r = self.ok(self.w.bring_in())
        self.assertEqual(r.outputs["outcome"], "rewritten-pr")

    # ---- D-3 / D-6: one PR per Drawboard head
    def _already_handled(self, state):
        d = self.w.drawboard_commit("new.txt", "from drawboard\n")
        self.w.add_pr(CLOUD_REPO, CLOUD_PFX + d[:12], state)
        r = self.ok(self.w.bring_in())
        self.assertEqual((r.outputs["build"], r.outputs["bump_branch"], r.outputs["commit"]), ("false", "", self.w.head(self.w.fork, T)))
        self.assertTrue(r.outputs["outcome"].endswith("already-handled"))
        self.assertEqual(len(self.w.pulls(CLOUD_REPO)), 1)

    def test_already_handled_when_a_cloud_pr_is_open(self):
        self._already_handled("open")

    def test_already_handled_when_a_cloud_pr_was_closed(self):
        self._already_handled("closed")

    def test_already_handled_a_person_pr_open_or_closed_opens_nothing_more(self):
        self.w.fork_commit("a.txt", "one\nFORK\nthree\n")
        self.w.drawboard_commit("a.txt", "one\nDRAWBOARD\nthree\n")
        self.ok(self.w.bring_in())
        self.assertEqual(len(self.w.pulls(FORK_REPO)), 1)
        for state in ("open", "closed"):
            st = self.w.gh_state()
            st["pulls"][FORK_REPO][0]["state"] = state
            self.w.gh_state(st)
            r = self.ok(self.w.bring_in())
            self.assertEqual(len(self.w.pulls(FORK_REPO)), 1, state)
            self.assertEqual((r.outputs["outcome"], r.outputs["build"]), ("conflict-pr-exists", "false"))

    def test_force_builds_although_a_pr_exists_and_names_the_branch_with_the_run(self):
        d = self.w.drawboard_commit("new.txt", "from drawboard\n")
        self.w.add_pr(CLOUD_REPO, CLOUD_PFX + d[:12], "closed")
        r = self.ok(self.w.bring_in("--force"))
        self.assertEqual((r.outputs["build"], r.outputs["bump_branch"]), ("true", f"{CLOUD_PFX}{d[:12]}-run4242"))
        r = self.ok(self.w.bring_in(SYNC_FORCE="true", GITHUB_RUN_ID="77"))
        self.assertEqual(r.outputs["bump_branch"], f"{CLOUD_PFX}{d[:12]}-run77")

    def test_force_does_not_override_the_person_pr(self):
        self.w.fork_commit("a.txt", "one\nFORK\nthree\n")
        self.w.drawboard_commit("a.txt", "one\nDRAWBOARD\nthree\n")
        self.ok(self.w.bring_in())
        r = self.ok(self.w.bring_in("--force"))
        self.assertEqual((r.outputs["build"], len(self.w.pulls(FORK_REPO))), ("false", 1))

    def test_the_token_reaches_gh_and_the_canary_is_not_reached(self):
        self.ok(self.w.bring_in())
        calls = self.w.gh_state()["calls"]
        self.assertTrue(calls and all(c["token_set"] for c in calls))
        self.assertEqual(canary_calls(), "")


if __name__ == "__main__":
    import unittest
    unittest.main()
