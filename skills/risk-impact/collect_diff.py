#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Tobias Hofmann
"""Collect the facts for a risk/impact assessment of a code change.

Determines the scope of the change (feature branch vs. its base branch, or the
last commit), lists the changed files with line counts, classifies them by
area and derives risk signals. It does not judge; the agent does that.

Standard library only. Requires git.

Usage:
  collect_diff.py                      # auto: branch since fork point, else last commit
  collect_diff.py --last-commit        # only HEAD
  collect_diff.py --base origin/main   # branch changes against an explicit ref
  collect_diff.py --working-tree       # include uncommitted changes (vs. the scope base)
  collect_diff.py --format json|markdown
  collect_diff.py --patch [--max-patch-lines N]   # append the (truncated) diff
"""
import argparse
import fnmatch
import json
import re
import subprocess
import sys

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git's empty tree
DEFAULT_BRANCHES = ("main", "master", "develop", "trunk")

# Ordered: first matching category wins. Patterns are matched case-insensitively
# against the full path (fnmatch, `*` also crosses `/`).
CATEGORIES = [
    ("lockfile", ["*package-lock.json", "*yarn.lock", "*pnpm-lock.yaml", "*poetry.lock",
                  "*Pipfile.lock", "*Cargo.lock", "*go.sum", "*Gemfile.lock",
                  "*composer.lock", "*packages.lock.json", "*gradle.lockfile"]),
    ("generated", ["*.min.js", "*.min.css", "*.map", "*/dist/*", "*/build/*",
                   "*_pb2.py", "*.pb.go", "*.generated.*", "*/__snapshots__/*", "*.snap"]),
    ("test", ["*/test/*", "*/tests/*", "*/__tests__/*", "*/spec/*", "*test_*.py", "*_test.*",
              "*.test.*", "*.spec.*", "test/*", "tests/*", "spec/*", "*/e2e/*", "*/cypress/*"]),
    ("docs", ["*.md", "*.rst", "*.adoc", "docs/*", "*/docs/*", "*LICENSE*", "*CHANGELOG*"]),
    ("migration", ["*/migrations/*", "*/migrate/*", "*/db/migrate/*", "*/flyway/*",
                   "*/liquibase/*", "*.sql", "*schema.prisma", "*/alembic/*", "*.cds"]),
    ("ci-cd", [".github/workflows/*", ".gitlab-ci.yml", "Jenkinsfile", ".circleci/*",
               "azure-pipelines*.yml", ".travis.yml", "*/.buildkite/*", "bitbucket-pipelines.yml"]),
    ("infra", ["*Dockerfile*", "docker-compose*", "*.tf", "*.tfvars", "*/k8s/*",
               "*/kubernetes/*", "*/helm/*", "*/charts/*", "*.bicep", "*/terraform/*",
               "*mta.yaml", "*manifest.yml", "*Procfile", "*/ansible/*"]),
    ("dependency", ["*package.json", "*pyproject.toml", "*requirements*.txt", "*Pipfile",
                    "*Cargo.toml", "*go.mod", "*Gemfile", "*pom.xml", "*build.gradle*",
                    "*composer.json", "*.csproj", "*setup.py", "*setup.cfg"]),
    ("config", [".env*", "*.env", "*.properties", "*.yaml", "*.yml", "*.toml", "*.ini",
                "*.conf", "*config*.json", "*/config/*", "*settings*.py", "*.xcconfig",
                "*xs-security.json", "*.json5"]),
    ("api-contract", ["*openapi*", "*swagger*", "*.proto", "*.graphql", "*.gql",
                      "*/api/*", "*/routes/*", "*/controllers/*", "*/endpoints/*",
                      "*.wsdl", "*.edmx"]),
]

# Content-independent path hints for security-sensitive code (additional flag,
# not exclusive with a category).
SENSITIVE_RE = re.compile(
    r"(auth|login|logout|session|token|jwt|oauth|saml|sso|passw|secret|credential|"
    r"crypt|cipher|hash|sign(ing|ature)|cert|permission|acl|rbac|role|policy|"
    r"security|csrf|cors|sanitiz|escap|payment|billing|invoice|pii|gdpr|consent|privacy)",
    re.IGNORECASE)

CODE_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".go", ".rs", ".rb", ".php",
            ".cs", ".cpp", ".c", ".h", ".hpp", ".swift", ".scala", ".vue", ".svelte",
            ".abap", ".sh", ".m", ".dart", ".lua", ".xml", ".html", ".css", ".scss"}


def git(*args, check=True):
    p = subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if check and p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or f"git {' '.join(args)} failed")
    return p.stdout.strip() if p.returncode == 0 else ""


def ref_exists(ref):
    return subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref + "^{commit}"],
                          capture_output=True).returncode == 0


def current_branch():
    b = git("rev-parse", "--abbrev-ref", "HEAD", check=False)
    return b if b and b != "HEAD" else "(detached HEAD)"


def find_default_branch():
    """Best guess for the branch the feature branch will be merged into."""
    head = git("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False)
    if head:
        return head.replace("refs/remotes/", "")
    for name in DEFAULT_BRANCHES:
        for cand in (f"origin/{name}", name):
            if ref_exists(cand):
                return cand
    return None


def resolve_scope(args):
    """Return dict(mode, base, head, label, note). base/head are commit-ish."""
    branch = current_branch()
    has_parent = ref_exists("HEAD~1")
    last_base = "HEAD~1" if has_parent else EMPTY_TREE

    if args.last_commit:
        return dict(mode="last-commit", base=last_base, head="HEAD",
                    label="last commit (explicitly requested)", note=None)

    base_ref = args.base or find_default_branch()
    if base_ref:
        if not ref_exists(base_ref):
            raise RuntimeError(f"base ref '{base_ref}' does not exist")
        mb = git("merge-base", base_ref, "HEAD", check=False)
        head_sha = git("rev-parse", "HEAD")
        if mb and mb != head_sha:
            return dict(mode="branch", base=mb, head="HEAD",
                        label=f"{branch} since fork point from {base_ref}",
                        note=None, base_ref=base_ref)
        if args.base:
            return dict(mode="branch", base=mb or base_ref, head="HEAD",
                        label=f"{branch} vs {base_ref} (no commits ahead)",
                        note="No commits ahead of the base; the branch diff is empty.",
                        base_ref=base_ref)
    note = ("On the base branch (or no base branch found): assessing the last commit only. "
            "Pass --base <ref> to assess a range.")
    return dict(mode="last-commit", base=last_base, head="HEAD",
                label="last commit", note=note)


def categorize(path):
    p = path.lower()
    for cat, pats in CATEGORIES:
        for pat in pats:
            if fnmatch.fnmatch(p, pat.lower()) or fnmatch.fnmatch("/" + p, pat.lower()):
                return cat
    ext = "." + p.rsplit(".", 1)[-1] if "." in p.rsplit("/", 1)[-1] else ""
    return "source" if ext in CODE_EXT else "other"


def changed_files(base, head, working_tree):
    rng = [base] if working_tree else [base, head]
    numstat = git("diff", "--numstat", "-M", "--no-color", *rng, check=False)
    status = git("diff", "--name-status", "-M", "--no-color", *rng, check=False)
    st = {}
    for line in status.splitlines():
        parts = line.split("\t")
        code = parts[0][0]
        path = parts[-1]
        st[path] = (code, parts[1] if code == "R" else None)
    files = []
    for line in numstat.splitlines():
        a, d, path = line.split("\t", 2)
        old = None
        if " => " in path:  # rename in numstat: resolve to the new path
            m = re.match(r"^(.*)\{(.*) => (.*)\}(.*)$", path)
            if m:
                old = (m.group(1) + m.group(2) + m.group(4)).replace("//", "/")
                path = (m.group(1) + m.group(3) + m.group(4)).replace("//", "/")
            else:
                old, path = path.split(" => ")
        binary = a == "-"
        code, old_from_status = st.get(path, ("M", None))
        files.append(dict(path=path, old_path=old or old_from_status,
                          status={"A": "added", "D": "deleted", "R": "renamed",
                                  "M": "modified"}.get(code, "modified"),
                          added=0 if binary else int(a), deleted=0 if binary else int(d),
                          binary=binary, category=categorize(path),
                          sensitive=bool(SENSITIVE_RE.search(path))))
    if working_tree:  # untracked files are invisible to `git diff`
        for path in git("ls-files", "--others", "--exclude-standard", check=False).splitlines():
            files.append(dict(path=path, old_path=None, status="added", added=0, deleted=0,
                              binary=False, category=categorize(path),
                              sensitive=bool(SENSITIVE_RE.search(path)), untracked=True))
    return files


def derive_signals(files, commits):
    total = sum(f["added"] + f["deleted"] for f in files)
    by_cat = {}
    for f in files:
        by_cat.setdefault(f["category"], []).append(f)
    src = by_cat.get("source", [])
    tests = by_cat.get("test", [])
    sig = []

    def add(level, text):
        sig.append(dict(level=level, text=text))

    if not files:
        add("info", "No file changes in scope.")
        return sig, total
    if total > 1000 or len(files) > 40:
        add("high", f"Large change: {len(files)} files, {total} changed lines; hard to review reliably.")
    elif total > 300 or len(files) > 15:
        add("medium", f"Sizeable change: {len(files)} files, {total} changed lines.")
    if src and not tests:
        add("medium", f"{len(src)} source file(s) changed, no test files changed.")
    if "migration" in by_cat:
        add("high", "Schema/data migration files changed: " + _names(by_cat["migration"])
            + ". Check reversibility, locking, data volume and deploy order.")
    if "dependency" in by_cat or "lockfile" in by_cat:
        add("medium", "Dependency manifests/lockfiles changed: "
            + _names(by_cat.get("dependency", []) + by_cat.get("lockfile", []))
            + ". Check new/upgraded packages, licenses and transitive effects.")
    if "ci-cd" in by_cat:
        add("medium", "CI/CD pipeline changed: " + _names(by_cat["ci-cd"]) + ".")
    if "infra" in by_cat:
        add("high", "Infrastructure/deployment files changed: " + _names(by_cat["infra"]) + ".")
    if "config" in by_cat:
        add("medium", "Configuration changed: " + _names(by_cat["config"])
            + ". Check per-environment values and defaults.")
    if "api-contract" in by_cat:
        add("medium", "API surface/contract files changed: " + _names(by_cat["api-contract"])
            + ". Check consumers and backward compatibility.")
    sens = [f for f in files if f["sensitive"] and f["category"] not in ("docs", "test", "lockfile")]
    if sens:
        add("high", "Security- or business-critical paths touched: " + _names(sens) + ".")
    deleted = [f for f in files if f["status"] == "deleted" and f["category"] in ("source", "api-contract")]
    if deleted:
        add("medium", "Deleted source/API files: " + _names(deleted) + ". Check for remaining callers.")
    if any(f["binary"] for f in files):
        add("info", "Binary files changed (not diffable): " + _names([f for f in files if f["binary"]]) + ".")
    if len(commits) > 30:
        add("info", f"{len(commits)} commits in scope; consider assessing in slices.")
    return sig, total


def _names(fs, n=5):
    names = [f["path"] for f in fs]
    return ", ".join(names[:n]) + (f" (+{len(names) - n} more)" if len(names) > n else "")


def collect(args):
    if git("rev-parse", "--is-inside-work-tree", check=False) != "true":
        raise RuntimeError("not inside a git repository")
    if not ref_exists("HEAD"):
        raise RuntimeError("repository has no commits yet")
    scope = resolve_scope(args)
    rng = f"{scope['base']}..{scope['head']}" if scope["base"] != EMPTY_TREE else scope["head"]
    log = git("log", "--format=%h%x09%an%x09%s", rng, check=False)
    commits = [dict(sha=a, author=b, subject=c) for a, b, c in
               (l.split("\t", 2) for l in log.splitlines() if l.count("\t") >= 2)]
    files = changed_files(scope["base"], scope["head"], args.working_tree)
    dirty = git("status", "--porcelain", check=False)
    signals, total = derive_signals(files, commits)
    out = dict(branch=current_branch(), scope=scope, commits=commits, files=files,
               totals=dict(files=len(files), lines=total,
                           added=sum(f["added"] for f in files),
                           deleted=sum(f["deleted"] for f in files)),
               signals=signals)
    if dirty and not args.working_tree:
        out["warning"] = ("Uncommitted changes exist and are NOT part of this assessment. "
                          "Re-run with --working-tree to include them.")
    if args.patch:
        r = [scope["base"]] if args.working_tree else [scope["base"], scope["head"]]
        patch = git("diff", "--no-color", "-M", *r, check=False).splitlines()
        if len(patch) > args.max_patch_lines:
            cut = len(patch) - args.max_patch_lines
            patch = patch[:args.max_patch_lines] + [f"... [{cut} more lines truncated; read files individually]"]
        out["patch"] = "\n".join(patch)
    return out


def to_markdown(d):
    s = d["scope"]
    o = [f"# Diff facts", "", f"- Branch: `{d['branch']}`", f"- Scope: {s['label']} (`{s['mode']}`)"]
    if s.get("note"):
        o.append(f"- Note: {s['note']}")
    if d.get("warning"):
        o.append(f"- WARNING: {d['warning']}")
    t = d["totals"]
    o += [f"- Size: {t['files']} files, +{t['added']} / -{t['deleted']} lines, "
          f"{len(d['commits'])} commit(s)", "", "## Signals"]
    o += [f"- [{x['level']}] {x['text']}" for x in d["signals"]] or ["- none"]
    o += ["", "## Commits"]
    o += [f"- {c['sha']} {c['subject']} ({c['author']})" for c in d["commits"][:30]] or ["- none"]
    o += ["", "## Files", "", "| Status | Category | + | - | Path |", "|---|---|---|---|---|"]
    for f in sorted(d["files"], key=lambda f: -(f["added"] + f["deleted"])):
        flag = " (sensitive path)" if f["sensitive"] else ""
        path = f["path"] + (f" (from {f['old_path']})" if f["old_path"] else "") + flag
        o.append(f"| {f['status']} | {f['category']} | {f['added']} | {f['deleted']} | {path} |")
    if "patch" in d:
        o += ["", "## Patch", "", "```diff", d["patch"], "```"]
    return "\n".join(o)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--last-commit", action="store_true", help="assess only HEAD")
    ap.add_argument("--base", help="base ref for the branch diff (default: auto-detected)")
    ap.add_argument("--working-tree", action="store_true", help="include uncommitted changes")
    ap.add_argument("--format", choices=("markdown", "json"), default="markdown")
    ap.add_argument("--patch", action="store_true", help="append the unified diff")
    ap.add_argument("--max-patch-lines", type=int, default=1500)
    args = ap.parse_args(argv)
    try:
        d = collect(args)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(d, indent=2) if args.format == "json" else to_markdown(d))
    return 0


if __name__ == "__main__":
    sys.exit(main())
