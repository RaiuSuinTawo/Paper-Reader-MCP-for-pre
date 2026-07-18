"""本地缓存与 manifest。

所有检索工具都从这里读已解析结果(content_list.json / images / full.md),零额外 API 开销。
- 缓存有效性以文件内容 sha256 判定:PDF 变了会重新解析。
- manifest.json 记录:论文键 → {file_hash, batch_id, data_id, state, local_dir, params...}。
"""
import hashlib
import json
import os
import re
import threading

from . import config

_LOCK = threading.RLock()
_MANIFEST = os.path.join(config.CACHE_DIR, "manifest.json")


def _safe_dirname(name: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_. ")
    return s[:100] or "paper"


def file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------- manifest 读写(带锁) ----------------
def _load() -> dict:
    if not os.path.isfile(_MANIFEST):
        return {"papers": {}}
    try:
        with open(_MANIFEST, "r", encoding="utf-8") as f:
            m = json.load(f)
        if not isinstance(m, dict):
            return {"papers": {}}
        m.setdefault("papers", {})
        return m
    except Exception:  # noqa: BLE001 —— manifest 损坏时不阻塞,当作空
        return {"papers": {}}


def _save(m: dict) -> None:
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    tmp = _MANIFEST + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _MANIFEST)


def get_entry(name: str):
    with _LOCK:
        return _load()["papers"].get(name)


def set_entry(name: str, **fields) -> dict:
    """合并式写入(值为 None 的字段忽略,便于增量更新)。"""
    with _LOCK:
        m = _load()
        entry = m["papers"].get(name, {})
        entry.update({k: v for k, v in fields.items() if v is not None})
        entry["name"] = name
        m["papers"][name] = entry
        _save(m)
        return entry


def all_entries() -> dict:
    with _LOCK:
        return dict(_load()["papers"])


def name_by_data_id(data_id: str):
    if not data_id:
        return None
    with _LOCK:
        for name, e in _load()["papers"].items():
            if e.get("data_id") == data_id:
                return name
    return None


# ---------------- 结果目录与文件定位 ----------------
def result_dir(name: str) -> str:
    return os.path.join(config.CACHE_DIR, _safe_dirname(name))


def _find(local_dir: str, suffix: str):
    if not local_dir or not os.path.isdir(local_dir):
        return None
    suffix = suffix.lower()
    for root, _d, files in os.walk(local_dir):
        for fn in files:
            if fn.lower().endswith(suffix):
                return os.path.join(root, fn)
    return None


def find_content_list(local_dir: str):
    # 优先 *content_list.json(排除 v2 变体的 endswith 差异)
    return _find(local_dir, "content_list.json")


def find_full_md(local_dir: str):
    hit = _find(local_dir, "full.md")
    return hit or _find(local_dir, ".md")


def is_ready(name: str, expect_hash: str = None) -> bool:
    """已解析且结果完整(state=done、目录存在、能找到 content_list.json,且哈希匹配)。"""
    e = get_entry(name)
    if not e or e.get("state") != "done":
        return False
    d = e.get("local_dir")
    if not d or not os.path.isdir(d) or find_content_list(d) is None:
        return False
    if expect_hash and e.get("file_hash") and e["file_hash"] != expect_hash:
        return False
    return True
