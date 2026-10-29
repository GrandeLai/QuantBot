"""策略文件本地存储 + git 版本管理整合。

负责策略代码的本地持久化、读取、列举。
加密存储由 quantbot.common.security 模块负责（位于 stock-assistant）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger
from pydantic import BaseModel


class StrategyMeta(BaseModel):
    """策略元数据."""

    id: str
    name: str
    description: str = ""
    version: str = "1.0.0"
    author: str = ""
    created_at: str = ""
    updated_at: str = ""
    tags: list[str] = []
    params: dict[str, object] = {}


class StrategyRecord(BaseModel):
    """策略完整记录（元数据 + 代码）."""

    meta: StrategyMeta
    code: str


class StrategyStorage:
    """策略文件本地存储（明文）."""

    def __init__(self, strategy_dir: Path) -> None:
        self._dir = strategy_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def _strategy_path(self, strategy_id: str) -> Path:
        return self._dir / f"{strategy_id}.json"

    def save(self, record: StrategyRecord) -> None:
        """保存策略到本地文件."""
        now = datetime.now(tz=UTC).isoformat()
        if not record.meta.created_at:
            record.meta.created_at = now
        record.meta.updated_at = now

        path = self._strategy_path(record.meta.id)
        path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        logger.info(f"[StrategyStorage] 已保存策略: {record.meta.id}")

        # Write code to .py file and git commit
        code_path = self._dir / f"{record.meta.id}.py"
        code_path.write_text(record.code, encoding="utf-8")
        try:
            from quantbot.common.strategy_persistence.git_manager import GitManager
            gm = GitManager(self._dir)
            gm.commit(f"{record.meta.id}.py", f"feat: save strategy '{record.meta.name}'")
        except Exception as e:
            logger.warning(f"[StrategyStorage] git commit failed: {e}")

    def load(self, strategy_id: str) -> StrategyRecord | None:
        """加载策略."""
        path = self._strategy_path(strategy_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return StrategyRecord.model_validate(data)

    def delete(self, strategy_id: str) -> bool:
        """删除策略文件."""
        path = self._strategy_path(strategy_id)
        if path.exists():
            path.unlink()
            logger.info(f"[StrategyStorage] 已删除策略: {strategy_id}")
            return True
        return False

    def list_all(self) -> list[StrategyMeta]:
        """列出所有策略的元数据."""
        metas: list[StrategyMeta] = []
        for path in sorted(self._dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                record = StrategyRecord.model_validate(data)
                metas.append(record.meta)
            except Exception as e:
                logger.warning(f"[StrategyStorage] 读取 {path.name} 失败: {e}")
        return metas

    def exists(self, strategy_id: str) -> bool:
        return self._strategy_path(strategy_id).exists()
