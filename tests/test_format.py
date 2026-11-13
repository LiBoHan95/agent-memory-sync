import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from amem import format as fmt
from tests.helpers import note, template_files


class FrontmatterTest(unittest.TestCase):
    def test_parse(self):
        meta, body = fmt.parse_frontmatter("---\na: 1\nb: 'x y'  \n# c\n---\nhi")
        self.assertEqual(meta, {"a": "1", "b": "x y"})
        self.assertEqual(body, "hi")

    def test_no_frontmatter(self):
        self.assertEqual(fmt.parse_frontmatter("hi")[0], None)

    def test_reject_nested_and_unclosed(self):
        for t in ("---\na:\n  b: 1\n---\n", "---\na: 1\n", "---\n- x\n---\n", "---\na: 1\na: 2\n---\n"):
            with self.assertRaises(fmt.FormatError):
                fmt.parse_frontmatter(t)

    def test_validate_note(self):
        _, errs = fmt.validate_note("rules/x.md", note("x").decode())
        self.assertEqual(errs, [])
        _, errs = fmt.validate_note("rules/x.md", note("x", status="superseded").decode())
        self.assertEqual([e.code for e in errs], ["superseded-by-missing"])
        _, errs = fmt.validate_note("rules/x.md", note("x", created="2026-13-01", type="bad").decode())
        self.assertEqual({e.code for e in errs}, {"date-invalid", "type-invalid"})


class VaultTest(unittest.TestCase):
    def test_template_is_valid(self):
        self.assertEqual(fmt.validate_vault(template_files()), [])

    def test_broken_links(self):
        fs = template_files()
        fs["MEMORY.md"] += b"\n- [x](rules/nope.md) \xe2\x80\x94 x\n"
        fs["rules/a.md"] = note("a", "见 [[ghost]] 和 [b](../../etc/passwd)")
        codes = {(i.path, i.code, i.severity) for i in fmt.validate_vault(fs)}
        self.assertIn(("MEMORY.md", "link-broken", "error"), codes)
        self.assertIn(("rules/a.md", "link-broken", "warning"), codes)
        self.assertIn(("rules/a.md", "link-outside", "error"), codes)

    def test_superseded_target_and_dup_name(self):
        fs = template_files()
        fs["rules/a.md"] = note("a", status="superseded", superseded_by="b")
        self.assertIn("superseded-by-broken", {i.code for i in fmt.validate_vault(fs)})
        fs["rules/b.md"] = note("b")
        self.assertEqual(fmt.errors(fmt.validate_vault(fs)), [])
        fs["projects/b.md"] = note("b", type="project")
        self.assertIn("name-duplicate", {i.code for i in fmt.validate_vault(fs)})

    def test_memory_limit(self):
        fs = template_files()
        fs["MEMORY.md"] = b"x\n" * 201
        self.assertIn("memory-too-long", {i.code for i in fmt.validate_vault(fs)})


class LogTest(unittest.TestCase):
    def test_append_and_parse(self):
        d = Path(tempfile.mkdtemp())
        when = datetime(2026, 11, 13, 14, 20)
        rel = fmt.append_log(d, "mac", "codex@mac", [("fact", "a\nb")], when)
        fmt.append_log(d, "mac", "codex@mac", [("correction", "c")], when)
        self.assertEqual(rel, "log/2026-11.mac.md")
        items, issues = fmt.parse_log(rel, (d / rel).read_text())
        self.assertEqual([(i.tag, i.text) for i in items], [("fact", "a b"), ("correction", "c")])
        self.assertEqual(issues, [])

    def test_bad_input(self):
        with self.assertRaises(ValueError):
            fmt.log_path("../x")
        with self.assertRaises(ValueError):
            fmt.format_log_entry("codex@mac", [("nope", "x")])


if __name__ == "__main__":
    unittest.main()
