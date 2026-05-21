"""
===============================================================================
学校通知自动爬取与智能总结智能体 — 数据抓取层
===============================================================================
职责：
  1. HTTP 请求（强制 gbk 解码 + lxml 树构建）
  2. 列表页 XPath 解析（type=1 校内通知 + type=2 学校发文）
  3. 翻页循环控制（纯 Python datetime 判断截止，严禁 LLM 参与）
  4. PDF 流式下载
  5. type=1 详情页（HTML 正文）抓取

编码铁律：
  - 目标站点 meta 标签声明 gb2312，但实际响应体为 UTF-8。
  - 经十六进制验证，中文标题为合法 UTF-8 字节序列，GBK 解码会报错。
  - requests.get 后立刻硬编码 response.encoding = 'utf-8'。
  - 禁止 print 原始网页文本到终端；调试输出统一写入 output/ 目录。

所有 XPath 规则从 config.py 读取，页面结构变化只需改 config。
===============================================================================
"""

import re
import time
import requests
from lxml import html
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import config

# =============================================================================
# 工具函数
# =============================================================================

def _build_absolute_url(href: str) -> str:
    """相对路径 → 完整 URL。以 http 开头则原样返回，否则拼接 BASE_URL。"""
    if href.startswith("http"):
        return href
    return config.BASE_URL + href


def _extract_edit_id(onclick: str) -> Optional[str]:
    """
    从 onclick="open_tz('40214948');" 中正则提取数字 ID。
    返回 None 表示匹配失败，该行将被丢弃。
    """
    if not onclick:
        return None
    m = re.search(r"open_tz\('(\d+)'\)", onclick)
    return m.group(1) if m else None


def _parse_date(date_str: str) -> Optional[date]:
    """字符串 → datetime.date。解析失败返回 None。"""
    try:
        return datetime.strptime(date_str.strip(), config.DATE_FORMAT).date()
    except (ValueError, AttributeError):
        return None


def _write_debug(filename: str, content: str) -> None:
    """
    将调试信息写入文件，严禁直接 print 原始网页文本到终端。
    文件保存在 output/ 目录下。
    """
    debug_dir = Path(config.PROJECT_ROOT) / "output"
    debug_dir.mkdir(parents=True, exist_ok=True)
    filepath = debug_dir / filename
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)


# =============================================================================
# HTTP 请求层
# =============================================================================

def fetch_page(url: str) -> html.HtmlElement:
    """
    GET 请求 → 强制 gbk 解码 → 返回 lxml HtmlElement。

    重试策略：最多 config.MAX_RETRIES 次，指数退避。
    全部失败抛出 RuntimeError。
    """
    full_url = _build_absolute_url(url)

    for attempt in range(config.MAX_RETRIES + 1):
        try:
            resp = requests.get(
                full_url,
                headers=config.HEADERS,
                timeout=config.REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            resp.encoding = config.PAGE_ENCODING

            return html.fromstring(resp.text)

        except requests.RequestException:
            if attempt < config.MAX_RETRIES:
                backoff = config.RETRY_BACKOFF * (2 ** attempt)
                time.sleep(backoff)

    # 所有重试均失败
    raise RuntimeError(
        f"请求失败（{config.MAX_RETRIES} 次重试已用尽）: {full_url}"
    )


# =============================================================================
# 列表页单行解析（type=1 校内通知）
# =============================================================================

def _parse_type1_row(row: html.HtmlElement) -> Optional[dict]:
    """
    解析校内通知列表中的一条 <tr>。

    页面列结构（3 列）：
      td[1] → 日期 (YYYY-MM-DD)
      td[2] → <a onclick="open_tz('ID')" title="完整标题">截断标题</a>
      td[3] → 发文单位（多单位用 <br/> 分隔）

    字段提取策略：
      - 标题取 @title 属性（页面 text() 会截断长标题）
      - 详情页 ID 从 onclick 正则提取
      - 单位取所有直接文本子节点（<br/> 分隔时 lxml 返回列表），用 " / " 连接

    Returns: dict 或 None（关键字段缺失时丢弃该行）
    """
    # 标题 — 取自 a 标签的 @title 属性
    titles = row.xpath(config.TYPE1_TITLE_XPATH)
    title = titles[0].strip() if titles else None

    # 详情页 ID — 从 onclick 正则提取
    onclick_vals = row.xpath(config.TYPE1_ONCLICK_XPATH)
    edit_id = _extract_edit_id(onclick_vals[0]) if onclick_vals else None

    # 日期 — td[1] 直接文本
    dates = row.xpath(config.TYPE1_DATE_XPATH)
    date_str = dates[0].strip() if dates else None

    # 发文单位 — td[3] 的所有直接文本子节点（<br/> 分隔多单位时返回列表）
    units = row.xpath(config.TYPE1_UNIT_XPATH)
    unit_str = " / ".join(u.strip() for u in units if u.strip()) if units else ""

    if not title or not date_str:
        return None

    # 拼接详情页 URL
    detail_url = None
    if edit_id:
        detail_url = _build_absolute_url(
            config.TYPE1_DETAIL_URL_TEMPLATE.format(edit_id=edit_id)
        )

    return {
        "date": date_str,
        "title": title,
        "unit": unit_str,
        "edit_id": edit_id,
        "detail_url": detail_url,
    }


# =============================================================================
# 列表页单行解析（type=2 学校发文）
# =============================================================================

def _parse_type2_row(row: html.HtmlElement) -> Optional[dict]:
    """
    解析学校发文列表中的一条 <tr>。

    页面列结构（4 列）：
      td[1] → 日期 (YYYY-MM-DD)
      td[2] → 发文编号（如 "苏大研〔2026〕29号"）
      td[3] → <a href="download.jsp?...pdf" title="完整标题">截断标题</a>
      td[4] → 发布人（人名，非单位）

    Returns: dict 或 None
    """
    titles = row.xpath(config.TYPE2_TITLE_XPATH)
    title = titles[0].strip() if titles else None

    hrefs = row.xpath(config.TYPE2_PDF_HREF_XPATH)
    pdf_url = _build_absolute_url(hrefs[0]) if hrefs else None

    dates = row.xpath(config.TYPE2_DATE_XPATH)
    date_str = dates[0].strip() if dates else None

    numbers = row.xpath(config.TYPE2_NUMBER_XPATH)
    number = numbers[0].strip() if numbers else ""

    publishers = row.xpath(config.TYPE2_PUBLISHER_XPATH)
    publisher = publishers[0].strip() if publishers else ""

    if not title or not date_str:
        return None

    return {
        "date": date_str,
        "title": title,
        "number": number,
        "publisher": publisher,
        "pdf_url": pdf_url,
    }


# =============================================================================
# 翻页截止判断（纯 Python，零 LLM 参与）
# =============================================================================

def _stop_pagination(page_dates: list[str], target: date) -> bool:
    """
    判断是否应停止翻页。

    前提：列表页按日期**倒序**排列（最新在前）。

    逻辑：
      遍历本页所有日期 →
        如果某条日期 < target → 已翻过目标日期，返回 True（停止）
        如果某条日期 == target 或 > target → 继续
      全部遍历完未触发 → 返回 False（继续翻页）

    全程仅使用 datetime.date 比较，严禁调用大模型。
    """
    for ds in page_dates:
        d = _parse_date(ds)
        if d is None:
            continue
        if d < target:
            return True
    return False


# =============================================================================
# 翻页循环抓取
# =============================================================================

def _fetch_list(
    list_url: str,
    row_xpath: str,
    next_page_xpath: str,
    parse_row_func,
    target_date: date,
    label: str,
) -> list[dict]:
    """
    通用翻页抓取流程（type=1 和 type=2 共用）。

    流程：
      1. GET 当前页 → lxml 树
      2. XPath 选中所有数据行 tr
      3. 逐行 parse，收集日期
      4. 保留 date == target_date 的通知
      5. 调用 _stop_pagination 判断是否停止
      6. 取「下页」链接继续 / 无链接则终止
    """
    results: list[dict] = []
    page_url = list_url
    page_num = 0

    while page_url:
        page_num += 1
        tree = fetch_page(page_url)
        rows = tree.xpath(row_xpath)

        if not rows:
            break  # 无数据行，终止

        page_dates: list[str] = []
        for row in rows:
            notice = parse_row_func(row)
            if notice is None:
                continue
            page_dates.append(notice["date"])

            d = _parse_date(notice["date"])
            if d is None:
                continue
            if d == target_date:
                notice["source_type"] = label
                results.append(notice)

        # 翻页截止判断（倒序：遇到早于目标日期的记录即停止）
        if _stop_pagination(page_dates, target_date):
            break

        # 获取「下页」链接
        next_links = tree.xpath(next_page_xpath)
        if next_links:
            page_url = _build_absolute_url(next_links[0])
            time.sleep(config.REQUEST_DELAY)
        else:
            page_url = None  # 无下页，终止

    return results


def fetch_type1_list(target_date: Optional[date] = None) -> list[dict]:
    """抓取 type=1（校内通知）目标日期的所有通知。"""
    if target_date is None:
        target_date = config.TARGET_DATE
    return _fetch_list(
        list_url=config.TYPE1_LIST_URL,
        row_xpath=config.TYPE1_ROW_XPATH,
        next_page_xpath=config.TYPE1_NEXT_PAGE_XPATH,
        parse_row_func=_parse_type1_row,
        target_date=target_date,
        label="校内通知",
    )


def fetch_type2_list(target_date: Optional[date] = None) -> list[dict]:
    """抓取 type=2（学校发文）目标日期的所有通知。"""
    if target_date is None:
        target_date = config.TARGET_DATE
    return _fetch_list(
        list_url=config.TYPE2_LIST_URL,
        row_xpath=config.TYPE2_ROW_XPATH,
        next_page_xpath=config.TYPE2_NEXT_PAGE_XPATH,
        parse_row_func=_parse_type2_row,
        target_date=target_date,
        label="学校发文",
    )


# =============================================================================
# PDF 下载
# =============================================================================

def download_pdf(
    pdf_url: str,
    save_dir: str,
    filename: str,
    session: Optional[requests.Session] = None,
) -> Optional[str]:
    """
    流式下载 PDF 到本地。

    若文件已存在则跳过下载（幂等）。

    Args:
        pdf_url:  PDF 完整 URL
        save_dir: 保存目录
        filename: 保存文件名
        session:  可选的 requests.Session（用于维持 Cookie 鉴权）。
                  不传则创建临时 Session。

    Returns:
        保存后的完整路径，失败返回 None
    """
    Path(save_dir).mkdir(parents=True, exist_ok=True)
    filepath = Path(save_dir) / filename

    if filepath.exists():
        return str(filepath)

    if session is None:
        session = requests.Session()
        session.headers.update(config.HEADERS)

    for attempt in range(config.MAX_RETRIES + 1):
        try:
            resp = session.get(
                pdf_url,
                timeout=config.REQUEST_TIMEOUT,
                stream=True,
            )
            resp.raise_for_status()
            with open(filepath, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            return str(filepath)
        except requests.RequestException:
            if attempt < config.MAX_RETRIES:
                time.sleep(config.RETRY_BACKOFF * (2 ** attempt))
            else:
                return None


# =============================================================================
# Type 1 详情页
# =============================================================================

def fetch_detail_content(detail_url: str) -> str:
    """
    抓取 type=1 通知详情页的正文纯文本。

    先用 config.TYPE1_DETAIL_CONTENT_XPATH 提取；
    若失败则尝试 3 个常见备选 XPath。
    返回空字符串表示提取失败。
    """
    try:
        tree = fetch_page(detail_url)

        # 首选 XPath
        parts = tree.xpath(config.TYPE1_DETAIL_CONTENT_XPATH)
        text = "\n".join(p.strip() for p in parts if p.strip())
        if len(text) > 50:
            return text

        # 备选 XPath
        for fb in [
            "//div[@class='content']//text()",
            "//div[@class='article']//text()",
            "//td//text()",
        ]:
            parts = tree.xpath(fb)
            text = "\n".join(p.strip() for p in parts if p.strip())
            if len(text) > 50:
                return text

        return ""
    except RuntimeError:
        return ""


# =============================================================================
# 自测入口（只抓取、不调用 LLM）
# =============================================================================

if __name__ == "__main__":
    target = config.TARGET_DATE

    # --- 抓取 ---
    t1 = fetch_type1_list(target)
    t2 = fetch_type2_list(target)

    # --- 汇总信息（安全，不含原始网页文本）---
    summary_lines = [
        f"目标日期: {target}",
        f"校内通知 (type=1): {len(t1)} 条",
        f"学校发文 (type=2): {len(t2)} 条",
        f"合计: {len(t1) + len(t2)} 条",
        "",
        "--- Type1 样本（前 3 条）---",
    ]
    for n in t1[:3]:
        summary_lines.append(
            f"  [{n['date']}] {n['title'][:60]} | {n['unit'][:30]} | {n['detail_url']}"
        )
    summary_lines.append("")
    summary_lines.append("--- Type2 样本（前 3 条）---")
    for n in t2[:3]:
        summary_lines.append(
            f"  [{n['date']}] {n['number']} {n['title'][:50]} | {n['publisher']}"
        )

    # 写入文件而非终端打印
    _write_debug("debug_scraper.txt", "\n".join(summary_lines))
    print(f"自测完成，结果已写入 output/debug_scraper.txt")
    print(f"  Type1: {len(t1)} 条, Type2: {len(t2)} 条")
