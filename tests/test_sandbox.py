"""沙盒边界 + 工具注册表的单元测试。

这条不变量是整个项目里最不能出错的一条：Agent 不许读也**不许写**沙盒之外的文件。
沙盒外就是 .env（API Key）、是 C:\Windows、是用户主目录。
Phase 5 起 write_file 也吃路径，所以同一套边界必须同时管住读写两侧。

直接运行，不依赖 pytest（项目目前只装了一个第三方包）：
    python tests\test_sandbox.py
"""

import inspect
import sys
import tempfile
from pathlib import Path

# tests/ 不是包，直接跑这个脚本时 sys.path[0] 是 tests/ 自己，
# 看不到项目根目录下的主模块。这里补进去，才能 import 到 tools。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # noqa: E402

import tools  # noqa: E402

# 应当被拦下的读取路径。每一行都代表一种真实可行的越界手法。
READ_ESCAPING_PATHS = [
    "../config.py",                     # 最朴素的上级跳一格
    "..\\config.py",                    # Windows 分隔符，效果完全一样
    "../.env",                          # 里面是 API Key，绝对不能读
    "C:\\Windows\\win.ini",             # 模型直接给 Windows 绝对路径
    "/etc/passwd",                      # 模型按 POSIX 习惯给绝对路径
    "../demo_workspace/../config.py",   # 绕一圈再出去
    "..\\..\\..\\Users\\30858\\secret",  # 多级跳出到用户目录
]

# 应当被拦下的写入路径。写比读危险：读是泄露，写是破坏。
WRITE_ESCAPING_PATHS = [
    "../evil.txt",
    "..\\evil.txt",
    "C:\\Windows\\evil.txt",
    "../.env",                          # 试图改写 API Key
]

# 应当被放行的路径：都在沙盒内。
INSIDE_PATHS = ["todo.txt", "notes/todo.txt"]


def check_registry() -> None:
    """校验工具说明书、名字表、真实函数三者是否真的对得上。

    守的是 Phase 3 踩过的坑：执行层把模型给的 JSON 键**按名字**
    当关键字参数转发给函数，名字对不上工具就永远调不动。

    用 inspect 去读函数真实的形参名，而不是手写一份名单比对——
    手写名单就是第二份真相，改函数时很容易忘了改它。
    """
    schema_names = {tool["function"]["name"] for tool in tools.AVAILABLE_TOOLS}
    handler_names = set(tools.TOOL_HANDLERS)
    assert schema_names == handler_names, (
        f"工具清单和实现表不一致："
        f"有说明书没实现 {sorted(schema_names - handler_names)}，"
        f"有实现没说明书 {sorted(handler_names - schema_names)}"
    )

    for tool in tools.AVAILABLE_TOOLS:
        declared = tool["function"]
        name = declared["name"]
        handler = tools.TOOL_HANDLERS[name]

        declared_params = set(declared["parameters"]["properties"])
        real_params = set(inspect.signature(handler).parameters)
        assert declared_params == real_params, (
            f"工具 {name} 的参数名对不上：说明书 {sorted(declared_params)}，"
            f"函数 {sorted(real_params)}"
        )

        # required 里每一项都必须真的在 properties 里声明过，否则模型会按空参数调用
        required = set(declared["parameters"].get("required", []))
        assert required <= declared_params, (
            f"工具 {name} 的 required 里有未声明的参数："
            f"{sorted(required - declared_params)}"
        )

        print(f"OK  工具 {name}：说明书参数与函数形参一致（{sorted(declared_params)}）")


def check_tools() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        (workspace / "notes").mkdir()
        (workspace / "todo.txt").write_text("hello sandbox", encoding="utf-8")

        # 把沙盒指向临时目录，测试不碰真实的 demo_workspace
        tools.WORKSPACE_DIR = workspace

        # 1) 正常读取必须放行，而且读到的确实是那个文件的内容
        text = tools.read_file("todo.txt")
        assert text == "hello sandbox", f"内容不符：{text!r}"
        print(f"OK  正常读取：todo.txt -> {text!r}")

        # 2) 相对解析必须仍然落在沙盒内（没有偷偷跳到别处）
        for relative_path in INSIDE_PATHS:
            target = tools.resolve_inside_workspace(relative_path)
            assert target.is_relative_to(workspace), (
                f"{relative_path} 解析到了沙盒之外：{target}"
            )
            print(f"OK  沙盒内解析正确：{relative_path} -> {target.relative_to(workspace)}")

        # 3) 越界读取路径必须全部被拒绝
        for relative_path in READ_ESCAPING_PATHS:
            try:
                tools.resolve_inside_workspace(relative_path)
            except PermissionError:
                print(f"OK  已拦截越界读取：{relative_path}")
            else:
                raise AssertionError(f"越界路径竟然被放行：{relative_path}")

        # 4) 沙盒内不存在的文件：必须报 FileNotFoundError，
        #    而不是被静默改写成另一个存在的路径
        try:
            tools.read_file("notes/nope.txt")
        except FileNotFoundError:
            print("OK  沙盒内不存在的文件报 FileNotFoundError（没有偷偷改路径）")
        else:
            raise AssertionError("沙盒内不存在的文件竟然读成功了")

        # 5) 越界写入路径必须全部被拒绝，而且**什么都没被创建**
        for relative_path in WRITE_ESCAPING_PATHS:
            try:
                tools.write_file(relative_path, "should never land here")
            except PermissionError:
                print(f"OK  已拦截越界写入：{relative_path}")
            else:
                raise AssertionError(f"越界写入竟然被放行：{relative_path}")
        # 沙盒的父目录里不应出现任何测试写出来的东西
        leaked = [name for name in workspace.parent.iterdir() if name.name in {"evil.txt"}]
        assert not leaked, f"越界写入竟然落盘了：{leaked}"
        print("OK  越界写入没有留下任何文件")

        # 6) 正常写入：父目录不存在时会自动创建，而且只建在沙盒内
        reply = tools.write_file("reports/summary.md", "# 标题\n内容")
        written = workspace / "reports" / "summary.md"
        assert written.is_file(), "write_file 没有真的落盘"
        assert written.read_text(encoding="utf-8") == "# 标题\n内容", "写入内容不符"
        assert "新文件" in reply, f"返回值应该说明是新文件：{reply!r}"
        print(f"OK  写入新文件并自动建父目录：reports/summary.md -> {reply}")

        # 7) 允许覆盖已有文件，但返回值必须明说是覆盖
        tools.write_file("notes/second.md", "第一版")
        before = (workspace / "notes" / "second.md").read_text(encoding="utf-8")
        assert before == "第一版"
        reply = tools.write_file("notes/second.md", "第二版")
        after = (workspace / "notes" / "second.md").read_text(encoding="utf-8")
        assert after == "第二版", f"覆盖没生效：{after!r}"
        assert "已覆盖" in reply, f"返回值应该说明是覆盖：{reply!r}"
        print(f"OK  覆盖已有文件：{reply}")

        # 8) 列目录：能区分文件和目录
        listing = tools.list_files()
        assert "[f] todo.txt" in listing, f"没列到文件：{listing!r}"
        assert "[d] notes" in listing, f"没列到目录：{listing!r}"
        print("OK  列目录正常，且区分了 [f] / [d]")

        # 9) 列子目录：只看一层，不会把整个沙盒递归倒出来
        sub = tools.list_files("notes")
        assert "second.md" in sub, f"子目录列不全：{sub!r}"
        assert "todo.txt" not in sub, f"子目录串到了上一层：{sub!r}"
        print("OK  列子目录只列一层")

        # 10) 列目录同样受沙盒约束
        try:
            tools.list_files("../")
        except PermissionError:
            print("OK  越界列目录被拦截")
        else:
            raise AssertionError("越界列目录竟然被放行")

        # 11) 列一个文件而不是目录：应当报 NotADirectoryError
        try:
            tools.list_files("todo.txt")
        except NotADirectoryError:
            print("OK  列一个非目录路径报 NotADirectoryError")
        else:
            raise AssertionError("列非目录竟然没报错")

    print("\n沙盒边界测试全部通过。")


def main() -> None:
    check_registry()
    check_tools()


if __name__ == "__main__":
    main()
