from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from amem import format as fmt

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "vault-template"


def note(name: str, body: str = "正文", **kw) -> bytes:
    meta = {"schema_version": "1", "name": name, "description": f"{name} 描述",
            "type": "rule", "status": "active", "source": "claude-code@mac",
            "created": "2026-11-13", "updated": "2026-11-13"}
    meta.update(kw)
    head = "\n".join(f"{k}: {v}" for k, v in meta.items())
    return f"---\n{head}\n---\n{body}\n".encode()


def template_files() -> dict[str, bytes]:
    return fmt.iter_files(TEMPLATE)


class NoScanner:
    def scan(self, texts):
        return []


def make_vault(git: bool = True) -> Path:
    d = Path(tempfile.mkdtemp(prefix="amem-test-"))
    vault = d / "vault"
    shutil.copytree(TEMPLATE, vault)
    if git:
        for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"],
                     ["add", "-A"], ["commit", "-qm", "init"]):
            subprocess.run(["git", "-C", str(vault), *args], check=True, capture_output=True)
    return vault
