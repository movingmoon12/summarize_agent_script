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

def parse_type1_detail(tree) -> str:
    """
    从 type=1 详情页的 lxml 树中提取正文纯文本。

    提取策略（按优先级尝试）：
      1. 用 config.TYPE1_DETAIL_CONTENT_XPATH 定位正文容器
      2. 失败则尝试 3 个常见备选 XPath
      3. 最低回退：提取 <body> 下所有文本（噪音较多但能兜底）

    使用 lxml 的 string() 或 //text() 获取节点下所有文本，
    清洗后返回。

    Args:
        tree: lxml HtmlElement（由 scraper.fetch_page 返回）

    Returns:
        纯文本正文；提取失败返回 ""
    """
    # 首选 XPath
    parts = tree.xpath(config.TYPE1_DETAIL_CONTENT_XPATH)
    text = _clean_text(parts)
    if len(text) > 50:
        return text

    # 备选 XPath（按优先级递减）
    for fb_xpath in [
        "//td[@class='_c']//text()",
        "//div[@class='content']//text()",
        "//div[@id='content']//text()",
    ]:
        parts = tree.xpath(fb_xpath)
        text = _clean_text(parts)
        if len(text) > 50:
            return text

    # 最终回退：提取 body 下所有文本，排除 script 和 style
    parts = tree.xpath("//body//*[not(self::script) and not(self::style)]//text()")
    return _clean_text(parts)


def _clean_text(parts: list[str]) -> str:
    """
    清洗 XPath 提取的文本片段列表。

    操作：
      1. 逐条 strip 空白
      2. 过滤掉只有空白/标点的碎片
      3. 用换行拼接

    Args:
        parts: tree.xpath("...//text()") 返回的字符串列表

    Returns:
        清洗后的纯文本
    """
    cleaned = []
    for p in parts:
        s = p.strip()
        if s and len(s) > 1:  # 丢弃单字符碎片（通常是标点或分隔符残留）
            cleaned.append(s)
    return "\n".join(cleaned)


# =============================================================================
# 自测入口
# =============================================================================

if __name__ == "__main__":
    """
    快速自测：下载 type=2 第一条通知的 PDF 并提取文本。

    使用方法：
      python parser.py
    """
    from scraper import fetch_type2_list, _write_debug

    target = config.TARGET_DATE
    print(f"Parser 模块自测 — 目标日期: {target}")

    # 获取 type=2 通知列表（取第一条测试 PDF 提取）
    notices = fetch_type2_list(target)
    if not notices:
        print(f"  无 {target} 的学校发文，跳过测试")
    else:
        print(f"  共 {len(notices)} 条学校发文，测试第一条...")
        processed = process_type2_pdfs(notices[:1], str(target))
        n = processed[0]
        preview = n.get("raw_text", "")[:500]

        result = (
            f"标题: {n['title']}\n"
            f"编号: {n['number']}\n"
            f"PDF: {n['pdf_url']}\n"
            f"提取字符数: {len(n.get('raw_text', ''))}\n"
            f"--- 前 500 字符 ---\n{preview}"
        )
        _write_debug("debug_parser.txt", result)
        print(f"  提取完成，结果写入 output/debug_parser.txt")
