# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Tobias Hofmann
"""Smoke tests for collect_diff.py. Standard library only; needs git.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "risk-impact" / "collect_diff.py"
SKILL = ROOT / "skills" / "risk-impact" / "SKILL.md"
SIGNALS = ROOT / "skills" / "risk-impact" / "test_signals.py"
sys.path.insert(0, str(SIGNALS.parent))
import test_signals as ts  # noqa: E402
import render_graphic as rg  # noqa: E402


class Repo:
    def __init__(self, path):
        self.path = Path(path)
        self.env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
        self.git("init", "-q", "-b", "main")

    def git(self, *a):
        subprocess.run(["git", *a], cwd=self.path, env=self.env, check=True,
                       capture_output=True, text=True)

    def commit(self, files, msg="c"):
        for name, content in files.items():
            p = self.path / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)

    def collect(self, *args):
        p = subprocess.run([sys.executable, str(SCRIPT), "--format", "json", *args],
                           cwd=self.path, capture_output=True, text=True)
        return p.returncode, (json.loads(p.stdout) if p.returncode == 0 else p.stderr)


class CollectDiffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Repo(self.tmp.name)
        self.repo.commit({"README.md": "x\n", "src/app.py": "a = 1\n"}, "base")

    def test_branch_scope_since_fork(self):
        self.repo.git("checkout", "-q", "-b", "feature/x")
        self.repo.commit({"src/app.py": "a = 2\n"}, "one")
        self.repo.commit({"src/b.py": "b = 1\n"}, "two")
        rc, d = self.repo.collect()
        self.assertEqual(rc, 0)
        self.assertEqual(d["scope"]["mode"], "branch")
        self.assertEqual(len(d["commits"]), 2)
        self.assertEqual({f["path"] for f in d["files"]}, {"src/app.py", "src/b.py"})

    def test_last_commit_flag(self):
        self.repo.git("checkout", "-q", "-b", "feature/x")
        self.repo.commit({"src/app.py": "a = 2\n"}, "one")
        self.repo.commit({"src/b.py": "b = 1\n"}, "two")
        _, d = self.repo.collect("--last-commit")
        self.assertEqual(d["scope"]["mode"], "last-commit")
        self.assertEqual([f["path"] for f in d["files"]], ["src/b.py"])

    def test_on_base_branch_falls_back_to_last_commit(self):
        self.repo.commit({"src/c.py": "c = 1\n"}, "three")
        _, d = self.repo.collect()
        self.assertEqual(d["scope"]["mode"], "last-commit")
        self.assertEqual([f["path"] for f in d["files"]], ["src/c.py"])

    def test_single_commit_repo(self):
        with tempfile.TemporaryDirectory() as t:
            r = Repo(t)
            r.commit({"a.py": "x = 1\n"})
            rc, d = r.collect()
            self.assertEqual(rc, 0)
            self.assertEqual(d["totals"]["files"], 1)

    def test_categories_and_signals(self):
        self.repo.git("checkout", "-q", "-b", "feature/y")
        self.repo.commit({"db/migrations/001_add.sql": "alter table t add c int;\n",
                          "package.json": "{}\n",
                          "src/auth/login.py": "x = 1\n",
                          "Dockerfile": "FROM x\n"}, "risky")
        _, d = self.repo.collect()
        cats = {f["path"]: f["category"] for f in d["files"]}
        self.assertEqual(cats["db/migrations/001_add.sql"], "migration")
        self.assertEqual(cats["package.json"], "dependency")
        self.assertEqual(cats["Dockerfile"], "infra")
        self.assertEqual(cats["src/auth/login.py"], "source")
        text = " ".join(s["text"] for s in d["signals"])
        self.assertIn("migration", text.lower())
        self.assertIn("Security", text)
        self.assertIn("no test files changed", text)

    def test_tests_suppress_missing_test_signal(self):
        self.repo.git("checkout", "-q", "-b", "feature/z")
        self.repo.commit({"src/app.py": "a = 3\n", "tests/test_app.py": "pass\n"})
        _, d = self.repo.collect()
        self.assertFalse(any("no test files" in s["text"] for s in d["signals"]))

    def test_uncommitted_warning_and_working_tree(self):
        self.repo.git("checkout", "-q", "-b", "feature/w")
        self.repo.commit({"src/app.py": "a = 2\n"})
        (self.repo.path / "src" / "app.py").write_text("a = 99\n")
        (self.repo.path / "new.py").write_text("n = 1\n")
        _, d = self.repo.collect()
        self.assertIn("warning", d)
        _, d = self.repo.collect("--working-tree")
        self.assertNotIn("warning", d)
        self.assertIn("new.py", {f["path"] for f in d["files"]})

    def test_rename_detected(self):
        self.repo.git("checkout", "-q", "-b", "feature/r")
        self.repo.git("mv", "src/app.py", "src/renamed.py")
        self.repo.git("commit", "-q", "-m", "mv")
        _, d = self.repo.collect()
        f = d["files"][0]
        self.assertEqual((f["status"], f["path"], f["old_path"]),
                         ("renamed", "src/renamed.py", "src/app.py"))

    def test_patch_truncation(self):
        self.repo.git("checkout", "-q", "-b", "feature/p")
        self.repo.commit({"src/big.py": "\n".join(f"x{i} = {i}" for i in range(50)) + "\n"})
        _, d = self.repo.collect("--patch", "--max-patch-lines", "10")
        self.assertIn("truncated", d["patch"])

    def test_markdown_output(self):
        p = subprocess.run([sys.executable, str(SCRIPT)], cwd=self.repo.path,
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0)
        self.assertIn("## Files", p.stdout)

    def test_not_a_repo(self):
        with tempfile.TemporaryDirectory() as t:
            p = subprocess.run([sys.executable, str(SCRIPT)], cwd=t, capture_output=True, text=True)
            self.assertEqual(p.returncode, 2)


APP = "def helper(x):\n    return x + 1\n\n\ndef core(x):\n    return helper(x) * 2\n"
TEST_OK = ("import unittest\nfrom app import core, helper\n\n\nclass T(unittest.TestCase):\n"
           "    def test_core(self):\n        self.assertEqual(core(1), 4)\n"
           "    def test_helper(self):\n        self.assertEqual(helper(1), 2)\n")


class TestSignalsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Repo(self.tmp.name)
        self.repo.commit({"app.py": APP, "tests/test_app.py": TEST_OK}, "base")
        self.repo.git("checkout", "-q", "-b", "feature/t")

    def signals(self, *args):
        p = subprocess.run([sys.executable, str(SIGNALS), "--format", "json", *args],
                           cwd=self.repo.path, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_passing_tests_and_usage(self):
        self.repo.commit({"app.py": APP.replace("x + 1", "1 + x")})
        d = self.signals("--no-coverage")
        self.assertTrue(d["tests"]["ran"])
        self.assertEqual(d["tests"]["framework"], "unittest")
        self.assertEqual(d["tests"]["passed"], 2)
        self.assertTrue(d["tests"]["passed_overall"])
        u = d["usage"][0]
        self.assertEqual(u["path"], "app.py")
        self.assertEqual(u["module"]["test_files"], 1)
        self.assertEqual([x["name"] for x in u["symbols"]], ["helper"])
        self.assertEqual(u["symbols"][0]["test_files"], 1)

    def test_failing_tests_raise_signal(self):
        self.repo.commit({"app.py": APP.replace("* 2", "* 3")})  # test_core expects 4
        d = self.signals("--no-coverage")
        self.assertFalse(d["tests"]["passed_overall"])
        self.assertEqual(d["tests"]["failed"], 1)
        self.assertTrue(any(s["level"] == "high" and "FAILED" in s["text"] for s in d["signals"]))

    def test_planted_module_is_not_imported(self):
        # a repo-local pytest.py must neither be imported (module probe) nor run
        marker = Path(self.tmp.name) / "PWNED"
        (Path(self.tmp.name) / "pytest.py").write_text(
            f"open({str(marker)!r}, 'w').write('x')\n")
        (Path(self.tmp.name) / "conftest.py").write_text("")
        self.repo.commit({"app.py": APP + "\n\ndef extra():\n    return 0\n"})
        self.signals("--no-run")
        self.assertFalse(marker.exists())

    def test_no_run(self):
        self.repo.commit({"app.py": APP + "\n\ndef extra():\n    return 0\n"})
        d = self.signals("--no-run")
        self.assertFalse(d["tests"]["ran"])
        self.assertTrue(any("not run" in s["text"] for s in d["signals"]))

    def test_untested_module_signal(self):
        self.repo.commit({"orphan_mod.py": "def lonely():\n    return 1\n"})
        d = self.signals("--no-coverage")
        self.assertTrue(any("no test file references" in s["text"] for s in d["signals"]))

    def test_changed_lines_and_symbols(self):
        self.repo.commit({"app.py": APP.replace("x + 1", "x + 10")})
        old = os.getcwd()
        os.chdir(self.repo.path)
        self.addCleanup(os.chdir, old)
        cl = ts.changed_lines("HEAD~1", "HEAD", False)
        lines, syms = cl["app.py"]
        self.assertEqual(lines, {2})

    def test_parse_lcov_and_coverage_mapping(self):
        lcov = Path(self.tmp.name) / "lcov.info"
        lcov.write_text("SF:/abs/proj/app.py\nDA:1,1\nDA:2,0\nDA:3,4\nend_of_record\n")
        cov = ts.parse_coverage(str(lcov), "lcov")
        files = [dict(path="app.py", category="source", status="modified")]
        res = ts.coverage_for_changes(cov, files, {"app.py": ({1, 2, 99}, [])})
        self.assertEqual(res[0]["changed_executable_lines"], 2)
        self.assertEqual(res[0]["changed_covered_lines"], 1)
        self.assertEqual(res[0]["changed_coverage_pct"], 50.0)
        self.assertEqual(res[0]["uncovered_changed_lines"], [2])

    def test_parse_cobertura_and_rejects_entities(self):
        good = Path(self.tmp.name) / "coverage.xml"
        good.write_text('<coverage><packages><package><classes><class filename="a/b.py">'
                        '<lines><line number="3" hits="2"/></lines></class></classes></package></packages></coverage>')
        self.assertEqual(ts.parse_coverage(str(good), None), {"a/b.py": {3: 2}})
        bad = Path(self.tmp.name) / "bad.xml"
        bad.write_text('<!DOCTYPE x [<!ENTITY a "b">]><coverage/>')
        self.assertIsNone(ts.parse_coverage(str(bad), None))

    def test_parse_counts(self):
        self.assertEqual(ts.parse_counts("2 failed, 10 passed, 1 skipped in 0.3s")["passed"], 10)
        self.assertEqual(ts.parse_counts("Tests:       1 failed, 5 passed, 6 total")["failed"], 1)
        self.assertEqual(ts.parse_counts("Ran 12 tests in 1s\n\nFAILED (failures=1)")["passed"], 11)


REPORT = {
    "title": "feature/x", "scope": "since fork from main · 2 commits",
    "ratings": {
        "risk": {"score": 4, "steps": [{"label": "Base", "delta": 3}, {"label": "fan-in", "delta": 1},
                                       {"label": "no test", "delta": 1}, {"label": "covered", "delta": -1}]},
        "business": {"score": 3, "direction": "positive", "steps": [{"label": "Base", "delta": 3}]},
        "change": {"score": 2, "steps": [{"label": "Base", "delta": 2}]}}}


class GraphicTests(unittest.TestCase):
    def test_render_basic(self):
        svg = rg.render(json.loads(json.dumps(REPORT)))
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn("Overall: High", svg)
        self.assertIn("3/5 · Medium · positive", svg)
        self.assertNotIn("Hotspots", svg)

    def test_overall_derived_from_max(self):
        d = json.loads(json.dumps(REPORT))
        d["ratings"]["change"] = {"score": 5, "steps": [{"label": "b", "delta": 5}]}
        self.assertIn("Overall: Critical", rg.render(d))

    def test_validation(self):
        d = json.loads(json.dumps(REPORT))
        self.assertEqual(rg.validate(d), ([], []))
        d["overall"] = "Low"                      # more than one level below max (High)
        self.assertTrue(rg.validate(d)[0])
        d["overall"] = "Medium"                   # one lower: allowed, warned
        e, w = rg.validate(d)
        self.assertEqual(e, [])
        self.assertTrue(w)
        d = json.loads(json.dumps(REPORT))
        d["ratings"]["risk"]["score"] = 6
        self.assertTrue(rg.validate(d)[0])
        d = json.loads(json.dumps(REPORT))
        d["ratings"]["risk"]["score"] = 2         # steps add up to 4
        self.assertTrue(any("add up" in x for x in rg.validate(d)[1]))

    def test_attribute_injection_is_escaped(self):
        d = json.loads(json.dumps(REPORT))
        d["title"] = 'x" onload="alert(1)'
        d["labels"] = {"overall": '" onclick="evil()'}
        svg = rg.render(d)
        self.assertNotIn('" onload="', svg)
        self.assertNotIn('" onclick="', svg)
        import xml.etree.ElementTree as ET
        ET.fromstring(svg)  # still well-formed

    def test_escaping_and_truncation(self):
        d = json.loads(json.dumps(REPORT))
        d["title"] = "<script>alert(1)</script> & co"
        d["ratings"]["risk"]["steps"][0]["label"] = "x" * 100
        svg = rg.render(d)
        self.assertNotIn("<script>", svg)
        self.assertIn("&lt;script&gt;", svg)
        self.assertIn("…", svg)

    def test_hotspots_and_history(self):
        d = json.loads(json.dumps(REPORT))
        d["hotspots"] = [{"path": f"src/f{i}.py", "test_files": i, "coverage": 50 if i else None, "lines": 10 * i + 1}
                         for i in range(4)]
        d["history"] = [{"risk": 5, "business": 3, "change": 3}]
        svg = rg.render(d)
        self.assertIn("Hotspots", svg)
        self.assertIn("stroke-dasharray", svg)
        self.assertIn("earlier runs", svg)

    def test_cli_prev_chain(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            (t / "old.json").write_text(json.dumps({**REPORT, "history": [{"risk": 5, "business": 5, "change": 5}]}))
            new = json.loads(json.dumps(REPORT))
            new["ratings"]["risk"] = {"score": 3, "steps": [{"label": "b", "delta": 3}]}
            (t / "new.json").write_text(json.dumps(new))
            p = subprocess.run([sys.executable, str(ROOT / "skills/risk-impact/render_graphic.py"),
                                str(t / "new.json"), "--prev", str(t / "old.json")],
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertTrue((t / "new.svg").exists())
            hist = json.loads((t / "new.json").read_text())["history"]
            self.assertEqual(hist, [{"risk": 5, "business": 5, "change": 5},
                                    {"risk": 4, "business": 3, "change": 2}])

    def test_cli_invalid_exits_2(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "bad.json").write_text("{}")
            p = subprocess.run([sys.executable, str(ROOT / "skills/risk-impact/render_graphic.py"),
                                str(Path(t) / "bad.json")], capture_output=True, text=True)
            self.assertEqual(p.returncode, 2)


class SkillFileTests(unittest.TestCase):
    def test_frontmatter(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\nname: risk-impact\n"))
        self.assertIn("description:", text.split("---")[1])


if __name__ == "__main__":
    unittest.main()
