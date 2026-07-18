"""配置与路径解析:集中管理环境变量、目录约定与安全路径处理。

环境变量:
- CLAUDE_PROJECT_DIR : 项目根(papers/ 与输出目录的根),默认当前工作目录
- MINERU_API_TOKEN   : MinerU 云端 API Token(必填,见 https://mineru.net)
- MINERU_API_BASE    : API 基地址,默认 https://mineru.net/api/v4
- MINERU_MODEL_VERSION / MINERU_POLL_INTERVAL / MINERU_MAX_WAIT / MINERU_HTTP_TIMEOUT : 可选调优
"""
import os


def _abs(p: str) -> str:
    return os.path.abspath(os.path.expanduser(p))


BASE_DIR = _abs(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
PAPERS_DIR = os.path.join(BASE_DIR, "papers")
CACHE_DIR = os.path.join(BASE_DIR, ".cache", "mineru")

# ---------------- MinerU 云端 API ----------------
MINERU_API_TOKEN = (os.environ.get("MINERU_API_TOKEN") or "").strip()
MINERU_API_BASE = (os.environ.get("MINERU_API_BASE") or "https://mineru.net/api/v4").rstrip("/")

DEFAULT_MODEL_VERSION = (os.environ.get("MINERU_MODEL_VERSION") or "vlm").strip()
POLL_INTERVAL = float(os.environ.get("MINERU_POLL_INTERVAL") or 30.0)   # 阻塞等待时的轮询周期(秒)
DEFAULT_MAX_WAIT = float(os.environ.get("MINERU_MAX_WAIT") or 1800.0)    # 便捷阻塞的总超时(秒) - 半小时
HTTP_TIMEOUT = float(os.environ.get("MINERU_HTTP_TIMEOUT") or 1800.0)


class MineruConfigError(RuntimeError):
    """配置缺失(如未设置 Token)。"""


def require_token() -> str:
    if not MINERU_API_TOKEN:
        raise MineruConfigError(
            "未配置 MINERU_API_TOKEN。请在 MCP 客户端的 env 中设置该环境变量"
            "(在 https://mineru.net 申请 API Token)。"
        )
    return MINERU_API_TOKEN


# ---------------- 路径解析 ----------------
def resolve_pdf(pdf_path: str) -> str:
    """把用户给的名字解析成真实存在的 PDF 绝对路径。

    依次尝试:原样(绝对/相对) → papers/ 下 → BASE_DIR 下;都允许省略 .pdf。
    找不到时抛出带"已尝试位置"的清晰错误。
    """
    if not pdf_path:
        raise FileNotFoundError("pdf_path 为空。请传论文文件名(可省略 .pdf)。")
    raw = pdf_path
    names = [raw] if raw.lower().endswith(".pdf") else [raw, raw + ".pdf"]
    cands = []
    for name in names:
        cands.append(name)
        cands.append(os.path.join(PAPERS_DIR, name))
        cands.append(os.path.join(BASE_DIR, name))
    for c in cands:
        if os.path.isfile(c):
            return os.path.abspath(c)
    tried = "\n  - ".join(dict.fromkeys(os.path.abspath(c) for c in cands))
    raise FileNotFoundError(
        f"找不到 PDF:{raw!r}\n论文目录(papers):{PAPERS_DIR}\n已尝试以下位置:\n  - {tried}\n"
        f"提示:可用 papers://catalog 列出的文件名(支持省略 .pdf)。"
    )


def paper_key(pdf_path: str) -> str:
    """统一的论文标识:去掉目录与 .pdf 后缀的文件名(缓存 / 工具入参的规范键)。"""
    base = os.path.basename(pdf_path or "")
    if base.lower().endswith(".pdf"):
        base = base[:-4]
    return base


# ---------------- 输出沙箱 ----------------
def is_within(base: str, target: str) -> bool:
    base, target = _abs(base), _abs(target)
    return target == base or target.startswith(base + os.sep)


def safe_output_path(output_path: str) -> str:
    """把输出路径夹到 BASE_DIR 内,禁止越界写。相对路径按 BASE_DIR 解析。"""
    if not output_path:
        raise ValueError("output_path 为空。")
    p = output_path if os.path.isabs(output_path) else os.path.join(BASE_DIR, output_path)
    p = _abs(p)
    if not is_within(BASE_DIR, p):
        raise ValueError(f"拒绝写入项目目录之外的位置:{p}(允许根:{BASE_DIR})")
    return p
