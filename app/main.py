"""FastAPI 入口: JSON Schema 反例最小化服务。"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .minimizer import (
    InvalidSchemaError,
    InstanceValidError,
    MinimizeResult,
    minimize,
)

SERVICE_VERSION = "1.0.0"
DEFAULT_BUDGET = int(os.getenv("DEFAULT_MAX_EVALUATIONS", "1000"))
MAX_BUDGET = int(os.getenv("MAX_BUDGET_LIMIT", "100000"))

app = FastAPI(
    title="JSON Schema 反例最小化 API",
    version=SERVICE_VERSION,
    description=(
        "对未通过 JSON Schema (Draft 7) 校验的实例执行确定性 delta debugging, "
        "删除对象字段、数组块并简化标量, 返回仍触发同一校验关键字的最小实例。"
    ),
)


class MinimizeRequest(BaseModel):
    # 任意合法 JSON 值; 布尔 schema 也允许。
    schema_: Any = Field(
        ...,
        alias="schema",
        description="JSON Schema (Draft 7), 对象或布尔值。",
    )
    instance: Any = Field(
        ...,
        description="未通过校验的 JSON 实例 (反例)。",
    )
    max_evaluations: int | None = Field(
        default=None,
        ge=1,
        description=(
            f"最大校验评估次数 (含初始校验); 默认 {DEFAULT_BUDGET}, "
            f"服务端硬上限 {MAX_BUDGET}。"
        ),
    )

    model_config = {"populate_by_name": True}


class TraceEntry(BaseModel):
    evaluation: int
    action: str
    accepted: bool
    keyword: str | None
    message: str | None


class MinimizeResponse(BaseModel):
    keyword: str | None = Field(
        description="锁定的首个校验错误关键字 (jsonschema validator 名)。"
    )
    original_instance: Any
    minimal_instance: Any
    evaluations: int
    max_evaluations: int
    stopped_early: bool = Field(description="是否因评估预算耗尽而提前停止。")
    trace: list[TraceEntry]


def _result_to_response(result: MinimizeResult) -> dict[str, Any]:
    return {
        "keyword": result.keyword,
        "original_instance": result.original_instance,
        "minimal_instance": result.minimal_instance,
        "evaluations": result.evaluations,
        "max_evaluations": result.max_evaluations,
        "stopped_early": result.stopped_early,
        "trace": result.trace,
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "json-schema-counterexample-minimizer"}


@app.get("/info")
def info() -> dict[str, Any]:
    return {
        "service": "json-schema-counterexample-minimizer",
        "version": SERVICE_VERSION,
        "draft": "Draft 7",
        "default_max_evaluations": DEFAULT_BUDGET,
        "max_budget_limit": MAX_BUDGET,
    }


@app.post(
    "/minimize",
    response_model=MinimizeResponse,
    responses={
        400: {"description": "schema 非法或预算超出服务端上限"},
        422: {"description": "原始实例通过了校验 (不是反例), 或请求体格式错误"},
    },
)
def minimize_endpoint(request: MinimizeRequest) -> Any:
    schema = request.schema_
    if not isinstance(schema, (dict, bool)):
        return JSONResponse(
            status_code=400,
            content={"detail": "schema 必须是 JSON 对象或布尔值。"},
        )

    budget = (
        request.max_evaluations
        if request.max_evaluations is not None
        else DEFAULT_BUDGET
    )
    if budget > MAX_BUDGET:
        return JSONResponse(
            status_code=400,
            content={
                "detail": (
                    f"max_evaluations {budget} 超过服务端上限 {MAX_BUDGET}; "
                    "可调高 MAX_BUDGET_LIMIT 环境变量。"
                )
            },
        )

    try:
        result = minimize(schema, request.instance, budget)
    except InstanceValidError as exc:
        # 需求约定: 原实例有效 -> 422。
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    except InvalidSchemaError as exc:
        return JSONResponse(
            status_code=400,
            content={"detail": f"非法的 JSON Schema: {exc}"},
        )

    return _result_to_response(result)
