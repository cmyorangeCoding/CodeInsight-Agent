"""
代码切块模块（chunker）

职责：把单个源码文件切成适合检索的片段（chunk）。
不负责文件扫描、不负责入库、不负责向量化。

切分策略：
  - Python：优先按函数/类（ast 解析）
  - Java：优先按方法/类（正则 + 括号匹配）
  - Markdown/配置：按章节或语义边界
  - 其他：按行窗口（约 80~150 行，重叠 15~20 行）

每个 chunk 至少包含：file_path、language、symbol_name、line_start、line_end、content。
embedding_text 由调用方组装（file_path + symbol_name + content），不在此模块生成。
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field


@dataclass
class CodeChunk:
    """单个代码片段。"""
    file_path: str # 文件路径
    language: str  # 文件语言类型
    symbol_name: str | None  # 符号名：类名/函数名/章节名，无则为None
    line_start: int  # 片段起始行号（1-based）
    line_end: int  # 片段结束行号（1-based）
    content: str  # 片段原始代码文本
    # embedding 用的文本由 ingestion 组装，这里不存


def _line_window_chunk(
    lines: list[str],
    file_path: str,
    language: str,
    chunk_size: int = 100,
    overlap: int = 15,
) -> list[CodeChunk]:
    """
    通用按行窗口切块，保留重叠。
    适用场景：无法语法解析的文件、语法报错文件、其他未知语言。
    :param lines: 文件按行拆分后的字符串列表
    :param file_path: 文件路径
    :param language: 语言标识
    :param chunk_size: 每个窗口最大行数
    :param overlap: 窗口之间重叠行数，保证上下文不被切断
    :return: CodeChunk 片段列表
    """

    chunks: list[CodeChunk] = []  # 存储切分完成的片段
    total = len(lines) # 获取文件总行数
    if total == 0:
        return chunks

    step = chunk_size - overlap # 窗口滑动步长 = 窗口大小-重叠行数
    start = 0
    while start < total:
        end = min(start + chunk_size, total)# 当前窗口结束下标，不超过总行
        content = "\n".join(lines[start:end])
        chunks.append(
            CodeChunk(
                file_path=file_path,
                language=language,
                symbol_name=None,
                line_start=start + 1,
                line_end=end,
                content=content,
            )
        )
        if end >= total:
            break
        start += step
    return chunks


def _chunk_python(
    content: str, file_path: str, lines: list[str]
) -> list[CodeChunk]:
    """Python：按顶层函数/类切块，剩余模块级代码用行窗口兜底。"""
    chunks: list[CodeChunk] = []
    try:
        tree = ast.parse(content) # 将源码解析为AST语法树
    except SyntaxError:
        # 语法错误时回退到通用切块
        return _line_window_chunk(lines, file_path, "python")

    # 收集顶层定义的行范围
    covered: list[tuple[int, int]] = [] # 保存类/函数占用的行区间
    for node in tree.body: # 遍历文件顶层节点
        # 判断节点是否为普通函数、异步函数、类
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = node.lineno
            end = getattr(node, "end_lineno", node.lineno) or node.lineno
            covered.append((start, end))
            chunks.append(
                CodeChunk(
                    file_path=file_path,
                    language="python",
                    symbol_name=node.name,
                    line_start=start,
                    line_end=end,
                    content="\n".join(lines[start - 1 : end]),
                )
            )

    # 未被顶层定义覆盖的行（模块级代码、import 等）用行窗口切块
    covered.sort()
    uncovered_ranges: list[tuple[int, int]] = []
    cursor = 1
    for s, e in covered:
        if cursor < s:
            uncovered_ranges.append((cursor, s - 1))
        cursor = max(cursor, e + 1)
    if cursor <= len(lines):
        uncovered_ranges.append((cursor, len(lines)))

    # 处理每一段未覆盖的模块代码
    for s, e in uncovered_ranges:
        if e < s:
            continue
        sub_lines = lines[s - 1 : e]
        # 模块级代码如果不多，直接作为一个 chunk
        if len(sub_lines) <= 120:
            chunks.append(
                CodeChunk(
                    file_path=file_path,
                    language="python",
                    symbol_name="<module>",
                    line_start=s,
                    line_end=e,
                    content="\n".join(sub_lines),
                )
            )
        else:
            chunks.extend(
                _line_window_chunk(sub_lines, file_path, "python")
            )
            # 修正行号（行窗口是从 1 开始的，需要加上偏移）
            for c in chunks:
                if c.symbol_name is None and c.line_start == 1:
                    c.line_start += s - 1
                    c.line_end += s - 1

    # 按行号排序
    chunks.sort(key=lambda c: (c.line_start, c.line_end))
    # 过滤掉空内容的 chunk（空行、纯空白）
    return [c for c in chunks if c.content.strip()]


# Java 类/方法声明正则
_JAVA_CLASS_RE = re.compile(
    r"^\s*(?:public|private|protected|abstract|final|static)*\s*"
    r"(?:class|interface|enum)\s+(\w+)",
    re.MULTILINE,
)
_JAVA_METHOD_RE = re.compile(
    r"^\s*(?:public|private|protected|static|final|synchronized|abstract|native)*\s*"
    r"[\w<>\[\],\s]+\s+(\w+)\s*\(",
    re.MULTILINE,
)


def _find_brace_end(lines: list[str], start_line: int) -> int:
    """从 start_line 开始，找到匹配的右大括号所在行（1-based）。"""
    depth = 0  # 大括号嵌套深度计数器
    found_open = False  # 是否已经找到第一个{标记
    for i in range(start_line - 1, len(lines)):
        for ch in lines[i]:
            if ch == "{":
                depth += 1
                found_open = True
            elif ch == "}":
                depth -= 1
                if found_open and depth == 0:
                    return i + 1
    return len(lines)


def _chunk_java(
    content: str, file_path: str, lines: list[str]
) -> list[CodeChunk]:
    """Java：按类/方法切块，无法解析时回退到行窗口。"""
    chunks: list[CodeChunk] = []
    declarations: list[tuple[int, str, str]] = []  # (line, name, kind)

    # 匹配所有类定义
    for m in _JAVA_CLASS_RE.finditer(content):
        line = content[: m.start()].count("\n") + 1
        declarations.append((line, m.group(1), "class"))

    # 匹配所有方法定义
    for m in _JAVA_METHOD_RE.finditer(content):
        line = content[: m.start()].count("\n") + 1
        declarations.append((line, m.group(1), "method"))

    # 没有识别到任何类、方法
    if not declarations:
        return _line_window_chunk(lines, file_path, "java")

    # 按声明行号从小到大排序
    declarations.sort(key=lambda x: x[0])

    # 遍历每一个类/方法声明，查找结束行
    for idx, (line, name, kind) in enumerate(declarations):
        end_line = _find_brace_end(lines, line)
        chunks.append(
            CodeChunk(
                file_path=file_path,
                language="java",
                symbol_name=name,
                line_start=line,
                line_end=end_line,
                content="\n".join(lines[line - 1 : end_line]),
            )
        )

    # 去重：方法可能在类内部，保留更精确的（方法）
    # 简单处理：如果一个 chunk 完全包含在另一个里，保留较小的
    filtered: list[CodeChunk] = []
    for c in chunks:
        contained = False
        for other in chunks:
            if other is c:
                continue
            if (
                other.line_start <= c.line_start
                and other.line_end >= c.line_end
                and (other.line_end - other.line_start) > (c.line_end - c.line_start)
            ):
                contained = True
                break
        if not contained:
            filtered.append(c)

    if not filtered:
        return _line_window_chunk(lines, file_path, "java")

    filtered.sort(key=lambda c: (c.line_start, c.line_end))
    return filtered


def _chunk_markdown(
    content: str, file_path: str, lines: list[str]
) -> list[CodeChunk]:
    """Markdown：按标题章节切块。"""
    chunks: list[CodeChunk] = []
    heading_re = re.compile(r"^(#{1,6})\s+(.+)$")
    section_starts: list[tuple[int, str]] = []

    # 遍历每行提取标题
    for i, line in enumerate(lines):
        m = heading_re.match(line)
        if m:
            section_starts.append((i + 1, m.group(2).strip()))

    # 文件没有标题，降级行窗口
    if not section_starts:
        return _line_window_chunk(lines, file_path, "markdown", chunk_size=80, overlap=10)

    # 根据标题切分章节区间
    for idx, (start, title) in enumerate(section_starts):
        # 下一个标题的上一行作为当前章节结束，最后一节取到文末
        end = (section_starts[idx + 1][0] - 1) if idx + 1 < len(section_starts) else len(lines)
        chunks.append(
            CodeChunk(
                file_path=file_path,
                language="markdown",
                symbol_name=title,
                line_start=start,
                line_end=end,
                content="\n".join(lines[start - 1 : end]),
            )
        )
    return chunks


def _detect_language(file_path: str) -> str:
    """根据扩展名推断语言。"""
    ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
    mapping = {
        "py": "python",
        "java": "java",
        "js": "javascript",
        "ts": "typescript",
        "tsx": "typescript",
        "jsx": "javascript",
        "go": "go",
        "rs": "rust",
        "c": "c",
        "cpp": "cpp",
        "h": "c",
        "hpp": "cpp",
        "md": "markdown",
        "yml": "yaml",
        "yaml": "yaml",
        "json": "json",
        "xml": "xml",
        "sh": "shell",
        "sql": "sql",
    }
    return mapping.get(ext, ext or "text")


# DashScope embedding 模型限制：每个输入最大 8192 token
# 粗略估算 1 token ≈ 3 字符，保守取 24000 字符硬上限
MAX_CHUNK_CHARS = 24000


def chunk_file(content: str, file_path: str) -> list[CodeChunk]:
    """
    把单个文件切成代码片段列表,主切分入口

    :param content: 文件文本内容
    :param file_path: 相对仓库根目录的文件路径
    :return: CodeChunk 列表
    """
    language = _detect_language(file_path)
    lines = content.splitlines()

    if not lines:
        return []

    if language == "python":
        chunks = _chunk_python(content, file_path, lines)
    elif language == "java":
        chunks = _chunk_java(content, file_path, lines)
    elif language == "markdown":
        chunks = _chunk_markdown(content, file_path, lines)
    else:
        # 其他语言：通用行窗口
        chunks = _line_window_chunk(lines, file_path, language)

    # 兜底保护：任何 chunk 内容不能超过 MAX_CHUNK_CHARS，否则 embedding API 报 8192 token 超限
    safe: list[CodeChunk] = []
    for c in chunks:
        if len(c.content) <= MAX_CHUNK_CHARS:
            safe.append(c)
        else:
            # 超大 chunk → 按字符硬切两半，分别产生新 chunk
            half = len(c.content) // 2
            safe.append(CodeChunk(
                file_path=c.file_path, symbol_name=c.symbol_name,
                language=c.language, line_start=c.line_start,
                line_end=c.line_start + half, content=c.content[:half],
            ))
            safe.append(CodeChunk(
                file_path=c.file_path, symbol_name=c.symbol_name,
                language=c.language, line_start=c.line_start + half,
                line_end=c.line_end, content=c.content[half:],
            ))
    return safe
