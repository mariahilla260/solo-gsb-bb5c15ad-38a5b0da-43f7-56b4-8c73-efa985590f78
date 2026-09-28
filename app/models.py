"""请求 / 响应数据模型。

请求中观测区间字段按需求命名为 ``min`` / ``max``（Python 内建名冲突通过
字段别名安全处理），输出统一使用 camelCase 风格之外的 snake_case 直白命名。
"""

from __future__ import annotations

import math
from typing import Annotated, List, Literal, Optional, Protocol, Sequence, Union

from pydantic import BaseModel, BeforeValidator, Field, model_validator


class _HasEndpoints(Protocol):
    """鸭子类型：仅要求提供观测端点属性（source/target）。"""

    source: str
    target: str


def collect_nodes(
    explicit_nodes: Optional[Sequence[str]],
    reference: str,
    observations: Sequence[_HasEndpoints],
) -> List[str]:
    """合并显式节点、观测端点与参考节点，按字典序返回。"""
    nodes = set(explicit_nodes or [])
    nodes.add(reference)
    for obs in observations:
        nodes.add(obs.source)
        nodes.add(obs.target)
    return sorted(nodes)


class ObservationIn(BaseModel):
    """一条有向观测：target 时钟 - source 时钟 ∈ [min, max] 毫秒。"""

    id: str = Field(..., min_length=1, description="观测的唯一 ID")
    source: str = Field(..., min_length=1, description="被减时钟节点 A")
    target: str = Field(..., min_length=1, description="被减时钟节点 B")
    min: float = Field(..., alias="min", description="区间下界（毫秒）")
    max: float = Field(..., alias="max", description="区间上界（毫秒）")

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @model_validator(mode="after")
    def _check(self) -> "ObservationIn":
        if not math.isfinite(self.min) or not math.isfinite(self.max):
            raise ValueError("min/max 必须为有限数值")
        if self.min > self.max:
            raise ValueError(
                f"观测 {self.id} 的区间非法：min({self.min}) > max({self.max})"
            )
        return self


class DiagnosisRequest(BaseModel):
    """单条诊断请求。"""

    reference: str = Field(..., min_length=1, description="参考时钟节点")
    observations: List[ObservationIn] = Field(
        default_factory=list, description="有向时钟观测列表"
    )
    nodes: Optional[List[str]] = Field(
        default=None,
        description="显式声明的全部节点；缺省时取观测端点与参考节点的并集",
    )
    name: Optional[str] = Field(
        default=None, description="可选名称，诊断与批量接口会原样回显"
    )

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _check(self) -> "DiagnosisRequest":
        if self.nodes is not None:
            if len(self.nodes) != len(set(self.nodes)):
                raise ValueError("nodes 中存在重复节点")
            if self.reference not in self.nodes:
                raise ValueError("reference 必须包含在 nodes 中")

        ids = [o.id for o in self.observations]
        if len(ids) != len(set(ids)):
            raise ValueError("observations 中存在重复的观测 ID")
        return self

    def effective_nodes(self) -> List[str]:
        """合并显式节点、观测端点与参考节点。"""
        return collect_nodes(self.nodes, self.reference, self.observations)


class ProofStep(BaseModel):
    """证据链中的一步：沿链方向经过的一条约束边。"""

    observation_id: str = Field(..., description="该边来源的观测 ID")
    constraint: Literal["upper", "lower"] = Field(
        ..., description="upper=对应 max 约束边，lower=对应 -min 约束边"
    )
    from_: str = Field(..., alias="from", description="边的起点节点")
    to: str = Field(..., description="边的终点节点")
    weight: float = Field(..., description="该边在差分约束图中的权重（毫秒）")

    model_config = {"populate_by_name": True}


class NodeBounds(BaseModel):
    node: str
    lower: Optional[float] = Field(
        ..., description="x_node - x_reference 的最紧下界（毫秒），不可达时为 null"
    )
    upper: Optional[float] = Field(
        ..., description="x_node - x_reference 的最紧上界（毫秒），不可达时为 null"
    )
    lower_proof: Optional[List[ProofStep]] = Field(
        ...,
        description="下界证据链：从该节点走回参考节点的约束边序列，"
        "权重和的相反数等于 lower；参考节点为空链，下界不可达时为 null",
    )
    upper_proof: Optional[List[ProofStep]] = Field(
        ...,
        description="上界证据链：从参考节点走到该节点的约束边序列，"
        "权重和等于 upper；参考节点为空链，上界不可达时为 null",
    )
    reachable_from_reference: bool = Field(
        ..., description="参考节点能否沿约束边到达该节点（决定上界）"
    )
    can_reach_reference: bool = Field(
        ..., description="该节点能否沿约束边到达参考节点（决定下界）"
    )
    constrained: bool = Field(
        ..., description="是否可从参考节点在两个方向上同时约束（双向可达）"
    )


class CycleStep(BaseModel):
    observation_id: str
    from_: str = Field(..., serialization_alias="from")
    to: str
    constraint: Literal["upper", "lower"] = Field(
        ..., description="upper=对应 max 约束边，lower=对应 -min 约束边"
    )
    weight: float = Field(..., description="该边在差分约束图中的权重（毫秒）")
    bound: float = Field(..., description="观测区间对应的原始端点值（毫秒）")


class ContradictionCycle(BaseModel):
    steps: List[CycleStep] = Field(..., description="沿矛盾环方向排列的边")
    accumulated_bound: float = Field(
        ..., description="环上权重之和（负数），即推出的 0 <= 该负数"
    )
    contradiction: str = Field(..., description="人类可读的矛盾式，例如 '0 <= -2.0'")


class FeasibleDiagnosis(BaseModel):
    feasible: Literal[True] = True
    reference: str
    name: Optional[str] = None
    node_bounds: List[NodeBounds]
    unconstrained_nodes: List[str] = Field(
        ..., description="无法从参考节点双向约束（界不完整）的节点"
    )


class InfeasibleDiagnosis(BaseModel):
    feasible: Literal[False] = False
    name: Optional[str] = None
    cycle: ContradictionCycle


DiagnosisResponse = Union[FeasibleDiagnosis, InfeasibleDiagnosis]


class BatchDiagnosisRequest(BaseModel):
    """批量诊断请求：顺序提交，结果按下标稳定返回。"""

    diagnoses: List[DiagnosisRequest] = Field(..., min_length=1)

    model_config = {"extra": "forbid"}


class BatchItemResponse(BaseModel):
    index: int
    name: Optional[str] = None
    result: Union[FeasibleDiagnosis, InfeasibleDiagnosis] = Field(
        ..., discriminator="feasible"
    )


class BatchDiagnosisResponse(BaseModel):
    count: int
    results: List[BatchItemResponse]


class RepairRequest(DiagnosisRequest):
    """修复建议请求：在单条诊断请求的基础上增加删除上限。"""

    max_repairs: int = Field(
        default=3,
        ge=0,
        description="最多允许删除的观测条数（默认 3）；搜索开销随其组合式增长",
    )


class RepairResponse(BaseModel):
    """修复建议响应。

    ``diagnosis`` 为应用建议（删除 ``removed_observation_ids``）后的诊断摘要：
    原系统本就可行或修复成功时为可行结果；达到上限未找到修复时为原系统的
    不可行结果（含矛盾环）。
    """

    name: Optional[str] = None
    max_repairs: int = Field(..., description="本次生效的删除上限")
    removed_observation_ids: List[str] = Field(
        ...,
        description="建议删除的观测 ID（按字典序）；空列表表示无需删除或上限内无解",
    )
    limit_reached: bool = Field(
        ..., description="是否在 max_repairs 上限内未找到可行修复"
    )
    diagnosis: Union[FeasibleDiagnosis, InfeasibleDiagnosis] = Field(
        ..., discriminator="feasible", description="应用建议后的诊断摘要"
    )


class CalibrationTargetIn(BaseModel):
    """校准目标：要求该节点相对参考节点的界宽不超过 max_width（毫秒）。"""

    node: str = Field(..., min_length=1, description="目标时钟节点")
    max_width: float = Field(
        ..., ge=0, description="允许的最大偏移区间宽度 upper - lower（毫秒），须 ≥ 0"
    )

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _check(self) -> "CalibrationTargetIn":
        if not math.isfinite(self.max_width):
            raise ValueError("max_width 必须为有限数值")
        return self


class CalibrationCandidateIn(ObservationIn):
    """一条候选校准观测：在普通观测的基础上附带非负整数成本。"""

    cost: int = Field(
        ..., ge=0, description="选用该观测的成本（非负整数）"
    )


class CalibrationPlanRequest(DiagnosisRequest):
    """校准方案请求：沿用诊断字段，另给目标宽度与至多 12 条候选观测。"""

    targets: List[CalibrationTargetIn] = Field(
        ..., min_length=1, description="需要校准（收窄界宽）的目标节点列表"
    )
    candidates: List[CalibrationCandidateIn] = Field(
        default_factory=list,
        max_length=12,
        description="至多 12 条带唯一 ID、非负整数成本的候选校准观测",
    )

    @model_validator(mode="after")
    def _check(self) -> "CalibrationPlanRequest":
        # 复用基础诊断的全部校验（reference∈nodes、基础观测 ID 唯一等）。
        DiagnosisRequest._check(self)

        if len({t.node for t in self.targets}) != len(self.targets):
            raise ValueError("targets 中存在重复目标节点")

        candidate_ids = [c.id for c in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidates 中存在重复的候选观测 ID")

        base_ids = {o.id for o in self.observations}
        overlap = base_ids.intersection(candidate_ids)
        if overlap:
            raise ValueError(
                f"候选观测 ID 与基础观测 ID 冲突：{sorted(overlap)}"
            )
        return self

    def effective_nodes(self) -> List[str]:
        """合并显式节点、观测端点、参考节点与校准目标节点。

        目标节点即纳入节点宇宙（等价于在 nodes 中显式声明），
        加入观测后仍不可达时会在结果中表现为未约束 / 无解。
        """
        nodes = set(super().effective_nodes())
        nodes.update(t.node for t in self.targets)
        return sorted(nodes)


class CalibrationPlanResponse(BaseModel):
    """校准方案响应。

    * ``status="optimal"``：找到合格子集（空集也算），``selected_observation_ids`` /
      ``total_cost`` 给出选择，``diagnosis`` 为加入所选观测后的可行诊断；
    * ``status="base_contradiction"``：基础观测本身矛盾，直接回传其矛盾环
      （``selected_observation_ids`` 为空、``total_cost=0``）；
    * ``status="no_solution"``：基础可行但不存在合格子集（任何候选组合都会
      引入矛盾，或加入后仍无法让所有目标界有限且宽度达标）。
    """

    name: Optional[str] = None
    status: Literal["optimal", "base_contradiction", "no_solution"] = Field(
        ..., description="求解状态"
    )
    selected_observation_ids: List[str] = Field(
        ...,
        description="选中的候选校准观测 ID（按字典序）；基础矛盾或无解时为空列表",
    )
    total_cost: int = Field(
        ..., description="选中观测的成本之和；基础矛盾或无解时为 0"
    )
    diagnosis: Optional[Union[FeasibleDiagnosis, InfeasibleDiagnosis]] = Field(
        ...,
        discriminator="feasible",
        description="optimal 时为加入所选观测后的诊断；"
        "base_contradiction 时为基础观测的矛盾诊断；no_solution 时为 null",
    )


# ---------------------------------------------------------------------------
# 窗口峰值
# ---------------------------------------------------------------------------


class EventIn(BaseModel):
    """一条事件：真实时间 = 本地时间戳 timestamp − 发生节点的时钟偏移。"""

    id: str = Field(..., min_length=1, description="事件的唯一 ID")
    node: str = Field(..., min_length=1, description="事件发生的时钟节点")
    timestamp: float = Field(
        ..., description="节点本地时间戳（毫秒），须为有限数值"
    )

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _check(self) -> "EventIn":
        if not math.isfinite(self.timestamp):
            raise ValueError("timestamp 必须为有限数值")
        return self


class WindowIn(BaseModel):
    """真实时间窗口：有限闭区间 [start, end]（毫秒）。"""

    start: float = Field(..., description="窗口起点（含），须为有限数值")
    end: float = Field(..., description="窗口终点（含），须为有限数值")

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _check(self) -> "WindowIn":
        if not math.isfinite(self.start) or not math.isfinite(self.end):
            raise ValueError("start/end 必须为有限数值")
        if self.start > self.end:
            raise ValueError(
                f"窗口非法：start({self.start}) > end({self.end})"
            )
        return self


class WindowPeakRequest(DiagnosisRequest):
    """窗口峰值请求：沿用诊断字段，另给至多 12 条事件与有限闭区间窗口。"""

    events: List[EventIn] = Field(
        default_factory=list,
        max_length=12,
        description="至多 12 条事件（唯一 ID、节点、有限本地时间戳），默认空数组",
    )
    window: WindowIn = Field(..., description="真实时间窗口 [start, end]（闭区间）")

    @model_validator(mode="after")
    def _check(self) -> "WindowPeakRequest":
        # 复用基础诊断的全部校验（reference∈nodes、观测 ID 唯一等）。
        DiagnosisRequest._check(self)

        ids = [e.id for e in self.events]
        if len(ids) != len(set(ids)):
            raise ValueError("events 中存在重复的事件 ID")
        return self

    def effective_nodes(self) -> List[str]:
        """合并显式节点、观测端点、参考节点与事件节点。"""
        nodes = set(super().effective_nodes())
        nodes.update(e.node for e in self.events)
        return sorted(nodes)


class NodeOffset(BaseModel):
    """见证偏移：求解节点集中某节点的一个可行偏移取值（毫秒）。"""

    node: str
    offset: float = Field(..., description="该节点的偏移取值；参考节点恒为 0")


class WindowPeakResponse(BaseModel):
    """窗口峰值响应。

    * ``status="ok"``：``max_in_window`` 为可同时入窗的最大事件数，
      ``selected_event_ids`` 为并列最优中字典序最小的选中集合，
      ``offsets`` 为一组覆盖全部求解节点的可行偏移见证——满足全部观测、
      参考节点偏移为 0，且该见证下入窗事件恰为所选集合；
    * ``status="base_contradiction"``：基础观测本身矛盾，``diagnosis``
      回传与单条诊断一致的矛盾环，其余求解字段为空。
    """

    name: Optional[str] = None
    status: Literal["ok", "base_contradiction"] = Field(..., description="求解状态")
    max_in_window: Optional[int] = Field(
        ..., description="可同时入窗的最大事件数；base_contradiction 时为 null"
    )
    selected_event_ids: List[str] = Field(
        ..., description="选中的事件 ID（按字典序）；base_contradiction 时为空列表"
    )
    offsets: Optional[List[NodeOffset]] = Field(
        ...,
        description="覆盖全部求解节点的一组可行偏移见证（按节点字典序）；"
        "base_contradiction 时为 null",
    )
    diagnosis: Optional[Union[FeasibleDiagnosis, InfeasibleDiagnosis]] = Field(
        ...,
        discriminator="feasible",
        description="base_contradiction 时为基础观测的矛盾诊断；ok 时为 null",
    )


# ---------------------------------------------------------------------------
# 冗余审计
# ---------------------------------------------------------------------------


class RedundancyAuditRequest(DiagnosisRequest):
    """冗余审计请求：沿用单条诊断的全部字段，观测至多 30 条。"""

    observations: List[ObservationIn] = Field(
        default_factory=list,
        max_length=30,
        description="至多 30 条有向时钟观测；逐条审计其区间是否已被其余观测蕴含",
    )


class RedundancyItem(BaseModel):
    """单条观测的冗余审计结论。

    冗余（``redundant=true``）表示：把该观测暂移后，剩余观测推出的
    ``x_target - x_source`` 最紧下界不小于原 ``min``、最紧上界不大于
    原 ``max``，即该观测的区间已被其余观测蕴含，移除它不改变可行域。
    """

    observation_id: str = Field(..., description="被审计的观测 ID")
    redundant: bool = Field(
        ..., description="剩余观测是否已蕴含该观测的区间（该观测可移除而不损失信息）"
    )
    lower: Optional[float] = Field(
        ...,
        description="冗余时：剩余观测推出的 x_target - x_source 最紧下界（毫秒）；"
        "非冗余时为 null",
    )
    upper: Optional[float] = Field(
        ...,
        description="冗余时：剩余观测推出的 x_target - x_source 最紧上界（毫秒）；"
        "非冗余时为 null",
    )
    lower_proof: Optional[List[ProofStep]] = Field(
        ...,
        description="冗余时：下界替代证据链（从 target 走回 source 的约束边序列，"
        "权重和的相反数等于 lower，不含该观测自身 ID）；非冗余时为 null",
    )
    upper_proof: Optional[List[ProofStep]] = Field(
        ...,
        description="冗余时：上界替代证据链（从 source 走到 target 的约束边序列，"
        "权重和等于 upper，不含该观测自身 ID）；非冗余时为 null",
    )


class RedundancyAuditResponse(BaseModel):
    """冗余审计响应。

    * ``status="ok"``：基础观测可行，``results`` 按观测 ID 排序给出逐条
      冗余结论；
    * ``status="base_contradiction"``：基础观测本身矛盾，``diagnosis``
      回传与单条诊断一致的矛盾环，``results`` 为空，不做冗余判定。
    """

    name: Optional[str] = None
    status: Literal["ok", "base_contradiction"] = Field(..., description="求解状态")
    results: List[RedundancyItem] = Field(
        ...,
        description="逐条观测的冗余结论（按观测 ID 排序）；"
        "base_contradiction 时为空列表",
    )
    diagnosis: Optional[Union[FeasibleDiagnosis, InfeasibleDiagnosis]] = Field(
        ...,
        discriminator="feasible",
        description="base_contradiction 时为基础观测的矛盾诊断；ok 时为 null",
    )


# ---------------------------------------------------------------------------
# 时间漂移诊断
# ---------------------------------------------------------------------------


def _check_int_second(value, field: str) -> None:
    """秒数字段必须为整数（拒绝 bool、浮点、字符串等）且非负。

    Pydantic v2 默认会把 bool / 2.0 / "3" 宽松收窄为 int，这里显式收紧。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} 必须为整数秒数")
    if value < 0:
        raise ValueError(f"{field} 必须为非负整数秒数")


def _validate_second(value):
    """秒数字段的前置校验：只接受非负 int（拒绝 bool / 浮点 / 字符串）。

    必须在 Pydantic 的宽松 int 收窄之前执行（Pydantic v2 会把
    bool / 2.0 / "3" 默认接受为 int）。
    """
    _check_int_second(value, "second")
    return value


# 非负整数秒：经过前置严格类型校验的 int。
NonNegativeSecond = Annotated[int, BeforeValidator(_validate_second)]


class DriftObservationIn(BaseModel):
    """一条带整数采样秒数的有向观测。

    在 ``second`` 秒采样时刻满足
    ``x_target(second) - x_source(second) ∈ [min, max]``（毫秒）。
    """

    id: str = Field(..., min_length=1, description="观测的唯一 ID")
    source: str = Field(..., min_length=1, description="被减时钟节点 A")
    target: str = Field(..., min_length=1, description="被减时钟节点 B")
    second: NonNegativeSecond = Field(..., description="整数采样秒数（非负）")
    min: float = Field(..., alias="min", description="区间下界（毫秒）")
    max: float = Field(..., alias="max", description="区间上界（毫秒）")

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @model_validator(mode="after")
    def _check(self) -> "DriftObservationIn":
        if not math.isfinite(self.min) or not math.isfinite(self.max):
            raise ValueError("min/max 必须为有限数值")
        if self.min > self.max:
            raise ValueError(
                f"观测 {self.id} 的区间非法：min({self.min}) > max({self.max})"
            )
        return self


class NodeRateIn(BaseModel):
    """一个非参考节点的非负最大漂移速率（毫秒/秒）。"""

    node: str = Field(..., min_length=1, description="非参考时钟节点")
    rate: float = Field(..., description="最大漂移速率（毫秒/秒），须 ≥ 0 且有限")

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _check(self) -> "NodeRateIn":
        if not math.isfinite(self.rate) or self.rate < 0:
            raise ValueError(f"节点 {self.node} 的漂移速率必须为非负有限数值")
        return self


class DriftDiagnosisRequest(BaseModel):
    """时间漂移诊断请求。"""

    reference: str = Field(..., min_length=1, description="参考时钟节点（各时刻偏移恒为 0）")
    observations: List[DriftObservationIn] = Field(
        default_factory=list, description="带整数采样秒数的有向时钟观测列表"
    )
    rates: List[NodeRateIn] = Field(
        default_factory=list,
        description="各非参考节点的非负最大漂移速率（毫秒/秒），节点不可重复",
    )
    query_seconds: List[NonNegativeSecond] = Field(
        default_factory=list,
        max_length=8,
        description="至多 8 个整数查询秒数（非负、彼此不同），默认空数组",
    )
    nodes: Optional[List[str]] = Field(
        default=None,
        description="显式声明的全部节点；缺省时取观测端点与参考节点的并集",
    )
    name: Optional[str] = Field(
        default=None, description="可选名称，响应原样回显"
    )

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _check(self) -> "DriftDiagnosisRequest":
        if self.nodes is not None:
            if len(self.nodes) != len(set(self.nodes)):
                raise ValueError("nodes 中存在重复节点")
            if self.reference not in self.nodes:
                raise ValueError("reference 必须包含在 nodes 中")

        ids = [o.id for o in self.observations]
        if len(ids) != len(set(ids)):
            raise ValueError("observations 中存在重复的观测 ID")

        rate_nodes = [r.node for r in self.rates]
        if len(rate_nodes) != len(set(rate_nodes)):
            raise ValueError("rates 中存在重复的节点")

        if len(self.query_seconds) != len(set(self.query_seconds)):
            raise ValueError("query_seconds 中存在重复的查询秒数")

        # 观测与查询涉及的时刻合计至多 40 个不同整数秒。
        moments = {o.second for o in self.observations}
        moments.update(self.query_seconds)
        if len(moments) > 40:
            raise ValueError(
                f"观测与查询合计至多 40 个不同时刻，当前为 {len(moments)} 个"
            )

        # 速率完备性：节点宇宙（显式 nodes ∪ reference ∪ 观测端点）中的
        # 每个非参考节点都必须给出速率；参考节点与未知节点不允许给速率。
        universe = set(self.effective_nodes())
        missing = sorted(n for n in universe if n != self.reference and n not in set(rate_nodes))
        if missing:
            raise ValueError(f"缺少节点的漂移速率：{missing}")
        extra = sorted(n for n in rate_nodes if n not in universe)
        if extra:
            raise ValueError(f"速率声明了不在节点集合中的节点：{extra}")
        if self.reference in set(rate_nodes):
            raise ValueError("参考节点偏移恒为 0，不应提供漂移速率")
        return self

    def effective_nodes(self) -> List[str]:
        """合并显式节点、观测端点与参考节点。"""
        return collect_nodes(self.nodes, self.reference, self.observations)


class TimePoint(BaseModel):
    """时间展开图中的一个带时刻节点。"""

    node: str
    second: int = Field(..., description="整数采样/查询秒数")


class DriftObservationCycleStep(BaseModel):
    """矛盾环中来自一条原观测的约束边。"""

    type: Literal["observation"] = "observation"
    observation_id: str
    from_: TimePoint = Field(..., alias="from", description="边的起点（节点 + 秒数）")
    to: TimePoint = Field(..., description="边的终点（节点 + 秒数）")
    constraint: Literal["upper", "lower"] = Field(
        ..., description="upper=对应 max 约束边，lower=对应 -min 约束边"
    )
    weight: float = Field(..., description="该边在时间展开差分图中的权重（毫秒）")
    bound: float = Field(..., description="观测区间对应的原始端点值（毫秒）")

    model_config = {"populate_by_name": True}


class DriftSegmentCycleStep(BaseModel):
    """矛盾环中来自相邻时刻漂移限制的约束边。"""

    type: Literal["drift"] = "drift"
    segment_id: str = Field(
        ...,
        description="漂移时段标识 drift:<node>:<from_second>:<to_second>",
    )
    node: str
    from_second: int = Field(..., description="时段起点秒数")
    to_second: int = Field(..., description="时段终点秒数")
    constraint: Literal["forward", "backward"] = Field(
        ...,
        description="forward=偏移增量上界 x(t2)-x(t1)<=rate*Δt；"
        "backward=偏移增量下界 x(t1)-x(t2)<=rate*Δt",
    )
    weight: float = Field(..., description="该边权重 rate*Δt（毫秒）")
    bound: float = Field(..., description="允许的最大偏移变化量（毫秒）")


DriftCycleStep = Annotated[
    Union[DriftObservationCycleStep, DriftSegmentCycleStep],
    Field(discriminator="type"),
]


class DriftContradictionCycle(BaseModel):
    steps: List[DriftCycleStep] = Field(..., description="沿矛盾环方向排列的边")
    accumulated_bound: float = Field(
        ..., description="环上权重之和（负数），即推出的 0 <= 该负数"
    )
    contradiction: str = Field(..., description="人类可读的矛盾式，例如 '0 <= -2.0'")


class DriftNodeBounds(BaseModel):
    node: str
    lower: Optional[float] = Field(
        ..., description="x_node(second) - x_reference(second) 的最紧下界（毫秒），不可达时为 null"
    )
    upper: Optional[float] = Field(
        ..., description="x_node(second) - x_reference(second) 的最紧上界（毫秒），不可达时为 null"
    )


class DriftQueryResult(BaseModel):
    second: int = Field(..., description="查询秒数")
    node_bounds: List[DriftNodeBounds] = Field(
        ..., description="该时刻各节点相对参考节点的最紧上下界（按节点字典序）"
    )


class FeasibleDriftDiagnosis(BaseModel):
    feasible: Literal[True] = True
    reference: str
    name: Optional[str] = None
    timeline: List[int] = Field(
        ..., description="观测与查询涉及的全部不同时刻（按秒数升序）"
    )
    queries: List[DriftQueryResult] = Field(
        ..., description="各查询时刻的节点界（按秒数升序、节点字典序）"
    )


class InfeasibleDriftDiagnosis(BaseModel):
    feasible: Literal[False] = False
    name: Optional[str] = None
    cycle: DriftContradictionCycle


DriftDiagnosisResponse = Union[FeasibleDriftDiagnosis, InfeasibleDriftDiagnosis]
