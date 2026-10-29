"""通过本机 Codex CLI 运行独立、结构化的模型请求，不读取登录凭据。"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile

from quantbot.ai.domain import strict_json
from quantbot.ai.transport import ProviderError


class CodexClient:
    """只复用 Codex 登录；模型与推理强度由 QuantBot 显式配置并冻结。"""

    def __init__(self, config, environ=None):
        self.environ = dict(os.environ if environ is None else environ)
        self.model = config["llm"]["model"].strip()
        if not self.model:
            raise ValueError("Codex 决策需要明确填写 llm.model，保证回测模型可追溯")
        codex = config.get("codex", {})
        executable = codex.get("executable", "codex")
        self.executable = shutil.which(executable, path=self.environ.get("PATH", os.defpath))
        if not self.executable:
            raise ValueError("未找到 Codex CLI；安装后设置 codex.executable 或 PATH")
        self.timeout = config["llm"]["timeout_seconds"]
        self.effort = codex.get("reasoning_effort", "")

    def check(self):
        """只检查安装与登录；不请求模型，也不把登录成功当成模型权限证明。"""
        try:
            version = subprocess.run(
                [self.executable, "--version"], capture_output=True, timeout=10, env=self.environ,
            )
            root_help = subprocess.run(
                [self.executable, "--help"], capture_output=True, timeout=10, env=self.environ,
            )
            help_result = subprocess.run(
                [self.executable, "exec", "--help"], capture_output=True, timeout=10, env=self.environ,
            )
            login = subprocess.run(
                [self.executable, "login", "status"], capture_output=True, timeout=10, env=self.environ,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProviderError("Codex 本机检查失败或超时；未调用模型") from exc
        version_text = version.stdout.decode("utf-8", errors="replace").strip()
        required = (b"--ignore-user-config", b"--ephemeral", b"--output-schema", b"--output-last-message")
        compatible = (
            version.returncode == 0 and root_help.returncode == 0
            and b"--no-daemon" in root_help.stdout and help_result.returncode == 0
            and all(flag in help_result.stdout for flag in required)
        )
        return {
            "backend": "codex_cli", "model": self.model,
            "cli_version": version_text if version_text.startswith("codex-cli ") else "unknown",
            "cli_compatible": compatible, "authenticated": login.returncode == 0,
            "ready": compatible and login.returncode == 0,
            "model_called": False, "model_access_verified": False,
        }

    def complete(self, instructions, payload, schema):
        # 不在项目目录运行，也不恢复既有会话；每个时点仅传入调用方提供的事实。
        with tempfile.TemporaryDirectory(prefix="quantbot-codex-") as temporary:
            root = Path(temporary)
            schema_path, answer_path = root / "schema.json", root / "answer.json"
            instructions_path = root / "instructions.md"
            schema_path.write_text(json.dumps(schema, allow_nan=False), encoding="utf-8")
            instructions_path.write_text(instructions, encoding="utf-8")
            arguments = [
                self.executable, "--no-daemon", "exec", "--ignore-user-config", "--ephemeral",
                "--skip-git-repo-check", "--sandbox", "read-only", "--color", "never",
                "--model", self.model, "--json", "--cd", str(root),
                "--output-schema", str(schema_path), "--output-last-message", str(answer_path),
            ]
            overrides = {
                "approval_policy": "never",
                "web_search": "disabled",
                "project_doc_max_bytes": 0,
                "model_instructions_file": str(instructions_path),
                "developer_instructions": "",
                "mcp_servers": {},
                **{f"features.{feature}": False for feature in (
                    "shell_tool", "unified_exec", "apps", "plugins", "hooks", "multi_agent",
                    "memories", "shell_snapshot", "browser_use", "browser_use_external", "computer_use",
                    "skill_search", "workspace_dependencies", "view_image", "code_mode_host",
                )},
            }
            if self.effort:
                overrides["model_reasoning_effort"] = self.effort
            for name, value in overrides.items():
                # JSON strings/bools are valid TOML literals; the only table here is empty.
                arguments.extend(["-c", f"{name}={json.dumps(value)}"])
            arguments.append("-")
            prompt = json.dumps(payload, ensure_ascii=False, allow_nan=False)
            with tempfile.TemporaryFile() as events, tempfile.TemporaryFile() as errors:
                try:
                    process = subprocess.Popen(
                        arguments, stdin=subprocess.PIPE, stdout=events, stderr=errors,
                        cwd=root, env=self.environ, start_new_session=os.name == "posix",
                    )
                except OSError as exc:
                    raise ProviderError("无法启动 Codex CLI，未生成决策") from exc
                try:
                    process.communicate(prompt.encode("utf-8"), timeout=self.timeout)
                except BaseException as exc:
                    # 同时结束 CLI 子进程，避免超时后仍继续推理或等待外部工具。
                    if os.name == "posix":
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    else:
                        process.kill()
                    process.wait()
                    if isinstance(exc, subprocess.TimeoutExpired):
                        raise ProviderError("Codex 调用超时；未自动重试，调用用量可能未知") from None
                    raise
                if process.returncode:
                    errors.seek(0)
                    diagnostic = errors.read(16384).decode("utf-8", errors="replace")
                    # PATH 别名等非致命警告也可能提到权限；仅识别明确的退出错误。
                    if any(line.strip() in {
                        "Error: Operation not permitted (os error 1)",
                        "Error: Permission denied (os error 13)",
                    } for line in diagnostic.splitlines()):
                        raise ProviderError(
                            f"Codex 受到本机权限限制（退出码 {process.returncode}）；"
                            "请检查启动进程对 Codex 本地运行资源的权限；未自动重试，调用用量可能未知"
                        )
                    raise ProviderError(
                        f"Codex CLI 退出码 {process.returncode}；请检查登录、模型权限或 CLI 版本，未自动重试"
                    )
                if events.tell() > 4_000_000:
                    raise ProviderError("Codex 事件输出超过限制，未采用决策")
                events.seek(0)
                request_id, usage = _completion(events.read().decode("utf-8"))
            if not answer_path.is_file() or answer_path.stat().st_size > 65536:
                raise ProviderError("Codex 未返回有效结果文件或结果超过长度限制")
            content = answer_path.read_text(encoding="utf-8")
            if not content.strip():
                raise ProviderError("Codex 返回空结果，未生成决策")
            return strict_json(content), request_id, usage


def _completion(text):
    """只保留完成标记与用量；不保存 CLI 的推理文本或原始 stderr。"""
    request_id, usage, completed = "", {}, False
    turn_started = False
    for line in text.splitlines():
        event = strict_json(line)
        if not isinstance(event, dict):
            raise ProviderError("Codex 事件结构无效")
        kind = event.get("type")
        if not isinstance(kind, str):
            raise ProviderError("Codex 事件类型无效")
        if kind == "thread.started":
            request_id = event.get("thread_id", "")
        elif kind == "turn.started":
            turn_started = True
            continue
        elif kind == "turn.completed":
            if completed:
                raise ProviderError("Codex 返回多个完成事件，未采用决策")
            completed = True
            reported = event.get("usage", {})
            if isinstance(reported, dict):
                usage = {key: value for key, value in reported.items()
                         if key in {"input_tokens", "output_tokens", "cached_input_tokens"}
                         and type(value) is int and value >= 0}
                if {"input_tokens", "output_tokens"} <= usage.keys():
                    usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
        elif kind in {"item.started", "item.updated", "item.completed"}:
            item = event.get("item")
            if not isinstance(item, dict):
                raise ProviderError("Codex 事件条目结构无效，未采用决策")
            item_type = item.get("type")
            if not isinstance(item_type, str):
                raise ProviderError("Codex 事件条目类型无效，未采用决策")
            if item_type == "error":
                # CLI 0.162.0 会把主动禁用执行器的固定启动提示包装成 error 条目。
                # 仅兼容这条已复现的 fail-closed 提示；模型切换或其他警告仍拒绝。
                if (kind == "item.completed" and not turn_started and item.get("message") ==
                        "Code Mode is unavailable because code-mode host is disabled. "
                        "Code mode will fail closed; enable `features.code_mode_host` "
                        "and install `codex-code-mode-host`."):
                    continue
                raise ProviderError("Codex 返回 CLI 警告或错误条目，未采用决策；请检查模型及工具配置")
            if item_type not in {"agent_message", "reasoning"}:
                # 只报告已知类型，不泄露事件文本、命令内容或未知字段。
                label = item_type if item_type in {
                    "command_execution", "file_change", "mcp_tool_call", "collab_tool_call",
                    "web_search", "todo_list",
                } else "unknown"
                raise ProviderError(f"Codex 返回工具或未知事件（{label}），未采用决策")
        else:
            raise ProviderError("Codex 返回失败或未知事件，未采用决策且不自动重试")
    if not completed or not isinstance(request_id, str) or not request_id:
        raise ProviderError("Codex 缺少完整的会话与完成记录，未采用决策")
    return request_id, usage


class CodexChatRouter:
    """研究文字共用 Codex；CLI 完成后返回整段，不伪装逐 token 流。"""

    def __init__(self, config):
        self.client = CodexClient(config)

    async def chat(self, messages):
        schema = {
            "type": "object", "additionalProperties": False, "required": ["message"],
            "properties": {"message": {"type": "string"}},
        }
        result, _, _ = await asyncio.to_thread(
            self.client.complete,
            "你是 QuantBot 研究助手。依据给定消息回答；没有数据时说明缺口。"
            "不使用工具，不访问文件或外部资料。研究文字不构成交易指令。只返回符合 schema 的 JSON。",
            {"messages": messages}, schema,
        )
        if not isinstance(result, dict) or set(result) != {"message"} or not isinstance(result["message"], str):
            raise ValueError("Codex 研究回答字段不符合契约")
        if not result["message"].strip():
            raise ValueError("Codex 研究回答为空")
        return result["message"]

    async def stream(self, messages):
        yield await self.chat(messages)
