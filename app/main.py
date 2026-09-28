"""FastAPI 入口：单条诊断、批量诊断、修复建议、校准方案、窗口峰值、冗余审计与健康检查。"""

from __future__ import annotations

import math
from typing import List

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .models import (
    BatchDiagnosisRequest,
    BatchDiagnosisResponse,
    BatchItemResponse,
    CalibrationPlanRequest,
    CalibrationPlanResponse,
    ContradictionCycle,
    CycleStep,
    DiagnosisRequest,
    DiagnosisResponse,
    DriftContradictionCycle,
    DriftDiagnosisRequest,
    DriftDiagnosisResponse,
    DriftNodeBounds,
    DriftObservationCycleStep,
    DriftQueryResult,
    DriftSegmentCycleStep,
    FeasibleDiagnosis,
    FeasibleDriftDiagnosis,
    InfeasibleDiagnosis,
    InfeasibleDriftDiagnosis,
    NodeOffset,
    RedundancyAuditRequest,
    RedundancyAuditResponse,
    RedundancyItem,
    RepairRequest,
    RepairResponse,
    TimePoint,
    WindowPeakRequest,
    WindowPeakResponse,
    collect_nodes,
)
from .solver import (
    CalibrationCandidate,
    DriftObservation,
    Observation,
    WindowEvent,
    audit_redundancy,
    diagnose,
    diagnose_drift,
    plan_calibration,
    plan_window_peak,
    suggest_repair,
)

app = FastAPI(
    title="分布式时钟约束诊断 API",
    description=(
        "提交形如「B 时钟 - A 时钟 ∈ [min, max] 毫秒」的有向观测，"
        "服务将其转换为差分约束，判断时钟偏移是否一致；"
        "可行时给出相对参考节点的最紧上下界及对应的证据链"
        "（lower_proof / upper_proof），不可行时返回稳定的矛盾环，"
        "并可进一步给出最小观测删除集合的修复建议；"
        "还可从带成本的候选观测中规划满足目标界宽的最小成本校准方案；"
        "另可在真实时间窗口下求同时入窗的最大事件集合及可行偏移见证；"
        "并可逐条审计观测区间是否已被其余观测蕴含（冗余审计）；"
        "时间漂移接口在时间展开的差分约束图上检查整条时间轴是否可行，"
        "可行时给出各查询时刻相对参考节点的最紧偏移上下界。"
    ),
    version="1.6.0",
)


def _sanitize_error_value(value):
    """把校验错误详情中的非有限浮点（NaN/±inf）替换为 None。

    FastAPI 0.115 的默认 422 处理器在序列化含 NaN/inf 的 ``input`` 字段时
    会因 ``allow_nan=False`` 抛出 ValueError，使非法输入得到 500 而非 422。
    """
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _sanitize_error_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_error_value(v) for v in value]
    return value


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """与默认处理器一致地返回 422，但先清洗非有限浮点避免序列化崩溃。"""
    return JSONResponse(
        status_code=422,
        content={"detail": _sanitize_error_value(jsonable_encoder(exc.errors()))},
    )


def _to_observation(raw) -> Observation:
    return Observation(
        id=raw.id,
        source=raw.source,
        target=raw.target,
        min=raw.min,
        max=raw.max,
    )


def _to_candidate(raw) -> CalibrationCandidate:
    return CalibrationCandidate(
        id=raw.id,
        source=raw.source,
        target=raw.target,
        min=raw.min,
        max=raw.max,
        cost=raw.cost,
    )


def _diagnosis_model(name, result):
    """把 solver 返回的诊断字典包装成响应模型。"""
    if result["feasible"]:
        return FeasibleDiagnosis(name=name, **result)

    cycle = result["cycle"]
    steps: List[CycleStep] = [
        CycleStep(
            observation_id=s["observation_id"],
            from_=s["from"],
            to=s["to"],
            constraint=s["constraint"],
            weight=s["weight"],
            bound=s["bound"],
        )
        for s in cycle["steps"]
    ]
    return InfeasibleDiagnosis(
        name=name,
        cycle=ContradictionCycle(
            steps=steps,
            accumulated_bound=cycle["accumulated_bound"],
            contradiction=cycle["contradiction"],
        ),
    )


def _run_diagnosis(request: DiagnosisRequest):
    result = diagnose(
        nodes=request.effective_nodes(),
        reference=request.reference,
        observations=[_to_observation(o) for o in request.observations],
    )
    return _diagnosis_model(request.name, result)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/v1/diagnose", response_model=DiagnosisResponse, tags=["diagnosis"])
def diagnose_once(request: DiagnosisRequest) -> DiagnosisResponse:
    """提交一组观测与参考节点，返回单次诊断结果。"""
    return _run_diagnosis(request)


@app.post(
    "/api/v1/diagnose/batch",
    response_model=BatchDiagnosisResponse,
    tags=["diagnosis"],
)
def diagnose_batch(request: BatchDiagnosisRequest) -> BatchDiagnosisResponse:
    """批量诊断；结果按提交顺序（index）稳定返回。"""
    items = [
        BatchItemResponse(index=i, name=req.name, result=_run_diagnosis(req))
        for i, req in enumerate(request.diagnoses)
    ]
    return BatchDiagnosisResponse(count=len(items), results=items)


def _to_drift_observation(raw) -> DriftObservation:
    return DriftObservation(
        id=raw.id,
        source=raw.source,
        target=raw.target,
        second=raw.second,
        min=raw.min,
        max=raw.max,
    )


def _drift_cycle_step_model(step):
    """把 solver 返回的矛盾环步骤字典包装为对应响应模型（按 type 分派）。"""
    if step["type"] == "drift":
        return DriftSegmentCycleStep(
            segment_id=step["segment_id"],
            node=step["node"],
            from_second=step["from_second"],
            to_second=step["to_second"],
            constraint=step["constraint"],
            weight=step["weight"],
            bound=step["bound"],
        )
    return DriftObservationCycleStep(
        observation_id=step["observation_id"],
        from_=TimePoint(node=step["from"]["node"], second=step["from"]["second"]),
        to=TimePoint(node=step["to"]["node"], second=step["to"]["second"]),
        constraint=step["constraint"],
        weight=step["weight"],
        bound=step["bound"],
    )


def _drift_diagnosis_model(name, result) -> DriftDiagnosisResponse:
    """把时间漂移 solver 返回的字典包装为响应模型。"""
    if result["feasible"]:
        queries = [
            DriftQueryResult(
                second=q["second"],
                node_bounds=[
                    DriftNodeBounds(node=b["node"], lower=b["lower"], upper=b["upper"])
                    for b in q["node_bounds"]
                ],
            )
            for q in result["queries"]
        ]
        return FeasibleDriftDiagnosis(
            name=name,
            reference=result["reference"],
            timeline=result["timeline"],
            queries=queries,
        )

    cycle = result["cycle"]
    return InfeasibleDriftDiagnosis(
        name=name,
        cycle=DriftContradictionCycle(
            steps=[_drift_cycle_step_model(s) for s in cycle["steps"]],
            accumulated_bound=cycle["accumulated_bound"],
            contradiction=cycle["contradiction"],
        ),
    )


@app.post(
    "/api/v1/diagnose/drift",
    response_model=DriftDiagnosisResponse,
    tags=["diagnosis"],
)
def diagnose_drift_once(request: DriftDiagnosisRequest) -> DriftDiagnosisResponse:
    """时间漂移诊断：沿整条时间轴检查可行性并求查询时刻最紧偏移界。

    每个节点在各整数采样秒的偏移建模为时间展开差分变量；观测约束同一时刻
    两端点偏移之差，漂移约束相邻时刻同一节点偏移变化不超过
    ``rate * 时间差``（参考节点各时刻偏移恒为 0）。直接复用现有差分约束
    求解器：负环即整条时间轴不可行，矛盾环回指原观测 ID 或漂移时段标识；
    可行时按查询秒数与节点排序给出相对参考节点的最紧上下界。
    """
    timeline = sorted({o.second for o in request.observations} | set(request.query_seconds))
    result = diagnose_drift(
        nodes=request.effective_nodes(),
        reference=request.reference,
        observations=[_to_drift_observation(o) for o in request.observations],
        rates={r.node: r.rate for r in request.rates},
        timeline=timeline,
        query_seconds=request.query_seconds,
    )
    return _drift_diagnosis_model(request.name, result)


@app.post(
    "/api/v1/diagnose/repair",
    response_model=RepairResponse,
    tags=["diagnosis"],
)
def diagnose_repair(request: RepairRequest) -> RepairResponse:
    """修复建议：不可行时搜索最小观测删除集合，使剩余系统恢复可行。"""
    observations = [_to_observation(o) for o in request.observations]
    result = diagnose(
        nodes=request.effective_nodes(),
        reference=request.reference,
        observations=observations,
    )
    if result["feasible"]:
        # 系统本就可行：无需删除，建议为空
        return RepairResponse(
            name=request.name,
            max_repairs=request.max_repairs,
            removed_observation_ids=[],
            limit_reached=False,
            diagnosis=_diagnosis_model(request.name, result),
        )

    removed_ids, remaining = suggest_repair(
        nodes=request.effective_nodes(),
        reference=request.reference,
        observations=observations,
        max_repairs=request.max_repairs,
    )
    if removed_ids is None:
        # 上限内无可行修复：建议为空，回传原不可行诊断
        return RepairResponse(
            name=request.name,
            max_repairs=request.max_repairs,
            removed_observation_ids=[],
            limit_reached=True,
            diagnosis=_diagnosis_model(request.name, result),
        )

    # 修复后诊断：节点集合按「显式 nodes ∪ reference ∪ 剩余观测端点」重新计算
    repaired = diagnose(
        nodes=collect_nodes(request.nodes, request.reference, remaining),
        reference=request.reference,
        observations=remaining,
    )
    return RepairResponse(
        name=request.name,
        max_repairs=request.max_repairs,
        removed_observation_ids=removed_ids,
        limit_reached=False,
        diagnosis=_diagnosis_model(request.name, repaired),
    )


@app.post(
    "/api/v1/diagnose/calibration-plan",
    response_model=CalibrationPlanResponse,
    tags=["diagnosis"],
)
def diagnose_calibration_plan(request: CalibrationPlanRequest) -> CalibrationPlanResponse:
    """校准方案：从候选观测中选总成本最小的子集，使各目标节点界宽达标。

    基础观测本身矛盾时直接回传现有矛盾环（status=base_contradiction）；
    否则枚举候选子集，矛盾组合不可选，并要求加入后各目标节点相对参考
    节点的上下界均有限、宽度不超过给定上限；同成本先取条数少、再取
    ID 序列字典序小；无合格子集时 status=no_solution。
    """
    base_observations = [_to_observation(o) for o in request.observations]
    # 节点集合纳入所有候选端点：未被选中的候选端点不应出现在最终诊断中，
    # 最终诊断的节点集合在求解后按「基础观测 ∪ 被选候选 ∪ 显式 nodes」重算。
    nodes = request.effective_nodes()

    base_result = diagnose(
        nodes=nodes,
        reference=request.reference,
        observations=base_observations,
    )
    if not base_result["feasible"]:
        return CalibrationPlanResponse(
            name=request.name,
            status="base_contradiction",
            selected_observation_ids=[],
            total_cost=0,
            diagnosis=_diagnosis_model(request.name, base_result),
        )

    plan = plan_calibration(
        nodes=nodes,
        reference=request.reference,
        observations=base_observations,
        targets=[(t.node, t.max_width) for t in request.targets],
        candidates=[_to_candidate(c) for c in request.candidates],
    )
    if plan is None:
        return CalibrationPlanResponse(
            name=request.name,
            status="no_solution",
            selected_observation_ids=[],
            total_cost=0,
            diagnosis=None,
        )

    selected_ids, total_cost, selected_observations = plan
    final_nodes = collect_nodes(
        request.nodes, request.reference, base_observations + selected_observations
    )
    final_result = diagnose(
        nodes=final_nodes,
        reference=request.reference,
        observations=base_observations + selected_observations,
    )
    return CalibrationPlanResponse(
        name=request.name,
        status="optimal",
        selected_observation_ids=selected_ids,
        total_cost=total_cost,
        diagnosis=_diagnosis_model(request.name, final_result),
    )


@app.post(
    "/api/v1/diagnose/redundancy-audit",
    response_model=RedundancyAuditResponse,
    tags=["diagnosis"],
)
def diagnose_redundancy_audit(request: RedundancyAuditRequest) -> RedundancyAuditResponse:
    """冗余审计：逐条暂移观测，检验其区间是否已被其余观测蕴含。

    基础观测本身矛盾时直接回传现有矛盾环（status=base_contradiction），
    不做冗余判定；可行时对每条观测，在剩余观测推出的 target−source
    最紧下界不小于其 min、最紧上界不大于其 max 时标为冗余（任一界
    不可达或不满足则为非冗余）。冗余项附不含自身 ID 的上下界替代
    证据链及界值；结果按观测 ID 排序。
    """
    observations = [_to_observation(o) for o in request.observations]
    nodes = request.effective_nodes()

    base_result = diagnose(
        nodes=nodes,
        reference=request.reference,
        observations=observations,
    )
    if not base_result["feasible"]:
        return RedundancyAuditResponse(
            name=request.name,
            status="base_contradiction",
            results=[],
            diagnosis=_diagnosis_model(request.name, base_result),
        )

    items = audit_redundancy(
        nodes=nodes,
        reference=request.reference,
        observations=observations,
    )
    return RedundancyAuditResponse(
        name=request.name,
        status="ok",
        results=[RedundancyItem(**item) for item in items],
        diagnosis=None,
    )


@app.post(
    "/api/v1/events/window-peak",
    response_model=WindowPeakResponse,
    tags=["events"],
)
def events_window_peak(request: WindowPeakRequest) -> WindowPeakResponse:
    """窗口峰值：求可同时落入真实时间窗口的最大事件集合与可行偏移见证。

    真实时间 = 本地时间戳 − 节点偏移，参考节点偏移为 0。在满足全部观测的
    同一组偏移下，使落入窗口 [start, end] 的事件数最多；并列时选排序后
    事件 ID 序列字典序最小者。事件节点并入求解节点集。基础观测矛盾时
    返回 status=base_contradiction 及现有矛盾环，不做事件求解。
    """
    observations = [_to_observation(o) for o in request.observations]
    # 节点宇宙：显式 nodes ∪ reference ∪ 观测端点 ∪ 事件节点
    nodes = request.effective_nodes()

    base_result = diagnose(
        nodes=nodes,
        reference=request.reference,
        observations=observations,
    )
    if not base_result["feasible"]:
        return WindowPeakResponse(
            name=request.name,
            status="base_contradiction",
            max_in_window=None,
            selected_event_ids=[],
            offsets=None,
            diagnosis=_diagnosis_model(request.name, base_result),
        )

    selected_ids, offsets = plan_window_peak(
        nodes=nodes,
        reference=request.reference,
        observations=observations,
        events=[
            WindowEvent(id=e.id, node=e.node, timestamp=e.timestamp)
            for e in request.events
        ],
        start=request.window.start,
        end=request.window.end,
    )
    return WindowPeakResponse(
        name=request.name,
        status="ok",
        max_in_window=len(selected_ids),
        selected_event_ids=selected_ids,
        offsets=[
            NodeOffset(node=node, offset=offsets[node])
            for node in sorted(offsets)
        ],
        diagnosis=None,
    )
