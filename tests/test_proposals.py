"""提案解析与批准/驳回写回的测试。"""
from __future__ import annotations

import difflib
import tempfile
import unittest
from pathlib import Path

from amem import proposals

RULE_TEXT = """---
schema_version: 1
name: change-scope-not-redo
description: 改动和删除以用户点名的部分为界
type: rule
status: active
source: dreaming@device-a
created: 2026-11-18
updated: 2026-11-18
superseded_by:
---
用户点名「删掉某个步骤」时，只动那一步。
"""

OLD_TEXT = RULE_TEXT.replace(
    "用户点名「删掉某个步骤」时，只动那一步。\n",
    "用户点名「删掉某个步骤」时，只动那一步。\n\n**How to apply:** 保留未点名的部分。\n")


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_section(index: int, action: str, target: str, content: str,
                 old: str | None = None, status: str = "pending") -> str:
    diff = difflib.unified_diff(
        old.splitlines() if old is not None else [],
        content.splitlines(),
        f"a/{target}", f"b/{target}", lineterm="")
    return (f"## {index}. {action} `{target}` · {status}\n\n"
            "````diff\n" + "\n".join(diff) + "\n````\n")


def make_proposal(*sections: str) -> str:
    return "# 整理提案 2026-11-18\n\n" + "\n".join(sections)


class ParseTest(unittest.TestCase):
    def test_parse_add_and_modify(self):
        text = make_proposal(
            make_section(1, "新增", "rules/scope.md", RULE_TEXT),
            make_section(2, "修改", "profile/user.md", OLD_TEXT, old=OLD_TEXT,
                         status="accepted"))
        sections = proposals.parse_sections(text)
        self.assertEqual([s.index for s in sections], [1, 2])
        self.assertEqual((sections[0].kind, sections[0].target, sections[0].status),
                         ("add", "rules/scope.md", "pending"))
        self.assertEqual((sections[1].kind, sections[1].status), ("modify", "accepted"))
        self.assertIsNotNone(sections[0].diff)
        self.assertEqual(proposals.section_summary(sections[0]), "新增 rules/scope.md")

    def test_parse_section_without_diff(self):
        sections = proposals.parse_sections(
            "# 提案\n\n## 1. 新增 `rules/x.md` · pending\n")
        self.assertEqual(len(sections), 1)
        self.assertIsNone(sections[0].diff)

    def test_parse_ignores_unrelated_headings(self):
        sections = proposals.parse_sections(
            "# 整理提案 2026-11-18\n\n## 小结\n\n没有条目标题。\n")
        self.assertEqual(sections, [])


class ApplyTest(unittest.TestCase):
    def test_apply_add(self):
        sec = proposals.parse_sections(
            make_proposal(make_section(1, "新增", "rules/scope.md", RULE_TEXT)))[0]
        self.assertEqual(proposals.apply_diff(None, sec), RULE_TEXT)

    def test_apply_modify(self):
        new_text = OLD_TEXT.replace("只动那一步。", "只动那一步，其余保留。")
        sec = proposals.parse_sections(
            make_proposal(make_section(1, "修改", "rules/scope.md", new_text,
                                       old=OLD_TEXT)))[0]
        self.assertEqual(proposals.apply_diff(OLD_TEXT, sec), new_text)

    def test_apply_conflict_when_file_changed(self):
        new_text = OLD_TEXT + "\n新增一行。\n"
        sec = proposals.parse_sections(
            make_proposal(make_section(1, "修改", "rules/scope.md", new_text,
                                       old=OLD_TEXT)))[0]
        with self.assertRaises(proposals.ProposalConflict):
            proposals.apply_diff(OLD_TEXT.replace("保留未点名的部分", "换了内容"), sec)

    def test_apply_add_requires_absent_target(self):
        sec = proposals.parse_sections(
            make_proposal(make_section(1, "新增", "rules/scope.md", RULE_TEXT)))[0]
        with self.assertRaises(proposals.ProposalConflict):
            proposals.apply_diff(OLD_TEXT, sec)


class DecideTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / "vault"
        self.vault.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def proposal(self, name: str = "2026-11-18.md") -> Path:
        return self.vault / "proposals" / name

    def test_accept_add_writes_rule_and_marks_accepted(self):
        write(self.proposal(), make_proposal(
            make_section(1, "新增", "rules/scope.md", RULE_TEXT),
            make_section(2, "新增", "rules/other.md", RULE_TEXT)))
        result = proposals.decide(self.vault, "2026-11-18.md", 1, "accept")
        self.assertEqual((result.action, result.target),
                         ("accept", "rules/scope.md"))
        self.assertEqual((self.vault / "rules" / "scope.md").read_text(), RULE_TEXT)
        self.assertFalse((self.vault / "rules" / "other.md").exists())
        text = self.proposal().read_text()
        self.assertIn("## 1. 新增 `rules/scope.md` · accepted", text)
        self.assertIn("## 2. 新增 `rules/other.md` · pending", text)

    def test_accept_modify_applies_patch(self):
        write(self.vault / "rules" / "scope.md", OLD_TEXT)
        new_text = OLD_TEXT + "\n新增一行说明。\n"
        write(self.proposal(), make_proposal(
            make_section(1, "修改", "rules/scope.md", new_text, old=OLD_TEXT)))
        proposals.decide(self.vault, "2026-11-18.md", 1, "accept")
        self.assertEqual((self.vault / "rules" / "scope.md").read_text(), new_text)
        self.assertIn("· accepted", self.proposal().read_text())

    def test_reject_only_marks_status(self):
        write(self.proposal(), make_proposal(
            make_section(1, "新增", "rules/scope.md", RULE_TEXT)))
        result = proposals.decide(self.vault, "2026-11-18.md", 1, "reject")
        self.assertEqual(result.action, "reject")
        self.assertFalse((self.vault / "rules" / "scope.md").exists())
        self.assertIn("## 1. 新增 `rules/scope.md` · rejected",
                      self.proposal().read_text())

    def test_conflict_keeps_proposal_pending(self):
        write(self.vault / "rules" / "scope.md", OLD_TEXT)
        new_text = OLD_TEXT + "\n新增一行。\n"
        write(self.proposal(), make_proposal(
            make_section(1, "修改", "rules/scope.md", new_text, old=OLD_TEXT)))
        write(self.vault / "rules" / "scope.md", "完全不同的内容\n")
        with self.assertRaises(proposals.ProposalConflict):
            proposals.decide(self.vault, "2026-11-18.md", 1, "accept")
        self.assertEqual((self.vault / "rules" / "scope.md").read_text(), "完全不同的内容\n")
        self.assertIn("· pending", self.proposal().read_text())

    def test_add_existing_target_is_conflict(self):
        write(self.vault / "rules" / "scope.md", OLD_TEXT)
        write(self.proposal(), make_proposal(
            make_section(1, "新增", "rules/scope.md", RULE_TEXT)))
        with self.assertRaises(proposals.ProposalConflict):
            proposals.decide(self.vault, "2026-11-18.md", 1, "accept")
        self.assertEqual((self.vault / "rules" / "scope.md").read_text(), OLD_TEXT)

    def test_already_decided_is_conflict(self):
        write(self.proposal(), make_proposal(
            make_section(1, "新增", "rules/scope.md", RULE_TEXT, status="accepted")))
        with self.assertRaises(proposals.ProposalConflict):
            proposals.decide(self.vault, "2026-11-18.md", 1, "reject")

    def test_missing_section_is_not_found(self):
        write(self.proposal(), make_proposal(
            make_section(1, "新增", "rules/scope.md", RULE_TEXT)))
        with self.assertRaises(proposals.ProposalNotFound):
            proposals.decide(self.vault, "2026-11-18.md", 9, "accept")
        with self.assertRaises(proposals.ProposalNotFound):
            proposals.decide(self.vault, "../MEMORY.md", 1, "accept")

    def test_section_without_diff_cannot_be_accepted(self):
        write(self.proposal(), "# 提案\n\n## 1. 新增 `rules/scope.md` · pending\n")
        with self.assertRaises(proposals.ProposalError):
            proposals.decide(self.vault, "2026-11-18.md", 1, "accept")
        self.assertFalse((self.vault / "rules" / "scope.md").exists())
        self.assertIn("· pending", self.proposal().read_text())

    def test_outside_target_rejected(self):
        for target in ("projects/x.md", "MEMORY.md", "rules/../MEMORY.md",
                       "/etc/passwd", "rules/x.txt"):
            with self.subTest(target=target):
                text = make_proposal(make_section(1, "新增", target, RULE_TEXT))
                write(self.proposal(), text)
                with self.assertRaises(proposals.ProposalError):
                    proposals.decide(self.vault, "2026-11-18.md", 1, "accept")
                self.assertIn("· pending", self.proposal().read_text())

    def test_invalid_note_format_rejected(self):
        write(self.proposal(), make_proposal(
            make_section(1, "新增", "rules/bad.md", "# 没有 frontmatter\n")))
        with self.assertRaises(proposals.ProposalError):
            proposals.decide(self.vault, "2026-11-18.md", 1, "accept")
        self.assertFalse((self.vault / "rules" / "bad.md").exists())

    def test_secret_content_rejected(self):
        bad = RULE_TEXT + '\napi_key = "abcdefghijklmnop"\n'
        write(self.proposal(), make_proposal(
            make_section(1, "新增", "rules/bad.md", bad)))
        with self.assertRaises(proposals.ProposalError):
            proposals.decide(self.vault, "2026-11-18.md", 1, "accept")
        self.assertFalse((self.vault / "rules" / "bad.md").exists())

    def test_symlink_target_rejected(self):
        outside = Path(self.tmp.name) / "outside.md"
        outside.write_text("外部文件\n", encoding="utf-8")
        (self.vault / "rules").mkdir(parents=True)
        (self.vault / "rules" / "link.md").symlink_to(outside)
        write(self.proposal(), make_proposal(
            make_section(1, "新增", "rules/link.md", RULE_TEXT)))
        with self.assertRaises(proposals.ProposalError):
            proposals.decide(self.vault, "2026-11-18.md", 1, "accept")
        self.assertEqual(outside.read_text(), "外部文件\n")

    def test_preview_reports_conflict(self):
        write(self.vault / "rules" / "scope.md", "别的内容\n")
        new_text = OLD_TEXT + "\n新增一行。\n"
        sec = proposals.parse_sections(make_proposal(
            make_section(1, "修改", "rules/scope.md", new_text, old=OLD_TEXT)))[0]
        content, problem = proposals.preview(self.vault, sec)
        self.assertIsNone(content)
        self.assertIn("不一致", problem or "")

    def test_preview_returns_new_content(self):
        sec = proposals.parse_sections(make_proposal(
            make_section(1, "新增", "rules/scope.md", RULE_TEXT)))[0]
        content, problem = proposals.preview(self.vault, sec)
        self.assertEqual(content, RULE_TEXT)
        self.assertIsNone(problem)


if __name__ == "__main__":
    unittest.main()
