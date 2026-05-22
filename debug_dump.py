"""
===============================================================================
全链路 Debug Dump — 所有中间产出落盘到时间戳目录
===============================================================================
用途：
  将 scraper / parser / LLM 三个模块的全部中间产物保存到本地目录，
  用于排查问题、验证数据质量、审查 LLM 输入输出。

使用方式：
  在主流程跑完后调用 dump_debug_info()，传入已处理的通知列表和目标日期。

  目录结构：
    debug_dir/
    ├── 00_system_prompt.txt          # LLM 使用的 System Prompt
    ├── 01_scraper/
    │   ├── type1_list.html           # 校内通知列表页原始 HTML
    │   ├── type2_list.html           # 学校发文列表页原始 HTML
    │   ├── type1_notices.json        # 解析后的通知列表（结构化 JSON）
    │   └── type2_notices.json
    ├── 02_parser/
    │   ├── type1/{editId}/
    │   │   ├── detail.html            # 详情页原始 HTML
    │   │   ├── extracted_text.txt     # 位置感知纯文本（送 LLM 的输入源）
    │   │   ├── images.json            # 检测到的图片元数据
    │   │   ├── tables.md              # 提取的 Markdown 表格
    │   │   └── attachments.json       # 附件元数据 + 下载链接
    │   └── type2/{name}/
    │       └── extracted_text.txt     # PDF 提取的纯文本
    ├── 03_llm/
    │   ├── {idx}_{id}_input.txt       # 送入 LLM 的文本（已清洗 URL）
    │   └── {idx}_{id}_output.md       # LLM 返回的摘要
    └── daily_report.md                # 最终产出的日报（副本）
===============================================================================
"""

import json
import shutil
from datetime import date, datetime
from pathlib import Path

import requests

import config
from llm_handler import _sanitize_for_llm, _build_user_message, SYSTEM_PROMPT


def dump_debug_info(
    debug_dir: Path,
    type1_notices: list[dict],
    type2_notices: list[dict],
    target_date: date,
) -> None:
    """
    将 scraper / parser / LLM 三个模块的全部中间产出保存到 debug_dir。

    Args:
        debug_dir:      目标目录（应已创建）
        type1_notices:  校内通知列表（已完成详情抓取 + 附件解析 + 摘要）
        type2_notices:  学校发文列表（已完成 PDF 提取 + 摘要）
        target_date:    目标日期
    """
    print(f"\n写入全链路 Debug 数据到: {debug_dir}")

    # ——— 00: System Prompt ———
    (debug_dir / "00_system_prompt.txt").write_text(SYSTEM_PROMPT, encoding="utf-8")

    # ——— 01: Scraper 层 ———
    _dump_scraper(debug_dir / "01_scraper", type1_notices, type2_notices)

    # ——— 02: Parser 层 ———
    _dump_parser(debug_dir / "02_parser", type1_notices, type2_notices)

    # ——— 03: LLM 层 ———
    _dump_llm(debug_dir / "03_llm", type1_notices, type2_notices)

    # ——— 最终报告副本 ———
    report_src = (
        Path(config.REPORT_OUTPUT_DIR)
        / config.REPORT_FILENAME_TEMPLATE.format(date=target_date.strftime("%Y-%m-%d"))
    )
    if report_src.exists():
        shutil.copy(report_src, debug_dir / "daily_report.md")

    print(f"  Debug 数据写入完成: {debug_dir}")


# =============================================================================
# 子模块
# =============================================================================

def _dump_scraper(
    scraper_dir: Path,
    type1_notices: list[dict],
    type2_notices: list[dict],
) -> None:
    """保存 Scraper 层产出：列表页原始 HTML + 解析后的通知列表 JSON。"""
    scraper_dir.mkdir(parents=True, exist_ok=True)

    # 原始列表页 HTML（重新请求，仅用于 debug）
    for label, url in [
        ("type1_list.html", config.TYPE1_LIST_URL),
        ("type2_list.html", config.TYPE2_LIST_URL),
    ]:
        try:
            resp = requests.get(url, headers=config.HEADERS, timeout=config.REQUEST_TIMEOUT)
            resp.encoding = config.PAGE_ENCODING
            (scraper_dir / label).write_text(resp.text, encoding="utf-8")
        except Exception as e:
            (scraper_dir / label).write_text(f"获取失败: {e}", encoding="utf-8")

    # 解析后的通知列表 JSON
    _safe_json_dump(scraper_dir / "type1_notices.json", type1_notices)
    _safe_json_dump(scraper_dir / "type2_notices.json", type2_notices)


def _dump_parser(
    parser_dir: Path,
    type1_notices: list[dict],
    type2_notices: list[dict],
) -> None:
    """保存 Parser 层产出：详情页 HTML + 提取文本 + 图片/表格/附件元数据。"""
    from lxml import etree
    from scraper import fetch_page

    parser_dir.mkdir(parents=True, exist_ok=True)

    # type1：每条通知的详情页 HTML + 提取产物
    type1_parser_dir = parser_dir / "type1"
    type1_parser_dir.mkdir(parents=True, exist_ok=True)
    for notice in type1_notices:
        edit_id = notice.get("edit_id", "unknown")
        notice_dir = type1_parser_dir / str(edit_id)
        notice_dir.mkdir(parents=True, exist_ok=True)

        # 详情页原始 HTML
        try:
            tree = fetch_page(notice.get("detail_url", ""))
            html_str = etree.tostring(tree, encoding="unicode", pretty_print=True)
            (notice_dir / "detail.html").write_text(html_str, encoding="utf-8")
        except Exception as e:
            (notice_dir / "detail.html").write_text(f"获取失败: {e}", encoding="utf-8")

        # 提取的纯文本
        (notice_dir / "extracted_text.txt").write_text(
            notice.get("raw_text", ""), encoding="utf-8"
        )

        # 图片/表格/附件元数据
        _safe_json_dump(notice_dir / "images.json", notice.get("images", []))
        tables_md = "\n\n".join(notice.get("tables", []))
        if tables_md:
            (notice_dir / "tables.md").write_text(tables_md, encoding="utf-8")
        _safe_json_dump(notice_dir / "attachments.json", notice.get("attachments_meta", []))

    # type2：PDF 提取的文本
    type2_parser_dir = parser_dir / "type2"
    type2_parser_dir.mkdir(parents=True, exist_ok=True)
    for notice in type2_notices:
        number = notice.get("number", "").replace("/", "_").replace("\\", "_")
        title_short = notice.get("title", "unknown")[:20]
        safe_name = f"{number}_{title_short}" if number else title_short
        notice_dir = type2_parser_dir / safe_name
        notice_dir.mkdir(parents=True, exist_ok=True)
        (notice_dir / "extracted_text.txt").write_text(
            notice.get("raw_text", ""), encoding="utf-8"
        )


def _dump_llm(
    llm_dir: Path,
    type1_notices: list[dict],
    type2_notices: list[dict],
) -> None:
    """保存 LLM 层产出：清洗后的输入文本 + 返回的摘要。"""
    llm_dir.mkdir(parents=True, exist_ok=True)

    all_notices = type1_notices + type2_notices
    idx = 0
    for notice in all_notices:
        raw_text = notice.get("raw_text", "").strip()
        if not raw_text:
            continue
        idx += 1
        notice_id = notice.get("edit_id", notice.get("number", str(idx)))

        # 入 LLM 的清洗后文本（与实际调用完全一致）
        sanitized = _sanitize_for_llm(raw_text)
        user_message = _build_user_message(notice.get("title", ""), sanitized)
        (llm_dir / f"{idx:02d}_{notice_id}_input.txt").write_text(
            user_message, encoding="utf-8"
        )

        # LLM 返回的摘要
        (llm_dir / f"{idx:02d}_{notice_id}_output.md").write_text(
            notice.get("summary", ""), encoding="utf-8"
        )

    print(f"  已保存 {idx} 条 LLM 输入/输出对")


def _safe_json_dump(path: Path, data) -> None:
    """安全写入 JSON（处理 datetime 等不可序列化对象）。"""

    def _default(obj):
        if hasattr(obj, "isoformat"):
            return obj.isoformat()
        return str(obj)

    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=_default),
        encoding="utf-8",
    )
