"""持仓期权历史研究 API；所有结果保存到独立目录。"""

import re
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from quantbot.stocks.options.overlay_report import options_backtest
from quantbot.stocks.options.overlay_example import example_input


class OverlayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data: dict
    portfolio: dict
    policy: dict = Field(default_factory=dict)


def router(root):
    routes = APIRouter(prefix="/api/quantbot/options", tags=["持仓期权回测"])
    root = Path(root)

    @routes.get("/example")
    def example():
        return example_input()

    @routes.post("/backtest")
    def backtest(req: OverlayRequest):
        identifier = uuid4().hex
        try:
            report = options_backtest(payload=req.data, portfolio=req.portfolio, policy=req.policy, output=root / identifier)
        except (ValueError, TypeError, ArithmeticError) as exc:
            raise HTTPException(400, str(exc)) from None
        except OSError as exc:
            raise HTTPException(503, str(exc)) from None
        report["download_url"] = f"/api/quantbot/options/reports/{identifier}"
        return {k: v for k, v in report.items() if k != "input_data"}

    @routes.get("/reports/{identifier}")
    def report(identifier: str):
        if re.fullmatch(r"[0-9a-f]{32}", identifier) is None:
            raise HTTPException(404, "报告不存在")
        path = root / identifier / "report.json"
        if not path.is_file():
            raise HTTPException(404, "报告不存在")
        return FileResponse(path, media_type="application/json", filename=f"quantbot-options-{identifier}.json")

    return routes
