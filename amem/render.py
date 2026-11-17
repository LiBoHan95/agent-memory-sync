"""Web 使用的轻量 Markdown 渲染。

只覆盖 vault 里实际用到的语法（标题、列表、表格、代码块、引用、链接、
``[[wikilink]]``）。所有源文本先做 HTML 转义，输出只包含本模块生成的标签；
不追求完整 CommonMark，也不允许原始 HTML。
"""
from __future__ import annotations

import html
import posixpath
import re
from typing import Callable, Mapping
from urllib.parse import quote

Linker = Callable[[str], str | None]
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})\s*(.*?)\s*$")
_HEADING_PREFIX_RE = re.compile(r"^(#{1,6})[ \t]+(.*)$")
_UL_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
_OL_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
_HR_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(?:\|\s*:?-{1,}:?\s*)+\|?\s*$")
_LINK_RE = re.compile(r"\[([^\[\]]+)\]\(([^)\[\s]+)(?:\s+\"[^\"]*\")?\)")
_WIKI_RE = re.compile(r"\[\[([^\[\]|#]+)(?:([|#])([^\[\]]*))?\]\]")
_CODE_RE = re.compile(r"`([^`\n]+)`")
# 内容里禁止再出现 *，避免回溯型匹配；空内容和首尾空白不算强调。
_BOLD_ITALIC_RE = re.compile(r"\*\*\*(?=\S)([^*\n]+?)(?<=\S)\*\*\*")
_BOLD_RE = re.compile(r"\*\*(?=\S)([^*\n]+?)(?<=\S)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(?=\S)([^*\n]+?)(?<=\S)(?<!\*)\*(?!\*)")
_BAD_SCHEME_RE = re.compile(r"^(?:javascript|vbscript|data|file):", re.I)
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)
_ALLOWED_SCHEMES = frozenset({"http", "https", "mailto", "tel"})
_PLACEHOLDER_RE = re.compile(r"\x00(\d+)\x00")


def render_markdown(text: str, *, base_path: str = "",
                    wikilinks: Mapping[str, str] | None = None,
                    path_linker: Linker | None = None) -> str:
    """把 Markdown 渲染成安全的 HTML。

    base_path 是当前文件在 vault 内的相对路径，用来解析相对链接；
    wikilinks 是 name -> href；path_linker 把 vault 内相对路径转成 href，
    返回 None 表示不链接。
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    i, n = 0, len(lines)

    def inline(value: str) -> str:
        return _inline(value, base_path=base_path, wikilinks=wikilinks,
                       path_linker=path_linker)

    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        m = _FENCE_RE.match(line)
        if m:
            fence, info = m.group(1), m.group(2)
            lang = info.split()[0] if info.split() else ""
            lang = re.sub(r"[^\w.+-]", "", lang)
            i += 1
            code: list[str] = []
            close_re = re.compile(
                r"^\s*" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*$")
            while i < n and not close_re.match(lines[i]):
                code.append(lines[i])
                i += 1
            if i < n:
                i += 1
            cls = f' class="language-{html.escape(lang, quote=True)}"' if lang else ""
            body = html.escape("\n".join(code), quote=False)
            out.append(f"<pre><code{cls}>{body}</code></pre>")
            continue

        heading = _parse_heading(line)
        if heading is not None:
            level, content = heading
            out.append(f"<h{level}>{inline(content)}</h{level}>")
            i += 1
            continue

        if _HR_RE.match(line):
            out.append("<hr>")
            i += 1
            continue

        if _is_table_start(lines, i):
            table, i = _render_table(lines, i, inline)
            out.append(table)
            continue

        if _UL_RE.match(line) or _OL_RE.match(line):
            ordered = bool(_OL_RE.match(line))
            item_re = _OL_RE if ordered else _UL_RE
            items: list[str] = []
            while i < n:
                m = item_re.match(lines[i])
                if m:
                    items.append(m.group(1))
                    i += 1
                    continue
                # 缩进的续行并入上一条，而不是拆成新段落。
                continuation = lines[i]
                if (items and continuation[:1] in (" ", "\t")
                        and continuation.strip()
                        and not _is_block_start(lines, i)):
                    items[-1] += " " + continuation.strip()
                    i += 1
                    continue
                break
            tag = "ol" if ordered else "ul"
            rendered = "".join(f"<li>{inline(item)}</li>" for item in items)
            out.append(f"<{tag}>{rendered}</{tag}>")
            continue

        if _QUOTE_RE.match(line):
            parts = []
            while i < n:
                m = _QUOTE_RE.match(lines[i])
                if not m:
                    break
                parts.append(m.group(1))
                i += 1
            out.append(f"<blockquote><p>{inline(' '.join(parts))}</p></blockquote>")
            continue

        para = [line]
        i += 1
        while i < n and lines[i].strip() and not _is_block_start(lines, i):
            para.append(lines[i])
            i += 1
        out.append(f"<p>{inline(' '.join(para))}</p>")

    return "\n".join(out)


def _parse_heading(line: str) -> tuple[int, str] | None:
    """线性解析标题，避免在长行/尾随空格上触发正则回溯。"""
    m = _HEADING_PREFIX_RE.match(line)
    if not m:
        return None
    content = m.group(2).rstrip()
    hashes = len(content) - len(content.rstrip("#"))
    if hashes and len(content) > hashes and content[-hashes - 1] in " \t":
        content = content[:-hashes].rstrip()
    return len(m.group(1)), content


def _is_block_start(lines: list[str], i: int) -> bool:
    line = lines[i]
    return bool(
        _FENCE_RE.match(line) or _parse_heading(line) is not None
        or _HR_RE.match(line)
        or _UL_RE.match(line) or _OL_RE.match(line) or _QUOTE_RE.match(line)
        or _is_table_start(lines, i)
    )


def _is_table_start(lines: list[str], i: int) -> bool:
    return (
        i + 1 < len(lines) and "|" in lines[i]
        and bool(_TABLE_SEP_RE.match(lines[i + 1]))
    )


def _split_row(line: str) -> list[str]:
    row = line.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [cell.strip() for cell in row.split("|")]


def _render_table(lines: list[str], i: int, inline: Callable[[str], str]) -> tuple[str, int]:
    header = _split_row(lines[i])
    aligns = []
    for cell in _split_row(lines[i + 1]):
        left, right = cell.startswith(":"), cell.endswith(":")
        aligns.append("center" if left and right else "right" if right else "left" if left else "")
    i += 2
    rows = []
    while i < len(lines) and lines[i].strip() and "|" in lines[i]:
        rows.append(_split_row(lines[i]))
        i += 1

    def cell(tag: str, value: str, align: str) -> str:
        style = f' style="text-align:{align}"' if align else ""
        return f"<{tag}{style}>{inline(value)}</{tag}>"

    head = "".join(cell("th", value, aligns[j] if j < len(aligns) else "")
                   for j, value in enumerate(header))
    body_rows = []
    for row in rows:
        cells = "".join(cell("td", value, aligns[j] if j < len(aligns) else "")
                        for j, value in enumerate(row))
        body_rows.append(f"<tr>{cells}</tr>")
    table = (f"<table><thead><tr>{head}</tr></thead>"
             + (f"<tbody>{''.join(body_rows)}</tbody>" if body_rows else "")
             + "</table>")
    return table, i


def _inline(text: str, *, base_path: str, wikilinks: Mapping[str, str] | None,
            path_linker: Linker | None) -> str:
    text = text.replace("\x00", "�")
    text = html.escape(text, quote=False)

    codes: list[str] = []

    def stash(m: re.Match[str]) -> str:
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"

    text = _CODE_RE.sub(stash, text)

    def wiki(m: re.Match[str]) -> str:
        name = m.group(1).strip()
        sep, value = m.group(2), (m.group(3) or "").strip()
        alias, anchor = name, ""
        if sep == "|" and value:
            alias = value
        elif sep == "#" and value:
            anchor = "#" + quote(value, safe="")
        href = wikilinks.get(name) if wikilinks else None
        if href:
            return (f'<a href="{html.escape(href + anchor, quote=True)}">'
                    f"{alias}</a>")
        return f'<span class="wikilink">{name}</span>'

    text = _WIKI_RE.sub(wiki, text)

    def link(m: re.Match[str]) -> str:
        label, url = m.group(1), m.group(2).strip()
        safe = _safe_href(url, base_path, path_linker)
        if safe is None:
            return label
        # href 里的 * 用实体表示，避免后续强调替换误改 URL；
        # 浏览器解析属性时会把 &#42; 还原成 *。
        attr = html.escape(safe, quote=True).replace("*", "&#42;")
        return f'<a href="{attr}">{label}</a>'

    text = _LINK_RE.sub(link, text)
    text = _BOLD_ITALIC_RE.sub(r"<strong><em>\1</em></strong>", text)
    text = _BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = _ITALIC_RE.sub(r"<em>\1</em>", text)
    text = _PLACEHOLDER_RE.sub(
        lambda m: f"<code>{codes[int(m.group(1))]}</code>", text)
    return text


def _safe_href(url: str, base_path: str, path_linker: Linker | None) -> str | None:
    if not url:
        return None
    # 链接来自已经 HTML 转义的文本；先还原一次，避免调用方再次转义时出现 &amp;amp;。
    # 浏览器也只解码一次，所以不会因此把 &amp;#106; 变成 javascript:。
    url = html.unescape(url)
    # 浏览器会剥掉开头的 C0 控制字符，不能让 "\\x01javascript:" 绕过检查
    if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in url):
        return None
    # 反斜杠会被浏览器按 WHATWG 规则归一化成斜杠，可能变成 //host
    if "\\" in url:
        return None
    if _BAD_SCHEME_RE.match(url) or url.startswith("//"):
        return None
    if url.startswith("#"):
        return url
    if _SCHEME_RE.match(url):
        scheme = url.split(":", 1)[0].lower()
        return url if scheme in _ALLOWED_SCHEMES else None

    path, _, anchor = url.partition("#")
    if path.startswith(("/", "~")):
        return None
    if path_linker is None:
        return url
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(base_path), path))
    if resolved in ("", ".") or resolved == ".." or resolved.startswith("../"):
        return None
    href = path_linker(resolved)
    if href is None:
        return None
    return href + (f"#{anchor}" if anchor else "")
