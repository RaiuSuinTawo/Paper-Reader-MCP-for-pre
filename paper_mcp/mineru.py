"""MinerU 云端 API(mineru.net /api/v4)客户端:只负责 HTTP 交互,不涉及缓存/业务。

工作流(上传本地文件):
  request_upload() → 拿到 batch_id 与预签名 URL
  upload_file()    → PUT 文件到预签名 URL(系统在上传完成后自动建任务)
  batch_results()  → 轮询任务状态,done 时得到 full_zip_url
  download_zip()   → 下载并解压结果
"""
import io
import os
import zipfile

import httpx

from . import config


class MineruError(RuntimeError):
    """MinerU API 返回错误(code != 0 或 HTTP 异常)。"""


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.require_token()}",
        "Content-Type": "application/json",
        "Accept": "*/*",
    }


def _unwrap(resp: httpx.Response) -> dict:
    """校验 HTTP 与业务 code,返回 data 字段。"""
    resp.raise_for_status()
    try:
        payload = resp.json()
    except Exception as e:  # noqa: BLE001
        raise MineruError(f"MinerU 返回非 JSON:{resp.text[:200]!r}") from e
    code = payload.get("code")
    if code not in (0, "0", None):
        raise MineruError(
            f"MinerU API 错误 code={code} msg={payload.get('msg')!r} "
            f"trace_id={payload.get('trace_id')!r}"
        )
    return payload.get("data") or {}


def request_upload(files, model_version, enable_formula=True, enable_table=True, language=None):
    """POST /file-urls/batch:申请上传 URL(文件上传完成后系统自动建任务)。

    files: [{"name": "x.pdf", "data_id": "...", "is_ocr": bool}, ...]
    返回 (batch_id, [upload_url ...]),upload_url 与 files 同序。
    """
    body = {
        "enable_formula": enable_formula,
        "enable_table": enable_table,
        "files": files,
    }
    if language:
        body["language"] = language
    if model_version:
        body["model_version"] = model_version
    url = f"{config.MINERU_API_BASE}/file-urls/batch"
    with httpx.Client(timeout=config.HTTP_TIMEOUT) as c:
        data = _unwrap(c.post(url, headers=_headers(), json=body))
    return data.get("batch_id"), (data.get("file_urls") or [])


def upload_file(upload_url: str, file_path: str) -> None:
    """PUT 本地文件到预签名 URL。不带鉴权头,也不要设置 Content-Type(与签名保持一致)。"""
    with open(file_path, "rb") as f:
        content = f.read()
    with httpx.Client(timeout=config.HTTP_TIMEOUT) as c:
        r = c.put(upload_url, content=content)
        r.raise_for_status()


def batch_results(batch_id: str) -> list:
    """GET /extract-results/batch/{batch_id} → extract_result 列表。

    每项含 file_name / data_id / state(done|running|pending|converting|failed)/
    err_msg / full_zip_url(done 时)/ extract_progress。
    """
    url = f"{config.MINERU_API_BASE}/extract-results/batch/{batch_id}"
    with httpx.Client(timeout=config.HTTP_TIMEOUT) as c:
        data = _unwrap(c.get(url, headers=_headers()))
    return data.get("extract_result") or []


def download_zip_bytes(zip_url: str) -> bytes:
    """下载 full_zip_url,返回 zip 字节(不解压)。"""
    with httpx.Client(timeout=config.HTTP_TIMEOUT, follow_redirects=True) as c:
        r = c.get(zip_url)
        r.raise_for_status()
        return r.content


def extract_zip(blob: bytes, dest_dir: str) -> str:
    """把 zip 字节解压到 dest_dir,返回 dest_dir。"""
    os.makedirs(dest_dir, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        z.extractall(dest_dir)
    return dest_dir


def download_zip(zip_url: str, dest_dir: str) -> str:
    """下载并解压(= download_zip_bytes + extract_zip)。"""
    return extract_zip(download_zip_bytes(zip_url), dest_dir)
