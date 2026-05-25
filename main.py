"""
===============================================================================
学校通知自动爬取与智能总结智能体 — 主入口
===============================================================================
职责：
  1. 流程编排（6 个阶段顺序执行，阶段间独立 try-catch）
  2. Markdown 日报生成（排版优雅，URL 直接拼接不经过 LLM）

完整数据流：
  Phase 1: 抓取通知列表（type1 校内通知 + type2 学校发文）
  Phase 2: 抓取 type1 详情页 → 位置感知文本 + 图片/表格/附件元数据
  Phase 3: 下载 type2 PDF → 提取文本
  Phase 4: 解析附件下载链接（调 getFileInfo.jsp 获取 dlcode → 拼接 download_url）
  Phase 5: 批量并行调用 LLM 生成摘要（URL 不入 LLM）
  Phase 6: 组装 Markdown 日报 → 保存到 output/reports/

异常处理原则：
  - 每个阶段独立 try-catch，阶段失败不阻断后续流程。
  - 单条通知处理失败不影响其他通知（try-catch 包裹每条）。
  - 即使所有数据源均失败，仍生成一份"今日无通知"的报告。
  - 未经审查的原始网页文本严禁打印到终端（遵循项目编码规范）。
===============================================================================
"""

import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path

import config
from scraper import (
    fetch_type1_list,
    fetch_type2_list,
    fetch_and_parse_detail,
    resolve_attachment_urls,
)
from parser import process_type2_pdfs
from llm_handler import summarize_batch

# ---------------------------------------------------------------------------
# 双轨制日志配置（入口处一次性设定，所有子模块共享）
#   - 文件：DEBUG 级别，含完整堆栈 → output/debug/{date}_run.log
#   - 终端：WARNING 级别，仅关键告警 → stderr
# ---------------------------------------------------------------------------
_LOG_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LOG_DATE_FMT = "%H:%M:%S"


def _setup_logging() -> Path:
    """配置双轨制日志，返回日志文件路径。"""
    root = logging.getLogger("summarize_agent")
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    # 文件 handler：所有 DEBUG+ 写盘，供事后排查
    log_dir = Path(config.PROJECT_ROOT) / "output" / "debug"
    log_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().strftime("%Y%m%d")
    log_path = log_dir / f"{today}_run.log"
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(_LOG_FMT, _LOG_DATE_FMT))
    root.addHandler(file_handler)

    # 终端 handler：仅 WARNING+，不刷屏
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(logging.Formatter(_LOG_FMT, _LOG_DATE_FMT))
    root.addHandler(console_handler)

    return log_path


# =============================================================================
# 工具函数
# =============================================================================

def _weekday_cn(d: date) -> str:
    """date → 中文星期（如"周四"）。"""
    names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    return names[d.weekday()]


def _format_filesize(size_bytes: int) -> str:
    """字节数 → 人类可读的文件大小字符串。"""
    if size_bytes <= 0:
        return ""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


# =============================================================================
# Phase 1: 抓取通知列表
# =============================================================================

def _phase_fetch_lists(target_date: date) -> tuple[list[dict], list[dict]]:
    """
    抓取两个通知列表（type1 + type2），各自独立 try-catch。

    Returns:
        (type1_notices, type2_notices) — 失败侧返回空列表
    """
    _log = logging.getLogger("summarize_agent.main")
    t1, t2 = [], []

    # ——— type1：校内通知 ———
    try:
        t1 = fetch_type1_list(target_date)
        print(f"  [校内通知] {len(t1)} 条")
    except Exception:
        _log.exception("Phase 1: type1 列表抓取整体失败")
        print(f"  [错误] 校内通知列表抓取失败，已跳过")
        t1 = []

    # ——— type2：学校发文 ———
    try:
        t2 = fetch_type2_list(target_date)
        print(f"  [学校发文] {len(t2)} 条")
    except Exception:
        _log.exception("Phase 1: type2 列表抓取整体失败")
        print(f"  [错误] 学校发文列表抓取失败，已跳过")
        t2 = []

    return t1, t2


# =============================================================================
# Phase 2: 抓取 type1 详情页
# =============================================================================

def _phase_fetch_type1_details(notices: list[dict]) -> list[dict]:
    """
    对每条校内通知抓取详情页正文。

    逐条 try-catch：单条失败仅标记 raw_text 为空，不阻断其他通知。
    同时初始化 type2 兼容字段（images/tables/attachments_meta），
    避免后续报告生成阶段做 None 检查。

    Args:
        notices: fetch_type1_list() 的返回值

    Returns:
        同 notices，每条增加 raw_text / images / tables / attachments_meta 字段
    """
    total = len(notices)
    for i, notice in enumerate(notices):
        detail_url = notice.get("detail_url", "")
        if not detail_url:
            notice["raw_text"] = ""
            notice["images"] = []
            notice["tables"] = []
            notice["attachments_meta"] = []
            continue

        try:
            detail = fetch_and_parse_detail(detail_url)
            notice["raw_text"] = detail.get("text", "")
            notice["images"] = detail.get("images", [])
            notice["tables"] = detail.get("tables", [])
            notice["attachments_meta"] = detail.get("attachments_meta", [])
            text_len = len(notice["raw_text"])
            print(f"  ({i+1}/{total}) {notice['title'][:40]} → {text_len} 字符")
        except Exception as e:
            print(f"  [错误] ({i+1}/{total}) {notice['title'][:40]} - {e}")
            notice["raw_text"] = ""
            notice["images"] = []
            notice["tables"] = []
            notice["attachments_meta"] = []

    return notices


# =============================================================================
# Phase 3: 下载 type2 PDF 并提取文本
# =============================================================================

def _phase_download_type2_pdfs(
    notices: list[dict],
    target_date_str: str,
) -> list[dict]:
    """
    批量下载学校发文 PDF 并提取文本。

    process_type2_pdfs 内部已逐条 try-catch，单条失败不影响其他。
    这里外层再加 try-catch 防止整体性异常（如 Session 创建失败）。
    """
    if not notices:
        return notices

    try:
        return process_type2_pdfs(notices, target_date_str)
    except Exception:
        logging.getLogger("summarize_agent.main").exception("Phase 3: PDF 批量处理整体失败")
        print(f"  [错误] PDF 批量处理失败，已跳过")
        for n in notices:
            if "raw_text" not in n:
                n["raw_text"] = ""
        return notices


# =============================================================================
# Phase 4: 解析附件下载链接
# =============================================================================

def _phase_resolve_attachments(notices: list[dict]) -> list[dict]:
    """
    对每条校内通知的附件元信息列表，逐条调 getFileInfo.jsp 获取下载链接。

    只有 attachments_meta 非空的通知才发起 HTTP 请求。
    """
    if not notices:
        return notices

    import requests

    # 创建共享 Session（复用 Cookie，避免每条通知重新鉴权）
    session = requests.Session()
    session.headers.update(config.HEADERS)
    try:
        session.get(config.TYPE1_LIST_URL, timeout=config.REQUEST_TIMEOUT)
    except requests.RequestException:
        pass

    for i, notice in enumerate(notices):
        atts = notice.get("attachments_meta")
        if not atts:
            continue

        try:
            notice["attachments_meta"] = resolve_attachment_urls(
                atts,
                detail_page_url=notice.get("detail_url", ""),
                session=session,
            )
            print(f"  ({i+1}/{len(notices)}) 附件 {len(atts)} 个 → {notice['title'][:40]}")
        except Exception as e:
            print(f"  [错误] ({i+1}) 附件链接解析失败: {e}")

    session.close()
    return notices


# =============================================================================
# Phase 5: LLM 批量摘要
# =============================================================================

def _phase_summarize(notices: list[dict]) -> list[dict]:
    """
    批量并行调用 LLM 生成摘要。

    只有 raw_text 非空的通知才送入 LLM（空文本直接标记"正文为空"）。
    summarize_batch 内部已逐条 try-catch，单条失败写入错误信息。
    """
    if not notices:
        return notices

    # 拆分有文本 / 无文本
    with_text = [n for n in notices if n.get("raw_text", "").strip()]
    without_text = [n for n in notices if not n.get("raw_text", "").strip()]

    for n in without_text:
        n["summary"] = "正文内容为空，无法生成摘要"

    if without_text:
        print(f"  跳过 {len(without_text)} 条无正文通知")

    if with_text:
        print(f"  正在并行处理 {len(with_text)} 条通知...")
        try:
            summarize_batch(with_text, max_workers=5)
            for n in with_text:
                if "summary" not in n:
                    n["summary"] = "摘要生成失败：服务暂时无响应，请稍后重试"
        except Exception:
            logging.getLogger("summarize_agent.main").exception("Phase 5: LLM 批量摘要整体失败")
            print(f"  [错误] 大模型摘要生成失败，已跳过")
            for n in with_text:
                if "summary" not in n:
                    n["summary"] = "摘要生成失败：服务不可用，请稍后重试"

    return notices


# =============================================================================
# Phase 6: 生成 Markdown 日报
# =============================================================================

def _generate_report(
    type1_notices: list[dict],
    type2_notices: list[dict],
    target_date: date,
) -> str:
    """
    组装排版优雅的 Markdown 日报。

    报告结构：
      1. 头部：日期 + 星期 + 统计概览
      2. 一、校内通知（每条：标题 + 元信息 + LLM摘要 + 图片/附件列表）
      3. 二、学校发文（每条：标题 + 元信息 + LLM摘要 + PDF链接）
      4. 脚部：生成时间戳

    图片/附件/PDF 的 URL 在此阶段直接拼入 Markdown，
    全程不经过 LLM，保证 URL 零错误。

    Args:
        type1_notices: 校内通知列表（已完成详情抓取 + 附件解析 + 摘要）
        type2_notices: 学校发文列表（已完成 PDF 提取 + 摘要）
        target_date:   目标日期

    Returns:
        报告文件的完整路径
    """
    date_str = target_date.strftime("%Y-%m-%d")
    weekday = _weekday_cn(target_date)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    total = len(type1_notices) + len(type2_notices)

    # ——— 组装 Markdown ———
    lines: list[str] = []

    # 头部
    lines.append(f"# 苏州大学通知摘要日报")
    lines.append("")
    lines.append(f"**日期**：{date_str}（{weekday}）  ")
    lines.append(f"**生成时间**：{now_str}  ")
    lines.append(f"**数据统计**：校内通知 {len(type1_notices)} 条 | 学校发文 {len(type2_notices)} 条 | 合计 {total} 条")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ——— 一、校内通知 ———
    lines.append("## 一、校内通知")
    lines.append("")

    if type1_notices:
        for i, notice in enumerate(type1_notices, 1):
            title = notice.get("title", "（无标题）")
            unit = notice.get("unit", "")
            date_val = notice.get("date", "")
            detail_url = notice.get("detail_url", "")
            summary = notice.get("summary", "摘要生成失败")
            attachments = notice.get("attachments_meta", [])

            # 标题
            lines.append(f"### {i}. {title}")
            lines.append("")

            # 元信息
            meta_parts = []
            if unit:
                meta_parts.append(f"**发布单位**：{unit}")
            if date_val:
                meta_parts.append(f"**发布日期**：{date_val}")
            if detail_url:
                meta_parts.append(f"**原文链接**：[查看详情]({detail_url})")
            if meta_parts:
                lines.append("  ".join(meta_parts))
                lines.append("")

            # LLM 摘要（自然语句格式，2-4 句）
            lines.append(summary)
            lines.append("")

            # 图片与表格：不在报告中输出
            # 设计理由：
            #   图片 — 纯文本爬虫无法可靠判断图片内容（是二维码？装饰图？流程图？），
            #      alt 属性 80%+ 为空，盲目输出 = 给用户一串裸 URL，不如不输出。
            #   表格 — 摘要报告的核心定位是"5秒扫完"，嵌入原始表格直接破坏阅读节奏。
            #      LLM 摘要已经提炼了表格中的关键信息（人名、时间、数据）。
            #   图片和表格 → 用户点击上方「查看详情」链接即可查看完整原文。

            # 附件列表（download_url 由 resolve_attachment_urls 解析，不经过 LLM）
            if attachments:
                lines.append("**附件**：")
                for att in attachments:
                    orig = att.get("original_name", "未知文件")
                    dl_url = att.get("download_url", "")
                    size = _format_filesize(att.get("size_bytes", 0))
                    size_str = f"（{size}）" if size else ""
                    if dl_url:
                        lines.append(f"- [{orig}]({dl_url}) {size_str}")
                    else:
                        lines.append(f"- {orig} {size_str}（下载链接解析失败，请访问原文链接下载）")
                lines.append("")

            lines.append("---")
            lines.append("")
    else:
        lines.append("*今日无校内通知*")
        lines.append("")
        lines.append("---")
        lines.append("")

    # ——— 二、学校发文 ———
    lines.append("## 二、学校发文")
    lines.append("")

    if type2_notices:
        for i, notice in enumerate(type2_notices, 1):
            title = notice.get("title", "（无标题）")
            number = notice.get("number", "")
            publisher = notice.get("publisher", "")
            date_val = notice.get("date", "")
            pdf_url = notice.get("pdf_url", "")
            summary = notice.get("summary", "摘要生成失败")

            # 标题
            lines.append(f"### {i}. {title}")
            lines.append("")

            # 元信息
            meta_parts = []
            if number:
                meta_parts.append(f"**编号**：{number}")
            if publisher:
                meta_parts.append(f"**发布人**：{publisher}")
            if date_val:
                meta_parts.append(f"**发布日期**：{date_val}")
            if pdf_url:
                meta_parts.append(f"**PDF原文**：[下载PDF]({pdf_url})")
            if meta_parts:
                lines.append("  ".join(meta_parts))
                lines.append("")

            # LLM 摘要
            lines.append(summary)
            lines.append("")

            lines.append("---")
            lines.append("")
    else:
        lines.append("*今日无学校发文*")
        lines.append("")
        lines.append("---")
        lines.append("")

    # ——— 脚部 ———
    pass  # 无额外说明段落，报告正文到此结束

    report_content = "\n".join(lines)

    # ——— 写入文件 ———
    report_dir = Path(config.REPORT_OUTPUT_DIR)
    report_dir.mkdir(parents=True, exist_ok=True)

    filename = config.REPORT_FILENAME_TEMPLATE.format(date=date_str)
    filepath = report_dir / filename

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(report_content)

    return str(filepath)


# =============================================================================
# 主流程
# =============================================================================

def main(target_date: date | None = None):
    """
    主流程入口。

    6 个阶段顺序执行，阶段间独立 try-catch，确保单一阶段失败不阻断整体流程。

    Args:
        target_date: 目标日期，默认使用 config.TARGET_DATE（当天）
    """
    if target_date is None:
        target_date = config.TARGET_DATE

    date_str = target_date.strftime("%Y-%m-%d")

    # 初始化双轨制日志
    log_path = _setup_logging()
    logger = logging.getLogger("summarize_agent.main")

    print("=" * 60)
    print(f"  学校通知自动爬取与智能总结智能体")
    print(f"  目标日期: {date_str}（{_weekday_cn(target_date)}）")
    print(f"  LLM 模型: {config.LLM_MODEL}")
    print(f"  日志文件: {log_path}")
    print("=" * 60)

    # ===== Phase 1: 抓取通知列表 =====
    print(f"\n[1/5] 抓取通知列表...")
    type1_notices, type2_notices = _phase_fetch_lists(target_date)

    total = len(type1_notices) + len(type2_notices)
    if total == 0:
        print(f"\n  目标日期 {date_str} 没有发布任何通知。")

    # ===== Phase 2: 抓取 type1 详情页 =====
    if type1_notices:
        print(f"\n[2/5] 抓取校内通知详情页（共 {len(type1_notices)} 条）...")
        type1_notices = _phase_fetch_type1_details(type1_notices)
    else:
        print(f"\n[2/5] 无校内通知，跳过详情页抓取。")

    # ===== Phase 3: 下载 type2 PDF =====
    if type2_notices:
        print(f"\n[3/5] 下载学校发文 PDF 并提取文本（共 {len(type2_notices)} 条）...")
        type2_notices = _phase_download_type2_pdfs(type2_notices, date_str)
    else:
        print(f"\n[3/5] 无学校发文，跳过 PDF 下载。")

    # ===== Phase 4: 解析附件下载链接 =====
    has_attachments = any(n.get("attachments_meta") for n in type1_notices)
    if has_attachments:
        print(f"\n[4/5] 解析附件下载链接...")
        type1_notices = _phase_resolve_attachments(type1_notices)
    else:
        print(f"\n[4/5] 无附件，跳过下载链接解析。")

    # ===== Phase 5: LLM 摘要 =====
    all_notices = type1_notices + type2_notices
    if all_notices:
        print(f"\n[5/5] 大模型生成摘要（共 {len(all_notices)} 条）...")
        all_notices = _phase_summarize(all_notices)
        # 拆分回两个列表（summarize_batch 是原地修改，但重新拆分保持引用清晰）
        type1_notices = [n for n in all_notices if n.get("source_type") == "校内通知"]
        type2_notices = [n for n in all_notices if n.get("source_type") == "学校发文"]
    else:
        print(f"\n[5/5] 无通知，跳过 LLM 摘要。")

    # ===== Phase 6: 生成报告 =====
    print(f"\n生成 Markdown 报告...")
    try:
        report_path = _generate_report(type1_notices, type2_notices, target_date)
        print(f"\n{'=' * 60}")
        print(f"  完成！报告已保存至:")
        print(f"  {report_path}")
        print(f"{'=' * 60}")

        # ===== Debug Dump: 全链路中间产出落盘 =====
        debug_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        debug_dir = Path(config.DEBUG_OUTPUT_DIR) / f"debug_{debug_ts}"
        debug_dir.mkdir(parents=True, exist_ok=True)
        try:
            from debug_dump import dump_debug_info
            dump_debug_info(debug_dir, type1_notices, type2_notices, target_date)
        except Exception as e:
            print(f"  [警告] Debug 数据写入失败（不影响报告）: {e}")
    except Exception:
        logging.getLogger("summarize_agent.main").exception("Phase 6: 报告生成严重错误")
        print(f"\n[严重错误] 报告生成失败，已生成兜底报告")
        # 兜底：写一份最小化报告（不在报告中暴露异常细节）
        _write_fallback_report(target_date, "报告生成过程中发生未预期的错误，请查看运行日志排查")


def _write_fallback_report(target_date: date, error_msg: str) -> None:
    """
    当正常报告生成流程崩溃时，写一份最小化兜底报告。

    确保用户在任何情况下至少能得到一份记录文件，
    便于排查问题和确认"系统确实运行过了"。
    """
    date_str = target_date.strftime("%Y-%m-%d")
    report_dir = Path(config.REPORT_OUTPUT_DIR)
    report_dir.mkdir(parents=True, exist_ok=True)
    filepath = report_dir / config.REPORT_FILENAME_TEMPLATE.format(date=date_str)

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    content = (
        f"# 苏州大学通知摘要日报（异常）\n\n"
        f"**日期**：{date_str}\n\n"
        f"**生成时间**：{now_str}\n\n"
        f"---\n\n"
        f"## 异常信息\n\n"
        f"报告生成过程中发生严重错误，无法正常产出摘要报告。\n\n"
        f"**错误详情**：\n```\n{error_msg}\n```\n\n"
        f"请检查网络连接、目标服务器状态及 API Key 配置后重试。\n"
    )

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"  兜底报告已保存至: {filepath}")


# =============================================================================
# 命令行入口
# =============================================================================

if __name__ == "__main__":
    main(date(2026, 5, 20))
