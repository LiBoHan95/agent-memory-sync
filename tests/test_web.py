"""只读 Web 的渲染、数据加载、路由、认证与路径安全测试。"""
from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from amem import web
from amem.render import render_markdown

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "web.sh"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_vault(tmp: Path) -> tuple[Path, Path, Path]:
    vault = tmp / "vault"
    write(vault / "MEMORY.md", """# 记忆索引

- [用户](profile/user.md) — 用户画像
- [[pref]] — 偏好
""")
    write(vault / "profile" / "user.md", """---
name: user-name
description: 用户画像
status: active
updated: 2026-11-01
---

# 用户画像

喜欢 **简洁** 的中文回答，见 [[pref]]。

<script>alert(1)</script>

[坏链接](javascript:alert(1))
[好链接](https://example.com/x?a=1&b=2)
""")
    write(vault / "profile" / "pref.md", """---
name: pref
description: 偏好
status: active
updated: 2026-11-02
---

# 偏好

| 项 | 值 |
|---|---|
| 语言 | 中文 |

```python
print('<x>')
```
""")
    write(vault / "log" / "2026-11.device-a.md",
          "## 2026-11-12 10:00 · claude@device-a\n- [fact] 第一条 <script>alert(1)</script>\n"
          "## 2026-11-13 11:00 · codex@device-a\n- [preference] 第二条\n")
    write(vault / "log" / "2026-11.device-b.md",
          "## 2026-11-10 09:00 · pi@device-b\n- [project] 项目记录\n")
    write(vault / "log" / "2026-11.device-a.sync-conflict-20261113-120000-ABCD.md",
          "## 2026-11-13 12:00 · claude@device-a\n- [fact] 冲突副本\n")
    write(vault / "proposals" / "2026-11-12.md",
          "# 提案\n\n## 1. 新增 `profile/a.md` · pending\n\n## 2. 修改 `rules/b.md` · accepted\n")
    write(vault / "proposals" / "2026-11-13.md",
          "# 提案\n\n## 1. 新增 `rules/c.md` · pending\n")
    write(vault / ".dream-state", json.dumps({"last_run": "2026-11-13T04:30:00+08:00"}))

    archive = tmp / "archive"
    write(archive / "device-a" / "claude-code" / "one.jsonl", "{}")
    write(archive / "device-a" / "codex" / "two.jsonl", "{}\n")
    write(archive / "device-b" / "pi" / "three.jsonl", "{}\n")

    reports = tmp / "dream-reports"
    write(reports / "2026-11-12-043000.md", "# 报告一\n")
    write(reports / "2026-11-13-043000.md", "# 报告二\n")
    os.utime(reports / "2026-11-12-043000.md", (1000, 1000))
    os.utime(reports / "2026-11-13-043000.md", (2000, 2000))
    return vault, archive, reports


class RenderTest(unittest.TestCase):
    def test_raw_html_is_escaped(self):
        out = render_markdown("<script>alert(1)</script>\n\n<b>hi</b>")
        self.assertNotIn("<script", out.lower())
        self.assertIn("&lt;script&gt;", out)
        self.assertIn("&lt;b&gt;hi&lt;/b&gt;", out)

    def test_blocks(self):
        out = render_markdown("# 标题\n\n- a\n- b\n\n1. c\n\n> 引用\n\n---\n")
        for tag in ("<h1>标题</h1>", "<ul><li>a</li><li>b</li></ul>",
                    "<ol><li>c</li></ol>", "<blockquote>", "<hr>"):
            self.assertIn(tag, out)

    def test_code_and_table(self):
        out = render_markdown("| 项 | 值 |\n|---|---|\n| a | <x> |\n\n```py\nprint('<x>')\n```\n")
        self.assertIn("<table>", out)
        self.assertIn("&lt;x&gt;", out)
        self.assertIn('<code class="language-py">', out)
        self.assertNotIn("<x>", out)

    def test_fence_info_string_with_spaces(self):
        out = render_markdown('```js title="x"\ncode\n```\n\n# After')
        self.assertIn('<code class="language-js">', out)
        self.assertIn("code", out)
        self.assertIn("<h1>After</h1>", out)

    def test_heading_keeps_trailing_hash_without_space(self):
        self.assertIn("<h1>C#</h1>", render_markdown("# C#"))
        self.assertIn("<h1>Title</h1>", render_markdown("# Title ###"))

    def test_inline(self):
        out = render_markdown("**粗** 和 *斜* 和 `a<b`")
        self.assertIn("<strong>粗</strong>", out)
        self.assertIn("<em>斜</em>", out)
        self.assertIn("<code>a&lt;b</code>", out)

    def test_emphasis_edges(self):
        out = render_markdown("2 * 3 * 4 和 ***都很重要***")
        self.assertNotIn("<em>", out.replace("<em>都很重要</em>", ""))
        self.assertIn("<strong><em>都很重要</em></strong>", out)
        self.assertIn("2 * 3 * 4", out)

    def test_emphasis_does_not_corrupt_link_href(self):
        out = render_markdown("[链接](https://example.com/a*b*c)")
        self.assertIn("a&#42;b&#42;c", out)
        self.assertNotIn("<em>", out)

    def test_list_continuation_stays_in_item(self):
        out = render_markdown("- 第一行\n  续行\n- 第二项")
        self.assertEqual(
            out, "<ul><li>第一行 续行</li><li>第二项</li></ul>")

    def test_link_sanitizing(self):
        out = render_markdown(
            "[a](javascript:alert(1)) [b](data:text/html,x) [c](file:///etc/passwd) "
            "[d](//evil.example/x) [e](https://example.com/x?a=1&b=2)")
        self.assertNotIn("javascript:", out.lower())
        self.assertNotIn("data:", out.lower())
        self.assertNotIn("file:", out.lower())
        self.assertNotIn("evil.example", out)
        self.assertIn("https://example.com/x?a=1&amp;b=2", out)

    def test_scheme_allowlist_and_backslash(self):
        out = render_markdown(
            "[a](ftp://example.com/x) [b](blob:https://example.com/y) "
            "[c](\\\\evil.example/phish) [d](\\/evil.example/phish) "
            "[e](https://ok.example/x) [f](mailto:a@example.com)")
        self.assertNotIn("ftp://", out)
        self.assertNotIn("blob:", out)
        self.assertNotIn("evil.example", out)
        self.assertIn("https://ok.example/x", out)
        self.assertIn("mailto:a@example.com", out)

    def test_control_char_scheme_rejected(self):
        out = render_markdown("[x](\x01javascript:alert(1))")
        self.assertNotIn("javascript:", out.lower())
        self.assertIn("x", out)

    def test_relative_links_and_wikilinks(self):
        links = {"pref": "/memory/profile/pref.md"}

        def linker(path: str) -> str | None:
            return "/memory/" + path if path == "profile/pref.md" else None

        out = render_markdown("[p](pref.md) [[pref]] [[ghost]]", base_path="profile/user.md",
                              wikilinks=links, path_linker=linker)
        self.assertIn('href="/memory/profile/pref.md">p</a>', out)
        self.assertIn('href="/memory/profile/pref.md">pref</a>', out)
        self.assertIn('<span class="wikilink">ghost</span>', out)

    def test_wikilink_alias_and_anchor(self):
        links = {"pref": "/memory/profile/pref.md"}
        out = render_markdown("[[pref|别名]] [[pref#使用 说明]] [[ghost#节]]",
                              wikilinks=links)
        self.assertIn('href="/memory/profile/pref.md">别名</a>', out)
        self.assertIn('href="/memory/profile/pref.md#%E4%BD%BF%E7%94%A8%20%E8%AF%B4%E6%98%8E">pref</a>', out)
        self.assertIn('<span class="wikilink">ghost</span>', out)

    def test_absolute_and_outside_links_rejected(self):
        out = render_markdown("[a](/etc/passwd) [b](../x.md) [c](~/x.md)",
                              base_path="profile/user.md", path_linker=lambda p: "/memory/" + p)
        self.assertNotIn("/etc/passwd", out)
        self.assertNotIn("../x.md", out)
        self.assertIn("a", out)

    def test_nul_does_not_crash(self):
        self.assertIn("x", render_markdown("x\x00y"))

    def test_pathological_long_lines_render_quickly(self):
        start = time.monotonic()
        render_markdown("*a " * 20000)
        render_markdown("](x" * 20000)
        render_markdown("# " + "a" * 16000 + " " * 16000 + "b")
        self.assertLess(time.monotonic() - start, 2.0)


class WebDataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault, self.archive, self.reports = make_vault(Path(self.tmp.name))
        web.clear_caches()

    def tearDown(self):
        web.clear_caches()
        self.tmp.cleanup()

    def test_notes(self):
        notes = web.load_notes(self.vault)
        self.assertEqual(sorted(notes), ["profile/pref.md", "profile/user.md"])
        self.assertEqual(notes["profile/pref.md"].meta["name"], "pref")
        self.assertEqual(notes["profile/user.md"].title, "用户画像")

    def test_notes_ignore_sync_conflict_and_symlink(self):
        write(self.vault / "profile" / "bad.sync-conflict-1.md", "---\nname: x\n---\n# x\n")
        outside = Path(self.tmp.name) / "outside.md"
        write(outside, "# 外部\n")
        (self.vault / "profile" / "link.md").symlink_to(outside)
        self.assertNotIn("profile/bad.sync-conflict-1.md", web.load_notes(self.vault))
        self.assertNotIn("profile/link.md", web.load_notes(self.vault))

    def test_notes_ignore_symlinked_top_dir(self):
        outside = Path(self.tmp.name) / "outside-projects"
        write(outside / "x.md", "---\nname: x\n---\n# x\n")
        (self.vault / "projects").symlink_to(outside)
        self.assertNotIn("projects/x.md", web.load_notes(self.vault))

    def test_logs_grouped_and_sorted(self):
        logs = web.load_logs(self.vault)
        self.assertEqual(sorted(logs), ["device-a", "device-b"])
        self.assertEqual([i.when for i in logs["device-a"]],
                         ["2026-11-13 11:00", "2026-11-12 10:00"])
        self.assertEqual(logs["device-a"][0].tag, "preference")
        self.assertNotIn("冲突副本", " ".join(i.text for i in logs["device-a"]))

    def test_large_log_reads_tail(self):
        path = self.vault / "log" / "2026-11.device-c.md"
        body = ("## 2026-11-01 09:00 · x@device-c\n- [fact] 旧条目\n"
                + "x" * (web.MAX_FILE_BYTES + 100) + "\n"
                + "## 2026-11-30 09:00 · x@device-c\n- [fact] 最新条目\n")
        path.write_text(body, encoding="utf-8")
        logs = web.load_logs(self.vault)
        self.assertIn("最新条目", [i.text for i in logs["device-c"]])
        self.assertNotIn("旧条目", [i.text for i in logs["device-c"]])

    def test_log_tail_keeps_line_at_window_boundary(self):
        path = self.vault / "log" / "2026-11.device-c.md"
        head = "## 2026-11-30 09:00 · x@device-c\n- [fact] 边界条目\n"
        head_bytes = head.encode("utf-8")
        rest = head_bytes + b"x" * (web.MAX_FILE_BYTES - len(head_bytes))
        path.write_bytes(b"a\n" + rest)
        # 读取窗口正好从行首开始，这一整行不能被当成半个字符丢掉。
        self.assertTrue(web._read_text_tail(path).startswith("## 2026-11-30"))
        self.assertIn("边界条目", [i.text for i in web.load_logs(self.vault)["device-c"]])

    def test_archive(self):
        archive = web.load_archive(self.archive)
        self.assertEqual(sorted(archive), ["device-a", "device-b"])
        stats = {s.agent: s for s in archive["device-a"]}
        self.assertEqual(stats["claude-code"].files, 1)
        self.assertEqual(stats["codex"].files, 1)
        self.assertEqual(archive["device-b"][0].files, 1)

        before = web.load_archive(self.archive)["device-a"][0].files
        write(self.archive / "device-a" / "claude-code" / "new.jsonl", "{}")
        self.assertEqual(web.load_archive(self.archive)["device-a"][0].files, before)
        web.clear_caches()
        self.assertEqual(web.load_archive(self.archive)["device-a"][0].files, before + 1)

    def test_archive_ignores_invalid_device_names(self):
        write(self.archive / "My Device" / "claude-code" / "x.jsonl", "{}")
        self.assertNotIn("My Device", web.load_archive(self.archive))

    def test_devices_merge(self):
        devices = {d.name: d for d in web.build_devices(
            web.load_logs(self.vault), web.load_archive(self.archive))}
        self.assertEqual(sorted(devices), ["device-a", "device-b"])
        self.assertEqual(devices["device-a"].archive_files, 2)
        self.assertEqual(devices["device-a"].last_log, "2026-11-13 11:00")

    def test_reports_and_proposals(self):
        reports = web.load_reports(self.reports)
        self.assertEqual([r.name for r in reports],
                         ["2026-11-13-043000.md", "2026-11-12-043000.md"])
        proposals = {p.name: p for p in web.load_proposals(self.vault)}
        self.assertEqual(proposals["2026-11-12.md"].pending, 1)
        self.assertEqual(proposals["2026-11-12.md"].total, 2)
        self.assertEqual(proposals["2026-11-13.md"].pending, 1)

        write(self.vault / "proposals" / "2026-11-14.md",
              "# 提案\n\n## 1. 新增 `rules/d.md` · accepted\n\n"
              "正文提到 pending 不应该被计数。\n")
        proposals = {p.name: p for p in web.load_proposals(self.vault)}
        self.assertEqual(proposals["2026-11-14.md"].pending, 0)
        self.assertEqual(proposals["2026-11-14.md"].total, 1)

        write(self.vault / "proposals" / "2026-11-15.sync-conflict-1.md",
              "# 提案\n\n## 1. 修改 `rules/e.md` · pending\n")
        proposals = {p.name: p for p in web.load_proposals(self.vault)}
        self.assertNotIn("2026-11-15.sync-conflict-1.md", proposals)

    def test_hidden_files_excluded_from_lists(self):
        write(self.vault / "proposals" / ".hidden.md", "# hidden\n")
        write(self.reports / ".hidden.md", "# hidden\n")
        self.assertNotIn(".hidden.md", [p.name for p in web.load_proposals(self.vault)])
        self.assertNotIn(".hidden.md", [r.name for r in web.load_reports(self.reports)])

    def test_short_invalid_utf8_is_not_silently_empty(self):
        path = self.vault / "profile" / "tiny.md"
        path.write_bytes(b"\xff")
        self.assertIsNone(web._read_text(path))
        self.assertNotIn("profile/tiny.md", [n.rel for n in web.load_notes(self.vault).values()])

    def test_intermediate_symlink_directory_is_rejected(self):
        (self.vault / "profile" / "up").symlink_to(self.vault, target_is_directory=True)
        write(self.vault / "secret.md", "# 不应通过笔记路由读取\n")
        app = web.WebApp(web.WebConfig(
            vault=self.vault, archive=self.archive, reports=self.reports))
        status, _, _ = app.route("/memory/profile/up/secret.md", {})
        self.assertEqual(status, 404)

    def test_state_and_issues(self):
        self.assertEqual(web.load_state(self.vault)["last_run"], "2026-11-13T04:30:00+08:00")
        errors, warnings, ok = web.vault_issues(self.vault)
        self.assertTrue(ok)
        self.assertGreaterEqual(errors, 0)
        self.assertGreaterEqual(warnings, 0)
        web.clear_caches()
        with mock.patch.object(web.fmt, "iter_files", side_effect=OSError("unreadable")):
            self.assertEqual(web.vault_issues(self.vault), (0, 0, False))

    def test_validation_handles_pathological_link_line_quickly(self):
        write(self.vault / "profile" / "evil.md", "](x" * 20000 + "<!--" * 20000)
        web.clear_caches()
        start = time.monotonic()
        web.vault_issues(self.vault)
        self.assertLess(time.monotonic() - start, 2.0)

    def test_validation_helpers_skip_large_files_and_comments(self):
        write(self.vault / "profile" / "huge.md",
              "# big\n" + "x" * (web.MAX_FILE_BYTES + 1))
        files = web.fmt.iter_files(self.vault, max_bytes=web.MAX_FILE_BYTES)
        self.assertNotIn("profile/huge.md", files)
        self.assertIn("profile/user.md", files)
        wikis, paths = web.fmt.extract_links(
            "<!-- [a](old.md) --> [b](new.md) [[pref]]")
        self.assertEqual(wikis, ["pref"])
        self.assertEqual(paths, ["new.md"])


class WebAppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault, self.archive, self.reports = make_vault(Path(self.tmp.name))
        self.app = web.WebApp(web.WebConfig(
            vault=self.vault, archive=self.archive, reports=self.reports))

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_pages(self):
        for path in ("/", "/memory", "/memory/MEMORY.md", "/memory/profile/user.md",
                     "/devices", "/devices/device-a", "/history", "/reports",
                     "/reports/2026-11-12-043000.md", "/proposals",
                     "/proposals/2026-11-12.md"):
            with self.subTest(path=path):
                status, ctype, body = self.app.route(path, {})
                self.assertEqual(status, 200)
                self.assertIn("text/html", ctype)
                self.assertIn("<!doctype html>", body)

    def test_dashboard_labels_are_chinese_and_cards_are_balanced(self):
        status, _, body = self.app.route("/", {})
        self.assertEqual(status, 200)
        self.assertIn("最近整理", body)
        self.assertIn("记忆库校验", body)
        self.assertIn('class="statusline"', body)
        self.assertNotIn("最近 dreaming", body)
        self.assertNotIn("vault:", body)
        self.assertEqual(body.count('class="card"'), 4)

    def test_raw_and_escaped(self):
        status, _, body = self.app.route("/memory/profile/user.md",
                                         {"raw": ["1"]})
        self.assertEqual(status, 200)
        self.assertIn("&lt;script&gt;", body)
        self.assertNotIn("<script>alert", body)

    def test_rendered_note_escapes_and_links(self):
        status, _, body = self.app.route("/memory/profile/user.md", {})
        self.assertEqual(status, 200)
        self.assertNotIn("<script>alert", body)
        self.assertNotIn("javascript:", body.lower())
        self.assertIn('href="/memory/profile/pref.md"', body)
        self.assertIn('href="https://example.com/x?a=1&amp;b=2"', body)

    def test_log_text_escaped(self):
        status, _, body = self.app.route("/devices/device-a", {})
        self.assertEqual(status, 200)
        self.assertIn("&lt;script&gt;", body)
        self.assertNotIn("<script>alert", body)

    def test_not_found_and_traversal(self):
        for path in ("/nope", "/memory/../../etc/passwd", "/memory/profile/../rules/x.md",
                     "/memory/profile/user.txt", "/devices/..", "/history/not-a-hash",
                     "/reports/../MEMORY.md", "/proposals/../MEMORY.md"):
            with self.subTest(path=path):
                status, _, _ = self.app.route(path, {})
                self.assertEqual(status, 404)

    def test_note_symlink_outside_vault_is_404(self):
        outside = Path(self.tmp.name) / "outside.md"
        write(outside, "# 外部\n")
        (self.vault / "profile" / "link.md").symlink_to(outside)
        status, _, _ = self.app.route("/memory/profile/link.md", {})
        self.assertEqual(status, 404)

    def test_healthz(self):
        status, ctype, body = self.app.route("/healthz", {})
        self.assertEqual((status, body), (200, "ok\n"))
        self.assertIn("text/plain", ctype)

    def test_hidden_and_conflict_notes_are_404(self):
        write(self.vault / "profile" / ".hidden.md", "# hidden\n")
        write(self.vault / "profile" / "x.sync-conflict-1.md", "# conflict\n")
        for path in ("/memory/profile/.hidden.md",
                     "/memory/profile/x.sync-conflict-1.md"):
            with self.subTest(path=path):
                status, _, _ = self.app.route(path, {})
                self.assertEqual(status, 404)

    def test_large_multibyte_note_is_not_dropped(self):
        limit = web.MAX_FILE_BYTES
        path = self.vault / "profile" / "big.md"
        path.write_bytes(b"a" * (limit - 2) + "中".encode() + b"\n")
        text = web._read_text(path)
        self.assertIsNotNone(text)
        self.assertTrue(text.startswith("a"))
        status, _, _ = self.app.route("/memory/profile/big.md", {})
        self.assertEqual(status, 200)

    def test_report_and_proposal_symlinks_are_404(self):
        outside = Path(self.tmp.name) / "outside.md"
        write(outside, "# 外部\n")
        (self.reports / "link.md").symlink_to(outside)
        (self.vault / "proposals" / "link.md").symlink_to(outside)
        for path in ("/reports/link.md", "/proposals/link.md"):
            with self.subTest(path=path):
                status, _, _ = self.app.route(path, {})
                self.assertEqual(status, 404)

    def test_auth_page(self):
        page = self.app.auth_page("/memory", {"raw": ["1"], "a": ["1", "2"]})
        self.assertIn('action="/memory"', page)
        self.assertIn('<input type="hidden" name="raw" value="1">', page)
        self.assertIn('<input type="hidden" name="a" value="1">', page)
        self.assertIn('<input type="hidden" name="a" value="2">', page)
        self.assertIn("访问令牌", page)
        page = self.app.auth_page("/memory", {"raw": ["1"], "token": ["secret"]})
        self.assertNotIn("token=secret", page)
        self.assertNotIn('name="token" value="secret"', page)


class WebHTTPTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault, self.archive, self.reports = make_vault(Path(self.tmp.name))
        self.server = None
        self.thread = None

    def tearDown(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            if self.thread is not None:
                self.thread.join(timeout=5)
        self.tmp.cleanup()

    def start(self, token: str | None = None) -> None:
        cfg = web.WebConfig(vault=self.vault, archive=self.archive,
                            reports=self.reports, host="127.0.0.1", port=0,
                            token=token)
        self.server = web.make_server(cfg)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def request(self, method: str, path: str, headers: dict[str, str] | None = None,
                body: bytes | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
        finally:
            conn.close()

    def raw_request(self, payload: bytes) -> bytes:
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            sock.sendall(payload)
            chunks = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            sock.close()

    def test_get_head_and_methods(self):
        self.start()
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("default-src 'none'", headers["content-security-policy"])
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertIn("记忆", body.decode())

        status, headers, body = self.request("HEAD", "/")
        self.assertEqual((status, body), (200, b""))
        self.assertGreater(int(headers["content-length"]), 0)

        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE"):
            with self.subTest(method=method):
                status, headers, _ = self.request(method, "/", body=b"x")
                self.assertEqual(status, 405)
                self.assertEqual(headers["allow"], "GET, HEAD")

    def test_host_header_required_to_be_loopback_without_token(self):
        self.start()
        status, _, _ = self.request("GET", "/", headers={"Host": "evil.example"})
        self.assertEqual(status, 400)
        status, _, _ = self.request(
            "GET", "/", headers={"Host": f"127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)

    def test_healthz_needs_no_token(self):
        self.start(token="secret")
        for path in ("/healthz", "/healthz/"):
            with self.subTest(path=path):
                status, _, body = self.request("GET", path)
                self.assertEqual((status, body), (200, b"ok\n"))

    def test_token_auth(self):
        self.start(token="secret")
        status, headers, _ = self.request("GET", "/")
        self.assertEqual(status, 401)
        self.assertEqual(headers["www-authenticate"], "Bearer")

        status, _, _ = self.request("GET", "/?token=wrong")
        self.assertEqual(status, 401)

        status, headers, _ = self.request("GET", "/?token=secret")
        self.assertEqual(status, 302)
        self.assertEqual(headers["location"], "/")
        self.assertIn("amem_token=secret", headers["set-cookie"])
        self.assertIn("HttpOnly", headers["set-cookie"])

        status, _, _ = self.request("GET", "/", headers={"Cookie": "amem_token=secret"})
        self.assertEqual(status, 200)

        status, _, _ = self.request("GET", "/", headers={"Authorization": "Bearer secret"})
        self.assertEqual(status, 200)

    def test_token_redirect_keeps_query(self):
        self.start(token="secret")
        status, headers, _ = self.request("GET", "/memory?raw=1&token=secret")
        self.assertEqual(status, 302)
        self.assertEqual(headers["location"], "/memory?raw=1")

    def test_malformed_cookie_is_401_not_disconnect(self):
        self.start(token="secret")
        status, headers, _ = self.request("GET", "/", headers={"Cookie": "<=x"})
        self.assertEqual(status, 401)
        self.assertIn("content-security-policy", headers)

    def test_unknown_methods_are_405(self):
        self.start()
        for method in ("CONNECT", "FOO"):
            with self.subTest(method=method):
                data = self.raw_request(
                    f"{method} / HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                    "Connection: close\r\n\r\n".encode())
                self.assertIn(b" 405 ", data.split(b"\r\n", 1)[0])
                self.assertIn(b"Allow: GET, HEAD", data)

    def test_auth_form_submission_keeps_query(self):
        self.start(token="secret")
        path = "/memory/profile/user.md?raw=1"
        status, _, body = self.request("GET", path)
        self.assertEqual(status, 401)
        self.assertIn(b'action="/memory/profile/user.md"', body)
        self.assertIn(b'name="raw" value="1"', body)
        # 浏览器提交 GET 表单时用表单字段替换 action 的查询串；模拟提交后的请求。
        action = body.split(b'action="', 1)[1].split(b'"', 1)[0].decode()
        status, headers, _ = self.request("GET", f"{action}?raw=1&token=secret")
        self.assertEqual(status, 302)
        self.assertEqual(headers["location"], path)

    def test_auth_form_encodes_special_path(self):
        write(self.vault / "profile" / "a%2Fb.md", "# 特殊文件名\n")
        self.start(token="secret")
        encoded = "/memory/profile/a%252Fb.md"
        status, _, body = self.request("GET", encoded)
        self.assertEqual(status, 401)
        self.assertIn(f'action="{encoded}"'.encode(), body)
        status, headers, _ = self.request("GET", f"{encoded}?token=secret")
        self.assertEqual(status, 302)
        self.assertEqual(headers["location"], encoded)
        status, _, _ = self.request("GET", encoded,
                                    headers={"Cookie": "amem_token=secret"})
        self.assertEqual(status, 200)

    def test_token_redirect_keeps_repeated_query(self):
        self.start(token="secret")
        status, headers, _ = self.request("GET", "/memory?a=1&a=2&token=secret")
        self.assertEqual(status, 302)
        self.assertEqual(headers["location"], "/memory?a=1&a=2")

    def test_cookie_safe_special_token_round_trip(self):
        token = "a+b&c#d"
        self.start(token=token)
        status, headers, _ = self.request("GET", "/?token=" + quote(token, safe=""))
        self.assertEqual(status, 302)
        cookie = headers["set-cookie"].split(";", 1)[0]
        status, _, _ = self.request("GET", "/", headers={"Cookie": cookie})
        self.assertEqual(status, 200)

    def test_malformed_absolute_uri_is_400(self):
        self.start()
        data = self.raw_request(
            b"GET http://[::1/ HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Connection: close\r\n\r\n")
        self.assertIn(b" 400 ", data.split(b"\r\n", 1)[0])

    def test_double_slash_redirect_is_rejected(self):
        self.start(token="secret")
        status, _, _ = self.request("GET", "/%2f%2fevil.example/?token=secret")
        self.assertEqual(status, 400)

    def test_host_is_not_restricted_when_token_is_set(self):
        self.start(token="secret")
        status, _, _ = self.request(
            "GET", "/", headers={"Authorization": "Bearer secret",
                                 "Host": "evil.example"})
        self.assertEqual(status, 200)

    def test_encoded_traversal_is_404(self):
        self.start()
        status, _, _ = self.request("GET", "/memory/%2e%2e%2f%2e%2e%2fetc%2fpasswd")
        self.assertEqual(status, 404)


class ConfigTest(unittest.TestCase):
    def test_from_env(self):
        env = {"AMEM_VAULT": "/tmp/v", "AMEM_ARCHIVE": "/tmp/a",
               "AMEM_REPORTS": "/tmp/r", "AMEM_WEB_HOST": "127.0.0.1",
               "AMEM_WEB_PORT": "9001", "AMEM_WEB_TOKEN": "t"}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = web.WebConfig.from_env()
        self.assertEqual(cfg.vault, Path("/tmp/v"))
        self.assertEqual(cfg.archive, Path("/tmp/a"))
        self.assertEqual(cfg.reports, Path("/tmp/r"))
        self.assertEqual((cfg.host, cfg.port, cfg.token), ("127.0.0.1", 9001, "t"))

    def test_reports_default_follows_vault(self):
        with mock.patch.dict(os.environ, {"AMEM_VAULT": "/srv/vault"}, clear=True):
            cfg = web.WebConfig.from_env()
        self.assertEqual(cfg.reports, Path("/srv/dream-reports"))

    def test_empty_host_falls_back_to_loopback(self):
        with mock.patch.dict(os.environ, {"AMEM_WEB_HOST": ""}, clear=True):
            cfg = web.WebConfig.from_env()
        self.assertEqual(cfg.host, "127.0.0.1")

    def test_unsafe_token_rejected(self):
        for token in ("中文令牌", "a b", "a;b", "a,b", 'a"b', "a\\b", "café"):
            with self.subTest(token=token):
                cfg = web.WebConfig(vault=Path("."), archive=Path("."),
                                    reports=Path("."), port=0, token=token)
                with self.assertRaises(ValueError):
                    web.make_server(cfg)

    def test_cookie_safe_special_token_accepted(self):
        for token in ("a+b&c#d", "a%b=c", "token-with_~chars"):
            with self.subTest(token=token):
                self.assertTrue(web._valid_token(token))

    def test_non_loopback_requires_token_in_make_server(self):
        cfg = web.WebConfig(vault=Path("."), archive=Path("."), reports=Path("."),
                            host="0.0.0.0", port=0)
        with self.assertRaises(ValueError):
            web.make_server(cfg)

    def test_loopback_hosts(self):
        for host in ("127.0.0.1", "127.0.0.1:8787", "127.1", "127.0.0.1.",
                     "localhost:8787", "[::1]:8787", "::1"):
            with self.subTest(host=host):
                self.assertTrue(web._is_loopback_host(host))
        for host in ("", "127", "0.0.0.0", "evil.example", "evil.example:80",
                     "127.0.0.1.evil.example", "127.0.0.1:evil",
                     "127.0.0.1:80@evil.example", "[::1]evil",
                     "127.999.1", "127.0.0.0.1", "127.0.0.01"):
            with self.subTest(host=host):
                self.assertFalse(web._is_loopback_host(host))


class GitHistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.vault = root / "vault"
        self.vault.mkdir()
        write(self.vault / "MEMORY.md", "# 索引\n")
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "init"]):
            subprocess.run(["git", "-C", str(self.vault), *args], check=True,
                           capture_output=True, env=env)
        self.app = web.WebApp(web.WebConfig(
            vault=self.vault, archive=root / "archive", reports=root / "reports"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_history_and_commit_page(self):
        commits = web.load_commits(self.vault)
        self.assertEqual([c.subject for c in commits], ["init"])
        status, _, body = self.app.route("/history", {})
        self.assertEqual(status, 200)
        self.assertIn("init", body)
        status, _, body = self.app.route(f"/history/{commits[0].full}", {})
        self.assertEqual(status, 200)
        self.assertIn("MEMORY.md", body)
        status, _, _ = self.app.route("/history/deadbeef", {})
        self.assertEqual(status, 404)

    def test_commit_page_marks_truncated_diff(self):
        with mock.patch.object(web, "_git",
                               return_value="x" * (web.MAX_DIFF_BYTES + 10)):
            text, truncated = web.load_commit(self.vault, "deadbeef")
            self.assertTrue(truncated)
            self.assertEqual(len(text), web.MAX_DIFF_BYTES)
            status, _, body = self.app.route("/history/deadbeef", {})
            self.assertEqual(status, 200)
            self.assertIn("只显示开头部分", body)

    def test_commit_timeout_is_504(self):
        with mock.patch.object(web, "_git", side_effect=web.GitTimeout()):
            status, _, body = self.app.route("/history/deadbeef", {})
        self.assertEqual(status, 504)
        self.assertIn("超时", body)


class ScriptTest(unittest.TestCase):
    def test_help(self):
        env = dict(os.environ, PYTHON=sys.executable)
        r = subprocess.run(["/bin/bash", str(SCRIPT), "--help"], text=True,
                           capture_output=True, env=env, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("只读记忆库 Web", r.stdout)
        self.assertIn("--token", r.stdout)


class MainTest(unittest.TestCase):
    def test_non_loopback_requires_token(self):
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td) / "vault"
            vault.mkdir()
            env = {"AMEM_VAULT": str(vault)}
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch("sys.stderr"):
                with self.assertRaises(SystemExit):
                    web.main(["--vault", str(vault), "--host", "0.0.0.0",
                              "--port", "8787"])


if __name__ == "__main__":
    unittest.main()
