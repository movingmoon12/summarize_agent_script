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

import logging
import re
import time
import requests
from lxml import html
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import config

# ---------------------------------------------------------------------------
# 双轨制日志：对内详细记录（含堆栈），对外由 print() 输出中文友好提示
# ---------------------------------------------------------------------------
logger = logging.getLogger("summarize_agent.scraper")

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
        except Exception:
            # lxml 解析异常等非 HTTP 错误：不可恢复，不重试直接记录并抛出
            logger.exception("页面解析失败（非 HTTP 错误）: %s", full_url)
            raise

    # 所有重试均失败
    logger.error(
        "请求失败（%d 次重试已用尽）: %s", config.MAX_RETRIES, full_url,
    )
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

        # —— 单页抓取（P0 修复：失败不丢弃已抓取数据） ——
        try:
            tree = fetch_page(page_url)
        except Exception:
            # 对内：记录完整堆栈供 Debug
            logger.exception(
                "[%s] 第 %d 页请求失败，已保留前 %d 条记录。URL: %s",
                label, page_num, len(results), page_url,
            )
            # 对外：中文友好提示
            print(f"  [网络暂时开小差] {label}第 {page_num} 页请求失败，已保留前 {len(results)} 条记录，跳过后续翻页")
            break

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
    若未传入 session，则自动创建并访问列表页获取 OASESSIONID Cookie。

    Args:
        pdf_url:  PDF 完整 URL
        save_dir: 保存目录
        filename: 保存文件名
        session:  可选的 requests.Session（用于维持 Cookie 鉴权）。
                  不传则自动创建并鉴权。

    Returns:
        保存后的完整路径，失败返回 None
    """
    Path(save_dir).mkdir(parents=True, exist_ok=True)
    filepath = Path(save_dir) / filename

    if filepath.exists():
        return str(filepath)

    own_session = False
    if session is None:
        session = requests.Session()
        session.headers.update(config.HEADERS)
        own_session = True
        # 先访问列表页获取 OASESSIONID Cookie，否则 download.jsp 返回 HTML 登录页
        try:
            session.get(config.TYPE2_LIST_URL, timeout=config.REQUEST_TIMEOUT)
        except requests.RequestException:
            pass

    for attempt in range(config.MAX_RETRIES + 1):
        try:
            resp = session.get(
                pdf_url,
                timeout=config.REQUEST_TIMEOUT,
                stream=True,
            )
            resp.raise_for_status()

            # 用 iter_content 迭代器取第一个 chunk 做 HTML 防御检查
            # 不能用 resp.raw.read()——那会破坏 iter_content 的内部缓冲状态
            chunk_iter = resp.iter_content(chunk_size=8192)
            try:
                first_chunk = next(chunk_iter)
            except StopIteration:
                if own_session:
                    session.close()
                return None

            if (
                first_chunk
                and (first_chunk[:6] == b"\r\n<scr"
                     or first_chunk[:7] == b"<script"
                     or first_chunk[:6] == b"<html>"
                     or first_chunk[:6] == b"<HTML>")
            ):
                # 鉴权失败，服务器返回的是登录重定向页面
                if own_session:
                    session.close()
                return None

            # 流式写入
            with open(filepath, "wb") as f:
                f.write(first_chunk)
                for chunk in chunk_iter:
                    if chunk:
                        f.write(chunk)
            return str(filepath)

        except requests.RequestException:
            if attempt < config.MAX_RETRIES:
                time.sleep(config.RETRY_BACKOFF * (2 ** attempt))
            else:
                if own_session:
                    session.close()
                return None

    if own_session:
        session.close()
    return None


# =============================================================================
# Type 1 详情页
# =============================================================================

def fetch_and_parse_detail(detail_url: str) -> dict:
    """
    抓取 type=1 通知的详情页，返回 parser.parse_type1_detail() 的富 dict。

    设计理由：
      scraper 只负责 HTTP 请求（fetch_page），解析逻辑全在 parser.py。
      之前的 fetch_detail_content() 在 scraper 中直接调用 xpath 并用
      p.strip() 处理 HtmlElement 对象，有致命 bug（HtmlElement 没有 .strip() 方法）。
      现在改为委托 parser.parse_type1_detail()，职责清晰。

    Args:
        detail_url: 详情页完整 URL

    Returns:
        parser.parse_type1_detail() 的返回值：
        {"text": "...", "images": [...], "tables": [...], "attachments_meta": [...]}
        请求失败时返回空 dict（所有字段为空字符串/空列表）
    """
    from parser import parse_type1_detail

    try:
        tree = fetch_page(detail_url)
        return parse_type1_detail(tree)
    except Exception:
        # 对内：记录完整堆栈（含 lxml 解析异常等非 RuntimeError 的错误）
        logger.exception("详情页解析失败: %s", detail_url)
        # 对外：返回空结构，由上层逐条 try-catch 继续处理下一条
        return {"text": "", "images": [], "tables": [], "attachments_meta": []}


# =============================================================================
# 附件下载链接解析（调用 getFileInfo.jsp 获取 dlcode）
# =============================================================================

def resolve_attachment_urls(
    attachments_meta: list[dict],
    detail_page_url: str = "",
    session: Optional[requests.Session] = None,
) -> list[dict]:
    """
    对 parser.extract_attachments_meta() 返回的附件列表，
    逐条调用 /defaultroot/public/upload/uploadify/getFileInfo.jsp
    获取 dlcode（verifyCode），拼接完整下载 URL。

    请求格式：
      GET /defaultroot/public/upload/uploadify/getFileInfo.jsp
        ?saveFileName={save_name}&date={random}

    响应格式（JavaScript 风格 JSON）：
      {'saveFileName':'...','accLongSize':14848,'dlcode':'1EB04593E3FileName00',...}

    拼接的下载 URL 格式：
      /defaultroot/public/download/download.jsp
        ?verifyCode={dlcode}&FileName={save_name}&path=customform&name={url_encoded_original_name}

    重要：name 参数是必须的！缺少 name 参数时 download.jsp 返回空 HTML 或 404。
    经 2026-05-21 实测验证：加 name 参数后 GET 返回 200 + 真实文件字节流。

    浏览器下载注意事项：
      download.jsp 的 GET 请求不返回 Content-Disposition 响应头，
      浏览器可能不会自动触发下载（而是尝试内联显示或重定向）。
      POST 请求返回附件头（实测 Content-Disposition: attachment）。
      因此，最终报告中建议以 detail_page_url（详情页）为主链接，
      用户点击后在详情页上使用系统自带的下载按钮（POST 方式）。
      download_url 可作为右键另存的备用链接。

    Args:
        attachments_meta: parser.extract_attachments_meta() 返回的列表
        detail_page_url: 通知详情页 URL（作为主要下载入口，可选）
        session: 可选的 requests.Session（复用 Cookie）

    Returns:
        同 attachments_meta，但每个 dict 增加了字段：
          - download_url:      直接下载链接（GET 方式，需登录态，建议右键另存）
          - detail_page_url:   详情页 URL（推荐：用户点击后在页面内下载）
          - size_bytes:        文件大小（字节，解析失败则为 0）
    """
    from urllib.parse import quote

    own_session = False
    if session is None:
        session = requests.Session()
        session.headers.update(config.HEADERS)
        own_session = True
        # 先访问列表页获取 OASESSIONID Cookie（download.jsp 鉴权依赖）
        try:
            session.get(config.TYPE1_LIST_URL, timeout=config.REQUEST_TIMEOUT)
        except requests.RequestException:
            pass

    for att in attachments_meta:
        save_name = att.get("save_name", "")
        original_name = att.get("original_name", "")
        att["detail_page_url"] = detail_page_url  # 所有附件共享同一个详情页 URL
        if not save_name:
            att["download_url"] = None
            att["size_bytes"] = 0
            continue

        # P1 修复：getFileInfo.jsp 加重试（之前一次失败就永久丢失附件链接）
        dlcode = None
        file_size = 0
        for attempt in range(config.MAX_RETRIES + 1):
            try:
                resp = session.get(
                    f"{config.BASE_URL}/defaultroot/public/upload/uploadify/getFileInfo.jsp",
                    params={"saveFileName": save_name, "date": str(int(time.time()))},
                    timeout=config.REQUEST_TIMEOUT,
                )
                resp.encoding = "utf-8"

                # 响应体是 JavaScript 风格 JSON（单引号），用正则提取 dlcode
                dlcode_match = re.search(r"'dlcode'\s*:\s*'([^']+)'", resp.text)
                size_match = re.search(r"'accLongSize'\s*:\s*(\d+)", resp.text)

                if dlcode_match:
                    dlcode = dlcode_match.group(1)
                file_size = int(size_match.group(1)) if size_match else 0
                break  # 成功，跳出重试循环

            except requests.RequestException:
                if attempt < config.MAX_RETRIES:
                    backoff = config.RETRY_BACKOFF * (2 ** attempt)
                    time.sleep(backoff)
                else:
                    logger.exception(
                        "附件链接解析失败（%d 次重试已用尽）: saveFileName=%s",
                        config.MAX_RETRIES, save_name,
                    )

        if dlcode:
            # name 参数是必须的（经实测验证），用 URL 编码处理中文文件名
            name_param = quote(original_name, safe='') if original_name else save_name
            att["download_url"] = (
                f"{config.BASE_URL}/defaultroot/public/download/download.jsp"
                f"?verifyCode={dlcode}"
                f"&FileName={save_name}"
                f"&path=customform"
                f"&name={name_param}"
            )
        else:
            att["download_url"] = None

        att["size_bytes"] = file_size

    if own_session:
        session.close()

    return attachments_meta


# =============================================================================
# 通用文件下载（兼容 PDF、图片、Office 文档等）
# =============================================================================

def download_attachment(
    download_url: str,
    save_dir: str,
    filename: str,
    session: Optional[requests.Session] = None,
) -> Optional[str]:
    """
    流式下载附件文件到本地（与 download_pdf 逻辑相同，命名更通用）。

    若文件已存在则跳过下载（幂等）。
    若未传入 session，则自动创建并访问列表页获取 OASESSIONID Cookie。

    Args:
        download_url: 文件下载链接（完整 URL）
        save_dir: 保存目录
        filename: 保存文件名（使用原始文件名，含扩展名）
        session: 可选的 requests.Session（用于维持 Cookie 鉴权）。
                 不传则自动创建并鉴权。

    Returns:
        保存后的完整路径，失败返回 None
    """
    Path(save_dir).mkdir(parents=True, exist_ok=True)
    filepath = Path(save_dir) / filename

    if filepath.exists():
        return str(filepath)

    own_session = False
    if session is None:
        session = requests.Session()
        session.headers.update(config.HEADERS)
        own_session = True
        try:
            session.get(config.TYPE1_LIST_URL, timeout=config.REQUEST_TIMEOUT)
        except requests.RequestException:
            pass

    for attempt in range(config.MAX_RETRIES + 1):
        try:
            resp = session.get(
                download_url,
                timeout=config.REQUEST_TIMEOUT,
                stream=True,
            )
            resp.raise_for_status()

            # HTML 登录页防御检查（用 iter_content 不用 raw.read）
            chunk_iter = resp.iter_content(chunk_size=8192)
            try:
                first_chunk = next(chunk_iter)
            except StopIteration:
                if own_session:
                    session.close()
                return None

            if (
                first_chunk
                and (first_chunk[:6] == b"\r\n<scr"
                     or first_chunk[:7] == b"<script"
                     or first_chunk[:6] == b"<html>"
                     or first_chunk[:6] == b"<HTML>")
            ):
                if own_session:
                    session.close()
                return None

            with open(filepath, "wb") as f:
                f.write(first_chunk)
                for chunk in chunk_iter:
                    if chunk:
                        f.write(chunk)
            return str(filepath)

        except requests.RequestException:
            if attempt < config.MAX_RETRIES:
                time.sleep(config.RETRY_BACKOFF * (2 ** attempt))
            else:
                if own_session:
                    session.close()
                return None

    if own_session:
        session.close()
    return None


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
