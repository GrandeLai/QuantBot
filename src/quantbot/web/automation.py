"""本地任务工作台 API；模型调用必须来自显式启用或评估请求。"""

from copy import deepcopy
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from quantbot.ai.evaluation import evaluate, walk_forward
from quantbot.ai.runtime import RuntimeStore
from quantbot.ai.research import export_study, get_study, list_studies, register_study, run_study
from quantbot.ai.research_ops import cancel_queued_study, enqueue_study, research_operations, run_due_study


class CreateJob(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spec: dict
    initial_cash: float = Field(default=10000, gt=0, le=1e9)
    parent: str | None = None
    model: str | None = Field(default=None, max_length=200)


class ControlJob(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["resume", "pause", "exit"]
    allow_model_calls: bool = False
    acknowledge_risk: bool = False


class EvaluateJob(BaseModel):
    model_config = ConfigDict(extra="forbid")
    last_bars: int = Field(default=30, ge=3, le=500)
    allow_model_calls: bool = False


class WalkForwardJobs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_ids: list[str] = Field(min_length=1, max_length=6)
    selection_bars: int = Field(default=30, ge=3, le=200, strict=True)
    test_bars: int = Field(default=10, ge=3, le=100, strict=True)
    folds: int = Field(default=3, ge=2, le=10, strict=True)
    gap_bars: int = Field(default=1, ge=0, le=100, strict=True)
    allow_model_calls: bool = False


class RegisterStudy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_ids: list[str] = Field(min_length=1, max_length=6)
    card: dict
    protocol: dict = Field(default_factory=dict)
    data_kind: Literal["historical_unverified", "synthetic"] = "historical_unverified"


class RunStudy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow_model_calls: bool = Field(default=False, strict=True)


class QueueStudy(RunStudy):
    planned_at: str


def router(config, root):
    routes = APIRouter(prefix="/api/quantbot/runtime", tags=["回测研究与运行管理"])

    def run(operation):
        try:
            return operation(RuntimeStore(root))
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from None
        except (OSError, RuntimeError) as exc:
            raise HTTPException(503, str(exc)) from None

    @routes.get("")
    def overview():
        result = run(lambda store: store.overview())
        for job in result["jobs"]:
            job.pop("row_hashes")
        return result

    @routes.post("/jobs")
    def create(req: CreateJob):
        def operation(store):
            selected = deepcopy(store.get(req.parent)["config"] if req.parent else config)
            selected["execution"]["initial_cash"] = req.initial_cash
            if req.model is not None:
                selected["llm"]["model"] = req.model
            return store.create(req.spec, selected, parent=req.parent)

        return run(operation)

    @routes.get("/research")
    def studies():
        return run(list_studies)

    @routes.get("/operations")
    def operations():
        return run(research_operations)

    @routes.post("/research-queue/run")
    def queue_run():
        return run(run_due_study)

    @routes.post("/research-queue/{queue_id}/cancel")
    def queue_cancel(queue_id: str):
        return run(lambda store: cancel_queued_study(store, queue_id))

    @routes.post("/research")
    def register(req: RegisterStudy):
        return run(lambda store: register_study(store, **req.model_dump()))

    @routes.get("/research/{study_id}")
    def study_detail(study_id: str):
        return run(lambda store: get_study(store, study_id))

    @routes.get("/research/{study_id}/export")
    def study_export(study_id: str):
        return run(lambda store: export_study(store, study_id))

    @routes.post("/research/{study_id}/run")
    def study_run(study_id: str, req: RunStudy):
        return run(lambda store: run_study(store, study_id, **req.model_dump()))

    @routes.post("/research/{study_id}/queue")
    def queue_study(study_id: str, req: QueueStudy):
        return run(lambda store: enqueue_study(store, study_id, **req.model_dump()))

    @routes.get("/jobs/{job_id}")
    def detail(job_id: str):
        return run(lambda store: {"job": store.get(job_id), **store.history(job_id)})

    @routes.post("/jobs/{job_id}/control")
    def control(job_id: str, req: ControlJob):
        return run(lambda store: store.control(job_id, **req.model_dump()))

    @routes.post("/jobs/{job_id}/evaluate")
    def evaluation(job_id: str, req: EvaluateJob):
        return run(lambda store: evaluate(store, job_id, **req.model_dump()))

    @routes.post("/evaluations/walk-forward")
    def rolling(req: WalkForwardJobs):
        return run(lambda store: walk_forward(store, **req.model_dump()))

    @routes.post("/jobs/{job_id}/orders/{client_order_id}/cancel")
    def cancel(job_id: str, client_order_id: str):
        return run(lambda store: store.cancel_order(job_id, client_order_id))

    return routes
