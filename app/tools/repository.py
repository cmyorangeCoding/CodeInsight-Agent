from pathlib import Path

class RepositoryAnalyzer:
    # 需要忽略扫描的目录集合
    IGNORE_DIRS = {
        ".git",
        ".idea",
        ".venv",
        "__pycache__",
        "node_modules",
        "target",
        "build",
        "dist",
        ".claims",        # langchain openwiki 里的声明数据目录（minified JSON，不是源码）
        ".cache",
        ".data",
        ".github/workflows/__pycache__",
    }
    # 需要参与检索的代码/配置文件后缀集合
    # 注意：.json 不再索引（大量 minified 数据文件单行超长，embedding API 限 8192 token）
    CODE_EXTENSIONS = {
        ".java",
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".vue",
        ".xml",
        ".yml",
        ".yaml",
        ".md",
    }

    def __init__(self, repo_path: str):
        # 将传入路径转为绝对路径
        self.repo_path = Path(repo_path).resolve()

        # 路径不存在 → 抛出文件不存在异常
        if not self.repo_path.exists():
            raise FileNotFoundError(
                f"Repository not found: {self.repo_path}"
            )
        # 路径不是文件夹 → 抛出不是目录异常
        if not self.repo_path.is_dir():
            raise NotADirectoryError(
                f"Not a directory: {self.repo_path}"
            )




    def _should_ignore(self, path: Path) -> bool:
        """判断当前路径是否属于需要忽略的目录"""
        return any(
            part in self.IGNORE_DIRS
            for part in path.parts
        )




    def get_repo_tree(self, max_depth: int = 3) -> str:
        """
        获取项目目录结构树字符串
        :param max_depth: 最大递归扫描深度
        :return: 带文件夹/文件图标的目录树文本
        """
        # 收集目录树每一行文本
        lines = []

        def walk(current_path: Path, depth: int):
            if depth > max_depth:
                return
            try:
                # 获取当前目录下所有子项，文件夹排在文件前面，名称不区分大小写排序
                children = sorted(
                    current_path.iterdir(),
                    key=lambda x: (x.is_file(), x.name.lower())
                )
            except PermissionError:
                # 权限不足时直接跳过该目录
                return

            for child in children:
                # 忽略黑名单目录
                if child.name in self.IGNORE_DIRS:
                    continue
                # 根据深度生成缩进空格
                indent = "  " * depth
                if child.is_dir():
                    lines.append(
                        f"{indent}📁 {child.name}/"
                    )
                    walk(child, depth + 1)
                else:
                    lines.append(
                        f"{indent}📄 {child.name}"
                    )

        # 根节点只作展示用途：显式声明下面的路径都相对于仓库根，
        # 避免模型把仓库文件夹名当成 read_file 的路径前缀
        lines.append(
            f"📁 {self.repo_path.name}/  (仓库根目录；以下路径均相对于它，"
            f"read_file 时不要加此前缀)"
        )
        walk(self.repo_path, 1)
        return "\n".join(lines)




    def index_files(self) -> list[dict]:
        """
        建立仓库文件索引
        返回列表，每个字典包含：
        - path: 相对于仓库根目录的文件路径
        - extension: 文件后缀（小写）
        - size: 文件字节大小
        """
        results = []
        # 递归遍历仓库下所有文件
        for file_path in self.repo_path.rglob("*"):
            # 只处理文件，跳过文件夹
            if not file_path.is_file():
                continue
            # 判断是否在忽略目录中
            if self._should_ignore(file_path):
                continue
            # 过滤不在允许后缀列表中的文件
            if file_path.suffix.lower() not in self.CODE_EXTENSIONS:
                continue
            try:
                # 获取文件字节大小
                size = file_path.stat().st_size
            except OSError:
                # 获取文件信息失败，跳过当前文件
                continue

            results.append({
                "path": str(
                    file_path.relative_to(self.repo_path)
                ),
                "extension": file_path.suffix.lower(),
                "size": size,
            })
        return results




    def read_file(
        self,
        relative_path: str,
        max_chars: int = 30000
    ) -> str:
        """
        安全读取仓库内文件内容
        :param relative_path: 相对于仓库根目录的文件路径
        :param max_chars: 最大读取字符上限，超出会截断
        :return: 文件文本；二进制/非utf8文件返回标记字符串
        """
        # 拼接并解析为绝对路径
        file_path = (
            self.repo_path / relative_path
        ).resolve()

        # 防止通过../../越界读取仓库外文件
        if self.repo_path not in file_path.parents:
            raise ValueError(
                "File path is outside repository."
            )
        # 文件不存在异常
        if not file_path.exists():
            raise FileNotFoundError(
                f"File not found: {relative_path}"
            )
        # 路径不是文件异常
        if not file_path.is_file():
            raise ValueError(
                f"Not a file: {relative_path}"
            )
        # 禁止读取黑名单目录内文件
        if self._should_ignore(file_path):
            raise ValueError(
                f"File is ignored: {relative_path}"
            )

        try:
            content = file_path.read_text(
                encoding="utf-8"
            )
        except UnicodeDecodeError:
            return "[Binary or non-UTF-8 file]"

        # 文本长度超过上限，截断并追加提示
        if len(content) > max_chars:
            return (
                content[:max_chars]
                + "\n\n[Content truncated]"
            )
        return content




    def search_code(
        self,
        keyword: str,
        extensions: set[str] | None = None,
        max_results: int = 20,
        context_lines: int = 3,
    ) -> list[dict]:
        """
        在项目代码中搜索关键词（大小写不敏感）
        :param keyword: 要搜索的关键词
        :param extensions: 需要检索的文件后缀集合，不传则使用默认CODE_EXTENSIONS
        :param max_results: 最大返回匹配结果数量，默认20条
        :param context_lines: 匹配行前后保留的上下文行数
        :return: 结果列表，每个字典包含文件路径、匹配行号、匹配行原文、上下文片段
        """
        # 未指定后缀时使用全局默认后缀集合
        if extensions is None:
            extensions = self.CODE_EXTENSIONS

        results = []
        # 递归遍历仓库所有文件
        for file_path in self.repo_path.rglob("*"):
            # 跳过文件夹
            if not file_path.is_file():
                continue
            # 跳过忽略目录内文件
            if self._should_ignore(file_path):
                continue
            # 过滤不在允许后缀列表的文件
            if file_path.suffix.lower() not in extensions:
                continue

            try:
                # utf8读取文本，按换行切分成行列表
                lines = file_path.read_text(
                    encoding="utf-8"
                ).splitlines()
            except (UnicodeDecodeError, PermissionError):
                continue

            # line_number从1开始计数
            for line_number, line in enumerate(
                lines,
                start=1
            ):
                # 大小写不敏感匹配关键词
                if keyword.lower() not in line.lower():
                    continue

                # 计算上下文起始、结束行号
                start = max(
                    0,
                    line_number - 1 - context_lines
                )
                end = min(
                    len(lines),
                    line_number + context_lines
                )
                # 截取上下文文本片段
                snippet = "\n".join(
                    lines[start:end]
                )

                results.append({
                    "file": str(
                        file_path.relative_to(
                            self.repo_path
                        )
                    ),
                    "line": line_number,
                    "content": line.strip(),
                    "snippet": snippet,
                })
                # 达到最大结果数量直接返回
                if len(results) >= max_results:
                    return results
        return results
