# Python 速记

## 函数

```python
def greet(name: str) -> str:
    return f"你好，{name}"
```

- `def` 定义函数，`return` 返回值
- 参数默认值写在签名里，调用时可以省略

## 路径处理：用 pathlib，别拼接字符串

```python
from pathlib import Path

path = Path("demo_workspace") / "todo.txt"   # 跨 Windows / Linux 都能用
```

拼接字符串写路径容易踩坑：Windows 用 `\`，Linux 用 `/`，方向不一样。

`Path.is_file()` 判断是不是文件，`Path.iterdir()` 遍历目录内容。

## 读文件

```python
with open("todo.txt", encoding="utf-8") as f:
    text = f.read()
```

- `with` 用完会自动关闭文件，不用手写 close()
- Windows 上中文文件不写 `encoding="utf-8"` 容易乱码，务必带上

## 列表推导

```python
names = [p.name for p in Path("demo_workspace").iterdir() if p.is_file()]
```

等价于「遍历 → 过滤 → 取字段」，是 Python 里最常用的写法。
