"""运行通知：把每日执行摘要 / 异常告警推送到 webhook。

支持两种格式，按 URL 自动识别：
    - 飞书自定义机器人（URL 含 open.feishu.cn / open.larksuite.com）
    - 通用 JSON webhook（Slack 兼容的 {"text": ...} 格式）

Webhook 地址通过环境变量注入（不写入配置文件）：
    export PABOT_WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/xxx"

未设置环境变量时静默跳过——通知是可选的运维增强，
绝不能因为通知失败而中断交易流程。
"""

from __future__ import annotations

import os

import requests

ENV_VAR = "PABOT_WEBHOOK_URL"
HEARTBEAT_ENV_VAR = "PABOT_HEARTBEAT_URL"


def heartbeat() -> bool:
    """向外部心跳服务报平安（死人开关）。

    配置 healthchecks.io 之类的免费心跳服务后：
        export PABOT_HEARTBEAT_URL="https://hc-ping.com/<uuid>"
    每次成功运行结束 ping 一次；连续缺 ping 时由**外部服务**发告警——
    这是唯一能发现"系统整体停摆"（cron 失效 / 机器关机）的手段，
    因为停摆的系统自己发不出任何告警。
    未配置时静默跳过。
    """
    url = os.environ.get(HEARTBEAT_ENV_VAR, "").strip()
    if not url:
        return False
    try:
        return requests.get(url, timeout=10).status_code < 300
    except Exception as e:  # noqa: BLE001
        print(f"[notify] 心跳发送失败: {e}")
        return False


def notify(text: str) -> bool:
    """发送一条文本通知，返回是否成功（未配置 / 失败返回 False）。

    任何异常都被吞掉并打印——通知永远不能让主流程崩溃。
    """
    url = os.environ.get(ENV_VAR, "").strip()
    if not url:
        return False
    try:
        if "open.feishu.cn" in url or "open.larksuite.com" in url:
            payload = {"msg_type": "text", "content": {"text": text}}
        else:
            payload = {"text": text}
        resp = requests.post(url, json=payload, timeout=10)
        ok = resp.status_code < 300
        if not ok:
            print(f"[notify] webhook 返回 {resp.status_code}: {resp.text[:200]}")
        return ok
    except Exception as e:  # noqa: BLE001 - 通知失败不影响交易
        print(f"[notify] 发送失败: {e}")
        return False
