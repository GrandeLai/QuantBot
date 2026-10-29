"""API Key 密钥管理.

优先使用 OS keyring（macOS Keychain / Windows Credential Manager / Linux Secret Service）。
降级方案：环境变量 + python-dotenv。
"""

from __future__ import annotations

from loguru import logger

_SERVICE_NAME = "quantbot"


def save_api_key(provider: str, api_key: str) -> None:
    """将 API Key 保存到 OS keyring.

    Args:
        provider: 提供商名称（如 'openai', 'binance_api', 'binance_secret'）
        api_key: API Key 明文
    """
    try:
        import keyring
        keyring.set_password(_SERVICE_NAME, provider, api_key)
        logger.info(f"[Keystore] API Key 已保存到 keyring: {provider}")
    except Exception as e:
        logger.warning(f"[Keystore] keyring 不可用，回退到环境变量: {e}")
        _save_to_env(provider, api_key)


def load_api_key(provider: str) -> str | None:
    """从 keyring 或环境变量加载 API Key.

    Args:
        provider: 提供商名称

    Returns:
        API Key 字符串，未找到则返回 None
    """
    # 先尝试 keyring
    try:
        import keyring
        key = keyring.get_password(_SERVICE_NAME, provider)
        if key:
            return key
    except Exception as e:
        logger.debug(f"[Keystore] keyring 读取失败: {e}")

    # 降级：环境变量
    import os
    env_key = f"QUANTBOT_{provider.upper()}"
    value = os.environ.get(env_key)
    if value:
        logger.debug(f"[Keystore] 从环境变量读取: {env_key}")
    return value


def delete_api_key(provider: str) -> bool:
    """从 keyring 删除 API Key."""
    try:
        import keyring
        keyring.delete_password(_SERVICE_NAME, provider)
        logger.info(f"[Keystore] API Key 已删除: {provider}")
        return True
    except Exception as e:
        logger.warning(f"[Keystore] 删除失败: {e}")
        return False


def list_providers() -> list[str]:
    """列出常见 API Key 提供商（预设列表）."""
    return [
        "openai",
        "anthropic",
        "deepseek",
        "binance_api",
        "binance_secret",
        "okx_api",
        "okx_secret",
        "okx_passphrase",
        "futu_trade_env",
        "longbridge_app_key",
        "longbridge_app_secret",
        "longbridge_access_token",
        "tushare_token",
    ]


def _save_to_env(provider: str, api_key: str) -> None:
    """降级方案：将 API Key 追加写入 .env 文件（明文，仅用于开发环境）."""
    import os
    from pathlib import Path

    env_key = f"QUANTBOT_{provider.upper()}"
    env_file = Path(".env")

    # 读取现有内容
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []

    # 替换或追加
    key_found = False
    for i, line in enumerate(lines):
        if line.startswith(f"{env_key}="):
            lines[i] = f"{env_key}={api_key}"
            key_found = True
            break

    if not key_found:
        lines.append(f"{env_key}={api_key}")

    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.warning(f"[Keystore] ⚠️  API Key 以明文写入 .env（仅限开发环境）: {env_key}")
    os.environ[env_key] = api_key
