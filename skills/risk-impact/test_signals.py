#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Tobias Hofmann
"""Test and coverage facts for a risk/impact assessment.

Runs the project's own test suite (auto-detected or given with --cmd), reads
coverage data if it can be produced or found, and relates both to the changed
code: how well are the changed lines covered, and how heavily is the changed
code used by tests (test fan-in)? It does not judge; the agent does that.

It EXECUTES the project's tests, i.e. repository code. Use --no-run to only
analyse (usage, existing coverage artifacts) without running anything.

Standard library only. Scope flags are the same as collect_diff.py.

Usage:
  test_signals.py [--last-commit | --base REF] [--working-tree]
                  [--cmd "pytest -q"] [--no-run] [--no-coverage]
                  [--timeout 600] [--format json|markdown]
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from xml.parsers import expat
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect_diff as cd  # noqa: E402

TAIL_LINES = 40
MAX_COVERAGE_BYTES = 100 * 1024 * 1024   # refuse absurdly large coverage files
MAX_GO_RANGE = 100_000                   # lines in one Go coverage block
MAX_COVERAGE_LINES = 5_000_000           # expanded Go lines per profile
MAX_SYMBOLS_PER_FILE = 15


# ---------------------------------------------------------------- changed lines

def changed_lines(base, head, working_tree):
    """{path: (set(new-side changed line numbers), [symbol names from hunk headers])}"""
    rng = [base] if working_tree else [base, head]
    out = cd.git("diff", "-U0", "--no-color", "-M", *rng, check=False)
    res, path = {}, None
    for line in out.splitlines():
        if line.startswith("+++ "):
            path = None if line.endswith("/dev/null") else line[6:]
            if path:
                res.setdefault(path, (set(), []))
        elif line.startswith("@@") and path:
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@ ?(.*)", line)
            if not m:
                continue
            start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            res[path][0].update(range(start, start + count))
            sym = _symbol(m.group(3))
            if sym and sym not in res[path][1]:
                res[path][1].append(sym)
    return res


_SYM_RE = re.compile(
    r"(?:def|class|function|func|fn|interface|struct|enum|trait|module|method)\s+([A-Za-z_][\w$]*)"
    r"|(?:public|private|protected|static|async|export|const|let|var|final|\s)+"
    r"(?:[\w<>\[\],.?]+\s+)?([A-Za-z_][\w$]*)\s*\(")


def _symbol(ctx):
    """Name of the enclosing function/class from git's hunk-header context."""
    m = _SYM_RE.search(ctx or "")
    if m:
        return m.group(1) or m.group(2)
    return None


# ---------------------------------------------------------------- test fan-in

def usage_of(files, lines_by_path):
    """For changed source files: how many test files / other files reference the
    module and the enclosing symbols of the change."""
    tracked = cd.git("ls-files", check=False).splitlines()
    test_files = {p for p in tracked if cd.categorize(p) == "test"}
    result = []
    for f in files:
        if f["category"] not in ("source", "api-contract") or f["status"] == "deleted":
            continue
        stem = Path(f["path"]).stem
        syms = lines_by_path.get(f["path"], (set(), []))[1][:MAX_SYMBOLS_PER_FILE]
        entry = dict(path=f["path"], module=_refs(stem, f["path"], test_files, tracked),
                     symbols=[dict(name=s, **_refs(s, f["path"], test_files, tracked)) for s in syms])
        result.append(entry)
    return result


def _refs(word, own_path, test_files, tracked):
    if len(word) < 3 or word in ("index", "main", "init", "__init__"):
        return dict(test_files=None, other_files=None)  # too generic to count
    out = cd.git("grep", "-l", "-w", "-F", "-I", "--", word, check=False)
    hits = [p for p in out.splitlines() if p != own_path]
    t = sum(1 for p in hits if p in test_files)
    return dict(test_files=t, other_files=len(hits) - t)


# ---------------------------------------------------------------- running tests

def detect_command(root, tmp, want_cov):
    """Return (framework, argv list or None, coverage_file or None, coverage_kind)."""
    r = Path(root)
    has = lambda *n: any((r / x).exists() for x in n)
    pkg = {}
    if has("package.json"):
        try:
            pkg = json.loads((r / "package.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    deps = {**pkg.get("devDependencies", {}), **pkg.get("dependencies", {})}
    script = pkg.get("scripts", {}).get("test", "")

    if has("pytest.ini", "conftest.py", "tox.ini", "pyproject.toml", "setup.cfg", "requirements.txt"):
        if _py_mod("pytest"):
            cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
            cov = None
            if want_cov and _py_mod("pytest_cov"):
                cov = os.path.join(tmp, "cov.json")
                cmd += ["--cov=.", f"--cov-report=json:{cov}"]
            return "pytest", cmd, cov, "coveragepy"
    if has("package.json") and script and "no test specified" not in script:
        cmd = ["npm", "test", "--silent"]
        cov = None
        if want_cov and "vitest" in deps:
            cov = os.path.join(tmp, "lcov.info")
            cmd += ["--", "--coverage.enabled", "--coverage.reporter=lcov",
                    f"--coverage.reportsDirectory={tmp}"]
        elif want_cov and "jest" in deps:
            cov = os.path.join(tmp, "lcov.info")
            cmd += ["--", "--coverage", "--coverageReporters=lcov", f"--coverageDirectory={tmp}"]
        return ("vitest" if "vitest" in deps else "jest" if "jest" in deps else "npm test"), cmd, cov, "lcov"
    if has("go.mod") and shutil.which("go"):
        cov = os.path.join(tmp, "c.out") if want_cov else None
        return "go test", ["go", "test", "./..."] + ([f"-coverprofile={cov}"] if cov else []), cov, "go"
    if has("Cargo.toml") and shutil.which("cargo"):
        return "cargo test", ["cargo", "test"], None, None
    if has("pom.xml") and shutil.which("mvn"):
        return "maven", ["mvn", "-q", "-B", "test"], None, None
    if has("gradlew"):
        return "gradle", ["./gradlew", "test", "--console=plain"], None, None
    if has("build.gradle", "build.gradle.kts") and shutil.which("gradle"):
        return "gradle", ["gradle", "test", "--console=plain"], None, None
    if has("Gemfile") and has("spec") and shutil.which("bundle"):
        return "rspec", ["bundle", "exec", "rspec"], None, None
    if any(r.glob("*.sln")) or any(r.glob("**/*.csproj")):
        if shutil.which("dotnet"):
            return "dotnet test", ["dotnet", "test", "--nologo"], None, None
    tdir = next((d for d in ("tests", "test") if (r / d).is_dir()), None)
    if tdir and any((r / tdir).glob("**/test*.py")):
        if want_cov and _py_mod("coverage"):
            cov = os.path.join(tmp, "cov.json")
            return "unittest", ["sh", "-c",
                                f'"{sys.executable}" -m coverage run --data-file="{tmp}/.cov" -m unittest discover -s {tdir}'
                                f' && "{sys.executable}" -m coverage json --data-file="{tmp}/.cov" -o "{cov}"'], cov, "coveragepy"
        return "unittest", [sys.executable, "-m", "unittest", "discover", "-s", tdir], None, None
    return None, None, None, None


def _py_mod(name):
    """Is a module importable? Checked without importing it and without looking at the
    repository: find_spec locates it but does not execute it, and the working directory
    (a planted pytest.py) is not on the search path."""
    code = ("import importlib.util, sys; sys.path[:] = [p for p in sys.path if p not in ('', '.')]; "
            f"sys.exit(0 if importlib.util.find_spec({name!r}) else 1)")
    return subprocess.run([sys.executable, "-I", "-c", code], cwd=tempfile.gettempdir(),
                          capture_output=True).returncode == 0


def run_tests(argv, root, timeout):
    t0 = time.time()
    try:
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [root, os.environ.get("PYTHONPATH")]))}
        p = subprocess.run(argv, cwd=root, env=env, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        rc, out, timed_out = p.returncode, (p.stdout or "") + "\n" + (p.stderr or ""), False
    except subprocess.TimeoutExpired as e:
        rc, out, timed_out = None, ((e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes)
                                    else (e.stdout or "")), True
    except FileNotFoundError as e:
        return dict(ran=False, reason=f"command not found: {e.filename}")
    counts = parse_counts(out)
    return dict(ran=True, exit_code=rc, timed_out=timed_out, passed_overall=(rc == 0 and not timed_out),
                duration_s=round(time.time() - t0, 1), **counts,
                output_tail="\n".join(out.strip().splitlines()[-TAIL_LINES:]))


def parse_counts(out):
    """Best effort. Keys with None mean 'not parseable'; the exit code stays authoritative."""
    c = dict(passed=None, failed=None, errors=None, skipped=None)

    def num(rx, text=out):
        m = re.search(rx, text)
        return int(m.group(1)) if m else None

    # pytest summary line, e.g. "2 failed, 10 passed, 1 skipped in 0.3s"
    if re.search(r"\b\d+ (passed|failed|error)", out) and " in " in out:
        c.update(passed=num(r"(\d+) passed"), failed=num(r"(\d+) failed"),
                 errors=num(r"(\d+) errors?\b"), skipped=num(r"(\d+) skipped"))
    # jest / vitest "Tests:  1 failed, 5 passed, 6 total"
    elif re.search(r"^\s*Tests:\s", out, re.M):
        line = re.search(r"^\s*Tests:.*$", out, re.M).group(0)
        c.update(passed=num(r"(\d+) passed", line), failed=num(r"(\d+) failed", line),
                 skipped=num(r"(\d+) skipped", line))
    # python unittest "Ran 12 tests" + "FAILED (failures=1, errors=2)" / "OK"
    elif re.search(r"^Ran \d+ tests?", out, re.M):
        total = num(r"(?m)^Ran (\d+) tests?")
        f = num(r"failures=(\d+)") or 0
        e = num(r"errors=(\d+)") or 0
        s = num(r"skipped=(\d+)") or 0
        c.update(passed=total - f - e - s, failed=f, errors=e, skipped=s)
    # go test
    elif re.search(r"^(ok|FAIL|---)\s", out, re.M):
        c.update(passed=len(re.findall(r"^ok\s", out, re.M)), failed=len(re.findall(r"^FAIL\s", out, re.M)))
    return c


# ---------------------------------------------------------------- coverage parsing

def parse_coverage(path, kind):
    """-> {normalized path: {line: hits}} or None"""
    try:
        if kind == "lcov" or path.endswith((".info",)):
            return _lcov(path)
        if kind == "go" or path.endswith(".out"):
            return _go(path)
        if path.endswith(".xml"):
            return _cobertura(path)
        return _coveragepy(path)
    except (OSError, ValueError, expat.ExpatError, KeyError):
        return None


def _lcov(path):
    cov, cur = {}, None
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("SF:"):
            cur = cov.setdefault(line[3:].strip(), {})
        elif line.startswith("DA:") and cur is not None:
            n, h = line[3:].split(",")[:2]
            cur[int(n)] = int(h)
    return cov


def _coveragepy(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cov = {}
    for p, d in data["files"].items():
        m = {n: 1 for n in d.get("executed_lines", [])}
        m.update({n: 0 for n in d.get("missing_lines", [])})
        cov[p] = m
    return cov


def _cobertura(path):
    if Path(path).stat().st_size > MAX_COVERAGE_BYTES:
        raise ValueError("coverage file too large")
    # Parse with expat directly and refuse any DTD/entity declaration. This works on the
    # parsed document in whatever encoding it has (UTF-16 included), unlike a byte search.
    cov, cur = {}, [None]

    def refuse(*_a):
        raise ValueError("DTD/entity declarations are not accepted in coverage XML")

    def start(tag, attrs):
        if tag == "class":
            cur[0] = cov.setdefault(attrs.get("filename"), {})
        elif tag == "line" and cur[0] is not None:
            cur[0][int(attrs["number"])] = int(attrs.get("hits", 0))

    p = expat.ParserCreate()
    p.StartDoctypeDeclHandler = refuse
    p.EntityDeclHandler = refuse
    p.StartElementHandler = start
    p.Parse(Path(path).read_bytes(), True)
    return cov


def _go(path):
    cov, total = {}, 0
    for line in Path(path).read_text(encoding="utf-8").splitlines()[1:]:
        m = re.match(r"(.+):(\d+)\.\d+,(\d+)\.\d+ \d+ (\d+)", line)
        if m:
            start, end, hits = int(m.group(2)), int(m.group(3)), int(m.group(4))
            if end < start or end - start > MAX_GO_RANGE:
                continue  # malformed or absurd block: do not expand it
            f = cov.setdefault(m.group(1), {})
            for n in range(start, end + 1):
                f[n] = max(f.get(n, 0), hits)
            total += end - start + 1
            if total > MAX_COVERAGE_LINES:
                raise ValueError("coverage profile too large")
    return cov


def find_existing_coverage(root, since):
    """Coverage artifacts produced by the project itself, newer than `since`."""
    cands = ["coverage.json", "coverage/lcov.info", "lcov.info", "coverage.xml", "coverage/coverage.xml",
             "target/site/jacoco/jacoco.xml", "build/reports/cobertura.xml", "cover.out", "coverage.out"]
    for rel in cands:
        p = Path(root) / rel
        if p.is_file() and p.stat().st_mtime >= since:
            return str(p), ("lcov" if p.suffix == ".info" else "go" if p.suffix == ".out" else None)
    return None, None


def _match(cov, path):
    path = path.replace("\\", "/")
    for k, v in cov.items():
        k2 = k.replace("\\", "/")
        if k2 == path or k2.endswith("/" + path) or path.endswith("/" + k2.lstrip("./")):
            return v
    return None


def coverage_for_changes(cov, files, lines_by_path):
    out = []
    for f in files:
        if f["category"] != "source" or f["status"] == "deleted":
            continue
        fc = _match(cov, f["path"])
        changed = lines_by_path.get(f["path"], (set(), []))[0]
        if fc is None:
            out.append(dict(path=f["path"], in_coverage_report=False))
            continue
        executable = {n for n in changed if n in fc}        # changed lines that are code
        covered = {n for n in executable if fc[n] > 0}
        file_exec = len(fc)
        out.append(dict(path=f["path"], in_coverage_report=True, changed_executable_lines=len(executable),
                        changed_covered_lines=len(covered),
                        changed_coverage_pct=round(100 * len(covered) / len(executable), 1) if executable else None,
                        file_coverage_pct=round(100 * sum(1 for h in fc.values() if h > 0) / file_exec, 1) if file_exec else None,
                        uncovered_changed_lines=sorted(executable - covered)[:50]))
    return out


# ---------------------------------------------------------------- orchestration

def derive_signals(d):
    sig = []
    add = lambda lvl, t: sig.append(dict(level=lvl, text=t))
    t = d["tests"]
    if not t.get("ran"):
        add("medium", "Tests were not run: " + t.get("reason", "unknown reason") + ". Test status is unknown.")
    elif t.get("timed_out"):
        add("high", "Test run timed out; test status is unknown.")
    elif not t["passed_overall"]:
        n = " (" + ", ".join(f"{k}={t[k]}" for k in ("failed", "errors") if t.get(k)) + ")" if (t.get("failed") or t.get("errors")) else ""
        add("high", f"Tests FAILED{n}. Check whether the failures relate to the changed code.")
    elif t.get("passed") == 0:
        add("high", "Test run succeeded but executed 0 tests.")
    if d["coverage"]["available"]:
        for c in d["coverage"]["changed"]:
            if not c["in_coverage_report"]:
                add("high", f"{c['path']} is absent from the coverage report: no test loads it.")
            elif c["changed_coverage_pct"] is not None and c["changed_coverage_pct"] < 50:
                add("high", f"Only {c['changed_coverage_pct']}% of changed executable lines in {c['path']} are covered.")
            elif c["changed_coverage_pct"] is not None and c["changed_coverage_pct"] < 80:
                add("medium", f"{c['changed_coverage_pct']}% of changed executable lines in {c['path']} are covered.")
    else:
        add("info", "No coverage data: " + d["coverage"].get("reason", "") + " Judge test depth from the test files instead.")
    for u in d["usage"]:
        hot = [s for s in u["symbols"] if (s["test_files"] or 0) >= 3]
        m = u["module"]["test_files"] or 0
        if hot:
            add("medium", f"{u['path']}: changed symbol(s) "
                + ", ".join(f"{s['name']} ({s['test_files']} test files)" for s in hot[:4])
                + " are heavily used by tests, so this is likely core code.")
        elif m >= 5:
            add("medium", f"{u['path']}: module referenced by {m} test files, likely core code.")
        if (m == 0 and not any((s["test_files"] or 0) for s in u["symbols"])
                and u["module"]["test_files"] is not None):
            add("medium", f"{u['path']}: no test file references this module or the changed symbols.")
    return sig


def collect(args):
    class A:  # reuse collect_diff scope resolution
        last_commit, base, working_tree, patch, max_patch_lines = (
            args.last_commit, args.base, args.working_tree, False, 0)
    diff = cd.collect(A)
    scope = diff["scope"]
    root = cd.git("rev-parse", "--show-toplevel")
    lines = changed_lines(scope["base"], scope["head"], args.working_tree)
    files = diff["files"]
    tmp = tempfile.mkdtemp(prefix="risk-impact-")
    try:
        started = time.time()
        fw, argv, cov_file, cov_kind = (None, None, None, None)
        if args.cmd:
            import shlex
            fw, argv = "custom", shlex.split(args.cmd)
        elif args.no_run:
            pass  # nothing is executed: no detection, no probing of the repository's tooling
        else:
            fw, argv, cov_file, cov_kind = detect_command(root, tmp, not args.no_coverage)
        if args.no_run:
            tests = dict(ran=False, framework=fw, reason="--no-run given")
        elif not argv:
            tests = dict(ran=False, reason="no test framework detected; pass --cmd")
        else:
            tests = dict(framework=fw, command=" ".join(argv), **run_tests(argv, root, args.timeout))
        cov = dict(available=False, reason="coverage disabled with --no-coverage." if args.no_coverage else "")
        if not args.no_coverage:
            data = None
            if cov_file and os.path.exists(cov_file):
                data, src = parse_coverage(cov_file, cov_kind), "generated by the test run"
            if data is None:
                p, kind = find_existing_coverage(root, started - 1 if tests.get("ran") else 0)
                if p:
                    data, src = parse_coverage(p, kind), os.path.relpath(p, root)
            if data:
                cov = dict(available=True, source=src, changed=coverage_for_changes(data, files, lines))
            elif not cov["reason"]:
                cov["reason"] = ("no coverage report produced or found (e.g. install pytest-cov / "
                                 "coverage, or run the tests with coverage and keep the report).")
        d = dict(scope=scope, tests=tests, coverage=cov, usage=usage_of(files, lines))
        d["signals"] = derive_signals(d)
        return d
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def to_markdown(d):
    t, c = d["tests"], d["coverage"]
    o = ["# Test and coverage facts", "", f"- Scope: {d['scope']['label']}"]
    if t.get("ran"):
        o.append(f"- Tests ({t['framework']}): `{t['command']}` -> exit {t['exit_code']}, "
                 f"passed={t.get('passed')} failed={t.get('failed')} errors={t.get('errors')} "
                 f"skipped={t.get('skipped')}, {t['duration_s']}s")
    else:
        o.append(f"- Tests: not run ({t.get('reason')})")
    o += ["", "## Signals"] + [f"- [{s['level']}] {s['text']}" for s in d["signals"]]
    if not d["signals"]:
        o.append("- none")
    o += ["", "## Coverage of changed code"]
    if c["available"]:
        o += [f"Source: {c['source']}", "", "| File | Changed lines covered | File coverage |", "|---|---|---|"]
        for x in c["changed"]:
            if x["in_coverage_report"]:
                o.append(f"| {x['path']} | {x['changed_covered_lines']}/{x['changed_executable_lines']} "
                         f"({x['changed_coverage_pct']}%) | {x['file_coverage_pct']}% |")
            else:
                o.append(f"| {x['path']} | not in report | - |")
    else:
        o.append(f"Not available: {c['reason']}")
    o += ["", "## Test usage of changed code (fan-in)", "", "| File / symbol | Test files | Other files |", "|---|---|---|"]
    for u in d["usage"]:
        o.append(f"| {u['path']} (module) | {u['module']['test_files']} | {u['module']['other_files']} |")
        for s in u["symbols"]:
            o.append(f"| &nbsp;&nbsp;{s['name']} | {s['test_files']} | {s['other_files']} |")
    if t.get("ran") and not t.get("passed_overall"):
        o += ["", "## Test output (tail)", "", "```", t["output_tail"], "```"]
    return "\n".join(o)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--last-commit", action="store_true")
    ap.add_argument("--base")
    ap.add_argument("--working-tree", action="store_true")
    ap.add_argument("--cmd", help="test command to run instead of the auto-detected one")
    ap.add_argument("--no-run", action="store_true", help="do not execute tests")
    ap.add_argument("--no-coverage", action="store_true")
    ap.add_argument("--timeout", type=int, default=600, help="seconds (default 600)")
    ap.add_argument("--format", choices=("markdown", "json"), default="markdown")
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
