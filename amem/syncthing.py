"""Syncthing 本地 REST API 摘要。

服务器上的 Web/客户端需要知道各设备是否在线、最后同步时间。API key 只留在
服务器进程环境里，摘要结果不含密钥；未配置或不可达时返回结构化的降级信息。
"""
from __future__ import annotations

import json
import os
import threading
import time
from urllib.request import Request, urlopen

DEFAULT_URL = "http://127.0.0.1:8384"
MAX_BYTES = 1024 * 1024

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, dict]] = {}


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _get_json(base: str, path: str, api_key: str, timeout: float) -> dict:
    request = Request(base + path, headers={
        "X-API-Key": api_key,
        "Accept": "application/json",
    })
    with urlopen(request, timeout=timeout) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Syncthing 响应过大")
    parsed = json.loads(data.decode("utf-8"))
    if not isinstance(parsed, (dict, list)):
        raise ValueError("Syncthing 响应格式不正确")
    return parsed


def summarize(*, url: str | None = None, api_key: str | None = None,
              timeout: float = 2.0, ttl: float = 15.0) -> dict:
    """返回设备连接摘要。结果缓存 ttl 秒，失败也会缓存，避免轮询打爆本地 API。"""
    base = (url or os.environ.get("AMEM_SYNCTHING_URL") or DEFAULT_URL).rstrip("/")
    key = api_key if api_key is not None else (os.environ.get("AMEM_SYNCTHING_API_KEY") or "")
    if not key:
        return {"configured": False, "ok": False,
                "error": "未配置 Syncthing API key", "devices": [],
                "online": 0, "total": 0, "updated": int(time.time())}
    if not (base.startswith("http://") or base.startswith("https://")):
        return {"configured": True, "ok": False,
                "error": "Syncthing URL 不合法", "devices": [],
                "online": 0, "total": 0, "updated": int(time.time())}

    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(base)
        if cached and now - cached[0] < ttl:
            return cached[1]

    try:
        status = _get_json(base, "/rest/system/status", key, timeout)
        config = _get_json(base, "/rest/config/devices", key, timeout)
        connections = _get_json(base, "/rest/system/connections", key, timeout)
        stats = _get_json(base, "/rest/stats/device", key, timeout)
    except (OSError, ValueError):
        result = {"configured": True, "ok": False, "error": "Syncthing API 不可达",
                  "devices": [], "online": 0, "total": 0}
    else:
        my_id = str(status.get("myID") or "")
        conn_map = connections.get("connections") or {}
        stat_map = stats if isinstance(stats, dict) else {}
        devices = []
        for item in config if isinstance(config, list) else []:
            did = str(item.get("deviceID") or "")
            conn = conn_map.get(did) or {}
            stat = stat_map.get(did) or {}
            devices.append({
                "id": did[:7],
                "name": item.get("name") or did[:7],
                "connected": bool(conn.get("connected")),
                "paused": bool(item.get("paused")),
                "self": bool(my_id) and did == my_id,
                "address": conn.get("address") or "",
                "last_seen": stat.get("lastSeen") or "",
            })
        devices.sort(key=lambda d: (not d["self"], not d["connected"],
                                    (d["name"] or "").lower()))
        others = [d for d in devices if not d["self"]]
        result = {
            "configured": True, "ok": True, "error": None,
            "my_id": my_id[:7],
            "devices": devices,
            "online": sum(1 for d in others if d["connected"]),
            "total": len(others),
        }
    result["updated"] = int(time.time())
    with _cache_lock:
        _cache[base] = (now, result)
    return result
