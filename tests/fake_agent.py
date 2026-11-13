"""测试用 headless agent：在 cwd/vault 里做一组改动，其中有合法的，也有越权的。"""
import os
import sys
from pathlib import Path

assert len(sys.argv) == 2 and "无人值守" in sys.argv[1]
mode = os.environ.get("FAKE_MODE", "normal")
if mode == "fail":
    sys.exit(3)
v = Path("vault")
assert "用中文" in Path("input.md").read_text()
head = "---\nschema_version: 1\nname: {n}\ndescription: d\ntype: {t}\nstatus: active\n" \
       "source: dreaming@mac\ncreated: 2026-11-13\nupdated: 2026-11-13\n---\n{b}\n"
(v / "projects/demo.md").write_text(head.format(n="demo", t="project", b="项目背景"))
(v / "rules/answer-first.md").write_text(head.format(n="answer-first", t="rule", b="先给结论"))
(v / "AGENT_PROTOCOL.md").unlink()
(v / "evil.sh").write_text("rm -rf ~")
if mode == "secret":
    (v / "projects/demo.md").write_text(head.format(n="demo", t="project", b="ghp_" + "a" * 36))
print('{"result": "新增 1 条项目，1 条规则转提案"}')
