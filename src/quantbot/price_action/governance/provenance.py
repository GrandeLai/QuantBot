"""代码版本与源码指纹。"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class SourceFingerprint:
    """一次运行可持久化的代码身份。"""

    code_version: str
    source_hash: str
    git_head: str | None
    dirty: bool
    file_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "code_version": self.code_version,
            "source_hash": self.source_hash,
            "git_head": self.git_head,
            "dirty": self.dirty,
            "file_count": self.file_count,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SourceFingerprint":
        return cls(
            code_version=str(value["code_version"]),
            source_hash=str(value["source_hash"]),
            git_head=value.get("git_head"),
            dirty=bool(value["dirty"]),
            file_count=int(value["file_count"]),
        )


def _default_source_files(root: Path) -> list[Path]:
    package = root / "src" / "quantbot"
    if not package.is_dir():
        package = root / "quantbot.price_action"
    if package.is_dir():
        linked = next((path for path in package.rglob("*") if path.is_symlink()), None)
        if linked is not None:
            raise ValueError(f"源码集合不得包含软链接：{linked}")
        files = list(package.rglob("*.py"))
        files.extend(path for path in (
            root / "main.py",
            root / "requirements.txt",
            root / "pyproject.toml",
            root / "uv.lock",
        ) if path.is_file())
    else:
        files = list(root.rglob("*.py"))
    return sorted(
        (path for path in files if path.is_file() and "__pycache__" not in path.parts),
        key=lambda path: path.relative_to(root).as_posix(),
    )


def _hash_files(root: Path, files: Iterable[Path]) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    for path in files:
        if path.is_symlink():
            raise ValueError(f"源码集合不得包含软链接：{path}")
        if not path.is_file():
            raise ValueError(f"源码集合包含非普通文件：{path}")
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        # 长度前缀消除 ``(path, content)`` 拼接可能产生的边界歧义。
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
        count += 1
    return digest.hexdigest(), count


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args], check=False, capture_output=True,
            text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")


def source_fingerprint(
    root: str | Path,
    *,
    files: Iterable[str | Path] | None = None,
) -> SourceFingerprint:
    """计算源码哈希并解析 Git 身份。

    无仓库或仓库还没有 ``HEAD`` 时，绝不伪装成某个发布版本，而是明确
    返回 ``uncommitted+<完整源码哈希>``。哈希不依赖文件枚举顺序。
    """
    root_path = Path(root).resolve()
    selected = _default_source_files(root_path) if files is None else sorted(
        (Path(path) if Path(path).is_absolute() else root_path / path for path in files),
        key=lambda path: path.relative_to(root_path).as_posix(),
    )
    source_hash, file_count = _hash_files(root_path, selected)

    head_result = _git(root_path, "rev-parse", "--verify", "HEAD")
    git_head = head_result.stdout.strip() if head_result.returncode == 0 else None
    if not git_head:
        return SourceFingerprint(
            code_version=f"uncommitted+{source_hash}",
            source_hash=source_hash,
            git_head=None,
            dirty=True,
            file_count=file_count,
        )

    status = _git(root_path, "status", "--porcelain", "--untracked-files=all")
    tracked_result = _git(root_path, "ls-files", "-z")
    tracked = set(tracked_result.stdout.split("\0")) if tracked_result.returncode == 0 \
        else set()
    selected_relative = {
        path.relative_to(root_path).as_posix() for path in selected
    }
    # 普通 git status 不展示被 .gitignore/.git/info/exclude 隐藏的运行时代码。
    # 源码哈希里的每个文件都必须真实存在于 HEAD，否则不能称为干净版本。
    untracked_runtime_sources = selected_relative - tracked
    dirty = (
        status.returncode != 0
        or bool(status.stdout.strip())
        or tracked_result.returncode != 0
        or bool(untracked_runtime_sources)
    )
    code_version = git_head if not dirty else f"{git_head}+dirty.{source_hash}"
    return SourceFingerprint(
        code_version=code_version,
        source_hash=source_hash,
        git_head=git_head,
        dirty=dirty,
        file_count=file_count,
    )
