"""
===============================================================================
学校通知自动爬取与智能总结智能体 — 大模型调用层
===============================================================================
职责（本项目唯一允许调用大模型的模块）：
  1. 对单篇通知的纯文本内容进行去冗余摘要
  2. 支持并行处理多条通知（ThreadPoolExecutor）
  3. 入 LLM 前清洗文本中的 URL（URL 不过 LLM，由 main.py 在报告阶段直接拼接）

设计原则：
  - System Prompt 内置严格约束，禁止输出前言/套话/解释。
  - 输入 = 标题 + 纯文本正文（URL 已替换为占位符）。
  - 输出 = 固定格式的 3 行核心摘要，不超过 3 句话。
  - 所有 URL（详情页、PDF、附件下载、图片）存在 notice dict 中不动，
    由 main.py 在 Markdown 报告生成阶段直接拼接，不经过 LLM。

为什么 URL 不能过 LLM？
  1. LLM 可能改错 URL 中任意字符，导致链接报废。
  2. 长 URL（200+ 字符）浪费上下文窗口，但不对摘要产生任何价值。
  3. URL 是指针而非内容，不需要被"理解"或"总结"。
===============================================================================
"""

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from openai import OpenAI, APIStatusError, AuthenticationError, RateLimitError, APIConnectionError

import config

# ---------------------------------------------------------------------------
# 双轨制日志：对内详细记录（含堆栈），对外由 print() 输出中文友好提示
# ---------------------------------------------------------------------------
logger = logging.getLogger("summarize_agent.llm")

import config


# =============================================================================
# System Prompt — 去冗余摘要指令
# =============================================================================
# 设计思路：
#   1. 不规定固定格式（【事项】【时间】【行动】太过机械，阅读体验差）。
#   2. 要求自然中文陈述，像一句话新闻——信息密集但读起来流畅。
#   3. 表格/图片在定位文本中已嵌入为 [表格] / [图片N] 标记，
#      LLM 能读到原始数据，关键信息自然融入摘要，不单独 dump。
#   4. 严禁前言/导语/标签符号——4 句话内解决问题。

SYSTEM_PROMPT = (
    "你是一个学校行政通知的「核心信息提炼专家」。"
    "你的任务是对通知原文进行去冗余压缩，让读者在5秒内快速掌握核心信息。\n"
    "\n"
    "## 核心原则\n"
    "1. **绝对去冗余**：彻底删除所有背景铺垫、政策依据和行政客套话"
    "（如「为了进一步贯彻…」「经研究决定」「特此通知」等）。\n"
    "2. **事实说话**：只保留核心事实、关键时间点、责任对象以及具体行动指南（TODO）。"
    "严禁任何推测或原文没有的延伸。\n"
    "3. **按内容自适应排版**：不要死板套用单一模板。根据通知的复杂程度，"
    "动态选择最清晰的表达方式：\n"
    "   - 简单通知：直接输出2-3句极其精炼的陈述句。\n"
    "   - 复杂或多时段通知：使用简洁的 Markdown 列表（- 或 *）分点呈现，保证逻辑清晰。\n"
    "4. **表格的高效保留**：如果原文包含表格（以 [表格] 标记）：\n"
    "   - 如果表格数据极其简单（如只有一行），用一两句话精炼提炼。\n"
    "   - 如果表格涉及多方分工、不同截止时间或多项标准，"
    "请保留精简后的 Markdown 表格格式，或转化为清晰的对齐列表。"
    "严禁将多维度的表格强行压缩进一段长句中。\n"
    "5. **多媒体感知**：原文中若含有 [图片N] 标记，根据上下文智能推断："
    "如为二维码，在对应行动处提示「（需扫码）」；如无法判断或不重要，则直接忽略。\n"
    "\n"
    "## 格式与输出规范\n"
    "- **禁止废话**：直接输出总结后的内容，"
    "严禁包含任何前言、导语（如「以下是总结：」）或解释性尾巴。\n"
    "- **信息密度**：篇幅以「一目了然」为最高准则，杜绝大段密集的文字块。"
)


# =============================================================================
# 文本清洗 — 入 LLM 前去掉所有 URL
# =============================================================================
# parser.py（2026-05-22 起）输出的位置感知文本中：
#   - 图片已替换为 [图片1]、[图片2] 编号占位符（不含 URL）
#   - [附件] 区域仅含文件名（不含 URL）
#   - 正文中极少出现裸 URL，但仍保留清洗逻辑作为安全兜底

# 匹配正文中可能出现的裸 http(s) URL（安全兜底：正文极少含裸链，但万一有则清洗）
_BARE_URL_PATTERN = re.compile(r'https?://\S+')


def _sanitize_for_llm(text: str) -> str:
    """
    清洗文本中可能残留的裸 URL（安全兜底）。

    parser.py 已将图片替换为 [图片N] 编号占位符（不含 URL），
    附件也只保留文件名。正文中极少出现裸 URL，但万一有则清洗为 [链接]。

    Args:
        text: parser.py 输出的位置感知文本

    Returns:
        清洗后的文本
    """
    return _BARE_URL_PATTERN.sub('[链接]', text)


# =============================================================================
# 构建用户消息 — 标题 + 纯文本（URL 已清洗）
# =============================================================================

def _build_user_message(title: str, raw_text: str) -> str:
    """
    构建发给 LLM 的单条用户消息。

    格式：
      ## [通知标题]
      [清洗后的纯文本正文]

    设计理由：
      - 用 Markdown ## 标题标记让 LLM 区分标题和正文。
      - 正文已经过 _sanitize_for_llm() 清洗，不含任何 URL。
      - 不传日期、单位、编号等元数据——这些由 main.py 直接写入报告，无需 LLM 重复。

    Args:
        title:  通知标题（取自 @title 属性，完整不截断）
        raw_text: 清洗后的正文纯文本

    Returns:
        格式化的用户消息字符串
    """
    return f"## {title}\n\n{raw_text}"


# =============================================================================
# 单条通知摘要
# =============================================================================

def summarize_single(
    client: OpenAI,
    title: str,
    raw_text: str,
    max_retries: int = 2,
) -> str:
    """
    对单篇通知调用 LLM 生成去冗余摘要。

    流程：
      1. 清洗 raw_text 中的 URL → 送入 LLM 的是 URL 安全的文本
      2. 构建 User Message（标题 + 清洗后正文）
      3. 调用 OpenAI 兼容 API
      4. 失败重试最多 max_retries 次
      5. 返回 LLM 输出的纯净摘要文本

    Args:
        client:     OpenAI 客户端实例（由调用方创建并传入，复用连接）
        title:      通知标题
        raw_text:   正文纯文本（可能含 URL，会被清洗）
        max_retries: LLM 调用失败时的最大重试次数

    Returns:
        LLM 生成的摘要文本。失败返回错误提示字符串（不抛异常，不阻断批量处理流程）
    """
    # Step 1: 清洗 URL
    clean_text = _sanitize_for_llm(raw_text.strip() if raw_text else "")

    # Step 2: 空文本快速返回（不浪费 LLM 调用）
    if not clean_text:
        return "正文内容为空，无法生成摘要"

    # Step 3: 构建消息
    user_message = _build_user_message(title, clean_text)

    # Step 4: 调用 LLM（含重试）
    for attempt in range(max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=config.LLM_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                max_tokens=config.LLM_MAX_TOKENS,
                temperature=config.LLM_TEMPERATURE,
            )
            choice = response.choices[0]
            summary = choice.message.content
            finish_reason = choice.finish_reason

            # 从 response.usage 提取 token 消耗明细（用于诊断空响应问题）
            usage_info = ""
            if hasattr(response, "usage") and response.usage:
                u = response.usage
                usage_info = (
                    f"prompt_tokens={u.prompt_tokens}, "
                    f"completion_tokens={u.completion_tokens}, "
                    f"total_tokens={u.total_tokens}"
                )

            if summary:
                return summary.strip()

            # 空响应：记录 finish_reason + token 消耗，辅助定位根因
            logger.warning(
                "LLM 返回空响应 finish_reason=%s (attempt %d/%d) | model=%s | input_chars=%d | %s",
                finish_reason, attempt + 1, max_retries + 1,
                config.LLM_MODEL, len(user_message),
                usage_info,
            )
            if finish_reason == "content_filter":
                return "摘要生成失败：内容涉及安全限制，无法自动生成摘要，请点击原文链接查看"

            if attempt < max_retries:
                time.sleep(1.0)
            continue

        except AuthenticationError:
            # API Key 问题 → 不可重试，直接返回
            logger.exception("LLM 认证失败，请检查 API Key")
            return "摘要生成失败：API 密钥无效，请检查 .env 中的密钥配置"

        except RateLimitError:
            if attempt < max_retries:
                time.sleep(3.0 * (attempt + 1))  # 限频退避更长
                continue
            logger.exception("LLM 调用频率超限")
            return "摘要生成失败：调用过于频繁，请稍后重试"

        except APIConnectionError:
            if attempt < max_retries:
                time.sleep(2.0 * (attempt + 1))
                continue
            logger.exception("LLM 网络连接失败")
            return "摘要生成失败：网络连接失败，请检查 API 地址和网络状态"

        except APIStatusError as e:
            if attempt < max_retries:
                time.sleep(2.0 * (attempt + 1))
                continue
            logger.exception("LLM 服务端错误 status=%s", e.status_code)
            return "摘要生成失败：服务暂时不可用，请稍后重试"

        except Exception:
            if attempt < max_retries:
                time.sleep(2.0 * (attempt + 1))
                continue
            logger.exception("LLM 调用未知异常")
            return "摘要生成失败：遇到未知错误，请稍后重试"

    return "摘要生成失败：服务暂时无响应，请稍后重试"


# =============================================================================
# 批量并行摘要
# =============================================================================

def summarize_batch(
    notices: list[dict],
    max_workers: int = 5,
) -> list[dict]:
    """
    对多条通知并行调用 LLM 生成摘要。

    并行策略：
      - 使用 ThreadPoolExecutor（I/O 密集型任务，线程池足够）。
      - max_workers 默认 5（避免触发 API 频率限制，可根据实际限制调整）。
      - 每条通知独立调用 LLM，彼此无依赖，天然可并行。
      - 单条失败不影响其他通知（错误信息写入 summary 字段）。

    注意：
      每条通知的 raw_text 在 summarize_single 内部会被清洗（URL → 占位符），
      但 notice dict 中的原始数据（detail_url、pdf_url、images 等）完全不动，
      留给 main.py 在 Markdown 报告阶段直接使用。

    Args:
        notices: 通知列表，每条为 dict，必须包含：
                 - "title"    : 通知标题
                 - "raw_text" : 正文纯文本（可为空字符串）
                 其余字段（date, unit, detail_url, pdf_url, images, attachments 等）
                 原样保留，由 main.py 在报告阶段使用。
        max_workers: 并行线程数

    Returns:
        同 notices，但每条 dict 增加了 "summary" 字段（LLM 生成的摘要文本）。
        传入的 dict 是原地修改的（同时也返回引用以方便链式调用）。
    """
    if not notices:
        return notices

    # 创建共享的 OpenAI 客户端（复用 HTTP 连接池）
    # P2 修复：客户端创建失败（如 API Key 为空/格式错误）不再崩溃
    try:
        client = OpenAI(
            api_key=config.LLM_API_KEY,
            base_url=config.LLM_BASE_URL,
        )
    except Exception:
        logger.exception("OpenAI 客户端创建失败，请检查 API Key / Base URL 配置")
        for notice in notices:
            notice["summary"] = "摘要生成失败：API 配置有误，请检查 .env 文件中的密钥和地址配置"
        return notices

    # 构造任务列表：每条通知一个 future
    # 使用 dict 映射 future → notice_index，保证结果写回正确位置
    futures_map: dict = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for idx, notice in enumerate(notices):
            title = notice.get("title", "")
            raw_text = notice.get("raw_text", "")
            future = executor.submit(summarize_single, client, title, raw_text)
            futures_map[future] = idx

        # 等待所有任务完成，按完成顺序写回结果
        for future in as_completed(futures_map):
            idx = futures_map[future]
            try:
                notices[idx]["summary"] = future.result()
            except Exception:
                logger.exception("并行摘要任务异常，notice_index=%d", idx)
                notices[idx]["summary"] = "摘要生成失败：处理过程异常，请稍后重试"

    return notices


# =============================================================================
# 自测入口
# =============================================================================

if __name__ == "__main__":
    """
    自测：用 3 条模拟通知验证 LLM 摘要功能。

    前置条件：.env 中已配置有效的 DEEPSEEK_API_KEY。

    输出：
      直接打印每条通知的摘要结果到终端（此处为自测，允许终端输出）。
      生产流程中摘要结果写入 Markdown 报告文件（由 main.py 负责）。
    """
    # 模拟通知数据（模拟 parser 产出）
    mock_notices = [
        {
            "title": "关于举办2026年青年教师教学竞赛的通知",
            "unit": "教务处",
            "date": "2026-05-22",
            "raw_text": (
                "各学院（部）：\n"
                "为了进一步贯彻教育部关于深化本科教育教学改革的意见，"
                "在学校的统一部署和大力支持下，经研究决定，"
                "现将举办2026年青年教师教学竞赛。\n\n"
                "一、参赛对象\n"
                "全校40周岁以下（1986年1月1日以后出生）的在职青年教师。\n\n"
                "二、竞赛时间\n"
                "初赛：2026年6月10日前，由各学院自行组织。\n"
                "决赛：2026年6月25日，地点另行通知。\n\n"
                "三、报名方式\n"
                "请各学院于5月30日前将参赛教师名单报送教务处。\n\n"
                "特此通知。\n"
                "教务处\n"
                "2026年5月22日"
            ),
        },
        {
            "title": "关于校园网络维护的公告",
            "unit": "信息化建设与管理中心",
            "date": "2026-05-22",
            "raw_text": (
                "全校师生：\n"
                "为进一步提升校园网络服务质量，信息化建设与管理中心计划"
                "于2026年5月25日（周日）0:00至6:00对校园网核心设备进行升级维护。"
                "届时校园网将暂停服务，请各位师生提前做好安排。"
                "给您带来不便敬请谅解。\n"
                "信息化建设与管理中心\n"
                "2026年5月22日"
            ),
        },
        {
            "title": "关于开展2026年度科研成果统计工作的通知",
            "unit": "科学技术研究院",
            "date": "2026-05-22",
            "raw_text": (
                "各有关单位：\n"
                "根据学校年度工作安排，现将2026年度科研成果统计工作有关事项通知如下：\n"
                "一、统计范围：2025年12月1日至2026年5月31日期间取得的科研成果。\n"
                "二、填报方式：登录科研管理系统在线填报。\n"
                "三、截止时间：2026年6月15日。\n"
                "请各单位高度重视，认真组织填报工作。\n"
                "联系人：张老师，电话：12345678。\n"
                "科学技术研究院\n"
                "2026年5月22日"
            ),
        },
    ]

    print("=" * 60)
    print("llm_handler 自测开始")
    print(f"  模型: {config.LLM_MODEL}")
    print(f"  API:  {config.LLM_BASE_URL}")
    print(f"  并行数: 3 条通知")
    print("=" * 60)

    # 批量并行摘要
    result = summarize_batch(mock_notices, max_workers=3)

    for i, notice in enumerate(result):
        print(f"\n--- 通知 {i + 1}: {notice['title'][:50]}... ---")
        print(f"来源: {notice['unit']}")
        print(f"日期: {notice['date']}")
        print(f"摘要:\n{notice.get('summary', 'N/A')}")
        print()

    print("=" * 60)
    print("自测完成")

    # 验证 URL 清洗功能（parser.py 输出的文本中不应含图片 URL，但兜底清洗裸 URL）
    print("\n--- URL 清洗功能验证 ---")
    test_text = (
        "请扫描下方二维码报名。\n"
        "[图片1 (报名二维码)]\n"  # parser.py 新格式：编号占位符，不含 URL
        "详情请访问 https://example.com/register 查看。\n"
        "附件下载地址：http://old-site.edu.cn/file.doc"
    )
    cleaned = _sanitize_for_llm(test_text)
    print(f"原始文本:\n{test_text}")
    print(f"清洗后:\n{cleaned}")
    print(f"\n裸 URL 清洗: {'通过' if 'http' not in cleaned else '失败！裸 URL 未被清洗'}")
    print(f"图片占位符保留: {'通过' if '[图片1' in cleaned else '失败！图片占位符被误删'}")
