"""运行文件按模式和匿名账户分区，配置本身不承担运行期状态。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimePaths:
    state_file: Path
    journal_file: Path
    cache_dir: Path

    @property
    def logs_dir(self) -> Path:
        return self.journal_file.parent

    @property
    def broker_state_file(self) -> Path:
        return self.state_file.parent / "sim_broker_state.json"


def local_account_fingerprint(cfg: dict, mode: str) -> str:
    """本地账户身份由配置的状态根位置决定，不依赖派生目录。"""
    if mode == "dry-run":
        identity = "quantbot.price_action:dry-run:v1"
    elif mode == "sim":
        identity = f"quantbot.price_action:sim:{Path(cfg['live']['state_file']).resolve()}"
    else:
        raise ValueError("券商账户必须使用券商提供的匿名指纹")
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def runtime_paths(cfg: dict, mode: str, account_fingerprint: str) -> RuntimePaths:
    if mode not in {"dry-run", "sim", "paper", "live"}:
        raise ValueError(f"未知运行模式：{mode}")
    if len(account_fingerprint) != 64 or any(
        char not in "0123456789abcdef" for char in account_fingerprint
    ):
        raise ValueError("运行目录必须绑定完整匿名账户 SHA-256")
    state = Path(cfg["live"]["state_file"])
    journal = Path((cfg.get("review", {}) or {}).get(
        "journal_path", "logs/trading_journal.sqlite"
    ))
    partition = Path("runtime") / mode / account_fingerprint
    return RuntimePaths(
        state_file=state.parent / partition / state.name,
        journal_file=journal.parent / partition / journal.name,
        cache_dir=Path(cfg.get("live", {}).get("cache_dir", "data_cache_live")) / partition,
    )


def reject_legacy_state(cfg: dict, mode: str) -> None:
    """真实副作用模式不能把旧的未分区状态当成空账户自动迁移。"""
    if mode == "dry-run":
        return
    legacy = [Path(cfg["live"]["state_file"]), Path(
        (cfg.get("review", {}) or {}).get("journal_path", "logs/trading_journal.sqlite")
    )]
    if mode == "sim":
        legacy.append(Path("sim_broker_state.json"))
    existing = [str(path) for path in legacy if path.exists() or path.is_symlink()]
    if existing:
        raise RuntimeError("存在未分区旧状态，必须先人工核对和迁移，禁止按空账户启动："
                           + ", ".join(existing))


def read_runtime_paths(
    cfg: dict, mode: str, account_fingerprint: str | None = None,
) -> RuntimePaths:
    """离线选择报告账户；多个账户时要求显式指定，禁止按最近目录猜测。"""
    if account_fingerprint is not None:
        return runtime_paths(cfg, mode, account_fingerprint)
    if mode in {"sim", "dry-run"}:
        return runtime_paths(cfg, mode, local_account_fingerprint(cfg, mode))
    journal = Path((cfg.get("review", {}) or {}).get(
        "journal_path", "logs/trading_journal.sqlite"
    ))
    accounts = sorted(path.parent.name for path in
                      (journal.parent / "runtime" / mode).glob(f"*/{journal.name}"))
    if len(accounts) != 1:
        raise ValueError("无法唯一选择账本账户，请传 --account-fingerprint")
    return runtime_paths(cfg, mode, accounts[0])
