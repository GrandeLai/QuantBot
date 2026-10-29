"""QuantBot 配置管理模块."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用全局配置，从环境变量或 .env 文件读取."""

    model_config = SettingsConfigDict(
        env_file=None,
        env_prefix="QUANTBOT_",
        case_sensitive=False,
    )

    # 应用基础配置
    app_name: str = "QuantBot"
    app_version: str = "0.3.0"
    debug: bool = False

    # 数据库配置
    duckdb_path: Path = Path("data/quantbot.duckdb")
    sqlite_path: Path = Path("data/quantbot.sqlite")

    # Redis 配置
    redis_url: str = "redis://localhost:6379"

    # API 服务配置
    host: str = "127.0.0.1"
    port: int = 8001

    # 交易 provider 配置
    trading_provider: str = "auto"
    trading_mock_force_session: str = "regular"
    trading_risk_enabled: bool = True
    trading_risk_max_position_count: int = 10
    trading_risk_max_single_position_pct: float = 0.30
    trading_risk_daily_loss_limit_pct: float = 0.05
    trading_risk_max_order_value: float = 0.0
    futu_host: str = ""
    futu_port: int = 0
    futu_market: str = "US"
    futu_unlock_password: str = ""
    longbridge_app_key: str = ""
    longbridge_app_secret: str = ""
    longbridge_access_token: str = ""
    longbridge_region: str = ""

    # 数据目录
    data_dir: Path = Path("data")
    strategy_dir: Path = Path("data/strategies")

    # LLM 配置
    llm_default_model: str = Field(default="", description="默认 LLM 模型")
    openai_api_key: str = Field(default="", description="OpenAI API Key")
    anthropic_api_key: str = Field(default="", description="Anthropic API Key")

    # 日志配置
    log_level: str = "INFO"
    log_dir: Path = Path("logs")

    # OKX 加密货币交易配置
    okx_api_key: str = Field(default="", description="OKX API Key")
    okx_api_secret: str = Field(default="", description="OKX API Secret")
    okx_passphrase: str = Field(default="", description="OKX API Passphrase（创建 API Key 时设定）")
    okx_demo: bool = Field(default=True, description="使用 OKX demo sandbox（True = x-simulated-trading:1）")

    # ML 模型超参数
    ml_lgbm_n_estimators: int = Field(default=50, description="LightGBM 树的数量")
    ml_lgbm_num_leaves: int = Field(default=15, description="LightGBM 每棵树的最大叶子数")
    ml_lgbm_learning_rate: float = Field(default=0.1, description="LightGBM 学习率")
    ml_catboost_iterations: int = Field(default=100, description="CatBoost 迭代次数")
    ml_catboost_depth: int = Field(default=4, description="CatBoost 树深度")
    ml_catboost_learning_rate: float = Field(default=0.1, description="CatBoost 学习率")
    ml_feature_corr_threshold: float = Field(default=0.85, description="特征相关性去冗余阈值")

    # 信号过滤配置
    signal_filter_window: int = Field(default=20, description="滚动分位数过滤器回看窗口大小")
    signal_filter_quantile: float = Field(default=0.75, description="滚动分位数阈值（0~1）")
    signal_filter_min_periods: int = Field(default=5, description="过滤器热身期最小样本数")


def get_settings() -> Settings:
    """获取全局配置单例."""
    return Settings()
