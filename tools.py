"""工具层：工具的「说明书」和真正干活的函数。

这里只有两样东西，一一对应：
    - *_TOOL：写给模型看的 JSON 描述（模型照着它生成调用请求）
    - TOOL_HANDLERS：写给 Python 用的函数表（真的去干活）

Phase 5 有三个工具：list_files（看有什么）、read_file（读内容）、
write_file（把结果留下来）。加工具只改这一个文件，main.py 一行都不用动。

为什么单独一个模块：工具定义要贴合「模型能读懂的描述」，
执行层要处理「真实文件系统与错误」，两者的变化原因完全不同。
main.py 的执行器会从这两处各取一半，所以边界现在就划对。
"""

from pathlib import Path

from config import WORKSPACE_DIR

# 工具的「说明书」。发给模型的不是函数本身，而是这份 JSON 描述；
# 模型照着它生成一次工具调用请求。
# required 里必须列上 path，否则模型可能给出空参数。
READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "读取工作目录内一个文件的文本内容。"
            "当用户想了解某个文件里写了什么时，用这个工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "相对于工作目录的路径，例如 \"todo.txt\"",
                }
            },
            "required": ["path"],
        },
    },
}

# 「不知道有什么文件」时的第一个工具。
# path 是可选的（没有 required 字段），省略就是列工作目录本身。
# 描述里那句「不要猜文件名」是刻意的：模型有凭空编文件名的倾向，
# 把这句写进工具说明书，比在系统提示词里写「必须先看目录」更贴切——
# 前者是模型自己读到工具后自然形成的用法，后者是硬命令。
LIST_FILES_TOOL = {
    "type": "function",
    "function": {
        "name": "list_files",
        "description": (
            "列出工作目录里的文件和子目录，只看一层，不递归。"
            "如果你不知道有哪些文件、或者用户没有点名具体文件，"
            "先用这个工具，不要猜文件名。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要列出的目录，相对工作目录。省略表示工作目录本身。",
                },
            },
        },
    },
}

# 把内容写回工作目录。
# 允许覆盖已有文件——这是刻意选择，README 里有说明：
# 沙盒已经把破坏范围锁死在一个专用目录里，而拒绝覆盖会让「重跑同一个任务」
# 直接失败，还得再加一个 force 参数让模型学习怎么绕过。那比覆盖本身更复杂。
WRITE_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": (
            "把文本内容写入工作目录内的一个文件（UTF-8）。"
            "父目录不存在会自动创建，只能创建在工作目录内。"
            "已经存在的同名文件会被覆盖。"
            "不能用 .. 或绝对路径写到工作目录之外。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "相对工作目录的文件路径，例如 \"notes/summary.md\"",
                },
                "content": {
                    "type": "string",
                    "description": "要写入的完整文本内容",
                },
            },
            "required": ["path", "content"],
        },
    },
}

# 已注册工具清单。主程序把这份列表原样发给模型，
# 所以这里就是「模型知道自己会什么」的唯一来源。
# 三份必须名字对得上：这里的 name、TOOL_HANDLERS 的 key、函数形参名。
AVAILABLE_TOOLS = [LIST_FILES_TOOL, READ_FILE_TOOL, WRITE_FILE_TOOL]


def resolve_inside_workspace(path: str) -> Path:
    """把模型给的路径解析成绝对路径，并保证它落在工作目录内。

    这是整个项目最重要的一条不变量：Agent 不许碰沙盒之外的任何东西。

    参数名必须是 path，和上面工具声明里的 "path" 保持一致：
    Phase 3 的执行层把模型给的参数**按名字**当关键字参数传进来，
    两边名字对不上，工具就永远调不动（Phase 3 实测踩过）。

    必须先 resolve() 再比较。只靠字符串前缀判断会漏掉三种情况：
     ".." 跳级、符号链接指向外部、模型直接给一个绝对路径。
    resolve() 会把这三者都还原成真实路径，比较才有意义。

    越界时抛 PermissionError，绝不静默返回空值或改成沙盒内的路径：
    边界必须显式失败，静默降级等于把它写成装饰。
    """
    target = (WORKSPACE_DIR / path).resolve()
    root = WORKSPACE_DIR.resolve()

    if not target.is_relative_to(root):
        raise PermissionError(f"路径越出工作目录，拒绝访问：{path}")

    return target


def read_file(path: str) -> str:
    """读取工作目录内一个文件的文本内容。

    纯函数：成功返回内容，失败抛原生异常
    （FileNotFoundError / PermissionError / UnicodeDecodeError）。

    不吞异常、也不返回错误字符串——「怎么把失败说给模型听」
    是 Phase 3 执行层的职责，工具本身不该知道模型的存在。
    """
    return resolve_inside_workspace(path).read_text(encoding="utf-8")


def list_files(path: str | None = None) -> str:
    """列出一层目录内容，返回适合模型阅读的文字。

    只看一层、不递归：递归会把输出撑大，而且模型通常只需要知道
    「顶层有什么」就能决定下一步读哪个。想看子目录内容，自己再调一次。

    标 [f]/[d] 区分文件和目录，文件大小让模型能判断该不该读。
    空 path 表示工作目录本身。越界照常被 resolve_inside_workspace 拦下。
    """
    target = resolve_inside_workspace(path or ".")
    if not target.is_dir():
        raise NotADirectoryError(f"不是一个目录：{path}")

    entries = sorted(target.iterdir(), key=lambda entry: entry.name)
    if not entries:
        return f"目录是空的：{path or '.'}"

    lines = []
    for entry in entries:
        kind = "d" if entry.is_dir() else "f"
        size = "" if entry.is_dir() else f"  ({entry.stat().st_size} 字节)"
        lines.append(f"[{kind}] {entry.name}{size}")

    return f"工作目录 {path or '.'} 的内容：\n" + "\n".join(lines)


def write_file(path: str, content: str) -> str:
    """把文本写进工作目录内的一个文件，允许覆盖。

    父目录不存在就创建，但校验在创建之前已经做完，所以只会建在沙盒里面。
    返回文字里带上「已覆盖」还是「新文件」，让覆盖这件事显式可见。

    为什么手写 open 而不是 write_text：Path.write_text 走默认文本模式，
    在 Windows 上会把 \\n 静默翻译成 \\r\\n——落盘内容比模型给的多了 1 字节/行，
    返回值里的字节数也就说错了。指定 newline="" 让换行原样落盘，
    这样「模型写了什么 = 磁盘上有什么 = 我报告的字节数」三者一致。
    """
    target = resolve_inside_workspace(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    existed = target.is_file()
    with target.open("w", encoding="utf-8", newline="") as handle:
        handle.write(content)

    size = len(content.encode("utf-8"))
    state = "已覆盖已有文件" if existed else "已写入新文件"
    return f"已写入 {target.name}（{size} 字节，{state}）"


# 工具名 → 真正干活的函数。
#
# 为什么需要这张表：模型回过来的调用里只有「名字 + 参数」，没有函数引用。
# 它没法直接调用你代码里的 read_file，只能通过名字找到它。
# 这一张表就是「名字」翻译成「函数」的唯一地点。
#
# 加一个新工具 = 一个 *_TOOL 说明书 + AVAILABLE_TOOLS 里加一项 + 这里加一行。
# main.py 里没有任何工具名字，所以主流程一行都不用改。
TOOL_HANDLERS = {
    "list_files": list_files,
    "read_file": read_file,
    "write_file": write_file,
}
