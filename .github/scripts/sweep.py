#!/usr/bin/env python3
"""Weekly sweep of every public repo of the owner: is CI green, and do the hygiene rules still hold?
Reads public data only (GITHUB_TOKEN is used just to get a higher rate limit). Writes a Markdown report to the job summary
and exits 1 when anything needs attention, so the scheduled run shows red. Usage: sweep.py [owner] (env: GH_TOKEN, ONLY=repo1,repo2)."""
import os, re, sys, json, urllib.request, urllib.error, concurrent.futures as cf

OWNER = sys.argv[1] if len(sys.argv) > 1 else "SpeakingColors"
TOKEN = os.environ.get("GH_TOKEN", ""); ONLY = [x for x in os.environ.get("ONLY", "").split(",") if x]

def get(url, raw=False):
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "weekly-sweep"})
    if TOKEN and "api.github.com" in url: req.add_header("Authorization", "Bearer " + TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", "replace"); return body if raw else json.loads(body)
    except urllib.error.HTTPError as e:
        if e.code == 404: return None
        raise

def repos():
    if ONLY:                                                                   # direct lookup (used for local tests)
        got = []
        for n in ONLY:
            try:
                x = get(f"https://api.github.com/repos/{OWNER}/{n}")
                if x: got.append(x)
            except urllib.error.HTTPError as e: print(f"note: {n} could not be read ({e.code}), renamed or moved?", file=sys.stderr)
        return got
    out, page = [], 1
    while True:
        part = get(f"https://api.github.com/users/{OWNER}/repos?per_page=100&type=owner&page={page}") or []
        out += part; page += 1
        if len(part) < 100: break
    return [r for r in out if not r["fork"] and not r["archived"] and (not ONLY or r["name"] in ONLY)]

SHA = re.compile(r"@[0-9a-f]{40}(\s|$)")
def check(r):
    name, br, issues = r["name"], r["default_branch"], []
    # settings that keep strangers out
    for k in ("has_issues", "has_wiki", "has_projects", "has_discussions"):
        if r.get(k): issues.append(f"{k[4:]} is on")
    if r.get("pull_request_creation_policy") not in (None, "collaborators_only"): issues.append("pull requests are open to everyone")
    # CI: the newest finished run of every workflow on the default branch
    runs = (get(f"https://api.github.com/repos/{OWNER}/{name}/actions/runs?branch={br}&status=completed&per_page=30") or {}).get("workflow_runs", [])
    seen = {}
    for w in runs: seen.setdefault(w["workflow_id"], w)
    for w in seen.values():
        if w.get("event") == "dynamic": continue                               # Dependabot's own update runs are not our CI
        if w["conclusion"] in ("failure", "timed_out", "startup_failure"):
            jobs = (get(f"https://api.github.com/repos/{OWNER}/{name}/actions/runs/{w['id']}/jobs") or {}).get("jobs", [])
            if jobs and not any(j["conclusion"] in ("failure", "timed_out") for j in jobs): continue   # only cancelled or skipped jobs (a busy or stopped runner), not a code failure
            issues.append(f"CI failing: {w['name']} ({w['conclusion']}) {w['html_url']}")
    # workflow hygiene
    files = get(f"https://api.github.com/repos/{OWNER}/{name}/contents/.github/workflows") or []
    for f in files:
        if not f["name"].endswith((".yml", ".yaml")): continue
        text = get(f["download_url"], raw=True) or ""
        for m in re.finditer(r"^\s*-?\s*uses:\s*(\S+)", text, flags=re.M):
            u = m.group(1)
            if not u.startswith("./") and not SHA.search(u + " "): issues.append(f"{f['name']}: action not pinned to a commit: {u}")
        if not re.search(r"^permissions:", text, flags=re.M): issues.append(f"{f['name']}: no top level permissions")
        if "pull_request_target" in text: issues.append(f"{f['name']}: uses pull_request_target")
    if files and get(f"https://api.github.com/repos/{OWNER}/{name}/contents/.github/dependabot.yml") is None: issues.append("no dependabot.yml")
    cm = get(f"https://raw.githubusercontent.com/{OWNER}/{name}/{br}/CMakeLists.txt", raw=True) if r["size"] else None
    if cm and re.search(r"GIT_TAG\s+(main|master|develop)\b", cm): issues.append("CMake dependency pinned to a branch")
    return name, issues

def main():
    rs = repos(); bad = {}
    with cf.ThreadPoolExecutor(6) as ex:
        for name, issues in ex.map(lambda r: _safe(r), rs):
            if issues: bad[name] = issues
    lines = [f"# Weekly sweep: {OWNER}", "", f"{len(rs)} public repos checked, {len(rs) - len(bad)} clean, {len(bad)} need attention.", ""]
    for n in sorted(bad): lines += [f"## {n}"] + [f"- {i}" for i in bad[n]] + [""]
    text = "\n".join(lines); print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"): open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(text + "\n")
    return 1 if bad else 0

def _safe(r):
    try: return check(r)
    except Exception as e: return r["name"], [f"sweep could not read this repo: {e}"]

if __name__ == "__main__": sys.exit(main())
