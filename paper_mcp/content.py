"""检索/阅读层:在已缓存的 content_list.json 之上提供结构化访问。

content_list.json 是 MinerU 输出的"扁平、按阅读顺序"块列表,每块含:
  type(text|image|table|equation|...)、page_idx(0 起)、bbox
  text  : text、可选 text_level(>=1 为标题层级)
  image : img_path、image_caption[]、image_footnote[]
  table : table_body(HTML)、table_caption[]、table_footnote[]、可选 img_path
  equation: latex / text
"""
import json
import os
import re

from . import cache, config


class NotParsed(Exception):
    """论文尚未解析完成 / 缓存缺失。"""


def load_paper(paper: str) -> "Paper":
    name = config.paper_key(paper)
    e = cache.get_entry(name)
    if not e or e.get("state") != "done" or not e.get("local_dir"):
        raise NotParsed(
            f"论文 {name!r} 尚未解析完成。请先调用 parse_papers 解析(阻塞,完成后再检索)。"
        )
    cl = cache.find_content_list(e["local_dir"])
    if not cl:
        raise NotParsed(f"缓存中未找到 content_list.json(目录:{e['local_dir']}),建议重新解析(force=True)。")
    return Paper(name, e["local_dir"], cl)


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v if str(x).strip()]
    return [str(v)] if str(v).strip() else []


def _join(v, sep=" ") -> str:
    return sep.join(_as_list(v)).strip()


def _html_table_to_md(html: str) -> str:
    """把 MinerU 的表格 HTML 尽力转成 Markdown 表格;失败则原样返回 HTML。忽略 col/rowspan。"""
    if not html or "<" not in html:
        return html or ""
    try:
        from html.parser import HTMLParser

        class _T(HTMLParser):
            def __init__(self):
                super().__init__()
                self.rows, self.cur, self.cell = [], None, None

            def handle_starttag(self, tag, attrs):
                if tag == "tr":
                    self.cur = []
                elif tag in ("td", "th"):
                    self.cell = []

            def handle_endtag(self, tag):
                if tag in ("td", "th") and self.cell is not None:
                    self.cur.append(" ".join("".join(self.cell).split()))
                    self.cell = None
                elif tag == "tr" and self.cur is not None:
                    self.rows.append(self.cur)
                    self.cur = None

            def handle_data(self, data):
                if self.cell is not None:
                    self.cell.append(data)

        p = _T()
        p.feed(html)
        rows = [r for r in p.rows if r]
        if not rows:
            return html
        ncol = max(len(r) for r in rows)
        rows = [r + [""] * (ncol - len(r)) for r in rows]
        esc = lambda c: c.replace("|", "\\|")
        out = ["| " + " | ".join(esc(c) for c in rows[0]) + " |",
               "| " + " | ".join(["---"] * ncol) + " |"]
        out += ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows[1:]]
        return "\n".join(out)
    except Exception:  # noqa: BLE001
        return html


class Paper:
    def __init__(self, name, local_dir, content_list_path):
        self.name = name
        self.local_dir = local_dir
        with open(content_list_path, "r", encoding="utf-8") as f:
            blocks = json.load(f)
        self.blocks = blocks if isinstance(blocks, list) else []
        self._fig_idx = [i for i, b in enumerate(self.blocks) if b.get("type") == "image"]
        self._tbl_idx = [i for i, b in enumerate(self.blocks) if b.get("type") == "table"]
        self.fig_id = {idx: f"F{k + 1}" for k, idx in enumerate(self._fig_idx)}
        self.tbl_id = {idx: f"T{k + 1}" for k, idx in enumerate(self._tbl_idx)}

    # ---------------- 内部辅助 ----------------
    @staticmethod
    def _page(b) -> int:
        return int(b.get("page_idx", 0) or 0) + 1  # 转 1-based

    @staticmethod
    def _level(b) -> int:
        try:
            return int(b.get("text_level") or 0)
        except (TypeError, ValueError):
            return 0

    def _abs_img(self, img_path):
        """把 content_list 里的(通常相对)img_path 解析成缓存内的绝对路径。"""
        if not img_path:
            return None
        if os.path.isabs(img_path) and os.path.isfile(img_path):
            return img_path
        cand = os.path.join(self.local_dir, img_path)
        if os.path.isfile(cand):
            return os.path.abspath(cand)
        base = os.path.basename(img_path)
        for root, _d, files in os.walk(self.local_dir):
            if base in files:
                return os.path.join(root, base)
        return os.path.abspath(cand)  # 兜底

    # ---------------- info / outline ----------------
    def info(self) -> dict:
        pages = max((self._page(b) for b in self.blocks), default=0)
        n_eq = sum(1 for b in self.blocks if b.get("type") == "equation")
        title = None
        for b in self.blocks:
            if b.get("type") == "text" and self._level(b) == 1:
                title = (b.get("text") or "").strip()
                break
        return {
            "name": self.name,
            "title": title,
            "pages": pages,
            "n_figures": len(self._fig_idx),
            "n_tables": len(self._tbl_idx),
            "n_equations": n_eq,
            "local_dir": self.local_dir,
        }

    def outline(self) -> list:
        items = []
        for b in self.blocks:
            if b.get("type") != "text":
                continue
            lvl = self._level(b)
            txt = (b.get("text") or "").strip()
            if lvl >= 1 and txt:
                items.append({"level": lvl, "title": txt, "page": self._page(b)})
        return items

    def outline_text(self) -> str:
        items = self.outline()
        if not items:
            return "(未识别到标题层级;可用 read_paper 按页范围分段通读。)"
        return "\n".join(f"{'  ' * (it['level'] - 1)}- {it['title']} (p.{it['page']})" for it in items)

    # ---------------- 正文阅读 ----------------
    def _render(self, i) -> str:
        b = self.blocks[i]
        t = b.get("type")
        if t == "text":
            lvl = self._level(b)
            txt = (b.get("text") or "").strip()
            if not txt:
                return ""
            return f"{'#' * min(lvl, 6)} {txt}" if lvl >= 1 else txt
        if t == "equation":
            latex = (b.get("latex") or b.get("text") or "").strip()
            return f"$$ {latex} $$" if latex else ""
        if t == "image":
            cap = _join(b.get("image_caption"))
            return f"[图 {self.fig_id.get(i, 'F?')}(p.{self._page(b)}){': ' + cap if cap else ''} —— 用 view_image 看图]"
        if t == "table":
            cap = _join(b.get("table_caption"))
            return f"[表 {self.tbl_id.get(i, 'T?')}(p.{self._page(b)}){': ' + cap if cap else ''} —— 用 get_table 取表格]"
        return ""

    def _section_indices(self, section: str) -> list:
        s = section.strip().lower()
        start, start_level = None, 1
        for i, b in enumerate(self.blocks):
            if b.get("type") == "text" and self._level(b) >= 1 and s in (b.get("text") or "").strip().lower():
                start, start_level = i, self._level(b)
                break
        if start is None:
            return []
        out = [start]
        for i in range(start + 1, len(self.blocks)):
            b = self.blocks[i]
            if b.get("type") == "text" and 1 <= self._level(b) <= start_level:
                break  # 遇到同级或更高级标题即止
            out.append(i)
        return out

    def read(self, section=None, page_start=None, page_end=None, max_chars=8000) -> str:
        if section:
            idxs = self._section_indices(section)
            if not idxs:
                return f"(未找到章节 {section!r};可用 get_paper_outline 查看章节名。)"
        elif page_start or page_end:
            ps = page_start or 1
            pe = page_end or 10 ** 9
            idxs = [i for i, b in enumerate(self.blocks) if ps <= self._page(b) <= pe]
        else:
            idxs = list(range(len(self.blocks)))

        parts, total, truncated = [], 0, False
        for i in idxs:
            s = self._render(i)
            if not s:
                continue
            if parts and total + len(s) > max_chars:
                truncated = True
                break
            parts.append(s)
            total += len(s) + 2
        body = "\n\n".join(parts) if parts else "(所选范围无正文内容)"
        if truncated:
            body += f"\n\n…(已达 {max_chars} 字上限而截断;请缩小 section 或用更小的页范围继续读)"
        return body

    # ---------------- 检索 ----------------
    def search(self, query, regex=False, max_hits=20, ctx=90) -> list:
        if not query:
            return []
        if regex:
            try:
                pat = re.compile(query, re.I)
            except re.error as e:
                return [{"error": f"正则无效:{e}"}]
        hits, cur_section = [], None
        for b in self.blocks:
            if b.get("type") == "text" and self._level(b) >= 1:
                cur_section = (b.get("text") or "").strip()
            if b.get("type") != "text":
                continue
            txt = b.get("text") or ""
            m = pat.search(txt) if regex else None
            pos = m.start() if m else (txt.lower().find(query.lower()) if not regex else -1)
            if pos < 0:
                continue
            a = max(0, pos - ctx)
            snippet = txt[a: pos + len(query) + ctx].strip().replace("\n", " ")
            hits.append({"page": self._page(b), "section": cur_section, "snippet": snippet})
            if len(hits) >= max_hits:
                break
        return hits

    # ---------------- 图 ----------------
    def figures(self) -> list:
        out = []
        for i in self._fig_idx:
            b = self.blocks[i]
            p = self._abs_img(b.get("img_path"))
            out.append({
                "id": self.fig_id[i],
                "page": self._page(b),
                "caption": _join(b.get("image_caption")),
                "footnote": _join(b.get("image_footnote")),
                "path": p,
                "kb": round(os.path.getsize(p) / 1024, 1) if p and os.path.isfile(p) else None,
            })
        return out

    def figure(self, fid: str) -> dict:
        fid = (fid or "").strip().upper()
        for i in self._fig_idx:
            if self.fig_id[i] == fid:
                b = self.blocks[i]
                return {
                    "id": fid,
                    "page": self._page(b),
                    "caption": _join(b.get("image_caption")),
                    "footnote": _join(b.get("image_footnote")),
                    "path": self._abs_img(b.get("img_path")),
                    "hint": "如需查看图片内容,请把上面的 path 传给 view_image 工具读取。",
                }
        return {"error": f"未找到图 {fid};可用 list_figures 查看全部。"}

    # ---------------- 表 ----------------
    def tables(self) -> list:
        out = []
        for i in self._tbl_idx:
            b = self.blocks[i]
            out.append({
                "id": self.tbl_id[i],
                "page": self._page(b),
                "caption": _join(b.get("table_caption")),
                "footnote": _join(b.get("table_footnote")),
                "has_html": bool(b.get("table_body")),
                "img_path": self._abs_img(b.get("img_path")) if b.get("img_path") else None,
            })
        return out

    def table(self, tid: str, fmt="markdown") -> dict:
        tid = (tid or "").strip().upper()
        fmt = fmt if fmt in ("markdown", "html") else "markdown"
        for i in self._tbl_idx:
            if self.tbl_id[i] == tid:
                b = self.blocks[i]
                html = b.get("table_body") or ""
                return {
                    "id": tid,
                    "page": self._page(b),
                    "caption": _join(b.get("table_caption")),
                    "footnote": _join(b.get("table_footnote")),
                    "format": fmt,
                    "table": html if fmt == "html" else _html_table_to_md(html),
                    "img_path": self._abs_img(b.get("img_path")) if b.get("img_path") else None,
                }
        return {"error": f"未找到表 {tid};可用 list_tables 查看全部。"}

    # ---------------- 全文 markdown ----------------
    def markdown(self) -> str:
        md = cache.find_full_md(self.local_dir)
        if md:
            with open(md, "r", encoding="utf-8") as f:
                return f.read()
        return self.read(max_chars=10 ** 9)
