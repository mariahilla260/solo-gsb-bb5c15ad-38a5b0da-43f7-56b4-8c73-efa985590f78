"""时间漂移诊断（时间展开差分约束）测试。"""

import itertools

from app.solver import DriftObservation, diagnose_drift, drift_segment_id


def _dobs(id_, source, target, second, lo, hi):
    return DriftObservation(
        id=id_, source=source, target=target, second=second, min=lo, max=hi
    )


def _query_map(result):
    """{second: {node: (lower, upper)}} 便于断言。"""
    return {
        q["second"]: {b["node"]: (b["lower"], b["upper"]) for b in q["node_bounds"]}
        for q in result["queries"]
    }


def test_drift_single_moment_reduces_to_plain_diagnosis():
    # 同一时刻的观测与普通诊断等价；速率不参与同刻约束。
    result = diagnose_drift(
        nodes=["A", "B", "C"],
        reference="A",
        observations=[
            _dobs("o1", "A", "B", 0, 1, 5),
            _dobs("o2", "B", "C", 0, 2, 4),
        ],
        rates={"B": 10.0, "C": 10.0},
        timeline=[0],
        query_seconds=[0],
    )
    assert result["feasible"] is True
    assert result["timeline"] == [0]
    bounds = _query_map(result)[0]
    assert bounds["A"] == (0.0, 0.0)
    assert bounds["B"] == (1.0, 5.0)
    assert bounds["C"] == (3.0, 9.0)


def test_drift_propagates_bounds_across_adjacent_seconds():
    # t=0: B-A ∈ [0,10]，速率 1 ms/s；t=10 时界应放宽到 [-10, 20]。
    result = diagnose_drift(
        nodes=["A", "B"],
        reference="A",
        observations=[_dobs("o0", "A", "B", 0, 0, 10)],
        rates={"B": 1.0},
        timeline=[0, 10],
        query_seconds=[0, 10],
    )
    assert result["feasible"] is True
    q = _query_map(result)
    assert q[0]["B"] == (0.0, 10.0)
    assert q[10]["B"] == (-10.0, 20.0)
    # 参考节点各时刻恒为 0（速率 0）
    assert q[10]["A"] == (0.0, 0.0)


def test_drift_chains_over_multiple_segments():
    # t=0: B-A ∈ [0,0]；速率 2 ms/s；相邻时刻界宽每跳放宽 4。
    result = diagnose_drift(
        nodes=["A", "B"],
        reference="A",
        observations=[_dobs("o0", "A", "B", 0, 0, 0)],
        rates={"B": 2.0},
        timeline=[0, 1, 2],
        query_seconds=[0, 1, 2],
    )
    q = _query_map(result)
    assert q[0]["B"] == (0.0, 0.0)
    assert q[1]["B"] == (-2.0, 2.0)
    assert q[2]["B"] == (-4.0, 4.0)


def test_drift_unreachable_node_has_null_bounds():
    # C 与参考节点在任何时刻都没有观测连通：双向不可达，界为 None。
    result = diagnose_drift(
        nodes=["A", "B", "C"],
        reference="A",
        observations=[_dobs("o0", "A", "B", 0, 0, 10)],
        rates={"B": 1.0, "C": 1.0},
        timeline=[0, 5],
        query_seconds=[5],
    )
    assert result["feasible"] is True
    assert _query_map(result)[5]["C"] == (None, None)


def test_drift_observation_can_reach_back_to_earlier_fix():
    # t=0 固定 B=0（相对 A），t=10 观测 B-A ∈ [3, 7]，速率 1（允许变化 10）：
    # 可行；t=10 的下界 3、上界 7 由观测主导而非漂移。
    result = diagnose_drift(
        nodes=["A", "B"],
        reference="A",
        observations=[
            _dobs("o0", "A", "B", 0, 0, 0),
            _dobs("o1", "A", "B", 10, 3, 7),
        ],
        rates={"B": 1.0},
        timeline=[0, 10],
        query_seconds=[10],
    )
    assert result["feasible"] is True
    assert _query_map(result)[10]["B"] == (3.0, 7.0)


def test_drift_infeasible_when_observations_move_faster_than_rate():
    # t=0: B-A ∈ [0,2]；t=1: B-A ∈ [10,12]；速率 5（最多变化 5）：
    # 至少变化 8 > 5，矛盾环必含漂移时段标识与两条观测。
    result = diagnose_drift(
        nodes=["A", "B"],
        reference="A",
        observations=[
            _dobs("o0", "A", "B", 0, 0, 2),
            _dobs("o1", "A", "B", 1, 10, 12),
        ],
        rates={"B": 5.0},
        timeline=[0, 1],
        query_seconds=[1],
    )
    assert result["feasible"] is False
    cycle = result["cycle"]
    assert cycle["accumulated_bound"] == -3.0
    assert cycle["contradiction"] == "0 <= -3.0"
    kinds = {s["type"] for s in cycle["steps"]}
    assert kinds == {"observation", "drift"}
    obs_ids = {s["observation_id"] for s in cycle["steps"] if s["type"] == "observation"}
    assert obs_ids == {"o0", "o1"}
    drift_steps = [s for s in cycle["steps"] if s["type"] == "drift"]
    # B 的漂移边 + 参考节点 A 的 0 速率边都应出现在闭合环上
    segments = {s["segment_id"] for s in drift_steps}
    assert drift_segment_id("B", 0, 1) in segments
    assert drift_segment_id("A", 0, 1) in segments
    # 环闭合：每步 from 等于上一步 to（时间点一致）
    for i, step in enumerate(cycle["steps"]):
        prev = cycle["steps"][i - 1]
        if step["type"] == "drift":
            prev_to = (
                (prev["to"]["node"], prev["to"]["second"])
                if prev["type"] == "observation"
                else (prev["node"], prev["to_second"])
            )
            cur_from = (step["node"], step["from_second"])
        else:
            prev_to = (
                (prev["to"]["node"], prev["to"]["second"])
                if prev["type"] == "observation"
                else (prev["node"], prev["to_second"])
            )
            cur_from = (step["from"]["node"], step["from"]["second"])
        assert prev_to == cur_from


def test_drift_pure_observation_contradiction_has_only_observation_steps():
    # 同一时刻观测互相矛盾：矛盾环只含观测边。
    result = diagnose_drift(
        nodes=["A", "B"],
        reference="A",
        observations=[
            _dobs("o1", "A", "B", 0, 0, 10),
            _dobs("o2", "B", "A", 0, 3, 4),
        ],
        rates={"B": 1.0},
        timeline=[0],
        query_seconds=[],
    )
    assert result["feasible"] is False
    assert {s["type"] for s in result["cycle"]["steps"]} == {"observation"}
    assert result["cycle"]["accumulated_bound"] == -3.0


def test_drift_drift_only_cycle_binds_node_to_reference():
    # 参考节点速率 0 与矛盾观测共同把矛盾沿漂移时段报出：
    # t=0: B-A=0；t=5: B-A=10；B 速率 1（5 秒最多变化 5）-> 不可行。
    result = diagnose_drift(
        nodes=["A", "B"],
        reference="A",
        observations=[
            _dobs("p0", "A", "B", 0, 0, 0),
            _dobs("p5", "A", "B", 5, 10, 10),
        ],
        rates={"B": 1.0},
        timeline=[0, 5],
        query_seconds=[5],
    )
    assert result["feasible"] is False
    assert result["cycle"]["accumulated_bound"] == -5.0


def test_drift_result_independent_of_observation_submission_order():
    obs = [
        _dobs("o0", "A", "B", 0, 0, 0),
        _dobs("o1", "A", "B", 1, 1, 1),
        _dobs("o2", "A", "B", 2, 2, 2),
    ]
    results = [
        diagnose_drift(
            nodes=["A", "B"],
            reference="A",
            observations=list(perm),
            rates={"B": 1.0},
            timeline=[0, 1, 2],
            query_seconds=[0, 1, 2],
        )
        for perm in itertools.permutations(obs)
    ]
    for r in results[1:]:
        assert r == results[0]


def test_drift_zero_rate_pins_offset_forever():
    # B 速率 0：t=0 的观测 B-A ∈ [1,3] 在所有时刻保持不变。
    result = diagnose_drift(
        nodes=["A", "B"],
        reference="A",
        observations=[_dobs("o0", "A", "B", 0, 1, 3)],
        rates={"B": 0.0},
        timeline=[0, 9],
        query_seconds=[9],
    )
    assert result["feasible"] is True
    assert _query_map(result)[9]["B"] == (1.0, 3.0)


def test_drift_query_without_any_observation_gives_null_for_non_reference():
    result = diagnose_drift(
        nodes=["A", "B"],
        reference="A",
        observations=[],
        rates={"B": 1.0},
        timeline=[3],
        query_seconds=[3],
    )
    assert result["feasible"] is True
    assert result["timeline"] == [3]
    q = _query_map(result)[3]
    assert q["A"] == (0.0, 0.0)
    assert q["B"] == (None, None)


def test_drift_segment_id_format():
    assert drift_segment_id("B", 3, 8) == "drift:B:3:8"
