"""策略文件 Git 版本管理（generic git ops; no quant logic）."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger


class GitManager:
    """使用 GitPython 管理策略目录的版本历史."""

    def __init__(self, repo_dir: Path) -> None:
        self._dir = repo_dir

    def init(self) -> None:
        """初始化 Git 仓库（若已存在则跳过）."""
        import git
        if not (self._dir / ".git").exists():
            repo = git.Repo.init(str(self._dir))
            gitignore = self._dir / ".gitignore"
            gitignore.write_text("__pycache__/\n*.pyc\n")
            repo.index.add([".gitignore"])
            repo.index.commit("chore: init strategy repo")
            logger.info(f"[GitManager] Initialized repo at {self._dir}")

    def _repo(self) -> Any:
        import git
        try:
            return git.Repo(str(self._dir))
        except Exception:
            self.init()
            return git.Repo(str(self._dir))

    def commit(self, filename: str, message: str) -> str:
        """Stage a file and create a commit. Returns commit SHA."""
        repo = self._repo()
        try:
            repo.index.add([filename])
            commit = repo.index.commit(message)
            return commit.hexsha[:8]
        except Exception as e:
            logger.warning(f"[GitManager] commit failed for {filename}: {e}")
            return ""

    def log(self, filename: str, max_count: int = 50) -> list[dict[str, Any]]:
        """获取文件的提交历史."""
        repo = self._repo()
        try:
            commits = list(repo.iter_commits(paths=filename, max_count=max_count))
            return [
                {
                    "sha": c.hexsha[:8],
                    "message": c.message.strip(),
                    "author": c.author.name,
                    "date": c.committed_datetime.isoformat(),
                }
                for c in commits
            ]
        except Exception as e:
            logger.warning(f"[GitManager] log failed for {filename}: {e}")
            return []

    def get_version(self, filename: str, sha: str) -> str:
        """获取指定 commit SHA 时的文件内容."""
        repo = self._repo()
        try:
            commit = repo.commit(sha)
            blob = commit.tree[filename]
            return blob.data_stream.read().decode("utf-8")
        except Exception as e:
            logger.warning(f"[GitManager] get_version failed: {e}")
            return ""
