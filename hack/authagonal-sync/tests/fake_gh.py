#!/usr/bin/env python3
"""A stand-in gh for the suite: `gh api` only, answered from the JSON state file named by FAKE_GH_STATE; every call is appended to its "calls"."""
import json
import os
import sys
import urllib.parse

path = os.environ["FAKE_GH_STATE"]
with open(path) as f:
    st = json.load(f)
args = sys.argv[1:]


def finish(code, out=None, err=None):
    with open(path, "w") as f:
        json.dump(st, f)
    if out is not None:
        print(json.dumps(out))
    if err:
        print(f"fake gh: {err}", file=sys.stderr)
    sys.exit(code)


rec = {"args": args, "token_set": bool(os.environ.get("GH_TOKEN"))}
st.setdefault("calls", []).append(rec)
if args[:1] != ["api"]:
    finish(2, err=f"only `gh api` is stood in for, got {args}")
method, target, has_input, i = "GET", None, False, 1
while i < len(args):
    if args[i] == "-X":
        method, i = args[i + 1], i + 2
    elif args[i] == "--input":
        has_input, i = True, i + 2
    else:
        target, i = args[i], i + 1
payload = json.load(sys.stdin) if has_input else None
if has_input and method == "GET":
    method = "POST"
rec["method"], rec["target"], rec["payload"] = method, target, payload
url = urllib.parse.urlsplit(target)
query = dict(urllib.parse.parse_qsl(url.query))
parts = url.path.strip("/").split("/")

if parts[0] == "users" and method == "GET":
    finish(0, {"id": st.get("bot_id", 4242), "login": parts[1]})
if parts[0] == "repos" and len(parts) >= 4 and parts[3] == "pulls":
    repo = "/".join(parts[1:3])
    pulls = st.setdefault("pulls", {}).setdefault(repo, [])
    if method == "GET":
        want = query.get("head", "").split(":", 1)[-1]
        finish(0, [p for p in pulls if p["head"]["ref"] == want])
    if method == "POST":
        if len(payload.get("body") or "") > 65536:  # GitHub's limit on a PR body: a 422
            finish(1, err="HTTP 422: Validation Failed: body is too long (maximum is 65536 characters)")
        n = 100 + sum(len(v) for v in st["pulls"].values())
        pr = {"number": n, "state": "open", "title": payload["title"], "body": payload["body"], "head": {"ref": payload["head"]},
              "base": {"ref": payload["base"]}, "html_url": f"https://github.com/{repo}/pull/{n}"}
        pulls.append(pr)
        finish(0, pr)
if parts[0] == "repos" and "terraform.yml" in parts and parts[-1] == "runs":
    busy = st.get("terraform_busy_polls", 0)
    if busy > 0 or st.get("terraform_busy_forever"):
        st["terraform_busy_polls"] = busy - 1
        finish(0, {"workflow_runs": [{"id": 9, "status": "in_progress"}, {"id": 8, "status": "completed"}]})
    finish(0, {"workflow_runs": [{"id": 8, "status": "completed"}]})
finish(1, err=f"no answer for {method} {target}")
