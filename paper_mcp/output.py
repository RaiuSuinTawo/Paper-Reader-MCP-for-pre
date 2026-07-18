"""产出层:安全写文件 + 把选中的图收集到 pre 目录。

所有写操作都夹在 BASE_DIR 内(safe_output_path),避免越界写。
"""
import os
import re
import shutil
import urllib.parse

from . import config
from .content import load_paper


def _sanitize(name: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_. ")
    return s[:80] or "img"


def write_file(output_path: str, content: str) -> dict:
    if output_path is None or content is None:
        return {"error": f"参数有误:output_path={output_path!r}, content_为空={content is None}"}
    try:
        path = config.safe_output_path(output_path)
    except ValueError as e:
        return {"error": str(e)}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return {"ok": True, "path": path, "chars": len(content)}


def collect_figures(paper: str, figure_ids, dest_dir: str) -> dict:
    """把指定图拷进 dest_dir(项目内),返回可用于 <img src> 的相对引用路径。

    src_from_base:相对 BASE_DIR 的路径(已做 URL 转义,空格→%20)。
    若 HTML 与图不在同层,请据实际位置再自行调整相对路径。
    """
    try:
        p = load_paper(paper)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    try:
        dest = config.safe_output_path(dest_dir)
    except ValueError as e:
        return {"error": str(e)}
    os.makedirs(dest, exist_ok=True)

    if isinstance(figure_ids, str):
        figure_ids = [figure_ids]
    fig_map = {f["id"]: f for f in p.figures()}

    out = []
    for fid in figure_ids:
        f = fig_map.get(str(fid).strip().upper())
        if not f or not f["path"] or not os.path.isfile(f["path"]):
            out.append({"id": fid, "error": "未找到该图或源文件缺失"})
            continue
        ext = os.path.splitext(f["path"])[1] or ".png"
        fname = _sanitize(f"{p.name}_{f['id']}") + ext
        dst = os.path.join(dest, fname)
        shutil.copyfile(f["path"], dst)
        rel = os.path.relpath(dst, config.BASE_DIR).replace(os.sep, "/")
        out.append({
            "id": f["id"],
            "file": fname,
            "path": dst,
            "caption": f["caption"],
            "src_from_base": urllib.parse.quote(rel),
        })
    return {"dest_dir": dest, "count": sum(1 for o in out if "error" not in o), "figures": out}
