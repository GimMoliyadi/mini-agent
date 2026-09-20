"""项目配置：从 .env 加载环境变量，产出 LLM 连接所需的三项信息。

为什么单独一个模块：
「配置」和「主流程」是两件事。Phase 2 加工具、Phase 3 执行工具调用时
都要用同一份配置，如果写死在 main.py 里，后面就得反复搬家。
"""

from dataclasses import dataclass
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
ENV_FILE = PROJECT_ROOT / ".env"

# Agent 唯一允许读写的工作目录（沙盒）。
# 必须在这里命名，而不是在需要的地方各自写 "demo_workspace"：
# 它是安全边界的唯一定义点。路径校验、启动横幅、Phase 5 的写文件都要对齐它，
# 散写成字面量迟早会漂移到两个不同的值。
WORKSPACE_DIR = Path(os.environ.get("AGENT_WORKSPACE", PROJECT_ROOT / "demo_workspace")).resolve()

# 必须存在的三个变量：缺任何一个，"LLM 判断"这一步就不成立。
REQUIRED_ENV_VARS = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL")

# 单次请求的最长等待秒数。
# 主要防的是代理配错导致 CLI 无限挂住：超时会报错，无限等待只会让你以为程序死了。
REQUEST_TIMEOUT_SECONDS = 120

# 单个任务里最多问几次模型，也就是 Agent 循环最多能跑几轮。
#
# 为什么必须有一个天花板：模型每次拿到工具结果后都可能「还想再调一次工具」，
# 程序本身没有任何理由判断该收手了——如果模型一直这么回，循环就永远转下去，
# 不停花钱、不停刷屏。Phase 4 刻意只做最简单的保护（数个数），
# 不做「检测重复调用 / 检测原地打转」这类复杂防循环，那是更后面阶段的事。
#
# 注意它数的是「问了几次模型」，不是「执行了几次工具」：
# 每一轮都先问、后执行，所以最多能执行 MAX_AGENT_STEPS - 1 轮工具。
# 最后一次问必须留给收口——否则工具结果发出去了，却没有模型的回答。
# 定 8 是因为单个任务的读写通常在个位数轮以内；想改就改这一处。
MAX_AGENT_STEPS = 8

# 单个工具结果最多允许多少**字符**进上下文。
#
# 为什么要这个上限：工具结果会整段塞回对话历史，而历史每次请求都整份重发。
# 一旦读到一个巨大文件，这一轮就把上下文撑满，后面每一步都拖着这份内容走。
#
# 刻意用字符数而不是 token 数：数 token 要引 tokenizer、要装依赖、
# 还要知道当前模型的计数方式，对「防止上下文被撑爆」这个目的完全没必要——
# 字符数够糙，但够稳、一眼看得懂。
#
# 超限时会截断，并明确告诉模型「已截断、原始内容更长」，
# 不静默丢内容：静默截断会让模型以为自己拿到的就是全文，然后基于不完整信息作答。
# 定 4000 是因为它明显小于常见模型的上下文窗口，又够装下几页笔记。
MAX_TOOL_RESULT_CHARS = 4000

# read_file 的内容预算要小于全局工具结果上限，给文件名、分页元数据和边界标记
# 留出空间。这样正常的 read_file 不会先生成一个会被 main.py 截断的结果。
READ_RESULT_OVERHEAD_RESERVE_CHARS = 512
MAX_READ_RESULT_CHARS = MAX_TOOL_RESULT_CHARS - READ_RESULT_OVERHEAD_RESERVE_CHARS

# 发给模型时保留最近几个完整的工具回合。
# 更早的 read_file 结果会变成可重读的引用，避免长任务把整段旧内容永久带上。
MAX_RECENT_TOOL_ROUNDS = 2

# Context Management 实验模式。
# OFF 是控制组；WRITE_ONLY 是最初的 Phase 7A 方案；FULL 保留当前完整策略。
CONTEXT_MODES = ("OFF", "WRITE_ONLY", "FULL")
DEFAULT_CONTEXT_MODE = "WRITE_ONLY"

# 有副作用工具的最小审批模式。交互式 main.py 默认询问用户；自动入口必须显式选择
# ALLOW 或 DENY，避免测试因为 input() 卡住。
APPROVAL_MODES = ("ASK", "ALLOW", "DENY")
DEFAULT_APPROVAL_MODE = "ASK"


def get_context_mode() -> str:
    """读取并校验当前 Context Management 实验模式。"""
    mode = os.environ.get("CONTEXT_MODE", DEFAULT_CONTEXT_MODE).strip().upper()
    if mode not in CONTEXT_MODES:
        allowed = ", ".join(CONTEXT_MODES)
        raise ValueError(f"CONTEXT_MODE 必须是 {allowed} 之一，当前是：{mode!r}")
    return mode


def get_approval_mode() -> str:
    """读取并校验副作用工具审批模式。"""
    mode = os.environ.get("TOOL_APPROVAL_MODE", DEFAULT_APPROVAL_MODE).strip().upper()
    if mode not in APPROVAL_MODES:
        allowed = ", ".join(APPROVAL_MODES)
        raise ValueError(f"TOOL_APPROVAL_MODE 必须是 {allowed} 之一，当前是：{mode!r}")
    return mode

# 本地地址不走代理的绕过清单。
#
# 为什么必须显式写它：HTTP 库取代理时会调 urllib.request.getproxies()，
# 在 Windows 上这函数读系统代理注册表的 ProxyServer，**却不返回 ProxyOverride**
# （就是「局域网除外」那一栏）。结果你自己在 Windows 设置里填的
# 「127.0.0.1;<local>」被静默丢弃，localhost / 127.0.0.1 也被一股脑送进代理。
#
# 后果很隐蔽：请求本地 mock 服务器或本地 Ollama 时，收到的是代理转回来的
# 502，而不是「连接被拒绝」，看起来像服务端故障，实际是本机没人监听。
# NO_PROXY 是这条链路上唯一真正生效的绕过开关，所以这里补上默认值。
LOCAL_NO_PROXY = "127.0.0.1,localhost"


@dataclass(frozen=True)
class LLMConfig:
    """一次 LLM 会话需要的全部连接信息，集中在一处。

    frozen=True 表示建好之后不允许再改，避免中途被意外篡改后难以排查。
    """

    api_key: str
    base_url: str
    model: str


def load_env_file() -> None:
    """把 .env 里的键值对读进 os.environ。

    刻意用标准库手写，而不是装 python-dotenv：一个十几行的解析器你能看清它在做什么，
    这本身就是本项目「自己搭」的目的之一。

    规则很简单：
    - 跳过空行和以 # 开头的注释
    - 按第一个 = 拆成键和值（所以值里可以再出现 = 不会出错）
    - 去掉两端的空白，以及值外面那对可选的引号

    注意用 setdefault 而不是直接赋值：不覆盖已存在的环境变量。
    这样你在终端里临时 export 一个值，就能覆盖 .env 里的值，
    便于调试而不必每次都改文件。
    """
    if not ENV_FILE.is_file():
        return

    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            print(f"[警告] .env 第 {line_number} 行缺少 '='，已跳过：{line}")
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        # 先 strip 再 strip 引号：处理 KEY="value" 和 KEY='value' 两种写法
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        os.environ.setdefault(key, value)


def load_config() -> LLMConfig:
    """产出 LLMConfig；缺必要变量时直接终止，并明确告诉你缺什么、怎么补。

    这里用 SystemExit 而不是抛 KeyError：
    「没配 Key」属于配置错误，不是运行时异常。把它当成一个明确、可操作的终止，
    比让 KeyError 在聊天中途炸出来好得多。
    """
    load_env_file()
    get_context_mode()
    get_approval_mode()

    # 只补默认值，不覆盖：若你已经在终端里 export 过 NO_PROXY，以你的为准。
    # 放在 load_env_file 之后，是因为解析 .env 用 setdefault 写进 environ，
    # 这里同样必须排在后面才不会反过来污染 .env 的解析逻辑。
    os.environ.setdefault("NO_PROXY", LOCAL_NO_PROXY)

    missing = [name for name in REQUIRED_ENV_VARS if not os.environ.get(name)]
    if missing:
        raise SystemExit(
            "[错误] 缺少必要的环境变量：" + ", ".join(missing) + "\n"
            "请复制 .env.example 为 .env 并填入你的 API Key：\n"
            "  PowerShell : Copy-Item .env.example .env\n"
            "  CMD        : copy .env.example .env"
        )

    return LLMConfig(
        api_key=os.environ["OPENAI_API_KEY"],
        base_url=os.environ["OPENAI_BASE_URL"],
        model=os.environ["OPENAI_MODEL"],
    )
