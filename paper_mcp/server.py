"""Paper-Assistant-MCP —— FastMCP 实例与工具/资源/提示注册(基于 MinerU 云端 API)。

分层职责:
  A. 解析编排   parse_papers(阻塞,内含轮询与进度播报)            → parsing.py
  B. 检索/阅读  get_paper_info / get_paper_outline / read_paper /
               search_paper / list_figures / get_figure / view_image /
               list_tables / get_table                          → content.py
  C. 产出       write_file / collect_figures                     → output.py
  D. 资源/提示  papers://catalog / papers://{name}/markdown / papers_to_pre
"""
import asyncio
import os
import sys

import anyio
from mcp.server.fastmcp import Context, FastMCP, Image

from . import cache, config, output, parsing
from .content import NotParsed, load_paper

mcp = FastMCP(name="Paper-Assistant-MCP")


def _guard(fn):
    """统一异常兜底:NotParsed / 其它异常 → {"error": ...},避免把栈抛给客户端。"""
    try:
        return fn()
    except NotParsed as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


# ==================== A. 解析编排(阻塞) ====================
@mcp.tool(description=(
    "把 ./papers 下的 PDF 提交给 MinerU 云端解析(端到端:文/图/表/公式)并【阻塞等待】完成:"
    "提交后每 30s 自动轮询一次,期间以【进度条】+ 状态消息播报进度(解析中带页进度/下载中/解压中),"
    "直到全部完成或超时(max_wait 秒,默认 1800≈30 分钟)。已解析的按文件哈希命中缓存跳过;"
    "上次未完成的会续等原批次、不重复上传。papers 传 'all' 或文件名(可省略 .pdf)/文件名列表。"))
async def parse_papers(ctx: Context, papers="all", model_version: str = None,
                       enable_formula: bool = True, enable_table: bool = True,
                       language: str = None, ocr: bool = False, force: bool = False,
                       max_wait: float = None, poll_interval: float = None):
    loop = asyncio.get_running_loop()
    last = {"completed": 0.0, "total": None}   # 记住最近一次进度,供 keep-alive 复用

    def notify(msg: str, completed: float = None, total: float = None):
        # 经 stdio 播报状态,绝不 print 到 stdout(那是 JSON-RPC 协议通道)。
        # keep-alive:MCP 里只有【进度通知】能重置客户端的空闲超时,日志通知不能。
        # 所以下载/解压等只带文案的播报,也【复用上次进度值补发一次 report_progress】,
        # 确保每次播报都刷新空闲计时器,避免长时间下载被误判为卡死。
        print(f"[MinerU] {msg}", file=sys.stderr, flush=True)
        if completed is not None and total:
            last["completed"], last["total"] = completed, total
        prog, tot = last["completed"], last["total"]

        async def _emit():
            if tot:
                await ctx.report_progress(progress=prog, total=tot, message=msg)
            else:
                await ctx.info(msg)   # 尚无进度基准时退化为日志

        try:
            asyncio.run_coroutine_threadsafe(_emit(), loop).result(timeout=5)
        except Exception:  # noqa: BLE001 —— 客户端不支持进度/日志或跨线程调度失败都不应打断解析
            pass

    def run():
        return parsing.parse(
            papers, model_version, enable_formula, enable_table, language, ocr, force,
            max_wait, poll_interval, notify=notify)

    # 阻塞循环(time.sleep + 同步 httpx)放到工作线程,避免卡住事件循环,
    # notify 再经 run_coroutine_threadsafe 把播报调度回事件循环。
    try:
        return await anyio.to_thread.run_sync(run)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


# ==================== B. 检索 / 阅读 ====================
@mcp.tool(description="返回论文概览:标题、页数、图/表/公式数量、缓存目录。需先解析。")
def get_paper_info(paper: str):
    return _guard(lambda: load_paper(paper).info())


@mcp.tool(description="返回论文章节大纲(来自 MinerU 的标题层级,含页码,保持阅读顺序)。需先解析。")
def get_paper_outline(paper: str):
    return _guard(lambda: load_paper(paper).outline_text())


@mcp.tool(description=(
    "分段读正文(markdown)。三选一定位:section(章节名,模糊匹配)/ page_start+page_end(页范围)/ 都不填读全文。"
    "默认 max_chars=8000 上限截断,请分段读以控制上下文。图/表在正文中以 [图 Fk]/[表 Tk] 占位,"
    "需要时再用 view_image / get_table 获取。"))
def read_paper(paper: str, section: str = None, page_start: int = None,
               page_end: int = None, max_chars: int = 8000):
    return _guard(lambda: load_paper(paper).read(section, page_start, page_end, max_chars))


@mcp.tool(description="在论文正文中检索关键词或正则,返回命中片段 + 所在页/章节。用于快速定位而不必通读。")
def search_paper(paper: str, query: str, regex: bool = False, max_hits: int = 20):
    return _guard(lambda: load_paper(paper).search(query, regex, max_hits))


@mcp.tool(description=(
    "列出论文所有图:id(F1、F2…)、页码、图注 caption、脚注、本地路径、大小。"
    "先看清单判断哪些关键,再对关键图用 view_image 真正看图。"))
def list_figures(paper: str):
    return _guard(lambda: load_paper(paper).figures())


@mcp.tool(description=(
    "取某张图的路径 + 图注 + 脚注(不直接返回图片内容)。"
    "如需查看图片本身,请把返回的 path 传给 view_image 工具。"))
def get_figure(paper: str, figure_id: str):
    return _guard(lambda: load_paper(paper).figure(figure_id))


@mcp.tool(description=(
    "读取并返回图片内容(PNG/JPG)供模型直接'看图'理解——理解关键图表时调用。"
    "image_path 用 list_figures / get_figure 返回的 path。仅允许读取解析缓存或项目目录内的图片。"))
def view_image(image_path: str):
    p = os.path.abspath(image_path or "")
    if not (config.is_within(config.CACHE_DIR, p) or config.is_within(config.BASE_DIR, p)):
        return {"error": f"拒绝读取受限目录之外的文件:{p}"}
    if not os.path.isfile(p):
        return {"error": f"文件不存在:{p}"}
    ext = os.path.splitext(p)[1].lower().lstrip(".") or "png"
    if ext == "jpg":
        ext = "jpeg"
    with open(p, "rb") as f:
        return Image(data=f.read(), format=ext)


@mcp.tool(description="列出论文所有表:id(T1、T2…)、页码、表注、是否有可解析 HTML、表格截图路径(若有)。")
def list_tables(paper: str):
    return _guard(lambda: load_paper(paper).tables())


@mcp.tool(description="取某张表:format='markdown'(默认,由 MinerU 的 HTML 转换)或 'html'(原始)。含表注/脚注/截图路径。")
def get_table(paper: str, table_id: str, format: str = "markdown"):
    return _guard(lambda: load_paper(paper).table(table_id, format))


# ==================== C. 产出 ====================
@mcp.tool(description="把文本写入文件(UTF-8,自动建父目录)。仅允许写在项目目录(BASE_DIR)内。用于落盘 HTML/Markdown/讲稿。")
def write_file(output_path: str, content: str):
    return _guard(lambda: output.write_file(output_path, content))


@mcp.tool(description=(
    "把指定图(figure_ids:如 ['F1','F3'])拷贝到 dest_dir(项目内,如 ./pre/figs/<论文名>),"
    "返回可用于 <img src> 的相对引用路径(已 URL 转义)。用于拼装带图的汇报 HTML。"))
def collect_figures(paper: str, figure_ids, dest_dir: str):
    return _guard(lambda: output.collect_figures(paper, figure_ids, dest_dir))


# ==================== D. Resources ====================
@mcp.resource("papers://catalog", description="列出 ./papers 下所有论文及解析状态(已解析/解析中/未解析,含页数与图表数)。")
def catalog() -> str:
    if not os.path.isdir(config.PAPERS_DIR):
        return f"(papers 目录不存在:{config.PAPERS_DIR})"
    names = sorted(f for f in os.listdir(config.PAPERS_DIR) if f.lower().endswith(".pdf"))
    if not names:
        return "(papers 目录为空)"
    entries = cache.all_entries()
    lines = [
        f"论文目录:{config.PAPERS_DIR}",
        "用法:各工具 pdf_path/paper 直接填文件名(可省略 .pdf)。未解析的先调用 parse_papers 解析。",
        "—" * 24,
    ]
    for f in names:
        key = config.paper_key(f)
        e = entries.get(key)
        if e and e.get("state") == "done" and cache.is_ready(key):
            info = _guard(lambda: load_paper(key).info())
            st = (f"[已解析 · {info['pages']}页 · 图{info['n_figures']} 表{info['n_tables']}]"
                  if isinstance(info, dict) and "pages" in info else "[已解析]")
        elif e and e.get("state") in ("pending", "running", "converting"):
            st = f"[解析中:{e.get('state')}]"
        elif e and e.get("state") == "failed":
            st = f"[解析失败:{e.get('err_msg') or ''}]"
        else:
            st = "[未解析]"
        lines.append(f"- {f} {st}")
    return "\n".join(lines)


@mcp.resource("papers://{name}/markdown", description="返回某篇已解析论文的完整 markdown(MinerU 的 full.md)。")
def paper_markdown(name: str) -> str:
    try:
        return load_paper(name).markdown()
    except NotParsed as e:
        return str(e)
    except Exception as e:  # noqa: BLE001
        return f"读取失败:{type(e).__name__}: {e}"


# ==================== E. Prompt ====================
@mcp.prompt(description="驱动 Agent 基于 MinerU 解析结果,把 ./papers 下论文整理成带图的汇报 HTML + 逐页讲稿。")
def papers_to_pre(papers: str = "all", audience: str = "组会同学", language: str = "中文",
                  output_path: str = "./pre/slides.html", script_path: str = "./pre/script.md") -> str:
    return f"""你要基于 Paper-Assistant-MCP(MinerU 解析)把论文整理成一份用于 pre 的 HTML 展示。按以下步骤:

1. 确定要处理的论文:{papers}。若为 "all",先读取资源 papers://catalog 拿到全部 PDF 文件名及解析状态。
   所有工具的 paper 直接填【文件名】即可(可省略 .pdf)。

2. 解析(端到端得到文/图/表):
   - 对需要的论文调用 parse_papers(papers=...):这是【阻塞】工具,内部每 30s 轮询并播报进度条,不设置超时,直到全部完成才返回。
   - 已解析过的论文会自动命中缓存、上次未完成的会续等原批次,均不会重复消耗额度。

3. 对每一篇论文(逐篇理解):
   a. get_paper_info 看规模,get_paper_outline 看章节结构;
   b. read_paper 按 section 或页范围【分段】读正文(默认已按 max_chars 截断,不要一次读全文);必要时用 search_paper 定位关键结论;
   c. list_figures / list_tables 拿图表清单;判断哪些是关键图,对关键图用 view_image 真正【看图】理解;关键表用 get_table 取内容;
   d. 总结该论文:研究问题、方法、关键结果、结论;
   e. 用 write_file 把该篇总结写到 ./pre/docs/<论文名>_summary.md。

4. 汇总所有论文,生成面向【{audience}】、用【{language}】的 HTML 幻灯片:
   - 每篇 2-4 页 slide,含标题、要点;
   - 先用 collect_figures 把选中的关键图拷到 ./pre/figs/<论文名>/,再用返回的 src_from_base 作为 <img src>(注意空格已转义为 %20);
   - 风格简洁、适合投屏。
   - 用 write_file 把 HTML 写到 {output_path}。

5. 为每一页 slide 生成一段贴合内容的讲稿,用 write_file 写到 {script_path}(与 HTML 分开,勿覆盖)。

6. 完成后简要汇报:每篇用了哪几张图/表、HTML 路径({output_path})与讲稿路径({script_path})。"""
