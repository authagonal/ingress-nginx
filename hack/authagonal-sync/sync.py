#!/usr/bin/env python3
"""The Drawboard sync's logic, called by .github/workflows/authagonal-sync.yml. Python 3 standard library only; it runs git and gh as child processes.

  sync.py bring-in [--branch T] [--force] [--run-id N]
      Merges Drawboard's drawboard/main (D) into the run's branch (T) and pushes it, or opens the person PR when it cannot. Prints and writes
      (to $GITHUB_OUTPUT, or --github-output) build, commit, drawboard, bump_branch and outcome.
  sync.py bump --controller REF [--certgen REF] [--base REF] --commit SHA --drawboard D --branch NAME [--run-url URL]
      Opens the image bump PR into authagonal-cloud's develop, as one commit on NAME. Writes pr_url.
  sync.py edit-main-tf --file F --controller REF [--certgen REF]
      Rewrites the value of the ingress_nginx_controller_ref line (and ingress_nginx_certgen_ref when given) of F, found by local name.

Environment (every one has a default for the real repositories; the tests point them at local bare repositories):
  GH_TOKEN             the App token that pushes to the fork and calls the API (bring-in) or writes to authagonal-cloud (bump)
  SYNC_CLOUD_TOKEN     bring-in only: the token for authagonal-cloud's API reads, when GH_TOKEN does not cover it (default GH_TOKEN)
  SYNC_FORK_REPO       owner/name of the fork (default $GITHUB_REPOSITORY, else authagonal/ingress-nginx)
  SYNC_FORK_URL        where git fetches and pushes the fork (default https://github.com/$SYNC_FORK_REPO.git)
  SYNC_DRAWBOARD_URL   default https://github.com/DrawboardLtd/ingress-nginx.git, fetched anonymously
  SYNC_DRAWBOARD_BRANCH  default drawboard/main
  SYNC_CLOUD_REPO      default authagonal/authagonal-cloud;  SYNC_CLOUD_URL  default https://github.com/$SYNC_CLOUD_REPO.git
  SYNC_CLOUD_BASE      default develop
  SYNC_APP_SLUG        default authagonal-ingress-nginx-sync (the App that authors commits); SYNC_GIT_NAME and SYNC_GIT_EMAIL replace the lookup
  SYNC_FORCE           true or 1 is --force;  GITHUB_RUN_ID the run id;  GITHUB_REF_NAME the branch;  GITHUB_SERVER_URL, GITHUB_REPOSITORY the run URL
  SYNC_CONTROLLER, SYNC_CERTGEN, SYNC_BASE, SYNC_COMMIT, SYNC_DRAWBOARD, SYNC_BUMP_BRANCH, SYNC_RUN_URL   defaults of bump's arguments
  SYNC_TF_WAIT_SECONDS (default 1800) and SYNC_TF_POLL_SECONDS (default 30): bump's wait while a terraform.yml run on develop is in progress
The API base is gh's own (GH_HOST); this program never speaks HTTP itself."""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse

# The Drawboard head this fork was built from: the last brought-in head when T's first-parent history has no Drawboard-Head trailer yet.
LAST_BROUGHT_IN = "8af499c7d8c1324a7e6895da0f5fc16d2c9643e6"
TRAILER = "Drawboard-Head"
CLOUD_BRANCH_PREFIX = "authagonal-ingress-nginx/drawboard-"
PERSON_BRANCH_PREFIX = "authagonal-sync/drawboard-"
DRAWBOARD_WEB = "https://github.com/DrawboardLtd/ingress-nginx"
CERT_ISSUER = "https://token.actions.githubusercontent.com"
CERT_IDENTITY_REGEXP = r"^https://github\.com/authagonal/ingress-nginx/\.github/workflows/authagonal-images\.yml@refs/"
REF_RE = re.compile(r"^([^/]+)/([^:]+):([^@]+)@(sha256:[0-9a-f]{64})$")  # the form main.tf's regex() takes apart
MAX_COMMIT_LINKS = 600  # a PR body holds 65536 characters; a link line is about 100


class SyncError(Exception):
    pass


def log(msg):
    print(msg, flush=True)


# ------------------------------------------------------------------------------------------------ configuration and child processes
class Cfg:
    def __init__(self):
        e = os.environ
        self.fork_repo = e.get("SYNC_FORK_REPO") or e.get("GITHUB_REPOSITORY") or "authagonal/ingress-nginx"
        self.fork_url = e.get("SYNC_FORK_URL") or f"https://github.com/{self.fork_repo}.git"
        self.drawboard_url = e.get("SYNC_DRAWBOARD_URL") or "https://github.com/DrawboardLtd/ingress-nginx.git"
        self.drawboard_branch = e.get("SYNC_DRAWBOARD_BRANCH") or "drawboard/main"
        self.cloud_repo = e.get("SYNC_CLOUD_REPO") or "authagonal/authagonal-cloud"
        self.cloud_url = e.get("SYNC_CLOUD_URL") or f"https://github.com/{self.cloud_repo}.git"
        self.cloud_base = e.get("SYNC_CLOUD_BASE") or "develop"
        self.token = e.get("GH_TOKEN") or None
        self.cloud_token = e.get("SYNC_CLOUD_TOKEN") or self.token
        self.app_slug = e.get("SYNC_APP_SLUG") or "authagonal-ingress-nginx-sync"


def child_env(token=None, extra=None):
    """The environment of a git or gh child. The token reaches git as a per-URL http extraheader through GIT_CONFIG_*, never on a command line."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if token:
        env["GH_TOKEN"] = token
        n = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
        cred = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env[f"GIT_CONFIG_KEY_{n}"] = "http.https://github.com/.extraheader"
        env[f"GIT_CONFIG_VALUE_{n}"] = f"AUTHORIZATION: basic {cred}"
        env["GIT_CONFIG_COUNT"] = str(n + 1)
    env.update(extra or {})
    return env


def run(cmd, cwd=None, token=None, extra_env=None, input=None, check=True):
    r = subprocess.run(cmd, cwd=cwd, env=child_env(token, extra_env), input=input, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise SyncError(f"{' '.join(cmd[:4])} failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()[-600:]}")
    return r


def git(args, cwd=None, token=None, extra_env=None, check=True):
    return run(["git", *args], cwd=cwd, token=token, extra_env=extra_env, check=check)


def gh_api(path, method=None, payload=None, token=None):
    cmd = ["gh", "api"]
    if method:
        cmd += ["-X", method]
    cmd.append(path)
    if payload is not None:
        cmd += ["--input", "-"]
    r = run(cmd, token=token, input=json.dumps(payload) if payload is not None else None)
    return json.loads(r.stdout) if r.stdout.strip() else None


def pr_for_head(repo, branch, token):
    """The PRs of `repo`, in any state, whose head branch is exactly `branch` (a branch of the repo itself)."""
    q = urllib.parse.quote(f"{repo.split('/')[0]}:{branch}", safe=":/")
    prs = gh_api(f"repos/{repo}/pulls?head={q}&state=all&per_page=100", token=token) or []
    return [p for p in prs if (p.get("head") or {}).get("ref") == branch]


def app_identity(cfg):
    """(name, email) the App's commits carry, so GitHub attributes them to `<slug>[bot]`."""
    name, email = os.environ.get("SYNC_GIT_NAME"), os.environ.get("SYNC_GIT_EMAIL")
    if name and email:
        return name, email
    login = f"{cfg.app_slug}[bot]"
    try:
        uid = gh_api(f"users/{login}", token=cfg.token)["id"]
        return login, f"{uid}+{login}@users.noreply.github.com"
    except (SyncError, KeyError, TypeError, ValueError) as e:
        log(f"warning: could not read the id of {login} ({e}); committing without it")
        return login, f"{login}@users.noreply.github.com"


def identity_env(cfg):
    n, e = app_identity(cfg)
    return {"GIT_AUTHOR_NAME": n, "GIT_AUTHOR_EMAIL": e, "GIT_COMMITTER_NAME": n, "GIT_COMMITTER_EMAIL": e}


class Outputs:
    def __init__(self, path):
        self.path, self.items = path, {}

    def set(self, k, v):
        self.items[k] = str(v)

    def flush(self):
        for k, v in self.items.items():
            log(f"output {k}={v}")
        if self.path:
            with open(self.path, "a") as f:
                for k, v in self.items.items():
                    f.write(f"{k}={v}\n")


def is_ancestor(repo_dir, a, b):
    return git(["merge-base", "--is-ancestor", a, b], cwd=repo_dir, check=False).returncode == 0


def has_commit(repo_dir, sha):
    return git(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo_dir, check=False).returncode == 0


# ------------------------------------------------------------------------------------------------ bring-in (D-2, D-3, D-6)
def last_brought_in(clone, head):
    """The newest Drawboard-Head trailer on T's first-parent history, else the constant."""
    r = git(["log", "--first-parent", "-z", f"--format=%(trailers:key={TRAILER},valueonly,separator=%x20)", head], cwd=clone)
    for rec in r.stdout.split("\0"):
        for tok in rec.split():
            if re.fullmatch(r"[0-9a-f]{40}", tok):
                return tok
    return LAST_BROUGHT_IN


def resolved_head(clone, head, D, last):
    """`last` is not in D's history. When a person has since merged a rewritten D into T, that merge has no trailer, so `last` is stale: the newest
    Drawboard commit T and D share (their merge base) is then the head last brought in. It is that only when it is newer than `last`; a merge base
    that is `last` or one of its ancestors is the common history a real rewrite keeps. Returns that commit, or None for a rewrite (and for a `last` T does not hold, which it cannot compare)."""
    mb = git(["merge-base", head, D], cwd=clone, check=False)
    base = mb.stdout.strip() if mb.returncode == 0 else ""
    if not base or not has_commit(clone, last) or is_ancestor(clone, base, last):
        return None  # nothing to compare the base with, or it is no newer than `last`
    return base


def person_pr(cfg, clone, T, D, last, kind, detail, out):
    """D-6: D goes to the fork as authagonal-sync/drawboard-<D12>, and a PR from it into T says what a person does. One per D, in any state."""
    d12 = D[:12]
    branch = PERSON_BRANCH_PREFIX + d12
    out.set("build", "false")
    out.set("bump_branch", "")
    existing = pr_for_head(cfg.fork_repo, branch, cfg.token)
    if existing:
        log(f"{kind}: PR #{existing[0].get('number')} for {d12} already exists ({existing[0].get('state')}): nothing more")
        out.set("outcome", f"{kind}-pr-exists")
        return
    git(["push", cfg.fork_url, f"{D}:refs/heads/{branch}"], cwd=clone, token=cfg.token)
    if kind == "conflict":
        why = (f"Merging Drawboard's `{cfg.drawboard_branch}` ({D}) into `{T}` conflicts in:\n\n"
               + "\n".join(f"- `{p}`" for p in detail) + "\n")
    else:
        why = (f"Drawboard rewrote `{cfg.drawboard_branch}`: its history ({D}) no longer contains `{last}`, the Drawboard head this "
               f"branch last brought in. A merge would bring in the old and the new history together.\n")
    body = (f"The Drawboard sync could not bring Drawboard in by itself ({kind}).\n\n{why}\n"
            f"What to do: resolve it and merge this PR into `{T}` with a merge commit, so Drawboard's commits stay in the branch's history. "
            f"The next scheduled run then finds Drawboard contained, builds, and opens the image bump PR. "
            f"Until then the sync changes nothing on `{T}`, and this is the only PR it opens for `{d12}`: closing it does not make it open another.\n")
    pr = gh_api(f"repos/{cfg.fork_repo}/pulls", "POST", {"title": f"Drawboard {d12}: {kind}, needs a person", "head": branch, "base": T, "body": body},
                token=cfg.token)
    log(f"{kind}: opened {(pr or {}).get('html_url')}")
    out.set("outcome", f"{kind}-pr")


def bring_in(a):
    cfg = Cfg()
    T = a.branch or os.environ.get("GITHUB_REF_NAME")
    if not T:
        raise SyncError("no branch: pass --branch or set GITHUB_REF_NAME")
    force = a.force or os.environ.get("SYNC_FORCE", "").lower() in ("true", "1")
    run_id = a.run_id or os.environ.get("GITHUB_RUN_ID") or "0"
    out = Outputs(a.github_output)
    with tempfile.TemporaryDirectory(prefix="authagonal-sync-") as tmp:
        clone = os.path.join(tmp, "clone")
        git(["clone", "--quiet", "--no-tags", "--single-branch", "--branch", T, cfg.fork_url, clone], token=cfg.token)
        git(["fetch", "--quiet", "--no-tags", cfg.drawboard_url, f"+refs/heads/{cfg.drawboard_branch}:refs/remotes/drawboard/head"], cwd=clone)
        D = git(["rev-parse", "refs/remotes/drawboard/head"], cwd=clone).stdout.strip()
        head = git(["rev-parse", "HEAD"], cwd=clone).stdout.strip()
        d12 = D[:12]
        out.set("drawboard", D)
        log(f"T={T} at {head[:12]}, Drawboard {cfg.drawboard_branch} at {d12}")

        if is_ancestor(clone, D, head):
            log("Drawboard's head is already in T's history: nothing to bring in")
            outcome = "contained"
        else:
            last = last_brought_in(clone, head)
            if not has_commit(clone, last) or not is_ancestor(clone, last, D):
                resolved = resolved_head(clone, head, D, last)
                if resolved:
                    log(f"Drawboard's history does not contain the trailer's head {last[:12]}, but T holds {resolved[:12]} of it: a rewrite a person resolved")
                    last = resolved
            if not has_commit(clone, last) or not is_ancestor(clone, last, D):
                log(f"Drawboard's history does not contain the last brought-in head {last[:12]}: rewritten")
                person_pr(cfg, clone, T, D, last, "rewritten", None, out)
                out.set("commit", head)
                return out.flush()
            ident = identity_env(cfg)
            msg = f"Merge Drawboard's ingress-nginx {d12} into {T} [skip actions]\n\n{TRAILER}: {D}\n"
            m = git(["merge", "--no-ff", "--quiet", "-m", msg, D], cwd=clone, extra_env=ident, check=False)
            if m.returncode != 0:
                conflicted = sorted(set(git(["diff", "--name-only", "--diff-filter=U"], cwd=clone).stdout.split()))
                git(["merge", "--abort"], cwd=clone, check=False)
                if not conflicted:
                    raise SyncError(f"git merge failed without a conflict: {(m.stderr or m.stdout).strip()[-400:]}")
                log(f"merging D into T conflicts in {conflicted}")
                person_pr(cfg, clone, T, D, last, "conflict", conflicted, out)
                out.set("commit", head)
                return out.flush()
            # Drawboard's controller-v* tags in D's history that the fork lacks: the controller image is named after the newest git describe finds
            git(["fetch", "--quiet", "--no-tags", cfg.drawboard_url, "+refs/tags/controller-v*:refs/tags/controller-v*"], cwd=clone)
            have = {ln.split("refs/tags/", 1)[1] for ln in git(["ls-remote", "--tags", "--refs", cfg.fork_url, "controller-v*"], cwd=clone,
                                                                    token=cfg.token).stdout.splitlines() if "refs/tags/" in ln}
            tags = [t for t in git(["tag", "--list", "controller-v*"], cwd=clone).stdout.split()
                    if t not in have and is_ancestor(clone, f"{t}^{{commit}}", D)]
            new_head = git(["rev-parse", "HEAD"], cwd=clone).stdout.strip()
            # a fast-forward of T, never a force: the push is refused when T moved meanwhile, and the next run starts again
            push = git(["push", "--atomic", cfg.fork_url, f"HEAD:refs/heads/{T}", *[f"refs/tags/{t}:refs/tags/{t}" for t in tags]],
                       cwd=clone, token=cfg.token, check=False)
            if push.returncode != 0:
                raise SyncError(f"push of {new_head[:12]} to {T} refused (T moved meanwhile?); nothing was forced: "
                                f"{(push.stderr or push.stdout).strip()[-400:]}")
            log(f"merged D into T: {head[:12]} -> {new_head[:12]}; pushed with tags {tags}")
            head, outcome = new_head, "merged"

        out.set("commit", head)
        branch = CLOUD_BRANCH_PREFIX + d12
        if force:
            out.set("build", "true")
            out.set("bump_branch", f"{branch}-run{run_id}")
            out.set("outcome", f"{outcome}-forced")
            log(f"force: building {head[:12]} for a PR on {branch}-run{run_id}")
        elif pr_for_head(cfg.cloud_repo, branch, cfg.cloud_token):
            out.set("build", "false")
            out.set("bump_branch", "")
            out.set("outcome", f"{outcome}-already-handled")
            log(f"{cfg.cloud_repo} already has a PR (any state) for {branch}: nothing more")
        else:
            out.set("build", "true")
            out.set("bump_branch", branch)
            out.set("outcome", outcome)
            log(f"no PR for {branch} yet: build {head[:12]}")
        out.flush()


# ------------------------------------------------------------------------------------------------ edit-main-tf
def _line_re(name):
    return re.compile(r'^(\s*' + re.escape(name) + r'\s*=\s*)"([^"]*)"(.*)$', re.M)


def edit_main_tf_text(text, controller, certgen=None):
    """text with the values of the named lines replaced; nothing else changes. Returns (new text, [(name, old, new)])."""
    changes = []
    for name, ref in (("ingress_nginx_controller_ref", controller), ("ingress_nginx_certgen_ref", certgen)):
        if ref is None:
            continue
        if not REF_RE.match(ref):
            raise SyncError(f"{ref!r} is not <registry>/<repository>:<tag>@sha256:<64 hex>")
        found = _line_re(name).findall(text)
        if len(found) != 1:
            raise SyncError(f"expected exactly one line assigning {name}, found {len(found)}")
        text = _line_re(name).sub(lambda m: f'{m.group(1)}"{ref}"{m.group(3)}', text, count=1)
        changes.append((name, found[0][1], ref))
    return text, changes


def edit_main_tf(a):
    with open(a.file, newline="") as f:
        text = f.read()
    new, changes = edit_main_tf_text(text, a.controller, a.certgen)
    if new != text:
        with open(a.file, "w", newline="") as f:
            f.write(new)
    for name, old, ref in changes:
        log(f"{name}: {'unchanged' if old == ref else 'changed'} ({old} -> {ref})")


# ------------------------------------------------------------------------------------------------ bump (D-5)
def wait_for_terraform(cfg):
    """Waits while a terraform.yml run on develop is in progress (the shared dev state lock), then goes on either way."""
    limit = float(os.environ.get("SYNC_TF_WAIT_SECONDS", "1800"))
    poll = float(os.environ.get("SYNC_TF_POLL_SECONDS", "30"))
    t0 = time.monotonic()
    while True:
        try:
            runs = (gh_api(f"repos/{cfg.cloud_repo}/actions/workflows/terraform.yml/runs?branch={cfg.cloud_base}&per_page=30",
                           token=cfg.token) or {}).get("workflow_runs", [])
            busy = [str(r.get("id")) for r in runs if r.get("status") != "completed"]
            why = f"terraform.yml runs in progress: {', '.join(busy)}"
        except SyncError as e:
            busy, why = ["unreadable"], f"terraform.yml runs unreadable ({e})"
        if not busy:
            log(f"no terraform.yml run in progress on {cfg.cloud_base} after {time.monotonic() - t0:.0f}s")
            return
        if time.monotonic() - t0 + poll > limit:
            log(f"{why} after {time.monotonic() - t0:.0f}s: opening the PR anyway")
            return
        log(f"{why}: waiting")
        time.sleep(poll)


def ref_repo_and_digest(ref):
    name, digest = ref.split("@", 1)
    return name.rsplit(":", 1)[0], digest


def drawboard_commits(cfg, pinned_controller, D):
    """(shas, note): the commits in D's history that the commit develop's pinned controller was built from lacks. shas is None when unknown."""
    m = re.search(r":v[^-@:]*-([0-9a-f]{12})$", pinned_controller.split("@")[0])
    if not m:
        return None, f"develop's pinned controller `{pinned_controller}` carries no commit suffix"
    suffix = m.group(1)
    with tempfile.TemporaryDirectory(prefix="authagonal-sync-") as tmp:
        try:
            git(["init", "--quiet", "--bare", tmp])
            git(["fetch", "--quiet", "--no-tags", cfg.fork_url, "+refs/heads/*:refs/remotes/fork/*"], cwd=tmp)
            git(["fetch", "--quiet", "--no-tags", cfg.drawboard_url, f"+refs/heads/{cfg.drawboard_branch}:refs/remotes/drawboard/head"], cwd=tmp)
        except SyncError as e:
            return None, f"fetching the histories failed: {e}"
        r = git(["rev-parse", "--verify", "-q", f"{suffix}^{{commit}}"], cwd=tmp, check=False)
        if r.returncode != 0:
            return None, f"the commit `{suffix}` develop's pinned controller was built from is not in the fork's branches"
        pinned = r.stdout.strip()
        if not has_commit(tmp, D):
            raise SyncError(f"Drawboard commit {D} is not in the fetched history")
        revs = git(["log", "--format=%H %s", f"{pinned}..{D}"], cwd=tmp).stdout.splitlines()
        return revs, pinned


def commits_section(cfg, pinned_controller, D):
    revs, info = drawboard_commits(cfg, pinned_controller, D)
    if revs is None:
        log(f"warning: could not work out the Drawboard commits: {info}")
        return f"Could not work out which Drawboard commits this brings in: {info}."
    if not revs:
        return (f"None: develop's pinned controller was built from `{info[:12]}`, which already has every commit in Drawboard's `{D[:12]}`. "
                f"This is an image rebuild only.")
    lines = [f"- {DRAWBOARD_WEB}/commit/{r.split(' ', 1)[0]} {r.split(' ', 1)[1] if ' ' in r else ''}".rstrip() for r in revs[:MAX_COMMIT_LINKS]]
    more = len(revs) - MAX_COMMIT_LINKS
    if more > 0:
        lines.append(f"- and {more} older ones: {DRAWBOARD_WEB}/compare/{info}...{D}")
    return (f"{len(revs)} commits in Drawboard's `{D[:12]}` that the commit develop's pinned controller was built from (`{info[:12]}`) lacks:\n\n"
            + "\n".join(lines))


def bump_body(cfg, a, certgen_changed, commits_md):
    refs = [("controller", a.controller)]
    if a.certgen and certgen_changed:
        refs.append(("kube-webhook-certgen", a.certgen))
    shown = list(refs)
    if a.base:
        shown.append(("nginx base (not pinned in main.tf)", a.base))
    new = "\n".join(f"- {n}: `{r}`" for n, r in shown)
    cosign = []
    for n, r in shown:
        repo, digest = ref_repo_and_digest(r)
        cosign.append(f"# {n}\ncosign verify {repo}@{digest} \\\n  --certificate-oidc-issuer {CERT_ISSUER} \\\n"
                      f"  --certificate-identity-regexp '{CERT_IDENTITY_REGEXP}'")
    certgen_note = "" if certgen_changed or not a.certgen else "\nThe kube-webhook-certgen image this run used is the one develop already pins, so that line is unchanged.\n"
    return (f"Bumps `infra/platform/main.tf`'s ingress-nginx image refs to what the Drawboard sync built from "
            f"authagonal/ingress-nginx@{a.commit} (Drawboard `{a.drawboard[:12]}`).\n\n"
            f"## New refs\n\n{new}\n{certgen_note}\n"
            f"## Verify the signatures\n\n```\n" + "\n\n".join(cosign) + "\n```\n\n"
            f"## Run\n\n{a.run_url}\n\n"
            f"## Drawboard commits this brings in\n\n{commits_md}\n")


def bump(a):
    cfg = Cfg()
    for k in ("controller", "commit", "drawboard", "branch"):
        if not getattr(a, k):
            raise SyncError(f"--{k} is required")
    if not REF_RE.match(a.controller):
        raise SyncError(f"the controller ref {a.controller!r} is not <repository>:<tag>@sha256:<digest> (did the build publish?)")
    if a.certgen and not REF_RE.match(a.certgen):
        raise SyncError(f"the certgen ref {a.certgen!r} is not <repository>:<tag>@sha256:<digest>")
    if not a.run_url:
        e = os.environ
        a.run_url = f"{e.get('GITHUB_SERVER_URL', 'https://github.com')}/{e.get('GITHUB_REPOSITORY', cfg.fork_repo)}/actions/runs/{e.get('GITHUB_RUN_ID', '0')}"
    out = Outputs(a.github_output)
    wait_for_terraform(cfg)
    with tempfile.TemporaryDirectory(prefix="authagonal-sync-") as tmp:
        work = os.path.join(tmp, "cloud")
        git(["clone", "--quiet", "--depth", "1", "--branch", cfg.cloud_base, cfg.cloud_url, work], token=cfg.token)
        base_sha = git(["rev-parse", "HEAD"], cwd=work).stdout.strip()
        path = os.path.join(work, "infra", "platform", "main.tf")
        with open(path, newline="") as f:
            text = f.read()
        pinned = {n: m.group(2) for n in ("ingress_nginx_controller_ref", "ingress_nginx_certgen_ref") if (m := _line_re(n).search(text))}
        certgen_changed = bool(a.certgen) and pinned.get("ingress_nginx_certgen_ref") != a.certgen
        new, changes = edit_main_tf_text(text, a.controller, a.certgen if certgen_changed else None)
        if new == text:
            log(f"{cfg.cloud_base} at {base_sha[:12]} already pins these refs: no PR")
            out.set("pr_url", "")
            return out.flush()
        with open(path, "w", newline="") as f:
            f.write(new)
        commits_md = commits_section(cfg, pinned["ingress_nginx_controller_ref"], a.drawboard)
        ctag = a.controller.split("@")[0].rsplit(":", 1)[1]
        msg = (f"Bump ingress-nginx to {ctag}\n\n" + "\n".join(f"{n}: {r}" for n, _, r in changes)
               + f"\n\nBuilt by {a.run_url} from authagonal/ingress-nginx@{a.commit}\n")
        ident = identity_env(cfg)
        git(["switch", "--quiet", "-c", a.branch], cwd=work)
        git(["add", "infra/platform/main.tf"], cwd=work)
        git(["commit", "--quiet", "-m", msg], cwd=work, extra_env=ident)
        # the branch is the sync's own: a branch left by a run that failed before its PR is replaced, never develop
        git(["push", "--force", cfg.cloud_url, f"HEAD:refs/heads/{a.branch}"], cwd=work, token=cfg.token)
        pr = gh_api(f"repos/{cfg.cloud_repo}/pulls", "POST",
                    {"title": f"Bump ingress-nginx to {ctag}", "head": a.branch, "base": cfg.cloud_base,
                     "body": bump_body(cfg, a, certgen_changed, commits_md)}, token=cfg.token)
        url = (pr or {}).get("html_url", "")
        log(f"opened {url}")
        out.set("pr_url", url)
        out.flush()


# ------------------------------------------------------------------------------------------------ command line
def main(argv=None):
    env = os.environ.get
    p = argparse.ArgumentParser(prog="sync.py", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bring-in", help="merge Drawboard's head into the run's branch, or open the person PR")
    b.add_argument("--branch", help="T, the branch to bring Drawboard into (default $GITHUB_REF_NAME)")
    b.add_argument("--force", action="store_true", help="build and open a PR even when one exists for this Drawboard head")
    b.add_argument("--run-id", help="names the forced PR's branch (default $GITHUB_RUN_ID)")
    b.add_argument("--github-output", default=env("GITHUB_OUTPUT"))
    b.set_defaults(fn=bring_in)
    u = sub.add_parser("bump", help="open the image bump PR into authagonal-cloud's develop")
    u.add_argument("--controller", default=env("SYNC_CONTROLLER"), help="<repository>:<tag>@sha256:<digest> of the controller image")
    u.add_argument("--certgen", default=env("SYNC_CERTGEN"), help="the same for kube-webhook-certgen (optional)")
    u.add_argument("--base", default=env("SYNC_BASE"), help="the same for the nginx base (shown in the PR, not pinned)")
    u.add_argument("--commit", default=env("SYNC_COMMIT"), help="the commit that was built")
    u.add_argument("--drawboard", default=env("SYNC_DRAWBOARD"), help="Drawboard's head that bring-in used")
    u.add_argument("--branch", default=env("SYNC_BUMP_BRANCH"), help="the PR's branch, bring-in's bump_branch")
    u.add_argument("--run-url", default=env("SYNC_RUN_URL"))
    u.add_argument("--github-output", default=env("GITHUB_OUTPUT"))
    u.set_defaults(fn=bump)
    e = sub.add_parser("edit-main-tf", help="rewrite the ingress-nginx ref lines of a main.tf, found by local name")
    e.add_argument("--file", required=True)
    e.add_argument("--controller", required=True)
    e.add_argument("--certgen")
    e.set_defaults(fn=edit_main_tf)
    a = p.parse_args(argv)
    try:
        a.fn(a)
    except SyncError as ex:
        print(f"sync.py {a.cmd}: error: {ex}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
