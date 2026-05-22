"""
===============================================================================
学校通知自动爬取与智能总结智能体 — 配置中心
===============================================================================
所有可配置项集中在此文件，修改配置无需深入业务代码。

XPath 设计原则：
  1. 使用相对 XPath（以每行 tr 为锚点），避免绝对路径对 DOM 变动的脆弱性。
  2. 两个通知页结构不同，因此 XPath 规则分为 TYPE1 / TYPE2 两套，互不干扰。
  3. 每个 XPath 变量上方均有注释说明其定位逻辑，方便在实际页面上用浏览器
     DevTools 验证和微调。
"""

import os
from datetime import date
from dotenv import load_dotenv

load_dotenv()  # 从 .env 文件加载敏感配置（API Key 等）

# =============================================================================
# 一、目标 URL 配置
# =============================================================================

BASE_URL = "http://42.244.33.107"

# type=1：校内通知（页面内为标准化文本，点击标题进入详情页）
TYPE1_LIST_URL = f"{BASE_URL}/defaultroot/SInformAction.do?action=init&type=1"

# type=2：学校发文（列表中是 PDF 下载链接）
TYPE2_LIST_URL = f"{BASE_URL}/defaultroot/SInformAction.do?action=init&type=2"

# =============================================================================
# 二、日期配置
# =============================================================================

# 目标日期：只采集这一天的通知。默认为脚本运行当天。
# 手动指定示例：TARGET_DATE = date(2026, 5, 15)
TARGET_DATE = date.today()

# 页面中日期的显示格式（用于 datetime.strptime 解析）。
# 常见格式："2026-05-20" → "%Y-%m-%d"；"2026/05/20" → "%Y/%m/%d"
# 如果页面显示 "2026年5月20日"，则设为 "%Y年%m月%d日"
DATE_FORMAT = "%Y-%m-%d"

# =============================================================================
# 三、XPath 规则 — 校内通知 (type=1)
# =============================================================================
# 页面实际 DOM 结构（2026-05-20 抓取验证）：
#   <TABLE style="COLOR: #333333; ... BORDER-COLLAPSE: collapse" cellSpacing=0
#          cellPadding=4 border=0 width="100%">
#     <!-- 表头行：使用 <TH>，BACKGROUND-COLOR: lightgrey -->
#     <TR><TH>日期</TH><TH>标题</TH><TH>发布单位</TH></TR>
#     <!-- 数据行：使用 <TD class="listTableLine222">，每行 3 列 -->
#     <TR style="FONT-WEIGHT: normal;">
#       <td class="listTableLine222" align="center" nowrap>2026-05-20</td>
#       <td class="listTableLine222">
#         <a onclick="open_tz('40214948');" title="完整标题">标题文本</a>
#       </td>
#       <td class="listTableLine222" align="center">发文单位</td>
#     </TR>
#   </TABLE>
#
# XPath 选取逻辑：
#   1. 通过 style 属性中的 "COLOR: #333333" + "BORDER-COLLAPSE: collapse"
#      唯一定位到数据表格（区别于上方的搜索表单表格和下方的分页表格）。
#   2. 用 tr[td[@class='listTableLine222']] 只选中数据行：
#      - 表头行用的是 <TH>，不包含 <TD>，自动被排除。
#      - 分页表格的 <TD> 没有 listTableLine222 class，也自动被排除。
#   3. 行内 XPath 均以 ./td[N] 为锚点（相对路径），列号基于实际 DOM。
#
# 如何验证/微调（浏览器 F12 → Elements → Ctrl+F）：
#   输入 XPath 表达式，观察匹配到的元素数量和内容。
# =============================================================================

# 定位通知列表中的所有数据行 tr。
# 匹配逻辑：在 style 含 "COLOR: #333333" 和 "BORDER-COLLAPSE: collapse" 的
# table 内，选中所有包含 class="listTableLine222" 的 td 的 tr。
TYPE1_ROW_XPATH = (
    "//table[contains(@style,'COLOR: #333333') and "
    "contains(@style,'BORDER-COLLAPSE: collapse')]"
    "//tr[td[@class='listTableLine222']]"
)

# 标题：行内第 2 个 td 下 a 标签的 title 属性（@title 含完整标题，不截断）
# 注意：a 标签的 text() 在长标题时会被截断，因此取 @title 而非 text()
TYPE1_TITLE_XPATH = "./td[2]//a/@title"

# 详情页 ID：从 a 标签的 onclick 属性中提取。
# 页面 JS 代码：onclick="open_tz('40214948');"
# scraper.py 中会用正则 r"open_tz\('(\d+)'\)" 提取数字 ID，
# 然后拼接为完整 URL：/defaultroot/gov/info_view_my.jsp?whir_new_verifyCode=1&editId=ID
TYPE1_ONCLICK_XPATH = "./td[2]//a/@onclick"

# 日期：行内第 1 个 td 的文本（格式：YYYY-MM-DD）
TYPE1_DATE_XPATH = "./td[1]/text()"

# 发文单位：行内第 3 个 td 的文本。
# 多个单位用 <br/> 分隔，取到的文本会包含换行，后续用正则清洗。
TYPE1_UNIT_XPATH = "./td[3]/text()"

# "下页"按钮的链接（页面使用「下页」而非「下一页」）
TYPE1_NEXT_PAGE_XPATH = "//a[contains(.,'下页')]/@href"

# 详情页 URL 模板：{edit_id} 会被替换为从 onclick 中提取的数字 ID
TYPE1_DETAIL_URL_TEMPLATE = (
    "/defaultroot/gov/info_view_my.jsp?whir_new_verifyCode=1&editId={edit_id}"
)

# 详情页正文容器的 XPath（定位到 HTML 元素，由 parser.py 调用 text_content() 提取文本）。
# 经 2026-05-20 验证：正文在 <td id="content" class="_c"> 中。
# 注意：此处只选元素，不带 //text()——text_content() 方法会递归拼接所有后代文本，
# 避免 <span> 等内联标签切断文本流导致数字丢失。
TYPE1_DETAIL_CONTENT_XPATH = "//td[@id='content']"

# =============================================================================
# 四、XPath 规则 — 学校发文 (type=2)
# =============================================================================
# 页面实际 DOM 结构（2026-05-20 抓取验证）：
#   <TABLE style="COLOR: #333333; ... BORDER-COLLAPSE: collapse" ...>
#     <!-- 表头行：4 列 -->
#     <TR><TH>日期</TH><TH>编号</TH><TH>标题</TH><TH>发布人</TH></TR>
#     <!-- 数据行：每行 4 列 + 1 个 script -->
#     <TR style="FONT-WEIGHT: normal;">
#       <td class="listTableLine222" align="center" nowrap>2026-05-20</td>
#       <td class="listTableLine222" nowrap>苏大研〔2026〕29号</td>
#       <td class="listTableLine222">
#         <a href="/defaultroot/public/download/download.jsp?verifyCode=...&FileName=....pdf&name=...pdf&path=information"
#            onclick="..." title="完整标题">标题文本</a>
#       </td>
#       <td class="listTableLine222" align="center">发布人姓名</td>
#     </TR>
#   </TABLE>
#
# 注意：
#   - 第 4 列是「发布人」（人名），不是「发布单位」。
#   - PDF 下载链接是 a 标签的 href 属性，可能是相对路径，需拼接 BASE_URL。
#   - 标题同样取 @title 属性以避免截断。
# =============================================================================

# 定位通知列表中的所有数据行 tr（与 type=1 共用同一套表格定位逻辑）
TYPE2_ROW_XPATH = (
    "//table[contains(@style,'COLOR: #333333') and "
    "contains(@style,'BORDER-COLLAPSE: collapse')]"
    "//tr[td[@class='listTableLine222']]"
)

# 标题：行内第 3 个 td 下 a 标签的 title 属性（完整标题）
TYPE2_TITLE_XPATH = "./td[3]//a/@title"

# PDF 下载链接：行内第 3 个 td 下 a 标签的 href 属性
# 格式示例：/defaultroot/public/download/download.jsp?verifyCode=...&FileName=....pdf&...
TYPE2_PDF_HREF_XPATH = "./td[3]//a/@href"

# 日期：行内第 1 个 td 的文本
TYPE2_DATE_XPATH = "./td[1]/text()"

# 发文编号：行内第 2 个 td 的文本（如 "苏大研〔2026〕29号"）
TYPE2_NUMBER_XPATH = "./td[2]/text()"

# 发布人：行内第 4 个 td 的文本（注意：type=2 是发布「人」，不是发布「单位」）
TYPE2_PUBLISHER_XPATH = "./td[4]/text()"

# "下页"按钮的链接
TYPE2_NEXT_PAGE_XPATH = "//a[contains(.,'下页')]/@href"

# =============================================================================
# 五、请求控制配置
# =============================================================================

# 每次 HTTP 请求之间的间隔（秒），避免给服务器造成压力或被封 IP
REQUEST_DELAY = 1.0

# 单次请求超时时间（秒）
REQUEST_TIMEOUT = 30

# 请求失败时的最大重试次数
MAX_RETRIES = 3

# 重试间隔（秒），指数退避：delay * (2 ** retry_count)
RETRY_BACKOFF = 2.0

# 页面编码。
# 注意：页面 meta 标签声明 charset=gb2312，但实际响应体为 UTF-8 编码。
# 这是老旧 Java 系统的常见模板配置错误（模板写 gb2312，后端渲染输出 UTF-8）。
# 经 2026-05-20 十六进制验证：<title> 字节 e6 a0 a1 e5 86 85 = "校内通知"（UTF-8）。
PAGE_ENCODING = "utf-8"

# HTTP 请求头，模拟正常浏览器访问
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

# =============================================================================
# 六、大模型配置（DeepSeek）
# =============================================================================
# DeepSeek API 完全兼容 OpenAI SDK，只需修改 base_url 和 api_key。

# API Key：从环境变量 DEEPSEEK_API_KEY 读取（写入 .env 文件）
LLM_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")

# DeepSeek 兼容 OpenAI 接口的 base_url
LLM_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

# 模型名称
LLM_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")

# LLM 调用的最大 token 数（摘要输出较短，512 足够）
LLM_MAX_TOKENS = 512

# LLM 温度参数（摘要任务需要确定性输出，设低值）
LLM_TEMPERATURE = 0.3

# =============================================================================
# 七、输出配置
# =============================================================================

# 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# PDF 下载存放目录（按日期自动创建子目录，如 pdfs/2026-05-20/）
PDF_OUTPUT_DIR = os.path.join(PROJECT_ROOT, "output", "pdfs")

# Markdown 报告输出目录
REPORT_OUTPUT_DIR = os.path.join(PROJECT_ROOT, "output", "reports")

# 报告文件名格式：{date}_通知摘要报告.md
REPORT_FILENAME_TEMPLATE = "{date}_通知摘要报告.md"

# 全链路 Debug 数据输出目录（每次运行自动创建时间戳子目录）
DEBUG_OUTPUT_DIR = os.path.join(PROJECT_ROOT, "output", "debug")

# =============================================================================
# 八、PDF 解析配置
# =============================================================================

# PDF 文本提取策略：
#   - pdfplumber：适合表格型 PDF，保留排版结构，对中文支持好
#   - 如果 pdfplumber 提取结果为空，自动回退到 pypdf
# 当前首选 pdfplumber（已在 parser.py 中实现回退逻辑）
PDF_FALLBACK_ENABLED = True
