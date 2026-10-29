"""SEC 历史事件研究入口；网页只回放已导入的数据。"""

from pathlib import Path
import re
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from quantbot.stocks.edgar.event_example import example_input
from quantbot.stocks.edgar.event_study import event_study


class StudyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bundle: dict
    prices: dict
    universe: list[dict]
    policy: dict = Field(default_factory=dict)


def router(directory):
    root = Path(directory)
    routes = APIRouter(prefix="/api/quantbot/sec-study", tags=["SEC 历史事件研究"])

    @routes.get("/example")
    def example():
        return example_input()

    @routes.post("")
    def study(request: StudyRequest):
        identifier = uuid4().hex
        try:
            report = event_study(**request.model_dump(), output=root / identifier)
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from None
        except OSError as exc:
            raise HTTPException(503, str(exc)) from None
        return {**{k: v for k, v in report.items() if k not in {"event_input", "price_input"}},
                "download_url": f"/api/quantbot/sec-study/reports/{identifier}"}

    @routes.get("/reports/{identifier}")
    def download(identifier: str):
        if re.fullmatch(r"[0-9a-f]{32}", identifier) is None or not (root / identifier / "report.json").is_file():
            raise HTTPException(404, "事件报告不存在")
        return FileResponse(root / identifier / "report.json", media_type="application/json", filename=f"quantbot-sec-{identifier}.json")

    return routes
