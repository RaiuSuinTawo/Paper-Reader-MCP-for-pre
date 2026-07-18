"""解析编排层:串起 mineru(云端)与 cache(本地),对外由 server.py 暴露为单个阻塞工具。

parse():阻塞式解析。查缓存 → 对未解析的申请上传 URL 并上传 → 每 poll_interval 秒轮询,
        直到全部完成或超时。可续等:已在解析中的论文(按文件哈希命中在途批次)不重复上传,
        直接续等原批次,避免超时重试时白烧 MinerU 额度。

notify:Callable[[msg, completed=None, total=None], None]。parse() 在不同阶段用
【预置的多条文案随机选一条】调用它:
- 周期性"解析中"带 (completed, total) → 上层桥接成 MCP 进度条 notifications/progress;
- 下载中 / 解压中等瞬时子步骤只传 msg → 上层桥接成日志通知 ctx.info。
两者都同时写 stderr。
"""
import os
import random
import time

from . import cache, config, mineru

# ---------------- 分阶段状态文案(随机选一条) ----------------
_STATUS = {
    "parsing": [
        "MinerU 正在加速解析中,请稍候…",
        "解析引擎全力运转中,马上就好…",
        "正在识别文字 / 公式 / 表格,稍等片刻…",
        "论文有点长,MinerU 正在努力啃它…",
        "解析进行中,已经在路上啦…",
        "AI 正在逐页拆解论文,请再等一会儿…",
    ],
    "download": [
        "解析完成!正在下载解析后的文档…",
        "解析搞定,开始拉取结果包…",
        "解析已完成,正在把结果搬回本地…",
    ],
    "extract": [
        "解析完成的文档解压中…",
        "正在解压结果,马上就能用…",
        "结果包解压中,即将就绪…",
    ],
}


def _msg(phase: str, extra: str = "") -> str:
    m = random.choice(_STATUS.get(phase) or ["处理中…"])
    return f"{m} {extra}".rstrip()


def _progress_extra(tasks, pending) -> str:
    """从仍在解析的任务里取一条页进度,拼成 '(3/12 页)'。取不到则空。"""
    for t in tasks:
        pr = t.get("progress")
        if t.get("name") in pending and isinstance(pr, dict):
            ep, tp = pr.get("extracted_pages"), pr.get("total_pages")
            if ep is not None and tp:
                return f"({ep}/{tp} 页)"
    return ""


def _progress(tasks, pending):
    """把整批完成度折算成 (completed, total):以"论文数"为单位,

    每篇 done/failed 记 1.0,解析中按 已解析页/总页 折算小数 → 平滑的进度条。
    """
    total = len(pending)
    done = 0.0
    for t in tasks:
        if t.get("name") not in pending:
            continue
        if t.get("state") in ("done", "failed"):
            done += 1.0
        else:
            pr = t.get("progress")
            if isinstance(pr, dict) and pr.get("total_pages"):
                ep = pr.get("extracted_pages") or 0
                done += min(0.99, ep / pr["total_pages"])
    return round(done, 3), total


def _select_papers(papers):
    """把 "all" / 单名 / 名字列表(或逗号分隔)解析成 [(paper_key, abs_path), ...]。"""
    if papers is None or papers == "all" or papers == ["all"]:
        if not os.path.isdir(config.PAPERS_DIR):
            return []
        names = sorted(f for f in os.listdir(config.PAPERS_DIR) if f.lower().endswith(".pdf"))
        return [(config.paper_key(n), os.path.join(config.PAPERS_DIR, n)) for n in names]
    if isinstance(papers, str):
        papers = [p for p in papers.split(",")] if "," in papers else [papers]
    out = []
    for p in papers:
        path = config.resolve_pdf(p.strip() if isinstance(p, str) else p)  # 缺失时抛 FileNotFoundError
        out.append((config.paper_key(path), path))
    return out


def _classify(selected, force):
    """分三类:已缓存 / 在途可续等(name→batch_id) / 需上传[(name,path,hash)]。"""
    cached, resume, to_upload = [], {}, []
    for name, path in selected:
        h = cache.file_hash(path)
        if not force and cache.is_ready(name, expect_hash=h):
            cached.append({"name": name, "state": "done", "cached": True,
                           "local_dir": cache.get_entry(name)["local_dir"]})
            continue
        e = None if force else cache.get_entry(name)
        if e and e.get("batch_id") and e.get("file_hash") == h \
                and e.get("state") in ("pending", "running", "converting"):
            resume[name] = e["batch_id"]        # 已在解析中 → 续等原批次,不重复上传
        else:
            to_upload.append((name, path, h))
    return cached, resume, to_upload


def _upload_batch(to_upload, model_version, enable_formula, enable_table, language, ocr):
    """对需上传的文件:申请上传 URL → PUT 上传(系统自动建任务)。

    返回 (batch_id, uploaded_names, failed_tasks)。逐文件容错:个别上传失败记 failed 继续。
    """
    files = [{"name": os.path.basename(path), "data_id": h[:32], "is_ocr": ocr}
             for _n, path, h in to_upload]
    batch_id, urls = mineru.request_upload(files, model_version, enable_formula, enable_table, language)
    if len(urls) < len(to_upload):
        raise RuntimeError(f"MinerU 返回的上传 URL 数({len(urls)})少于文件数({len(to_upload)})。")

    uploaded, failed = [], []
    for (name, path, h), url in zip(to_upload, urls):
        try:
            mineru.upload_file(url, path)
            cache.set_entry(name, file_hash=h, batch_id=batch_id, data_id=h[:32],
                            state="pending", model_version=model_version, local_dir="", err_msg="")
            uploaded.append(name)
        except Exception as e:  # noqa: BLE001
            cache.set_entry(name, state="failed", err_msg=f"上传失败:{e}")
            failed.append({"name": name, "state": "failed", "cached": False, "err_msg": str(e)})
    return batch_id, uploaded, failed


def _settle(batch_id: str, notify=None) -> list:
    """拉取一个批次的结果;done 且未缓存时下载+解压进 manifest。

    notify(msg):可选。
    其并不在 parse的[启动 - 轮询] 循环之内
    下载/解压时被阻塞了，只在开始前各播报一条随机文案，如果下载或解压时间太长可能有隐患。
    """
    out = []
    for r in mineru.batch_results(batch_id):
        name = cache.name_by_data_id(r.get("data_id")) or config.paper_key(r.get("file_name", ""))
        state = r.get("state", "unknown")
        item = {"name": name, "state": state, "err_msg": r.get("err_msg") or "",
                "progress": r.get("extract_progress")}
        if state == "done" and r.get("full_zip_url"):
            if not cache.is_ready(name):
                dest = cache.result_dir(name)
                try:

                    if notify:
                        notify(_msg("download", f"[{name}]"))
                    blob = mineru.download_zip_bytes(r["full_zip_url"])

                    if notify:
                        notify(_msg("extract", f"[{name}]"))
                    mineru.extract_zip(blob, dest)

                    cache.set_entry(name, state="done", local_dir=dest, batch_id=batch_id, err_msg="")
                except Exception as e:  # noqa: BLE001
                    item["state"] = "failed"
                    item["err_msg"] = f"下载/解压失败:{e}"
                    cache.set_entry(name, state="failed", err_msg=item["err_msg"])
                    out.append(item)
                    continue
            item["local_dir"] = (cache.get_entry(name) or {}).get("local_dir")
        elif state == "failed":
            cache.set_entry(name, state="failed", batch_id=batch_id, err_msg=r.get("err_msg") or "")
        else:
            cache.set_entry(name, state=state, batch_id=batch_id)
        out.append(item)
    return out


def parse(papers="all", model_version=None, enable_formula=True, enable_table=True,
          language=None, ocr=False, force=False, max_wait=None, poll_interval=None,
          notify=None) -> dict:
    """阻塞式解析:提交 → 每 poll_interval 秒轮询,直到全部完成或超时。

    - 已解析的按文件哈希命中缓存,直接返回;
    - 在解析中的(上次提交尚未完成)续等原批次,不重复上传;
    - 其余上传新批次。每轮未完成经 notify 播报进度(进度条 + 文案),再等一个周期。
    """
    model_version = model_version or config.DEFAULT_MODEL_VERSION
    max_wait = config.DEFAULT_MAX_WAIT if max_wait is None else max_wait
    poll_interval = config.POLL_INTERVAL if poll_interval is None else poll_interval

    try:
        selected = _select_papers(papers)
    except FileNotFoundError as e:
        return {"error": str(e)}
    if not selected:
        return {"error": f"没有可解析的 PDF(papers 目录:{config.PAPERS_DIR})"}

    cached, resume, to_upload = _classify(selected, force)
    tasks = list(cached)
    batch_ids = set(resume.values())

    if to_upload:
        try:
            batch_id, uploaded, failed = _upload_batch(
                to_upload, model_version, enable_formula, enable_table, language, ocr)
        except Exception as e:  # noqa: BLE001
            return {"error": f"提交解析失败:{type(e).__name__}: {e}"}
        if batch_id:
            batch_ids.add(batch_id)
        tasks += [{"name": n, "state": "pending", "cached": False} for n in uploaded]
        tasks += failed

    pending = set(resume) | {t["name"] for t in tasks if t.get("state") == "pending"}
    if not pending:
        note = None if to_upload else "全部命中缓存,无需重新解析。"
        return {"tasks": tasks, "note": note}

    total_n = len(pending)
    if notify:
        notify("已提交,MinerU 开始解析…", 0, total_n)   # 进度条起点(0%)

    deadline = time.monotonic() + max_wait
    last = tasks
    while True:
        try:
            polled = []
            for bid in batch_ids:
                polled += _settle(bid, notify=notify)   # 轮询各批次;完成的就地下载解压(带播报)
        except Exception as e:  # noqa: BLE001
            return {"error": f"查询批次失败:{type(e).__name__}: {e}"}

        last = [t for t in polled if t["name"] in pending] + cached
        settled = {t["name"] for t in last if t["state"] in ("done", "failed")}
        if pending.issubset(settled):
            if notify:
                notify("全部解析完成 ✓", total_n, total_n)   # 进度条到 100%
            return {"tasks": last}
        if time.monotonic() >= deadline:
            return {"tasks": last, "timeout": True,
                    "note": (f"等待超过 {max_wait}s 仍未全部完成。可再次调用 parse_papers"
                             "(已完成的命中缓存、仍在解析的自动续等同一批次,均不会重复解析)。")}

        if notify:                                    # 未完成 → 播报"解析中"+ 推进进度条,再等一个周期
            completed, _ = _progress(last, pending)
            notify(_msg("parsing", _progress_extra(last, pending)), completed, total_n)
        time.sleep(poll_interval)
