"""PABot 命令行入口。

用法：
    python main.py download            # 下载 / 更新历史行情缓存
    python main.py snapshot            # 从当前缓存创建只读数据快照（实验可复现）
    python main.py backtest            # 运行 2015-2025 回测并生成报告
    python main.py walkforward         # Walk-forward 样本外验证（逐年折，约 30 分钟）
    python main.py paper               # 模拟盘执行一次每日决策（正式取证传 campaign）
    python main.py live                # 实盘执行一次每日决策（需二次确认）
    python main.py dry-run             # 演习模式：完整跑一遍实盘流程但不下单
    python main.py sim                 # 本地模拟盘：无需密钥，真实记账持仓与权益
    python main.py reconcile           # 影子对账：回测理论持仓 vs 实际持仓
    python main.py report              # 模拟盘绩效报告：实际 vs 影子回测 + 实测滑点
    python main.py review              # 从只追加账本生成最近交易日中文复盘
    python main.py policy-check        # 检查生产价格行为政策与源码身份
    python main.py experiment --spec hypothesis.json  # 执行预注册单变量实验
    python main.py candidate-bundle --experiment-artifact experiment.json
    python main.py candidate-verify --bundle candidate-dir
    python main.py paper-campaign-init --trading-dates dates.json ...
    python main.py paper-audit --campaign campaign.json
    python main.py paper-close --campaign campaign.json

可选参数：
    --config PATH   指定配置文件（默认 ./config.yaml）
    --force         download: 强制重新下载（忽略缓存）；
                    paper/live/dry-run: 跳过交易日与当日幂等性检查（人工补跑）；
                    永远不能绕过启动订单恢复门禁
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from quantbot.price_action.config import load_config
from quantbot.price_action.data.downloader import load_market_data


def _write_text_exclusive(target: str | Path, payload: str) -> Path:
    """原子写入 UTF-8 文本；目标已存在时拒绝静默覆盖证据。"""
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
    return path


def cmd_download(cfg: dict, force: bool) -> None:
    """下载全部股票池历史数据到本地缓存。"""
    load_market_data(cfg, force=force)


def cmd_backtest(cfg: dict) -> None:
    """运行组合回测并输出报告。"""
    from quantbot.price_action.backtest.engine import BacktestEngine
    from quantbot.price_action.backtest.metrics import save_report

    data = load_market_data(cfg)
    engine = BacktestEngine(data, cfg)
    result = engine.run()
    save_report(result)


def cmd_walkforward(cfg: dict) -> None:
    """运行 walk-forward 样本外验证，报告输出到 reports/walkforward/。"""
    from pathlib import Path

    from quantbot.price_action.backtest.walkforward import format_report, run_walkforward

    data = load_market_data(cfg)
    results, oos_equity = run_walkforward(data, cfg)
    report = format_report(results, oos_equity)

    out = Path("reports/walkforward")
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.txt").write_text(report, encoding="utf-8")
    oos_equity.to_csv(out / "oos_equity.csv", encoding="utf-8")
    print(report)
    print(f"\n[wf] 报告已保存至 {out.resolve()}/")


def cmd_trade(
    cfg: dict,
    mode: str,
    force: bool = False,
    *,
    confirm_live: bool = False,
    campaign_path: str | None = None,
) -> None:
    """执行一次每日交易决策。

    mode:  "paper" 模拟盘 / "live" 实盘 / "dry-run" 演习（不下单）
    force: 跳过交易日与当日幂等性检查（人工补跑用），不绕过恢复门禁
    """
    from quantbot.price_action.live.notify import notify
    from quantbot.price_action.live.runner import LiveRunner
    from quantbot.price_action.review import PaperCampaignSpec

    campaign = None
    if campaign_path is not None:
        if mode != "paper":
            raise ValueError("--campaign 只能用于 paper 命令")
        if force:
            raise ValueError("正式 Paper campaign 禁止使用 --force")
        campaign = PaperCampaignSpec.from_json(
            Path(campaign_path).read_text(encoding="utf-8")
        )

    if mode in {"paper", "live"}:
        configured_paper = (cfg.get("broker", {}) or {}).get("paper")
        expected_paper = mode == "paper"
        if configured_paper is not expected_paper:
            expected = "true" if expected_paper else "false"
            raise RuntimeError(
                f"命令 {mode} 与 broker.paper 不一致；请先明确设置 broker.paper: {expected}"
            )
        if mode == "live":
            raise RuntimeError("QuantBot 当前只开放模拟执行；旧价格行为入口不能绕过新策略的实盘验收")

    if mode == "dry-run":
        from quantbot.price_action.broker.base import DryRunBroker
        broker = DryRunBroker()
    elif mode == "sim":
        # 本地模拟盘：无需任何密钥，完整跑通每日流程并真实记账
        from quantbot.price_action.broker.sim_broker import SimBroker
        broker = SimBroker(cfg)
    else:
        from quantbot.price_action.broker.alpaca_broker import AlpacaBroker

        paper = mode == "paper"
        if not paper and not confirm_live:
            raise RuntimeError(
                "实盘必须同时满足 broker.paper: false 并显式传 --confirm-live；"
                "命令不会在 cron 中等待交互输入"
            )
        broker = AlpacaBroker(paper=paper)

    try:
        LiveRunner(
            broker,
            cfg,
            mode=mode,
            campaign=campaign,
        ).run_once(force=force)
    except Exception as e:
        # 任何异常都推送告警后再抛出（cron 场景下否则无人知晓）
        notify(f"⚠️ PABot 运行异常（{mode}）: {type(e).__name__}: {e}")
        raise


def cmd_review(
    cfg: dict, trading_date: str | None, output: str | None, *, journal_path=None,
) -> None:
    """从 SQLite 事实账本生成指定（或最近）交易日复盘。"""
    from pathlib import Path

    from quantbot.price_action.review import ReviewJournal, write_daily_review

    journal_path = Path(journal_path or
        (cfg.get("review", {}) or {}).get(
            "journal_path", "logs/trading_journal.sqlite"
        )
    )
    if not journal_path.exists():
        raise FileNotFoundError(f"复盘账本不存在：{journal_path}")
    with ReviewJournal(journal_path, readonly=True) as journal:
        day = trading_date or journal.latest_trading_date()
        if day is None:
            raise RuntimeError("复盘账本为空，尚无可生成报告的交易日")
        output_path = Path(output) if output else journal_path.parent / "review" / f"{day}.md"
        path = write_daily_review(journal, day, output_path)
    print(f"[review] 已生成 {day} 价格行为复盘：{path.resolve()}")


def cmd_policy_check(cfg: dict) -> None:
    """输出生产策略政策、配置与源码身份。"""
    from pathlib import Path

    from quantbot.price_action.config import config_hash
    from quantbot.price_action.governance import source_fingerprint, validate_production_config

    validate_production_config(cfg)
    source = source_fingerprint(Path(__file__).resolve().parents[3])
    print("[policy] 生产价格行为政策：通过")
    print(f"[policy] 配置 SHA-256：{config_hash(cfg)}")
    print(f"[policy] 源码版本：{source.code_version}")
    print(f"[policy] 源码 SHA-256：{source.source_hash}")
    print(f"[policy] Git HEAD：{source.git_head or '无（尚无版本基线）'}")
    print(f"[policy] 工作区状态：{'有未提交变更' if source.dirty else '干净'}")


def cmd_experiment(cfg: dict, spec_path: str | None, output: str | None) -> None:
    """执行一项预注册的单变量价格行为实验；不改生产配置。"""
    import copy
    from pathlib import Path

    from quantbot.price_action.experiment import (
        run_registered_experiment,
        verify_config_snapshot,
        write_experiment_artifact,
    )
    from quantbot.price_action.governance import HypothesisSpec

    if not spec_path:
        raise ValueError("experiment 命令必须传 --spec hypothesis.json")
    hypothesis = HypothesisSpec.from_json(
        Path(spec_path).read_text(encoding="utf-8")
    )

    # 必须先全量校验，再加载任何实验数据。load_market_data 会改写传入配置
    # 的实际 cache_dir，因此只给它配置副本，保持预注册哈希不变。
    verification = verify_config_snapshot(
        cfg,
        hypothesis.data_snapshot,
        project_root=Path(__file__).resolve().parents[3],
    )
    data = load_market_data(copy.deepcopy(cfg))
    artifact = run_registered_experiment(
        hypothesis,
        cfg,
        data,
        project_root=Path(__file__).resolve().parents[3],
        snapshot_verifier=lambda _cfg, _snapshot: verification,
    )
    out_path = Path(output) if output else (
        Path((cfg.get("governance", {}) or {}).get(
            "artifact_dir", "reports/governance"
        ))
        / "experiments"
        / f"{artifact.experiment_id}.json"
    )
    write_experiment_artifact(artifact, out_path)
    print(f"[experiment] 已生成不可覆盖实验产物：{out_path.resolve()}")
    print("[experiment] 后续 Walk-forward、压力、Shadow、Paper 门禁默认仍为未通过")


def cmd_candidate_bundle(
    cfg: dict,
    experiment_artifact: str | None,
    output_root: str | None,
):
    """把实验与精确基线冻结成不可覆盖候选包；不应用配置。"""
    from quantbot.price_action.governance import create_candidate_bundle

    if not experiment_artifact:
        raise ValueError("candidate-bundle 必须传 --experiment-artifact")
    root = Path(__file__).resolve().parents[3]
    destination = Path(output_root) if output_root else (
        Path((cfg.get("governance", {}) or {}).get(
            "artifact_dir", "reports/governance"
        ))
        / "candidates"
    )
    result = create_candidate_bundle(
        cfg,
        experiment_artifact,
        destination,
        project_root=root,
    )
    print(f"[candidate] 已生成不可覆盖候选包：{result.path.resolve()}")
    if result.eligible:
        print("[candidate] 源码/配置/快照身份闭环，可继续补齐前瞻发布证据")
    else:
        print(
            "[candidate] 仅可审计、不可晋级："
            + "、".join(result.ineligibility_reasons)
        )
    print("[candidate] 未修改 config.yaml，候选不会自动生效")
    return result


def cmd_candidate_verify(bundle_path: str | None):
    """只读重算候选包的文件、配置、源码和快照闭环。"""
    from quantbot.price_action.governance import verify_candidate_bundle

    if not bundle_path:
        raise ValueError("candidate-verify 必须传 --bundle")
    result = verify_candidate_bundle(
        bundle_path,
        project_root=Path(__file__).resolve().parents[3],
    )
    print(f"[candidate] 候选包完整性校验通过：{result.path.resolve()}")
    print(f"[candidate] bundle SHA-256：{result.bundle_sha256}")
    if not result.eligible:
        print(
            "[candidate] 完整但不可晋级："
            + "、".join(result.ineligibility_reasons)
        )
    return result


def cmd_paper_campaign_init(
    cfg: dict,
    *,
    campaign_id: str | None,
    account_fingerprint: str | None,
    trading_dates_path: str | None,
    output: str | None,
):
    """冻结 Paper 观察身份和预期交易日；不启动交易。"""
    import json

    from quantbot.price_action.config import config_hash
    from quantbot.price_action.governance import source_fingerprint
    from quantbot.price_action.review import PaperCampaignSpec

    required = {
        "--campaign-id": campaign_id,
        "--account-fingerprint": account_fingerprint,
        "--trading-dates": trading_dates_path,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ValueError("paper-campaign-init 缺少参数：" + ", ".join(missing))
    if (cfg.get("broker", {}) or {}).get("paper") is not True:
        raise ValueError("Paper campaign 只能绑定 broker.paper: true 的配置")
    assert campaign_id and account_fingerprint and trading_dates_path
    raw_sessions = json.loads(Path(trading_dates_path).read_text(encoding="utf-8"))
    if not isinstance(raw_sessions, list) or not raw_sessions:
        raise ValueError("--trading-dates 必须是非空 JSON 会话数组")
    if not all(
        isinstance(item, dict)
        and set(item) == {"execution_date", "signal_date"}
        and isinstance(item["execution_date"], str)
        and isinstance(item["signal_date"], str)
        for item in raw_sessions
    ):
        raise ValueError(
            "--trading-dates 每项必须且只能包含 execution_date 与 signal_date"
        )
    execution_dates = tuple(item["execution_date"] for item in raw_sessions)
    signal_dates = tuple(item["signal_date"] for item in raw_sessions)
    minimum = int(
        (cfg.get("review", {}) or {}).get("paper_min_trading_days", 60)
    )
    if len(execution_dates) < minimum:
        raise ValueError(
            f"冻结交易日只有 {len(execution_dates)} 个，少于门禁要求 {minimum} 个"
        )
    source = source_fingerprint(Path(__file__).resolve().parents[3])
    campaign = PaperCampaignSpec(
        campaign_id=campaign_id,
        candidate_config_hash=config_hash(cfg),
        source_hash=source.source_hash,
        account_fingerprint=account_fingerprint,
        expected_trading_dates=execution_dates,
        expected_signal_dates=signal_dates,
        minimum_trading_days=minimum,
    )
    target = Path(output) if output else (
        Path((cfg.get("governance", {}) or {}).get(
            "artifact_dir", "reports/governance"
        ))
        / "paper"
        / f"{campaign_id}.json"
    )
    _write_text_exclusive(target, campaign.to_json(indent=2) + "\n")
    print(f"[paper] 已冻结 Paper campaign：{target.resolve()}")
    print(f"[paper] 预期交易日 {len(campaign.expected_trading_dates)} 个；未启动交易")
    return campaign


def cmd_paper_account_fingerprint(cfg: dict) -> str:
    """只读连接 Alpaca Paper，并仅输出匿名账户 SHA-256。"""
    from quantbot.price_action.broker.alpaca_broker import AlpacaBroker

    if (cfg.get("broker", {}) or {}).get("paper") is not True:
        raise ValueError("paper-account-fingerprint 要求 broker.paper: true")
    fingerprint = AlpacaBroker(paper=True).account_fingerprint()
    print(f"[paper] 匿名账户 SHA-256：{fingerprint}")
    print("[paper] 未输出或保存 Alpaca 账号原文")
    return fingerprint


def cmd_paper_audit(
    cfg: dict,
    campaign_path: str | None,
    output: str | None,
):
    """从只追加账本审计完整 Paper campaign 并生成机器证据。"""
    from quantbot.price_action.review import PaperCampaignSpec, ReviewJournal, audit_paper_campaign
    from quantbot.price_action.live.paths import runtime_paths

    if not campaign_path:
        raise ValueError("paper-audit 必须传 --campaign")
    campaign = PaperCampaignSpec.from_json(
        Path(campaign_path).read_text(encoding="utf-8")
    )
    journal_path = runtime_paths(cfg, "paper", campaign.account_fingerprint).journal_file
    if not journal_path.exists():
        raise FileNotFoundError(f"复盘账本不存在：{journal_path}")
    with ReviewJournal(journal_path, readonly=True) as journal:
        records = {
            trading_day: journal.records_for_date(trading_day)
            for trading_day in campaign.expected_trading_dates
        }
    summary = audit_paper_campaign(campaign, records)
    target = Path(output) if output else (
        Path((cfg.get("governance", {}) or {}).get(
            "artifact_dir", "reports/governance"
        ))
        / "paper"
        / f"{campaign.campaign_id}-{summary.evidence_sha256[:12]}.json"
    )
    _write_text_exclusive(target, summary.to_json(indent=2) + "\n")
    print(f"[paper] 已生成不可覆盖审计产物：{target.resolve()}")
    print(
        "[paper] 连续合格交易日："
        f"{summary.consecutive_eligible_trading_days}/"
        f"{summary.minimum_trading_days}；"
        f"缺失 {len(summary.missing_dates)} 日，失败 {len(summary.failed_dates)} 日"
    )
    return summary


def cmd_paper_close(cfg: dict, campaign_path: str | None, *, force: bool = False):
    """当天真实收盘后只读采集账户、持仓和在途订单，禁止补填日期。"""
    from quantbot.price_action.broker.alpaca_broker import AlpacaBroker
    from quantbot.price_action.live.closing import collect_paper_close
    from quantbot.price_action.review import PaperCampaignSpec

    if not campaign_path or force:
        raise ValueError("paper-close 必须传 --campaign，且禁止 --force")
    if (cfg.get("broker", {}) or {}).get("paper") is not True:
        raise ValueError("paper-close 要求 broker.paper: true")
    campaign = PaperCampaignSpec.from_json(Path(campaign_path).read_text(encoding="utf-8"))
    result = collect_paper_close(AlpacaBroker(paper=True), cfg, campaign)
    status = "合格" if result.eligible else ", ".join(result.reason_codes)
    print(f"[paper-close] {result.execution_date}：{status}")
    return result


def cmd_hypothesis_init(
    cfg: dict,
    *,
    change: str | None,
    hypothesis_id: str | None,
    title: str | None,
    rationale: str | None,
    output: str | None,
) -> None:
    """冻结一项尚未执行的价格行为单变量假设。"""
    from datetime import datetime, timezone

    import yaml

    from quantbot.price_action.config import config_hash
    from quantbot.price_action.experiment import build_candidate_config
    from quantbot.price_action.governance import HypothesisSpec

    required = {
        "--change": change,
        "--hypothesis-id": hypothesis_id,
        "--title": title,
        "--rationale": rationale,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ValueError("hypothesis-init 缺少参数：" + ", ".join(missing))
    assert change is not None and hypothesis_id is not None \
        and title is not None and rationale is not None
    if "=" not in change:
        raise ValueError("--change 格式必须是 section.field=YAML_VALUE")
    path, raw_value = change.split("=", 1)
    value = yaml.safe_load(raw_value)
    candidate, _ = build_candidate_config(cfg, {path: value})
    snapshot = str((cfg.get("data", {}) or {}).get("snapshot") or "")
    if not snapshot:
        raise ValueError("预注册实验必须先在 config.yaml 设置 data.snapshot")
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    spec = HypothesisSpec(
        hypothesis_id=hypothesis_id,
        title=title,
        price_action_rationale=rationale,
        baseline_config_hash=config_hash(cfg),
        candidate_config_hash=config_hash(candidate),
        data_snapshot=snapshot,
        single_change={path: value},
        created_at=now,
        evaluation_windows=(
            f"{cfg['backtest']['start']}~{cfg['backtest']['end']}",
        ),
        failure_conditions=(
            "paired CAGR improvement < 0.5pp",
            "90% paired interval crosses zero",
            "walk-forward or stress gate fails",
        ),
        parameter_candidates=(value,),
    )
    target = Path(output) if output else (
        Path((cfg.get("governance", {}) or {}).get(
            "artifact_dir", "reports/governance"
        ))
        / "hypotheses"
        / f"{hypothesis_id}.json"
    )
    _write_text_exclusive(target, spec.to_json(indent=2) + "\n")
    print(f"[hypothesis] 已冻结预注册假设：{target.resolve()}")
    print("[hypothesis] 仅生成候选证据，不会修改 config.yaml")


def record_reconciliation(cfg: dict, result, *, journal_path=None) -> None:
    """把结构化对账结果写入复盘账本；对账失败也作为事实保留。"""
    import pandas as pd

    from quantbot.price_action.config import config_hash
    from quantbot.price_action.review import ReviewJournal

    review_cfg = cfg.get("review", {}) or {}
    if not review_cfg.get("enabled", False):
        return
    day = result.asof or str(pd.Timestamp(result.generated_at).date())
    run_key = f"reconcile:{result.generated_at}"
    path = journal_path or review_cfg.get("journal_path", "logs/trading_journal.sqlite")
    with ReviewJournal(path) as journal:
        run_id = journal.append_run(
            idempotency_key=run_key,
            trading_date=day,
            mode="reconcile",
            status=result.status.lower(),
            signal_date=result.asof,
            data_as_of=result.asof,
            data_status="fresh" if result.status != "UNKNOWN" else "unknown",
            config_hash=config_hash(cfg),
            metadata={"source": result.source, "error": result.error},
            occurred_at=result.generated_at,
        )
        journal.append_reconciliation(
            idempotency_key=f"{run_key}:positions",
            run_id=run_id,
            category="positions",
            status=result.status,
            expected=result.theory_positions,
            actual=result.actual_positions,
            details={
                "only_theory": result.only_theory,
                "only_actual": result.only_actual,
                "direction_mismatches": [
                    item.__dict__ for item in result.direction_mismatches
                ],
                "quantity_mismatches": [
                    item.__dict__ for item in result.quantity_mismatches
                ],
                "error": result.error,
            },
            occurred_at=result.generated_at,
        )


def main() -> int | None:
    parser = argparse.ArgumentParser(description="PABot 价格行为美股交易系统")
    parser.add_argument(
        "command",
        choices=["download", "snapshot", "backtest", "walkforward", "paper",
                 "live", "dry-run", "sim", "reconcile", "report", "review",
                 "policy-check", "hypothesis-init", "experiment",
                 "candidate-bundle", "candidate-verify",
                 "paper-account-fingerprint", "paper-campaign-init",
                 "paper-audit", "paper-close"],
        help="要执行的操作",
    )
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--force", action="store_true",
                        help=("download: 强制重新下载；交易命令: 跳过交易日/幂等"
                              "检查，但不绕过订单恢复门禁"))
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="仅 live：配合 broker.paper=false 明确授权本次连接实盘",
    )
    parser.add_argument("--date", default=None, help="review：交易日 YYYY-MM-DD")
    parser.add_argument(
        "--mode", choices=["sim", "dry-run", "paper", "live"], default=None,
        help="review / report / reconcile：运行分区；默认按 broker.paper 选择 paper/live",
    )
    parser.add_argument("--output", default=None, help="证据输出文件或候选包根目录")
    parser.add_argument("--spec", default=None, help="experiment：HypothesisSpec JSON")
    parser.add_argument(
        "--experiment-artifact", default=None,
        help="candidate-bundle：已有实验 JSON",
    )
    parser.add_argument("--bundle", default=None, help="candidate-verify：候选包目录")
    parser.add_argument(
        "--campaign", default=None,
        help="paper / paper-close / paper-audit：冻结的 campaign JSON",
    )
    parser.add_argument("--campaign-id", default=None, help="Paper campaign ID")
    parser.add_argument(
        "--account-fingerprint", default=None,
        help="匿名账户 SHA-256：冻结 campaign 或选择 review/report/reconcile 账户",
    )
    parser.add_argument(
        "--trading-dates", default=None,
        help=(
            "paper-campaign-init：严格递增的 "
            "{execution_date, signal_date} JSON 会话数组"
        ),
    )
    parser.add_argument(
        "--change", default=None,
        help="hypothesis-init：唯一改动，如 strategy.breakout_window=60",
    )
    parser.add_argument("--hypothesis-id", default=None, help="hypothesis-init：假设 ID")
    parser.add_argument("--title", default=None, help="hypothesis-init：假设标题")
    parser.add_argument(
        "--rationale", default=None,
        help="hypothesis-init：为什么该改动属于价格行为且可证伪",
    )
    args = parser.parse_args()

    production_commands = {
        "paper", "live", "dry-run", "sim", "reconcile",
        "policy-check", "hypothesis-init", "experiment", "candidate-bundle",
        "candidate-verify", "paper-campaign-init", "paper-audit",
        "paper-account-fingerprint", "paper-close",
    }
    cfg = load_config(
        args.config,
        production=args.command in production_commands,
    )

    if args.command == "download":
        cmd_download(cfg, args.force)
    elif args.command == "snapshot":
        from quantbot.price_action.data.snapshot import create_snapshot

        create_snapshot(cfg["data"]["cache_dir"])
    elif args.command == "backtest":
        cmd_backtest(cfg)
    elif args.command == "walkforward":
        cmd_walkforward(cfg)
    elif args.command == "reconcile":
        from quantbot.price_action.live.reconcile import cmd_reconcile
        from quantbot.price_action.live.paths import runtime_paths

        mode = args.mode or ("paper" if cfg["broker"]["paper"] else "live")
        if mode == "sim":
            from quantbot.price_action.broker.sim_broker import SimBroker
            broker = SimBroker(cfg)
        elif mode in {"paper", "live"}:
            if cfg["broker"]["paper"] is not (mode == "paper"):
                raise ValueError("对账 --mode 与 broker.paper 不一致")
            from quantbot.price_action.broker.alpaca_broker import AlpacaBroker
            broker = AlpacaBroker(paper=mode == "paper")
        else:
            raise ValueError("dry-run 没有实际账户持仓，不支持账户对账")
        fingerprint = broker.account_fingerprint()
        if args.account_fingerprint and args.account_fingerprint != fingerprint:
            raise ValueError("对账目标指纹与当前券商账户不一致")
        paths = runtime_paths(cfg, mode, fingerprint)

        result = cmd_reconcile(cfg, broker=broker, cache_dir=paths.cache_dir)
        record_reconciliation(cfg, result, journal_path=paths.journal_file)
    elif args.command == "report":
        from quantbot.price_action.live.report import cmd_report
        from quantbot.price_action.live.paths import read_runtime_paths

        mode = args.mode or ("paper" if cfg["broker"]["paper"] else "live")
        cmd_report(cfg, paths=read_runtime_paths(cfg, mode, args.account_fingerprint))
    elif args.command == "review":
        from quantbot.price_action.live.paths import read_runtime_paths

        mode = args.mode or ("paper" if cfg["broker"]["paper"] else "live")
        paths = read_runtime_paths(cfg, mode, args.account_fingerprint)
        cmd_review(cfg, args.date, args.output, journal_path=paths.journal_file)
    elif args.command == "policy-check":
        cmd_policy_check(cfg)
    elif args.command == "hypothesis-init":
        cmd_hypothesis_init(
            cfg,
            change=args.change,
            hypothesis_id=args.hypothesis_id,
            title=args.title,
            rationale=args.rationale,
            output=args.output,
        )
    elif args.command == "experiment":
        cmd_experiment(cfg, args.spec, args.output)
    elif args.command == "candidate-bundle":
        result = cmd_candidate_bundle(
            cfg, args.experiment_artifact, args.output
        )
        return 0 if result.eligible else 2
    elif args.command == "candidate-verify":
        result = cmd_candidate_verify(args.bundle)
        return 0 if result.eligible else 2
    elif args.command == "paper-campaign-init":
        cmd_paper_campaign_init(
            cfg,
            campaign_id=args.campaign_id,
            account_fingerprint=args.account_fingerprint,
            trading_dates_path=args.trading_dates,
            output=args.output,
        )
    elif args.command == "paper-account-fingerprint":
        cmd_paper_account_fingerprint(cfg)
    elif args.command == "paper-audit":
        summary = cmd_paper_audit(cfg, args.campaign, args.output)
        return 0 if summary.passed else 2
    elif args.command == "paper-close":
        if args.date:
            raise ValueError("paper-close 只采集当前交易日，不支持 --date 补录")
        result = cmd_paper_close(cfg, args.campaign, force=args.force)
        return 0 if result.eligible else 2
    else:
        cmd_trade(
            cfg,
            args.command,
            force=args.force,
            confirm_live=args.confirm_live,
            campaign_path=args.campaign,
        )


if __name__ == "__main__":
    sys.exit(main())
