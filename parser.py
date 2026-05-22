"""
===============================================================================
学校通知自动爬取与智能总结智能体 — 文本提取层
===============================================================================
职责：
  1. PDF 文件文本提取（pdfplumber 首选 → pypdf 回退）
  2. PDF 下载后的批量文本提取流程
  3. type=1 详情页 HTML 的正文解析（从 lxml 树提取纯文本）

设计原则：
  - PDF 对象在提取文本后立即关闭释放，不常驻内存。
  - 每个 PDF 独立处理，避免多文件同时打开导致内存暴涨。
  - 提取失败不阻断流程，返回空字符串并记录警告。
===============================================================================
"""
import gc
import re
from pathlib import Path
from typing import Optional

# pdfplumber：对中文表格型 PDF 支持好，保真度高（首选）
# pypdf：纯 Python 无外部依赖，兼容性好（回退方案）
# 两者均为轻量库，不作为项目依赖时 import 不会报错

import config


# =============================================================================
# PDF 文本提取
# =============================================================================

def _extract_with_pdfplumber(filepath: str) -> str:
    """
    使用 pdfplumber 提取 PDF 全文。

    逐页提取 → 立即拼接 → 关闭 PDF 对象 → 主动 gc。
    """
    import pdfplumber

    texts: list[str] = []
    with pdfplumber.open(filepath) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                texts.append(page_text)

    # 关闭后主动回收内存（pdfplumber 可能持有较大的 page 缓存）
    gc.collect()
    return "\n".join(texts)


def _extract_with_pypdf(filepath: str) -> str:
    """
    使用 pypdf 提取 PDF 全文（回退方案）。

    当 pdfplumber 不可用或提取结果为空时调用。
    """
    from pypdf import PdfReader

    texts: list[str] = []
    reader = PdfReader(filepath)
    for page in reader.pages:
        page_text = page.extract_text()
        if page_text:
            texts.append(page_text)

    # 显式释放 reader 对象引用
    del reader
    gc.collect()
    return "\n".join(texts)


def extract_pdf_text(filepath: str) -> str:
    """
    从 PDF 文件中提取纯文本内容。

    策略：
      1. 首选 pdfplumber（对中文 PDF 排版保真度更高，尤其适合公文类）
      2. 如果 pdfplumber 不可用 / 提取为空 / 抛异常 → 回退到 pypdf
      3. 提取完成后立即释放 PDF 对象，调用 gc.collect() 回收内存

    Args:
        filepath: PDF 文件的绝对路径

    Returns:
        提取到的纯文本字符串；失败返回空字符串 ""
    """
    if not Path(filepath).exists():
        print(f"  [警告] PDF 文件不存在: {filepath}")
        return ""

    text = ""

    # ---- 首选：pdfplumber ----
    try:
        text = _extract_with_pdfplumber(filepath)
        if text.strip():
            return text
    except Exception as e:
        print(f"  [回退] pdfplumber 提取失败: {e}")

    # ---- 回退：pypdf ----
    if config.PDF_FALLBACK_ENABLED:
        try:
            text = _extract_with_pypdf(filepath)
            if text.strip():
                return text
        except Exception as e:
            print(f"  [错误] pypdf 回退也失败: {e}")

    return text


# =============================================================================
# 批量 PDF 下载 + 文本提取（供 main.py 调用）
# =============================================================================

def process_type2_pdfs(
    type2_notices: list[dict],
    target_date_str: str,
) -> list[dict]:
    """
    对学校发文列表逐条下载 PDF 并提取文本。

    流程：
      1. 创建 requests.Session，先访问列表页获取 OASESSIONID Cookie（鉴权用）
      2. 创建 output/pdfs/{target_date_str}/ 目录
      3. 逐条下载 PDF → 提取文本 → 存入 notice["raw_text"] → 关闭 PDF
      4. 已存在的 PDF 跳过下载（幂等）

    设计理由：
      逐条处理而非批量下载，确保每个 PDF 在提取完文本后立即释放，
      避免同时持有多个 PDF 对象导致内存暴涨。

    Args:
        type2_notices: fetch_type2_list() 的返回值列表
        target_date_str: 日期字符串（如 "2026-05-20"），用于子目录名

    Returns:
        同 type2_notices，但每个 dict 增加了 "raw_text" 字段
    """
    import requests
    from scraper import download_pdf

    # 创建 Session 并先访问列表页，获取 OASESSIONID Cookie（PDF 下载鉴权依赖）
    session = requests.Session()
    session.headers.update(config.HEADERS)
    try:
        resp = session.get(config.TYPE2_LIST_URL, timeout=config.REQUEST_TIMEOUT)
        resp.encoding = config.PAGE_ENCODING
    except requests.RequestException:
        # 即使取 Cookie 失败，仍然尝试下载（某些 PDF 可能无鉴权）
        pass

    pdf_dir = str(Path(config.PDF_OUTPUT_DIR) / target_date_str)

    for i, notice in enumerate(type2_notices):
        title = notice.get("title", "unknown")
        pdf_url = notice.get("pdf_url")

        if not pdf_url:
            print(f"  [跳过] 无 PDF 链接: {title[:40]}")
            notice["raw_text"] = ""
            continue

        # 生成安全文件名：编号_标题前20字.pdf
        number = notice.get("number", "").replace("/", "_").replace("\\", "_")
        safe_title = title[:20].replace("/", "_").replace("\\", "_").replace(":", "_")
        filename = f"{number}_{safe_title}.pdf" if number else f"{safe_title}.pdf"

        # 下载 PDF（传入 Session 携带鉴权 Cookie）
        saved_path = download_pdf(pdf_url, pdf_dir, filename, session=session)
        if not saved_path:
            print(f"  [错误] PDF 下载失败，跳过: {title[:40]}")
            notice["raw_text"] = ""
            continue

        # 提取文本 → 立即释放 PDF 对象
        raw_text = extract_pdf_text(saved_path)
        notice["raw_text"] = raw_text

        if raw_text:
            print(f"  [提取] ({i+1}/{len(type2_notices)}) {title[:40]} → {len(raw_text)} 字符")
        else:
            print(f"  [警告] ({i+1}/{len(type2_notices)}) 文本为空: {title[:40]}")

    # Session 用完关闭
    session.close()
    return type2_notices


# =============================================================================
# Type 1 详情页 HTML 正文解析
# =============================================================================

def parse_type1_detail(tree) -> dict:
    """
    从 type=1 详情页的 lxml 树中提取全部可用信息。

    返回一个富 dict，包含：
      - text:           位置感知的正文纯文本（图片/表格/附件在原文位置插入标记）
      - images:         正文内图片列表 [{src, alt, title}, ...]
      - tables:         Markdown 格式的表格列表
      - attachments_meta: 附件元信息 [{original_name, save_name}, ...]

    文本提取策略（按优先级尝试）：
      1. 用 config.TYPE1_DETAIL_CONTENT_XPATH 定位正文容器
      2. 失败则尝试 3 个常见备选 XPath
      3. 最终回退：提取 body 下所有非脚本/样式文本

    位置感知策略（服务于 LLM 摘要）：
      不再使用 text_content() 一通碾平，而是：
        1. 先将正文容器内的 <img> 替换为 [图片: url] 标记
        2. 将 <table> 替换为 Markdown 表格（位于原位置）
        3. 再去掉剩余 HTML 标签得到纯文本
        4. 末尾追加 [附件] 区域
      这样 LLM 可以根据图片/表格在原文中的位置理解其语境，
      而不是孤立地看到一串图片 URL 和一串文本。

    Args:
        tree: lxml HtmlElement（由 scraper.fetch_page 返回）

    Returns:
        {"text": "...", "images": [...], "tables": [...], "attachments_meta": [...]}
    """
    # ——— 定位正文容器元素 ———
    candidate_xpaths = [
        config.TYPE1_DETAIL_CONTENT_XPATH,
        "//td[@class='_c']",
        "//div[@class='content']",
        "//div[@id='content']",
    ]

    content_elem = None
    for xp in candidate_xpaths:
        elems = tree.xpath(xp)
        if elems:
            content_elem = elems[0]
            if len(content_elem.text_content().strip()) > 50:
                break
            else:
                content_elem = None

    # ——— 先提取图片和表格（用于后续位置替换） ———
    images = detect_images_from_elem(content_elem) if content_elem is not None else []
    tables = extract_tables(content_elem) if content_elem is not None else []
    attachments_meta = extract_attachments_meta(tree)

    # ——— 构建位置感知的文本 ———
    text = ""
    if content_elem is not None:
        text = _build_positioned_text(content_elem, images, tables, attachments_meta)

    if len(text) < 50:
        # 最终回退：提取 body 下所有非脚本/样式元素的文本
        elems = tree.xpath(
            "//body//*[not(self::script) and not(self::style)]"
        )
        if elems:
            seen = set()
            parts = []
            for el in elems:
                txt = el.text_content().strip()
                if txt and txt not in seen:
                    seen.add(txt)
                    parts.append(txt)
            text = "\n".join(parts)

    return {
        "text": text,
        "images": images,
        "tables": tables,
        "attachments_meta": attachments_meta,
    }


def _build_positioned_text(
    content_elem,
    images: list[dict],
    tables: list[str],
    attachments_meta: list[dict],
) -> str:
    """
    构建位置感知的纯文本：将图片和表格按原文位置嵌入文本流中。

    策略：
      1. 将正文容器的子元素序列化为 HTML 字符串
      2. 用正则将 <img ...> 替换为 [图片: url]（位于原文位置）
      3. 用正则将 <table>...</table> 替换为 Markdown 表格（位于原文位置）
      4. 去除剩余 HTML 标签，清洗空白
      5. 末尾追加 [附件] 区域

    为什么不用 text_content()？
      text_content() 把所有后代文本无差别拼接，丢失了：
        - 图片在哪个段落之间（"如下图"失去了参照物）
        - 表格在文中何处（表格前后的说明文字失去了联系）
      位置感知文本让 LLM 能理解"这张图在讲什么""这个表属于哪个章节"。

    Args:
        content_elem: lxml HtmlElement（正文容器元素）
        images: detect_images_from_elem() 的返回值
        tables: extract_tables() 的返回值（Markdown 表格字符串列表）
        attachments_meta: extract_attachments_meta() 的返回值

    Returns:
        位置感知的纯文本字符串
    """
    from lxml import etree

    # ——— 1. 获取正文容器的 inner HTML ———
    inner_parts = []
    for child in content_elem:
        inner_parts.append(
            etree.tostring(child, encoding="unicode", method="html")
        )
    inner_html = "".join(inner_parts)
    if not inner_html.strip():
        # 没有子元素的情况：直接用 text_content 兜底
        return _fallback_positioned_text(content_elem, images, tables, attachments_meta)

    # ——— 2. 替换 <img> → [图片N] 占位符（URL 不过 LLM） ———
    # 设计理由：
    #   LLM 只负责文本摘要，URL 应留在结构化数据（images 列表）中不动。
    #   正文中只嵌入 [图片1]、[图片2] 编号占位符，让 LLM 感知图片位置但不接触 URL。
    #   main.py 生成 Markdown 报告时，按编号从 images 列表取回真实 URL 拼接。
    # 使用计数器与 images 列表一一配对（两者均为文档序，保证顺序一致）
    img_idx = [0]

    def _replace_img(match: re.Match) -> str:
        if img_idx[0] < len(images):
            alt = images[img_idx[0]].get("alt", "")
            img_idx[0] += 1
            idx = img_idx[0]  # 1-based，与 images 列表索引对应
            alt_suffix = f" ({alt})" if alt else ""
            return f"\n\n[图片{idx}{alt_suffix}]\n\n"
        return ""

    inner_html = re.sub(r"<img[^>]*/?>", _replace_img, inner_html)

    # ——— 3. 替换 <table>...</table> → [表格] + Markdown ———
    tbl_idx = [0]

    def _replace_table(match: re.Match) -> str:
        if tbl_idx[0] < len(tables):
            md = tables[tbl_idx[0]]
            tbl_idx[0] += 1
            return f"\n\n[表格]\n{md}\n\n"
        # 非数据表（如含图片的排版用 <table>）→ 剥掉表格标签，保留内容
        # 避免把上一步已插入的 [图片N] 标记一起清空
        content = match.group(0)
        content = re.sub(r"</?table[^>]*>", "", content, flags=re.IGNORECASE)
        content = re.sub(r"</?(?:tbody|thead|tfoot|colgroup|tr|td|th)[^>]*>", "", content, flags=re.IGNORECASE)
        return content

    inner_html = re.sub(
        r"<table[^>]*>.*?</table>",
        _replace_table,
        inner_html,
        flags=re.DOTALL,
    )

    # ——— 4. 去除剩余 HTML 标签 & HTML 实体 ———
    text = re.sub(r"<[^>]+>", "", inner_html)
    text = text.replace("&nbsp;", " ")
    text = text.replace("&lt;", "<")
    text = text.replace("&gt;", ">")
    text = text.replace("&amp;", "&")

    # ——— 5. 清洗空白 ———
    text = _clean_whitespace(text)

    # ——— 6. 末尾追加 [附件] 区域 ———
    if attachments_meta:
        text += "\n\n[附件]"
        for att in attachments_meta:
            text += f"\n- {att['original_name']}"

    return text


def _fallback_positioned_text(
    content_elem,
    images: list[dict],
    tables: list[str],
    attachments_meta: list[dict],
) -> str:
    """
    当正文容器没有子元素时的回退方案：直接用 text_content() 取文本，
    然后将图片/表格/附件追加到末尾。
    """
    text = _clean_whitespace(content_elem.text_content())

    if images:
        text += "\n\n[图片]"
        for i, img in enumerate(images, 1):
            alt = f" ({img['alt']})" if img.get("alt") else ""
            text += f"\n- [图片{i}]{alt}"

    if tables:
        for i, tbl in enumerate(tables):
            text += f"\n\n[表格 {i + 1}]\n{tbl}"

    if attachments_meta:
        text += "\n\n[附件]"
        for att in attachments_meta:
            text += f"\n- {att['original_name']}"

    return text


def _clean_whitespace(text: str) -> str:
    """
    清洗文本中的多余空白。

    操作：
      1. 将连续空白符（空格、制表符）压缩为单个空格
      2. 将 3 个以上连续换行压缩为双换行（保留段落分隔）
      3. 去除首尾空白
    """
    # 压缩行内空白
    text = re.sub(r'[ \t]+', ' ', text)
    # 压缩过多连续换行（保留最多双换行作为段落分隔）
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# =============================================================================
# 正文图片检测（在正文容器内搜索，避免匹配到页面装饰图）
# =============================================================================

def detect_images_from_elem(content_elem) -> list[dict]:
    """
    在正文容器元素内检测图片，提取 src / alt / title。

    使用相对 XPath（.//img）在 content_elem 内搜索，而非全文搜索，
    避免误匹配到页面头部/底部的装饰图片。

    src 为相对路径时自动拼接 config.BASE_URL 为绝对 URL。

    重要限制：
      纯文本提取无法获取图片的视觉内容（如流程图、图表、照片）。
      此函数仅报告图片存在，供调用方在最终报告中标注，
      提醒用户人工查看原文中的图片/附件。

    Args:
        content_elem: lxml HtmlElement（正文容器元素）

    Returns:
        [{src, alt, title}, ...] 列表，无图片返回空列表
    """
    images = []
    # .//img 表示在 content_elem 后代中搜索所有 img 标签
    for img in content_elem.xpath(".//img"):
        src = (img.get("src") or "").strip()
        if not src:
            continue
        images.append({
            "src": src if src.startswith("http") else config.BASE_URL + src,
            "alt": (img.get("alt") or "").strip(),
            "title": (img.get("title") or "").strip(),
        })
    return images


# 保留旧函数签名的兼容性包装（供可能已有的外部调用）
def detect_images(tree) -> list[dict]:
    """
    旧版兼容接口：在整个 tree 中搜索 <td id="content"> 下的图片。

    新代码请优先使用 detect_images_from_elem(content_elem)，
    避免将 XPath 硬编码在函数体内。

    Args:
        tree: lxml HtmlElement（整棵 DOM 树）

    Returns:
        [{src, alt, title}, ...] 列表
    """
    content_elems = tree.xpath("//td[@id='content']")
    if content_elems:
        return detect_images_from_elem(content_elems[0])
    return []


# =============================================================================
# HTML 表格 → Markdown 表格（逐行逐列有序提取，不碾平结构）
# =============================================================================

def _normalize_table_grid(table_elem) -> list[list[str]]:
    """
    将 HTML 表格转换为标准化的二维网格，填充 rowspan/colspan 覆盖的单元格。

    学校 CMS 的表格常常使用 rowspan 合并单元格（如"日期"列跨多行，
    或"参与对象"列跨多场活动）。如果逐 <tr> 逐 <td> 直接提取，
    合并行之后的 <tr> 里会少 <td>，导致列错位——本来是 5 列表格，
    第二数据行只有 2 列，Markdown 表格直接塌掉。

    算法：
      1. 用坐标集合 occupied: {(row, col), ...} 记录所有被占用的位置。
      2. 遍历每行每格，按 rowspan/colspan 标记其覆盖的所有坐标，
         所有坐标共享同一个 cell_text。
      3. 最后按 max_row × max_col 重建完整网格，
         未被标记的坐标填空字符串（理论上不应该出现，但兜底）。

    Args:
        table_elem: lxml HtmlElement（单个 <table> 元素）

    Returns:
        标准化二维列表，每行等宽，rowspan 单元格在后续行中重复填充
    """
    all_rows = table_elem.xpath(".//tr")
    if not all_rows:
        return []

    occupied = set()       # {(row, col), ...} 已被占用的所有坐标
    cell_data = {}         # {(row, col): text} 每个坐标的文本

    for r, tr in enumerate(all_rows):
        c = 0
        for cell in tr.xpath("./td | ./th"):
            # 跳过已被上一行 rowspan 占用的列位置
            while (r, c) in occupied:
                c += 1

            text = cell.text_content().strip()
            rowspan = int(cell.get("rowspan", 1))
            colspan = int(cell.get("colspan", 1))

            # 将此格覆盖的所有 (row, col) 坐标标记为已占用并填入相同文本
            for rr in range(r, r + rowspan):
                for cc in range(c, c + colspan):
                    occupied.add((rr, cc))
                    cell_data[(rr, cc)] = text

            c += colspan

    if not occupied:
        return []

    max_row = max(pos[0] for pos in occupied)
    max_col = max(pos[1] for pos in occupied)

    # 重建完整网格
    grid = []
    for r in range(max_row + 1):
        row = [cell_data.get((r, c), "") for c in range(max_col + 1)]
        grid.append(row)

    return grid


def extract_tables(content_elem) -> list[str]:
    """
    在正文容器内检测 HTML <table>，转换为 Markdown 表格字符串。

    流程：
      1. .//table 定位正文内所有表格
      2. 调用 _normalize_table_grid() 处理 rowspan/colspan，生成等宽二维网格
      3. 过滤退化表格（只有 1 行或 1 列，可能是排版用 <table>）
      4. 转换为 Markdown 表格

    Args:
        content_elem: lxml HtmlElement（正文容器元素）

    Returns:
        Markdown 表格字符串列表，无表格返回空列表
    """
    md_tables = []
    for table_elem in content_elem.xpath(".//table"):
        grid = _normalize_table_grid(table_elem)

        # 跳过退化表格
        if len(grid) < 2 or (grid and len(grid[0]) < 2):
            continue

        md_tables.append(_table_to_markdown(grid))

    return md_tables


def _table_to_markdown(rows: list[list[str]]) -> str:
    """
    将二维列表转换为 Markdown 表格字符串。

    自动对齐：取每列最大宽度（中文字符按 2 个 ASCII 字符宽度计算），
    用空格填充使表格在等宽字体下对齐。

    Args:
        rows: 第一行为表头，后续行为数据行

    Returns:
        Markdown 格式表格字符串
    """
    if not rows:
        return ""

    # 计算每列的显示宽度（中文字符约占 2 个 ASCII 宽度）
    def _display_width(s: str) -> int:
        w = 0
        for ch in s:
            if '一' <= ch <= '鿿' or '　' <= ch <= '〿' or '＀' <= ch <= '￯':
                w += 2
            else:
                w += 1
        return w

    col_count = max(len(r) for r in rows)
    col_widths = [0] * col_count

    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], _display_width(cell))

    def _pad_cell(cell: str, width: int) -> str:
        """用空格填充单元格到指定显示宽度。"""
        current = _display_width(cell)
        return cell + ' ' * (width - current)

    lines = []
    # 表头行
    header = rows[0]
    padded_header = [_pad_cell(header[i], col_widths[i]) for i in range(len(header))]
    lines.append('| ' + ' | '.join(padded_header) + ' |')

    # 分隔行（用 --- 填充每列）
    sep_cells = ['-' * col_widths[i] for i in range(col_count)]
    lines.append('| ' + ' | '.join(sep_cells) + ' |')

    # 数据行
    for row in rows[1:]:
        padded = [_pad_cell(row[i], col_widths[i]) for i in range(len(row))]
        lines.append('| ' + ' | '.join(padded) + ' |')

    return '\n'.join(lines)


# =============================================================================
# 附件元信息提取（从 hidden input 读取，不做 HTTP 请求）
# =============================================================================

def extract_attachments_meta(tree) -> list[dict]:
    """
    从详情页 DOM 树中提取附件元信息。

    附件在该校 CMS 系统中的存储机制：
      1. 原始文件名存入 <input id="infoPicName"> → value 中用 "|" 分隔
      2. 服务器存储名存入 <input id="infoPicSaveName"> → value 中用 "|" 分隔
      3. 下载链接需用存储名调 getFileInfo.jsp 获取 dlcode 后拼接
         （HTTP 调用部分在 scraper.resolve_attachment_urls() 中完成）

    XPath 选取逻辑：
      //input[@id='infoPicName']/@value → 原始文件名列表
      //input[@id='infoPicSaveName']/@value → 存储文件名列表
      两者一一对应，按顺序 zip。

    注意：
      infoPicName 虽然名字带 "Pic"，但实际承载所有附件类型
      （.doc, .docx, .xls, .xlsx, .pdf, .rar, .zip 等），
      不仅仅是图片。这是该 CMS 的历史遗留命名问题。

    Args:
        tree: lxml HtmlElement（整棵 DOM 树）

    Returns:
        [{original_name, save_name}, ...] 列表，无附件返回空列表
    """
    # 读取原始文件名列表
    orig_vals = tree.xpath("//input[@id='infoPicName']/@value")
    if not orig_vals or not orig_vals[0].strip():
        return []
    original_names = [n.strip() for n in orig_vals[0].split("|") if n.strip()]

    # 读取存储文件名列表
    save_vals = tree.xpath("//input[@id='infoPicSaveName']/@value")
    save_names = []
    if save_vals and save_vals[0].strip():
        save_names = [n.strip() for n in save_vals[0].split("|") if n.strip()]

    # 按位置 zip（如果数量不一致，以较短的为准）
    attachments = []
    for i, orig_name in enumerate(original_names):
        save_name = save_names[i] if i < len(save_names) else ""
        attachments.append({
            "original_name": orig_name,
            "save_name": save_name,
        })
    return attachments


# =============================================================================
# 自测入口
# =============================================================================

if __name__ == "__main__":
    """
    完整自测：对 3 条代表性通知分别测试 图片/表格/附件 的检测与提取。

    输出目录结构：
      output/
      ├── 0521_image_test/       # 图片检测（editId=40216468）
      │   ├── original.html      原始 HTML
      │   ├── extracted_text.txt 提取的纯文本正文
      │   └── images.json        图片列表 + 下载验证结果
      ├── 0521_table_test/       # 表格提取（editId=40214854）
      │   ├── original.html
      │   ├── extracted_text.txt
      │   └── tables.md          Markdown 表格
      └── 0521_attachment_test/  # 附件提取（editId=40219215）
          ├── original.html
          ├── extracted_text.txt
          └── attachments.json   附件列表 + 下载链接验证结果

    使用方法：
      python parser.py
    """
    import json
    from scraper import fetch_page, resolve_attachment_urls

    target = config.TARGET_DATE
    date_tag = target.strftime("%m%d")  # e.g. "0521"
    base_dir = Path(config.PROJECT_ROOT) / "output"

    # =========================================================================
    # 辅助函数
    # =========================================================================

    def _save(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _fetch_detail(edit_id: str):
        """拉取详情页 HTML 树。"""
        url = config.BASE_URL + config.TYPE1_DETAIL_URL_TEMPLATE.format(edit_id=edit_id)
        tree = fetch_page(url)
        # 同时保存原始 HTML 供人工核查
        from lxml import etree
        html_str = etree.tostring(tree, encoding="unicode", pretty_print=True)
        return tree, html_str, url

    # =========================================================================
    # 测试 1: 图片通知（群团赋能讲座，含腾讯会议二维码图）
    # =========================================================================
    print("=" * 60)
    print(f"测试 1/3: 图片检测 (editId=40216468)")
    test_dir = base_dir / f"{date_tag}_image_test"
    tree1, raw_html1, url1 = _fetch_detail("40216468")
    parsed1 = parse_type1_detail(tree1)

    _save(test_dir / "original.html", raw_html1)
    _save(test_dir / "extracted_text.txt", parsed1["text"])
    _save(test_dir / "images.json", json.dumps(parsed1["images"], ensure_ascii=False, indent=2))

    print(f"  正文: {len(parsed1['text'])} 字符  → {test_dir / 'extracted_text.txt'}")
    print(f"  图片: {len(parsed1['images'])} 张   → {test_dir / 'images.json'}")
    print(f"  表格: {len(parsed1['tables'])} 个")
    print(f"  附件: {len(parsed1['attachments_meta'])} 个")

    # =========================================================================
    # 测试 2: 表格通知（出国人员公示，含人员信息表格）
    # =========================================================================
    print("=" * 60)
    print(f"测试 2/3: 表格提取 (editId=40214854)")
    test_dir2 = base_dir / f"{date_tag}_table_test"
    tree2, raw_html2, url2 = _fetch_detail("40214854")
    parsed2 = parse_type1_detail(tree2)

    _save(test_dir2 / "original.html", raw_html2)
    _save(test_dir2 / "extracted_text.txt", parsed2["text"])
    if parsed2["tables"]:
        _save(test_dir2 / "tables.md", "\n\n".join(parsed2["tables"]))

    print(f"  正文: {len(parsed2['text'])} 字符  → {test_dir2 / 'extracted_text.txt'}")
    print(f"  图片: {len(parsed2['images'])} 张")
    print(f"  表格: {len(parsed2['tables'])} 个   → {test_dir2 / 'tables.md'}")
    print(f"  附件: {len(parsed2['attachments_meta'])} 个")

    # =========================================================================
    # 测试 3: 附件通知（社科项目申报，含 7 个 doc/xls/docx 附件）
    # =========================================================================
    print("=" * 60)
    print(f"测试 3/3: 附件提取 (editId=40219215)")
    test_dir3 = base_dir / f"{date_tag}_attachment_test"
    tree3, raw_html3, url3 = _fetch_detail("40219215")
    parsed3 = parse_type1_detail(tree3)

    _save(test_dir3 / "original.html", raw_html3)
    _save(test_dir3 / "extracted_text.txt", parsed3["text"])

    # 解析附件下载链接（传入详情页 URL 作为备用下载入口）
    attachments = resolve_attachment_urls(
        parsed3["attachments_meta"],
        detail_page_url=url3,
    )
    _save(test_dir3 / "attachments.json",
          json.dumps(attachments, ensure_ascii=False, indent=2))

    print(f"  正文: {len(parsed3['text'])} 字符  → {test_dir3 / 'extracted_text.txt'}")
    print(f"  图片: {len(parsed3['images'])} 张")
    print(f"  表格: {len(parsed3['tables'])} 个")
    print(f"  附件: {len(attachments)} 个   → {test_dir3 / 'attachments.json'}")

    # =========================================================================
    # 汇总
    # =========================================================================
    print("=" * 60)
    print("所有自测完成。输出目录：")
    print(f"  {test_dir}")
    print(f"  {test_dir2}")
    print(f"  {test_dir3}")
