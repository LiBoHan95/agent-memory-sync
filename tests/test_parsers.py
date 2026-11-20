import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from amem.parsers.base import read_head_lines
from amem.parsers.claude_code import ClaudeCodeParser
from amem.parsers.codex import CodexParser
from amem.parsers.dsh import DshParser
from amem.parsers.pi import PiParser


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


class CodexTest(unittest.TestCase):
    NAME = "rollout-2026-10-16T15-14-39-11111111-2222-3333-4444-555555555555.jsonl"

    def write(self, entries, tail=b"", name=None):
        p = Path(tempfile.mkdtemp(), name or self.NAME)
        p.write_bytes(b"".join(json.dumps(e).encode() + b"\n" for e in entries) + tail)
        return p

    @staticmethod
    def entry(kind, payload, ts="2026-10-16T07:14:39Z"):
        return {"timestamp": ts, "type": kind, "payload": payload}

    def test_filters_noise(self):
        entries = [
            self.entry("session_meta", {"session_id": "s1", "id": "s1", "cwd": "/w"}),
            self.entry("response_item", {"type": "message", "role": "developer",
                                         "content": [{"type": "input_text", "text": "skills"}]}),
            self.entry("response_item", {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "# AGENTS.md instructions for /w"}]}),
            self.entry("response_item", {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "用中文"}]}),
            self.entry("response_item", {"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "好的"}]}),
            self.entry("response_item", {"type": "function_call", "name": "shell"}),
            self.entry("event_msg", {"type": "task_complete", "last_agent_message": "好的"}),
        ]
        msgs, _ = CodexParser().parse(self.write(entries), 0, device="mac", rel="r")
        self.assertEqual([(m.role, m.text, m.session, m.cwd, m.origin) for m in msgs], [
            ("user", "用中文", "s1", "/w", "human"),
            ("assistant", "好的", "s1", "/w", "human")])

    def test_skips_subagent_session(self):
        entries = [
            self.entry("session_meta", {"thread_source": "subagent",
                                        "source": {"subagent": {}}, "cwd": "/w"}),
            self.entry("response_item", {"type": "message", "role": "user",
                                         "content": [{"type": "input_text", "text": "x"}]}),
        ]
        msgs, off = CodexParser().parse(self.write(entries), 0, device="mac", rel="r")
        self.assertEqual(msgs, [])
        self.assertGreater(off, 0)

    def test_incremental_uses_filename_session(self):
        p = self.write([], name=self.NAME)
        with open(p, "ab") as f:
            f.write(json.dumps(self.entry("response_item", {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "增量"}]})).encode() + b"\n")
        parser = CodexParser()
        msgs, off = parser.parse(p, 0, device="mac", rel="r")
        self.assertEqual([m.text for m in msgs], ["增量"])
        self.assertEqual(msgs[0].session, "11111111-2222-3333-4444-555555555555")
        self.assertEqual(parser.parse(p, off, device="mac", rel="r")[0], [])


class PiTest(unittest.TestCase):
    NAME = "2026-09-08T03-05-42-102Z_01a07efa-83d6-7a7e-a5da-8aea38314555.jsonl"

    def write(self, entries, tail=b""):
        p = Path(tempfile.mkdtemp(), self.NAME)
        p.write_bytes(b"".join(json.dumps(e).encode() + b"\n" for e in entries) + tail)
        return p

    def test_filters_noise(self):
        entries = [
            {"type": "session", "id": "s1", "cwd": "/w", "timestamp": "2026-09-08T03:05:42Z"},
            {"type": "model_change", "provider": "deepseek"},
            {"type": "message", "timestamp": "t1", "message": {
                "role": "user", "content": [{"type": "text", "text": "编译"}]}},
            {"type": "message", "timestamp": "t2", "message": {
                "role": "assistant", "content": [
                    {"type": "thinking", "thinking": "x"},
                    {"type": "text", "text": "好的"},
                    {"type": "toolCall", "name": "bash"}]}},
            {"type": "message", "timestamp": "t3", "message": {
                "role": "toolResult", "content": [{"type": "text", "text": "out"}]}},
            {"type": "message", "timestamp": "t4", "message": {
                "role": "user", "content": [{"type": "text", "text": "[pi-web-access] 提示"}]}},
            {"type": "compaction", "timestamp": "t5"},
        ]
        msgs, _ = PiParser().parse(self.write(entries), 0, device="mac", rel="r")
        self.assertEqual([(m.role, m.text, m.session, m.cwd, m.origin) for m in msgs], [
            ("user", "编译", "s1", "/w", "human"),
            ("assistant", "好的", "s1", "/w", "human")])

    def test_incremental_and_partial_line(self):
        p = self.write([
            {"type": "session", "id": "s1", "cwd": "/w", "timestamp": "t0"},
            {"type": "message", "timestamp": "t1", "message": {
                "role": "user", "content": [{"type": "text", "text": "一"}]}},
        ], tail=b'{"type": "message", "mess')
        parser = PiParser()
        msgs, off = parser.parse(p, 0, device="mac", rel="r")
        self.assertEqual([m.text for m in msgs], ["一"])
        with open(p, "ab") as f:
            f.write(b'age": {"role": "user", "content": [{"type": "text", "text": "\xe4\xba\x8c"}]}}\n')
        msgs, off2 = parser.parse(p, off, device="mac", rel="r")
        self.assertEqual([m.text for m in msgs], ["二"])
        self.assertEqual(off2, p.stat().st_size)

    def test_skips_dream_workdir(self):
        entries = [
            {"type": "session", "id": "s1", "cwd": "/tmp/amem-dream-abc", "timestamp": "t0"},
            {"type": "message", "timestamp": "t1", "message": {
                "role": "user", "content": [{"type": "text", "text": "x"}]}},
        ]
        msgs, _ = PiParser().parse(self.write(entries), 0, device="mac", rel="r")
        self.assertEqual(msgs, [])


@unittest.skipUnless(shutil.which("zstd"), "需要 zstd")
class DshTest(unittest.TestCase):
    NAME = "session.v4.jsonl.zstd"

    def write(self, entries):
        p = Path(tempfile.mkdtemp(), self.NAME)
        data = b"".join(json.dumps(e).encode() + b"\n" for e in entries)
        r = subprocess.run(["zstd", "-q", "-f", "-o", str(p), "-"], input=data,
                           capture_output=True)
        assert r.returncode == 0, r.stderr
        return p

    def test_filters_injections_and_tool_calls(self):
        entries = [
            {"type": "session", "id": "s1", "cwd": "/w", "createdAt": 1760000000000},
            {"type": "user/message", "seq": 1, "time": 1760000001000,
             "data": {"role": "user", "source": {"kind": "user"},
                      "content": [{"type": "text", "text": "用中文"}]}},
            {"type": "user/message", "seq": 2, "time": 1760000002000,
             "data": {"role": "user", "source": {"kind": "runtime-context"},
                      "content": [{"type": "text", "text": "Current runtime context"}]}},
            {"type": "assistant/message", "seq": 3, "time": 1760000003000,
             "data": {"message": {"role": "assistant", "content": [
                 {"type": "reasoning", "text": "想了很久"},
                 {"type": "text", "text": "好的"},
                 {"type": "tool-call", "name": "bash"}]}}},
            {"type": "system/message", "seq": 4, "time": 1760000004000,
             "data": {"message": {"role": "system", "content": [
                 {"type": "text", "text": "系统注入"}]}}},
            {"type": "user/message", "seq": 5, "time": 1760000005000,
             "data": {"role": "user", "source": {"kind": "user"},
                      "content": [{"type": "text", "text": "<system-reminder>x"}]}},
            {"type": "user/message", "seq": 6, "time": 1760000006000,
             "data": {"role": "user",
                      "content": [{"type": "text", "text": "缺 source 的注入"}]}},
        ]
        msgs, _ = DshParser().parse(self.write(entries), 0, device="mac", rel="r")
        self.assertEqual([(m.role, m.text, m.session, m.cwd, m.origin) for m in msgs], [
            ("user", "用中文", "s1", "/w", "human"),
            ("assistant", "好的", "s1", "/w", "human")])
        self.assertTrue(msgs[0].timestamp.startswith("2025-10-"))


    def test_bad_timestamps_do_not_crash(self):
        entries = [
            {"type": "session", "id": "s1", "cwd": "/w", "createdAt": 0},
            {"type": "user/message", "seq": 1, "time": float("nan"),
             "data": {"role": "user", "source": {"kind": "user"},
                      "content": [{"type": "text", "text": "一"}]}},
            {"type": "user/message", "seq": 2, "time": 10**30,
             "data": {"role": "user", "source": {"kind": "user"},
                      "content": [{"type": "text", "text": "二"}]}},
        ]
        msgs, _ = DshParser().parse(self.write(entries), 0, device="mac", rel="r")
        self.assertEqual([m.text for m in msgs], ["一", "二"])


@unittest.skipUnless(shutil.which("zstd"), "需要 zstd")
class CompressedTest(unittest.TestCase):
    def test_claude_jsonl_zst_and_offset(self):
        entries = [
            {"type": "user", "message": {"role": "user", "content": "用中文"},
             "timestamp": "2026-11-13T06:00:00Z", "sessionId": "s1", "cwd": "/w"},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "好的"}]},
             "timestamp": "2026-11-13T06:00:01Z", "sessionId": "s1", "cwd": "/w"},
        ]
        p = Path(tempfile.mkdtemp(), "s.jsonl.zst")
        data = b"".join(json.dumps(e).encode() + b"\n" for e in entries)
        r = subprocess.run(["zstd", "-q", "-f", "-o", str(p), "-"], input=data,
                           capture_output=True)
        assert r.returncode == 0, r.stderr
        parser = ClaudeCodeParser()
        self.assertTrue(parser.matches(p))
        msgs, off = parser.parse(p, 0, device="mac", rel="r")
        self.assertEqual([m.text for m in msgs], ["用中文", "好的"])
        self.assertEqual(off, 2)  # 压缩文件用已消费行数，而不是压缩字节数
        self.assertEqual(parser.parse(p, off, device="mac", rel="r")[0], [])

    def test_append_only_zstd_reads_only_new_lines(self):
        first = [
            {"type": "user", "message": {"role": "user", "content": "Q1"},
             "timestamp": "t1", "sessionId": "s1", "cwd": "/w"},
            {"type": "assistant", "message": {"role": "assistant", "content": "A1"},
             "timestamp": "t2", "sessionId": "s1", "cwd": "/w"},
        ]
        second = [{"type": "user", "message": {"role": "user", "content": "Q2"},
                   "timestamp": "t3", "sessionId": "s1", "cwd": "/w"}]
        p = Path(tempfile.mkdtemp(), "s.jsonl.zst")
        parser = ClaudeCodeParser()
        for entries, append in ((first, False), (second, True)):
            data = b"".join(json.dumps(e).encode() + b"\n" for e in entries)
            frame = subprocess.run(["zstd", "-q", "-c", "-"], input=data,
                                   capture_output=True, check=True).stdout
            with open(p, "ab" if append else "wb") as f:
                f.write(frame)
            if not append:
                msgs, off = parser.parse(p, 0, device="mac", rel="r")
                self.assertEqual([m.text for m in msgs], ["Q1", "A1"])
                self.assertEqual(off, 2)
            else:
                msgs, off = parser.parse(p, off, device="mac", rel="r")
                self.assertEqual([m.text for m in msgs], ["Q2"])
                self.assertEqual(off, 3)

    def test_incomplete_tail_is_not_counted_as_consumed(self):
        first = {"type": "user", "message": {"role": "user", "content": "Q1"},
                 "timestamp": "t1", "sessionId": "s1", "cwd": "/w"}
        second = {"type": "user", "message": {"role": "user", "content": "Q2"},
                  "timestamp": "t2", "sessionId": "s1", "cwd": "/w"}
        second_bytes = json.dumps(second).encode()
        cut = len(second_bytes) // 2
        p = Path(tempfile.mkdtemp(), "s.jsonl.zst")
        parser = ClaudeCodeParser()

        def append_frame(data: bytes, mode: str) -> None:
            frame = subprocess.run(["zstd", "-q", "-c", "-"], input=data,
                                   capture_output=True, check=True).stdout
            with open(p, mode) as f:
                f.write(frame)

        append_frame(json.dumps(first).encode() + b"\n" + second_bytes[:cut], "wb")
        msgs, off = parser.parse(p, 0, device="mac", rel="r")
        self.assertEqual([m.text for m in msgs], ["Q1"])
        self.assertEqual(off, 1)  # 半行不算已消费

        append_frame(second_bytes[cut:] + b"\n", "ab")
        msgs, off = parser.parse(p, off, device="mac", rel="r")
        self.assertEqual([m.text for m in msgs], ["Q2"])
        self.assertEqual(off, 2)

    def test_read_head_lines_tolerates_truncated_tail(self):
        line = json.dumps({"type": "session", "cwd": "/w"}).encode() + b"\n"
        frame = subprocess.run(["zstd", "-q", "-c", "-"], input=line,
                               capture_output=True, check=True).stdout
        p = Path(tempfile.mkdtemp(), "partial.jsonl.zst")
        # 合法前缀 + 下一帧的前半段，模拟 DSH 正在写入
        p.write_bytes(frame + frame[:len(frame) // 2])
        head = read_head_lines(p, 20)
        self.assertIn(line, head)

    def test_read_head_lines_reports_corrupt_zstd(self):
        p = Path(tempfile.mkdtemp(), "bad.jsonl.zst")
        p.write_bytes(b"not zstd")
        with self.assertRaises(RuntimeError):
            read_head_lines(p, 5)

    def test_filename_fallback_strips_compressed_suffix(self):
        uuid = "12345678-1234-1234-1234-123456789abc"
        cases = [
            (f"rollout-2026-11-13T06-00-00-{uuid}.jsonl.zst", CodexParser(), [
                {"type": "response_item", "payload": {
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": "hi"}]}}]),
            (f"2026-11-13T06-00-00_{uuid}.jsonl.zst", PiParser(), [
                {"type": "message", "message": {
                    "role": "user", "content": [{"type": "text", "text": "hi"}]}}]),
        ]
        for name, parser, entries in cases:
            p = Path(tempfile.mkdtemp(), name)
            data = b"".join(json.dumps(e).encode() + b"\n" for e in entries)
            subprocess.run(["zstd", "-q", "-f", "-o", str(p), "-"], input=data,
                           capture_output=True, check=True)
            msgs, _ = parser.parse(p, 0, device="mac", rel="r")
            self.assertEqual(msgs[0].session, uuid, name)


if __name__ == "__main__":
    unittest.main()
