"""前端契约回归测试.

覆盖 2026-04 前端增强计划中的关键闭环：
- /api 路由别名在开发环境可用
- 用户策略进入可运行策略目录
- 本地模拟盘接口已下线，前端应改走回测验证与交易执行链路
"""

from pathlib import Path

from fastapi.testclient import TestClient

from quantbot.common.strategy_persistence import StrategyMeta, StrategyRecord, StrategyStorage


def test_api_aliases_are_available(client: TestClient) -> None:
    """前端统一使用 /api 前缀时，关键 stock 接口应可访问."""
    assert client.get("/api/data/symbols").status_code == 200
    assert client.get("/api/crypto/status").status_code == 200


def test_available_strategies_include_user_strategies(
    client: TestClient,
    monkeypatch,
    tmp_path: Path,
) -> None:
    """可运行策略目录应包含 stock 本地用户策略.

    Stock-assistant 仅返回用户策略；模板策略由 Rust quant-assistant 提供。
    """
    monkeypatch.setenv("QUANTBOT_STRATEGY_DIR", str(tmp_path))

    storage = StrategyStorage(tmp_path)
    storage.save(
        StrategyRecord(
            meta=StrategyMeta(
                id="user001",
                name="My User Strategy",
                description="user-owned strategy",
                params={"lookback": 21},
            ),
            code=(
                "from quantbot.common.contracts import BaseStrategy\n"
                "class MyUserStrategy(BaseStrategy):\n"
                "    name = 'My User Strategy'\n"
                "    description = 'user-owned strategy'\n"
                "    default_params = {'lookback': 21}\n"
                "    def on_bar(self, bar, context):\n"
                "        return None\n"
            ),
        )
    )

    response = client.get("/api/portfolio/available-strategies")
    assert response.status_code == 200
    body = response.json()

    strategies = body["strategies"]
    assert any(item["id"] == "user001" and item["source"] == "user" for item in strategies)


def test_local_paper_api_is_not_exposed(client: TestClient) -> None:
    """本地模拟盘接口不应继续暴露给前端."""
    response = client.get("/api/paper/sessions")

    assert response.status_code == 404
