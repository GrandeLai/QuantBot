"""回放的逐步持久化证据；进程中断后只能检查，不能自动重放未确认的模型请求。"""

from datetime import UTC, datetime
import fcntl
from pathlib import Path

from quantbot.evidence_auth import append_json_line_locked

from .domain import strict_json


class ReplayJournal:
    def __init__(self, path, metadata):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 每次尝试使用独立文件，绝不继续追加一个先前失败或中断的运行。
        self.path.touch(exist_ok=False)
        self.sequence = 0
        self.write("started", {"schema_version": 1, **metadata})

    def write(self, kind, payload):
        append_json_line_locked(self.path, {"sequence": self.sequence, "recorded_at": datetime.now(UTC).isoformat(),
                                           "kind": kind, "payload": payload})
        self.sequence += 1

    def __enter__(self):
        return self

    def __exit__(self, error_type, error, traceback):
        if error is not None:
            self.write("failed", {"error": str(error), "error_type": error_type.__name__})
        return False


def inspect_replay(path, *, include_records=True):
    path = Path(path)
    if path.stat().st_size > 128_000_000:
        raise ValueError("回放日志超过 128 MB")
    with path.open("rb") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        try:
            lines = handle.readlines()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    records, corrupt_tail = [], False
    for index, line in enumerate(lines):
        try:
            record = strict_json(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            # 强制退出可能只写了最后一行的一部分；先前已 fsync 的记录仍可取回。
            if index == len(lines) - 1 and not line.endswith(b"\n"):
                corrupt_tail = True
                break
            raise ValueError(f"回放日志第 {index + 1} 行损坏") from None
        if (not isinstance(record, dict) or set(record) != {"sequence", "recorded_at", "kind", "payload"}
                or type(record["sequence"]) is not int or record["sequence"] != index
                or not isinstance(record["payload"], dict) or not isinstance(record["recorded_at"], str)
                or not isinstance(record["kind"], str)
                or record["kind"] not in {"started", "account", "orders_canceled", "execution_wait", "decision_started", "decision", "decision_error", "completed", "failed"}):
            raise ValueError(f"回放日志第 {index + 1} 行字段或序号无效")
        if record["kind"] in {"decision_started", "decision", "decision_error", "execution_wait"} and not isinstance(record["payload"].get("as_of"), str):
            raise ValueError(f"回放日志第 {index + 1} 行缺少决策时间")
        if record["kind"] == "completed" and (type(record["payload"].get("execution_completed")) is not bool
                                                or not isinstance(record["payload"].get("account"), dict)):
            raise ValueError("回放完成记录缺少明确的执行结论或账户")
        if index and record["kind"] == "started":
            raise ValueError("回放日志不能混入另一轮起始记录")
        if records and records[-1]["kind"] in {"completed", "failed"}:
            raise ValueError("回放日志在终态后仍有记录")
        records.append(record)
    if records and (records[0]["kind"] != "started" or records[0]["payload"].get("schema_version") != 1):
        raise ValueError("回放日志缺少版本化起始记录")
    terminal = records[-1] if records else None
    status = terminal["kind"] if terminal and terminal["kind"] in {"completed", "failed"} and not corrupt_tail else "unfinished"
    started = [r["payload"]["as_of"] for r in records if r["kind"] == "decision_started"]
    finished_rows = [r["payload"]["as_of"] for r in records if r["kind"] in {"decision", "decision_error"}]
    finished = set(finished_rows)
    if len(started) != len(set(started)) or len(finished_rows) != len(finished) or finished - set(started):
        raise ValueError("回放日志的决策起始和结果不能重复或失配")
    unresolved = [at for at in started if at not in finished]
    if status == "completed" and unresolved:
        raise ValueError("回放日志存在未确认调用，不能标记完成")
    reported_usage = []
    for record in records:
        if record["kind"] not in {"decision", "decision_error"}:
            continue
        decision = record["payload"].get("raw_decision", {})
        usage = decision.get("usage", {}) if isinstance(decision, dict) else {}
        total = usage.get("total_tokens") if isinstance(usage, dict) else None
        if type(total) is int and total >= 0:
            reported_usage.append(total)
    last_account = next((r["payload"] for r in reversed(records) if r["kind"] == "account"), None)
    if status == "completed" and last_account is not None:
        last_account = {**last_account, "account": terminal["payload"]["account"]}
    result = {"path": str(path.resolve()), "status": status, "record_count": len(records),
              "decision_attempts": len(started), "resolved_decisions": len(finished),
              "reported_tokens": sum(reported_usage) if reported_usage else None,
              "usage_reported_calls": len(reported_usage), "usage_missing_calls": len(started) - len(reported_usage),
              "unresolved_decision_calls": unresolved, "incomplete_tail": corrupt_tail,
              "completed_steps": sum(r["kind"] == "account" for r in records),
              "last_account": last_account,
              "error": terminal["payload"].get("error") if status == "failed" else None,
              "execution_completed": terminal["payload"].get("execution_completed", False) if status == "completed" else False,
              "boundary": "unfinished 可能仍在运行或已经中断；未确认调用的响应、计费和结果未知，禁止自动重试。记录数不是供应方 HTTP 请求数。"}
    if include_records:
        result["records"] = records
    return result
