#!/usr/bin/env bash
# 在本机构建 amem 安装包，并上传到 CNB 的 Release。
#
# 用法:
#   scripts/publish-client.sh              # 构建当前平台的安装包并上传
#   scripts/publish-client.sh --no-build   # 不构建，直接上传已有产物
#   scripts/publish-client.sh --dry-run    # 只打印仓库、版本和待上传文件
#
# 环境变量:
#   CNB_TOKEN  CNB 访问令牌，需要 repo-release:rw 权限。
#              也可以写在 ~/.config/amem/cnb.env（脚本会 source 它，建议 chmod 600）。
#   CNB_REPO   覆盖仓库路径，默认从 git remote 推导，例如 xcec_lab/agent-memory-sync。
#   CNB_TAG    覆盖标签名，默认 client-v<版本号>。
#
# 产物要求:
#   macOS:   client/src-tauri/target/release/bundle/dmg/*.dmg
#   Windows: client/src-tauri/target/release/bundle/nsis/*.exe
#
# 发版前先把提交推到 CNB：Release 的标签指向远端提交，本地未推送的代码对不上。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLIENT="$ROOT/client"
BUILD=1
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-build) BUILD=0 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
  shift
done

TOKEN="${CNB_TOKEN:-}"
if [[ -z "$TOKEN" && -f "$HOME/.config/amem/cnb.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$HOME/.config/amem/cnb.env"
  set +a
  TOKEN="${CNB_TOKEN:-}"
fi

REPO="${CNB_REPO:-}"
if [[ -z "$REPO" ]]; then
  remote="$(git -C "$ROOT" remote get-url cnb 2>/dev/null || git -C "$ROOT" remote get-url origin 2>/dev/null || true)"
  REPO="$(printf '%s' "$remote" | sed -E 's#^.*cnb\.cool[/:](.+)$#\1#; s#\.git$##')"
fi
if [[ -z "$REPO" || "$REPO" == "$remote" ]]; then
  echo "无法从 git remote 推导 CNB 仓库，请设置 CNB_REPO（格式：组织/仓库）" >&2
  exit 1
fi

VERSION="$(python3 -I -c 'import json,sys;print(json.load(open(sys.argv[1]))["version"])' \
  "$CLIENT/src-tauri/tauri.conf.json")"
TAG="${CNB_TAG:-client-v$VERSION}"
COMMIT="$(git -C "$ROOT" rev-parse HEAD)"

if [[ $BUILD -eq 1 ]]; then
  echo "==> 构建 amem $VERSION"
  (cd "$CLIENT" && npm run tauri build)
fi

shopt -s nullglob
case "$(uname -s)" in
  Darwin)
    FILES=("$CLIENT"/src-tauri/target/release/bundle/dmg/*.dmg)
    ;;
  MINGW*|MSYS*|CYGWIN*)
    FILES=("$CLIENT"/src-tauri/target/release/bundle/nsis/*.exe)
    ;;
  *)
    echo "当前平台不支持：$(uname -s)（只处理 macOS / Windows）" >&2
    exit 1
    ;;
esac

if [[ ${#FILES[@]} -eq 0 ]]; then
  echo "没有找到安装包，先执行 npm run tauri build（或去掉 --no-build）" >&2
  exit 1
fi

echo "==> 仓库 $REPO"
echo "==> 版本 ${VERSION}（${TAG}，提交 ${COMMIT:0:12}）"
printf '==> 待上传 %s\n' "${FILES[@]}"

if [[ $DRY_RUN -eq 1 ]]; then
  echo "==> dry-run，不上传"
  exit 0
fi

if [[ -z "$TOKEN" ]]; then
  cat >&2 <<'EOF'
缺少 CNB 访问令牌。创建方式：

  1. 打开 CNB →「个人设置」→「访问令牌」→「添加访问令牌」
  2. 授权范围勾选 releases 的读写（repo-release:rw）
  3. 保存令牌，写入环境变量或 ~/.config/amem/cnb.env：

     CNB_TOKEN=<令牌>
     chmod 600 ~/.config/amem/cnb.env
EOF
  exit 1
fi

if [[ -n "$(git -C "$ROOT" status --porcelain)" ]]; then
  echo "警告：工作区有未提交的改动，上传的安装包可能与远端提交不一致" >&2
fi

export CNB_TOKEN="$TOKEN" CNB_REPO="$REPO" CNB_TAG="$TAG" CNB_COMMIT="$COMMIT"
export CNB_RELEASE_NAME="amem $VERSION"
export CNB_RELEASE_BODY="本机构建。提交 ${COMMIT:0:12}，$(uname -s) $(uname -m)。"

python3 -I - "${FILES[@]}" <<'PY'
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.cnb.cool"
TOKEN = os.environ["CNB_TOKEN"]
REPO = os.environ["CNB_REPO"]
TAG = os.environ["CNB_TAG"]
NAME = os.environ["CNB_RELEASE_NAME"]
BODY = os.environ["CNB_RELEASE_BODY"]
COMMIT = os.environ.get("CNB_COMMIT", "")
FILES = sys.argv[1:]
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.cnb.api+json",
}


def request(method, url, data=None, headers=None):
    if url.startswith("/"):
        url = API + url
    body = None
    hdrs = dict(HEADERS)
    if headers:
        hdrs.update(headers)
    if data is not None:
        body = json.dumps(data).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        return exc.code, raw


def fail(message):
    print(f"错误：{message}", file=sys.stderr)
    raise SystemExit(1)


release_path = f"/{REPO}/-/releases"
tag_path = f"/{REPO}/-/releases/tags/{urllib.parse.quote(TAG)}"

status, data = request("GET", tag_path)
if status == 200 and isinstance(data, dict):
    release = data
    print(f"使用已有 Release {release.get('tag_name')}（{release.get('id')}）")
elif status == 404:
    status, data = request("POST", release_path, {
        "tag_name": TAG,
        "target_commitish": COMMIT or None,
        "name": NAME,
        "body": BODY,
        "make_latest": "true",
    })
    if status not in (200, 201) or not isinstance(data, dict):
        fail(f"创建 Release 失败：HTTP {status}\n{data}")
    release = data
    print(f"已创建 Release {TAG}（{release.get('id')}）")
else:
    fail(f"查询 Release 失败：HTTP {status}\n{data}")

release_id = release.get("id")
if not release_id:
    fail(f"Release 响应缺少 id：{release}")

for path in FILES:
    name = os.path.basename(path)
    size = os.path.getsize(path)
    print(f"上传 {name}（{size / 1024 / 1024:.1f} MiB）")

    status, data = request("POST", f"{release_path}/{release_id}/asset-upload-url", {
        "asset_name": name,
        "size": size,
        "overwrite": True,
    })
    if status not in (200, 201) or not isinstance(data, dict):
        fail(f"获取上传地址失败：HTTP {status}\n{data}")

    upload_url = data.get("upload_url")
    verify_url = data.get("verify_url")
    if not upload_url or not verify_url:
        fail(f"上传地址响应不完整：{data}")

    with open(path, "rb") as handle:
        payload = handle.read()
    req = urllib.request.Request(upload_url, data=payload, method="PUT", headers={
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/octet-stream",
    })
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            if resp.status not in (200, 201, 204):
                fail(f"上传文件失败：HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        fail(f"上传文件失败：HTTP {exc.code}\n{exc.read().decode('utf-8', 'replace')}")

    status, data = request("POST", verify_url, {})
    if status not in (200, 201, 204):
        fail(f"确认上传失败：HTTP {status}\n{data}")

    print(f"完成 {name}")

print(f"Release: https://cnb.cool/{REPO}/-/releases/tag/{urllib.parse.quote(TAG)}")
PY
