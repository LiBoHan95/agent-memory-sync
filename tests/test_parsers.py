import json
import tempfile
import unittest
from pathlib import Path

from amem.parsers.claude_code import ClaudeCodeParser


def u(content, **kw):
    return {"type": "user", "message": {"role": "user", "content": content},
            "timestamp": "2026-11-13T06:00:00Z", "sessionId": "s1", "cwd": "/w", **kw}


def a(blocks, **kw):
    return {"type": "assistant", "message": {"role": "assistant", "content": blocks},
            "timestamp": "2026-11-13T06:00:01Z", "sessionId": "s1", **kw}


class ClaudeCodeTest(unittest.TestCase):
    def write(self, entries, tail=b""):
        p = Path(tempfile.mkdtemp(), "s.jsonl")
        p.write_bytes(b"".join(json.dumps(e).encode() + b"\n" for e in entries) + tail)
        return p

    def test_filters_noise(self):
        entries = [
            u("用中文回答", promptSource="typed", origin={"kind": "human"}),
            u([{"type": "text", "text": "看这张图"}, {"type": "image"}]),
            u([{"type": "tool_result", "content": "x"}]),
            u("summary", isCompactSummary=True),
            u("meta", isMeta=True),
            u("done", origin={"kind": "task-notification"}),
            u("hi", origin={"kind": "peer"}),
            u("injected", promptSource="system"),
            u("<command-name>/clear</command-name>"),
            u("[Request interrupted by user for tool use]"),
            u("sub", isSidechain=True),
            u("dream", cwd="/tmp/amem-dream-abc"),
            u([{"type": "text", "text": "你是一名资深工程师"}], promptSource="sdk", entrypoint="sdk-cli"),
            a([{"type": "thinking", "thinking": "x"}, {"type": "tool_use"}]),
            a([{"type": "text", "text": "好的"}]),
            {"type": "attachment"}, {"type": "file-history-snapshot"},
        ]
        msgs, _ = ClaudeCodeParser().parse(self.write(entries), 0, device="mac", rel="r")
        self.assertEqual([(m.role, m.text, m.origin) for m in msgs], [
            ("user", "用中文回答", "human"), ("user", "看这张图", "human"),
            ("user", "你是一名资深工程师", "automated"), ("assistant", "好的", "human")])

    def test_incremental_and_partial_line(self):
        p = self.write([u("一")], tail=b'{"type": "user", "mess')
        parser = ClaudeCodeParser()
        msgs, off = parser.parse(p, 0, device="mac", rel="r")
        self.assertEqual([m.text for m in msgs], ["一"])
        with open(p, "ab") as f:
            f.write(b'age": {"content": "\xe4\xba\x8c"}}\n')
        msgs, off2 = parser.parse(p, off, device="mac", rel="r")
        self.assertEqual([m.text for m in msgs], ["二"])
        self.assertEqual(off2, p.stat().st_size)
        self.assertEqual(parser.parse(p, off2, device="mac", rel="r")[0], [])
        msgs, _ = parser.parse(p, off2 + 999, device="mac", rel="r")  # 文件被替换变短
        self.assertEqual(len(msgs), 2)


if __name__ == "__main__":
    unittest.main()
