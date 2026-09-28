"""FastAPI 接口测试。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_diagnose_feasible():
    payload = {
        "reference": "A",
        "nodes": ["A", "B", "C"],
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 1, "max": 5},
            {"id": "o2", "source": "B", "target": "C", "min": 2, "max": 4},
        ],
    }
    resp = client.post("/api/v1/diagnose", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["feasible"] is True
    bounds = {b["node"]: b for b in data["node_bounds"]}
    assert bounds["A"]["lower"] == 0.0 and bounds["A"]["upper"] == 0.0
    assert bounds["B"]["lower"] == 1.0 and bounds["B"]["upper"] == 5.0
    assert bounds["C"]["lower"] == 3.0 and bounds["C"]["upper"] == 9.0
    assert data["unconstrained_nodes"] == []


def test_diagnose_infeasible_cycle_uses_from_alias():
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 10},
            {"id": "o2", "source": "B", "target": "A", "min": 3, "max": 4},
        ],
    }
    resp = client.post("/api/v1/diagnose", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["feasible"] is False
    steps = data["cycle"]["steps"]
    assert data["cycle"]["accumulated_bound"] == -3.0
    assert data["cycle"]["contradiction"] == "0 <= -3.0"
    # 序列化别名为 from，且每步闭环
    assert set(steps[0].keys()) >= {"from", "to", "observation_id", "constraint"}
    for i, step in enumerate(steps):
        assert step["from"] == steps[i - 1]["to"]


def test_unconstrained_node_reported():
    payload = {
        "reference": "A",
        "nodes": ["A", "D"],
        "observations": [
            {"id": "solo", "source": "D", "target": "D", "min": -1, "max": 1}
        ],
    }
    resp = client.post("/api/v1/diagnose", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["unconstrained_nodes"] == ["D"]
    d = next(b for b in data["node_bounds"] if b["node"] == "D")
    assert d["lower"] is None and d["upper"] is None
    assert d["constrained"] is False


def test_proof_chains_walk_and_sum_to_bounds():
    payload = {
        "reference": "A",
        "nodes": ["A", "B", "C"],
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 1, "max": 5},
            {"id": "o2", "source": "B", "target": "C", "min": 2, "max": 4},
        ],
    }
    resp = client.post("/api/v1/diagnose", json=payload)
    assert resp.status_code == 200, resp.text
    bounds = {b["node"]: b for b in resp.json()["node_bounds"]}

    # 参考节点：空链
    assert bounds["A"]["lower_proof"] == [] and bounds["A"]["upper_proof"] == []

    # 单条观测同时给出两个方向的证据
    assert bounds["B"]["upper_proof"] == [
        {"observation_id": "o1", "constraint": "upper",
         "from": "A", "to": "B", "weight": 5.0}
    ]
    assert bounds["B"]["lower_proof"] == [
        {"observation_id": "o1", "constraint": "lower",
         "from": "B", "to": "A", "weight": -1.0}
    ]

    # 多步链：上界链从参考节点走到目标，权重和等于 upper
    assert bounds["C"]["upper_proof"] == [
        {"observation_id": "o1", "constraint": "upper",
         "from": "A", "to": "B", "weight": 5.0},
        {"observation_id": "o2", "constraint": "upper",
         "from": "B", "to": "C", "weight": 4.0},
    ]
    assert sum(s["weight"] for s in bounds["C"]["upper_proof"]) == bounds["C"]["upper"]
    # 下界链从目标走回参考节点，权重和的相反数等于 lower
    assert bounds["C"]["lower_proof"] == [
        {"observation_id": "o2", "constraint": "lower",
         "from": "C", "to": "B", "weight": -2.0},
        {"observation_id": "o1", "constraint": "lower",
         "from": "B", "to": "A", "weight": -1.0},
    ]
    assert sum(s["weight"] for s in bounds["C"]["lower_proof"]) == -bounds["C"]["lower"]


def test_proof_chains_closed_walks_for_all_nodes():
    # 任意可行结果：证据链首尾与衔接关系恒成立
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 1, "max": 5},
            {"id": "o2", "source": "B", "target": "C", "min": 2, "max": 4},
            {"id": "o3", "source": "A", "target": "C", "min": 0, "max": 8},
        ],
    }
    data = client.post("/api/v1/diagnose", json=payload).json()
    for b in data["node_bounds"]:
        up, lp = b["upper_proof"], b["lower_proof"]
        if up:
            assert up[0]["from"] == "A" and up[-1]["to"] == b["node"]
            for i in range(1, len(up)):
                assert up[i]["from"] == up[i - 1]["to"]
        if lp:
            assert lp[0]["from"] == b["node"] and lp[-1]["to"] == "A"
            for i in range(1, len(lp)):
                assert lp[i]["from"] == lp[i - 1]["to"]


def test_proof_tie_break_picks_lexicographically_smallest():
    # A->B->D 与 A->C->D 上界等权（均为 4）；下界 D->B->A 与 D->C->A 等权（均为 0）
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 2},
            {"id": "o2", "source": "A", "target": "C", "min": 0, "max": 1},
            {"id": "o3", "source": "B", "target": "D", "min": 0, "max": 2},
            {"id": "o4", "source": "C", "target": "D", "min": 0, "max": 3},
        ],
    }
    data = client.post("/api/v1/diagnose", json=payload).json()
    d = next(b for b in data["node_bounds"] if b["node"] == "D")
    assert d["upper"] == 4.0 and d["lower"] == 0.0
    # 首步四元组 ("o1",...) < ("o2",...)，上界链走 B
    assert [s["observation_id"] for s in d["upper_proof"]] == ["o1", "o3"]
    # 首步四元组 ("o3",...) < ("o4",...)，下界链走 B
    assert [s["observation_id"] for s in d["lower_proof"]] == ["o3", "o1"]


def test_proof_tie_break_decided_at_later_step():
    # 两条等权上界链共享首步 o1，第二步 ("o2",...) < ("o3",...) 决胜
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 1},
            {"id": "o2", "source": "B", "target": "C", "min": 0, "max": 1},
            {"id": "o3", "source": "B", "target": "D", "min": 0, "max": 1},
            {"id": "o4", "source": "C", "target": "E", "min": 0, "max": 1},
            {"id": "o5", "source": "D", "target": "E", "min": 0, "max": 1},
        ],
    }
    data = client.post("/api/v1/diagnose", json=payload).json()
    e = next(b for b in data["node_bounds"] if b["node"] == "E")
    assert e["upper"] == 3.0
    assert [s["observation_id"] for s in e["upper_proof"]] == ["o1", "o2", "o4"]


def test_proofs_independent_of_observation_submission_order():
    observations = [
        {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 2},
        {"id": "o2", "source": "A", "target": "C", "min": 0, "max": 1},
        {"id": "o3", "source": "B", "target": "D", "min": 0, "max": 2},
        {"id": "o4", "source": "C", "target": "D", "min": 0, "max": 3},
    ]
    orders = [
        observations,
        observations[::-1],
        [observations[2], observations[0], observations[3], observations[1]],
    ]
    results = [
        client.post("/api/v1/diagnose",
                    json={"reference": "A", "observations": obs}).json()
        for obs in orders
    ]
    for r in results[1:]:
        assert r["node_bounds"] == results[0]["node_bounds"]


def test_unreachable_bound_proofs_are_null():
    payload = {
        "reference": "A",
        "nodes": ["A", "D"],
        "observations": [
            {"id": "solo", "source": "D", "target": "D", "min": -1, "max": 1}
        ],
    }
    data = client.post("/api/v1/diagnose", json=payload).json()
    d = next(b for b in data["node_bounds"] if b["node"] == "D")
    assert d["lower"] is None and d["lower_proof"] is None
    assert d["upper"] is None and d["upper_proof"] is None
    a = next(b for b in data["node_bounds"] if b["node"] == "A")
    assert a["lower_proof"] == [] and a["upper_proof"] == []


def test_infeasible_response_has_no_proof_fields():
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 10},
            {"id": "o2", "source": "B", "target": "A", "min": 3, "max": 4},
        ],
    }
    data = client.post("/api/v1/diagnose", json=payload).json()
    assert data["feasible"] is False
    assert "node_bounds" not in data
    assert data["cycle"]["accumulated_bound"] == -3.0


def test_repair_feasible_diagnosis_carries_proofs():
    # 修复后的可行诊断与单条诊断结构一致（含证据链），删除语义不变
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 10},
            {"id": "o2", "source": "B", "target": "A", "min": 3, "max": 4},
        ],
    }
    data = client.post("/api/v1/diagnose/repair", json=payload).json()
    assert data["removed_observation_ids"] == ["o1"]
    diag = data["diagnosis"]
    assert diag["feasible"] is True
    b = next(x for x in diag["node_bounds"] if x["node"] == "B")
    # 剩余 o2：x_A - x_B ∈ [3, 4] -> x_B - x_A ∈ [-4, -3]
    assert b["lower"] == -4.0 and b["upper"] == -3.0
    assert b["upper_proof"] == [
        {"observation_id": "o2", "constraint": "lower",
         "from": "A", "to": "B", "weight": -3.0}
    ]
    assert b["lower_proof"] == [
        {"observation_id": "o2", "constraint": "upper",
         "from": "B", "to": "A", "weight": 4.0}
    ]


def test_invalid_interval_rejected():
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 9, "max": 1}
        ],
    }
    resp = client.post("/api/v1/diagnose", json=payload)
    assert resp.status_code == 422


def test_duplicate_observation_id_rejected():
    payload = {
        "reference": "A",
        "observations": [
            {"id": "x", "source": "A", "target": "B", "min": 0, "max": 1},
            {"id": "x", "source": "B", "target": "A", "min": 0, "max": 1},
        ],
    }
    resp = client.post("/api/v1/diagnose", json=payload)
    assert resp.status_code == 422


def test_reference_missing_from_nodes_rejected():
    payload = {"reference": "Z", "nodes": ["A", "B"], "observations": []}
    resp = client.post("/api/v1/diagnose", json=payload)
    assert resp.status_code == 422


def test_nodes_default_to_observation_endpoints():
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 1}
        ],
    }
    data = client.post("/api/v1/diagnose", json=payload).json()
    assert [b["node"] for b in data["node_bounds"]] == ["A", "B"]


def test_batch_order_and_index_stable():
    payload = {
        "diagnoses": [
            {
                "name": "ok",
                "reference": "A",
                "observations": [
                    {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 1}
                ],
            },
            {
                "name": "bad",
                "reference": "A",
                "observations": [
                    {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 1},
                    {"id": "o2", "source": "B", "target": "A", "min": 2, "max": 3},
                ],
            },
        ]
    }
    resp = client.post("/api/v1/diagnose/batch", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["count"] == 2
    assert data["results"][0]["index"] == 0
    assert data["results"][0]["name"] == "ok"
    assert data["results"][0]["result"]["feasible"] is True
    assert data["results"][1]["index"] == 1
    assert data["results"][1]["name"] == "bad"
    assert data["results"][1]["result"]["feasible"] is False


def test_batch_empty_rejected():
    resp = client.post("/api/v1/diagnose/batch", json={"diagnoses": []})
    assert resp.status_code == 422


def test_repair_feasible_returns_empty_suggestion():
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 1, "max": 5}
        ],
    }
    resp = client.post("/api/v1/diagnose/repair", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["removed_observation_ids"] == []
    assert data["limit_reached"] is False
    assert data["max_repairs"] == 3  # 默认上限
    assert data["diagnosis"]["feasible"] is True


def test_repair_infeasible_minimal_deletion():
    payload = {
        "name": "fixme",
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 10},
            {"id": "o2", "source": "B", "target": "A", "min": 3, "max": 4},
        ],
    }
    resp = client.post("/api/v1/diagnose/repair", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "fixme"
    assert data["removed_observation_ids"] == ["o1"]
    assert data["limit_reached"] is False
    diag = data["diagnosis"]
    assert diag["feasible"] is True
    assert diag["name"] == "fixme"
    bounds = {b["node"]: b for b in diag["node_bounds"]}
    # 剩余 o2：x_A - x_B ∈ [3, 4] -> x_B - x_A ∈ [-4, -3]
    assert bounds["B"]["lower"] == -4.0 and bounds["B"]["upper"] == -3.0


def test_repair_default_max_repairs_allows_two_deletions():
    # 两对独立矛盾需删 2 条，默认上限 3 足够
    payload = {
        "reference": "A",
        "observations": [
            {"id": "a1", "source": "A", "target": "B", "min": 0, "max": 10},
            {"id": "a2", "source": "B", "target": "A", "min": 3, "max": 4},
            {"id": "b1", "source": "C", "target": "D", "min": 0, "max": 10},
            {"id": "b2", "source": "D", "target": "C", "min": 3, "max": 4},
        ],
    }
    resp = client.post("/api/v1/diagnose/repair", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["max_repairs"] == 3
    assert data["removed_observation_ids"] == ["a1", "b1"]
    assert data["limit_reached"] is False
    assert data["diagnosis"]["feasible"] is True


def test_repair_limit_reached_returns_original_infeasible_diagnosis():
    payload = {
        "reference": "A",
        "max_repairs": 1,
        "observations": [
            {"id": "a1", "source": "A", "target": "B", "min": 0, "max": 10},
            {"id": "a2", "source": "B", "target": "A", "min": 3, "max": 4},
            {"id": "b1", "source": "C", "target": "D", "min": 0, "max": 10},
            {"id": "b2", "source": "D", "target": "C", "min": 3, "max": 4},
        ],
    }
    resp = client.post("/api/v1/diagnose/repair", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["removed_observation_ids"] == []
    assert data["limit_reached"] is True
    diag = data["diagnosis"]
    assert diag["feasible"] is False
    assert diag["cycle"]["accumulated_bound"] < 0


def test_repair_max_repairs_zero_on_infeasible():
    payload = {
        "reference": "A",
        "max_repairs": 0,
        "observations": [
            {"id": "bad", "source": "A", "target": "A", "min": 1, "max": 2}
        ],
    }
    resp = client.post("/api/v1/diagnose/repair", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["limit_reached"] is True
    assert data["removed_observation_ids"] == []
    assert data["diagnosis"]["feasible"] is False


def test_repair_negative_max_repairs_rejected():
    payload = {"reference": "A", "max_repairs": -1, "observations": []}
    resp = client.post("/api/v1/diagnose/repair", json=payload)
    assert resp.status_code == 422


def test_repair_unknown_field_rejected():
    payload = {"reference": "A", "observations": [], "bogus": 1}
    resp = client.post("/api/v1/diagnose/repair", json=payload)
    assert resp.status_code == 422


def test_repair_dropped_observation_node_vanishes():
    # bad 为 C 上的自环矛盾；删除后 C 不再是剩余系统的一部分
    payload = {
        "reference": "A",
        "observations": [
            {"id": "ok", "source": "A", "target": "B", "min": 0, "max": 1},
            {"id": "bad", "source": "C", "target": "C", "min": 1, "max": 2},
        ],
    }
    resp = client.post("/api/v1/diagnose/repair", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["removed_observation_ids"] == ["bad"]
    diag = data["diagnosis"]
    assert diag["feasible"] is True
    assert [b["node"] for b in diag["node_bounds"]] == ["A", "B"]


def test_repair_explicit_nodes_are_retained():
    # 显式声明的节点在修复后保留，被删观测的节点变为不受约束
    payload = {
        "reference": "A",
        "nodes": ["A", "B", "C"],
        "observations": [
            {"id": "ok", "source": "A", "target": "B", "min": 0, "max": 1},
            {"id": "bad", "source": "C", "target": "C", "min": 1, "max": 2},
        ],
    }
    resp = client.post("/api/v1/diagnose/repair", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["removed_observation_ids"] == ["bad"]
    diag = data["diagnosis"]
    assert [b["node"] for b in diag["node_bounds"]] == ["A", "B", "C"]
    assert diag["unconstrained_nodes"] == ["C"]


# ---------------------------------------------------------------------------
# 校准方案
# ---------------------------------------------------------------------------


def test_calibration_plan_optimal_combination():
    payload = {
        "reference": "A",
        "observations": [
            {"id": "base", "source": "A", "target": "B", "min": 0, "max": 10}
        ],
        "targets": [{"node": "B", "max_width": 3}],
        "candidates": [
            {"id": "exact", "source": "A", "target": "B",
             "min": 2, "max": 2, "cost": 10},
            {"id": "lo", "source": "A", "target": "B",
             "min": 0, "max": 4, "cost": 1},
            {"id": "hi", "source": "B", "target": "A",
             "min": -6, "max": -2, "cost": 1},
        ],
    }
    resp = client.post("/api/v1/diagnose/calibration-plan", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "optimal"
    assert data["selected_observation_ids"] == ["hi", "lo"]
    assert data["total_cost"] == 2
    diag = data["diagnosis"]
    assert diag["feasible"] is True
    bounds = {b["node"]: b for b in diag["node_bounds"]}
    # B-A ∈ [0,10] ∩ [0,4] ∩ [2,6] = [2,4]，宽 2
    assert bounds["B"]["lower"] == 2.0 and bounds["B"]["upper"] == 4.0


def test_calibration_plan_cost_beats_count():
    payload = {
        "reference": "A",
        "observations": [
            {"id": "base", "source": "A", "target": "B", "min": 0, "max": 10}
        ],
        "targets": [{"node": "B", "max_width": 3}],
        "candidates": [
            {"id": "exact", "source": "A", "target": "B",
             "min": 2, "max": 2, "cost": 10},
            {"id": "lo", "source": "A", "target": "B",
             "min": 0, "max": 4, "cost": 1},
            {"id": "hi", "source": "B", "target": "A",
             "min": -6, "max": -2, "cost": 1},
        ],
    }
    data = client.post("/api/v1/diagnose/calibration-plan", json=payload).json()
    # 成本 1+1=2 < exact 的 10，尽管条数更多
    assert data["status"] == "optimal"
    assert data["selected_observation_ids"] == ["hi", "lo"]


def test_calibration_plan_empty_selection_when_already_qualified():
    payload = {
        "reference": "A",
        "observations": [
            {"id": "base", "source": "A", "target": "B", "min": 0, "max": 2}
        ],
        "targets": [{"node": "B", "max_width": 2}],
        "candidates": [
            {"id": "exact", "source": "A", "target": "B",
             "min": 1, "max": 1, "cost": 1}
        ],
    }
    data = client.post("/api/v1/diagnose/calibration-plan", json=payload).json()
    assert data["status"] == "optimal"
    assert data["selected_observation_ids"] == []
    assert data["total_cost"] == 0
    assert data["diagnosis"]["feasible"] is True


def test_calibration_plan_base_contradiction():
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 10},
            {"id": "o2", "source": "B", "target": "A", "min": 3, "max": 4},
        ],
        "targets": [{"node": "B", "max_width": 1}],
        "candidates": [
            {"id": "exact", "source": "A", "target": "B",
             "min": 2, "max": 2, "cost": 1}
        ],
    }
    data = client.post("/api/v1/diagnose/calibration-plan", json=payload).json()
    assert data["status"] == "base_contradiction"
    assert data["selected_observation_ids"] == []
    assert data["total_cost"] == 0
    assert data["diagnosis"]["feasible"] is False
    assert data["diagnosis"]["cycle"]["accumulated_bound"] == -3.0


def test_calibration_plan_no_solution_when_unreachable():
    # C 为已声明目标，但无基础观测、无候选可将其与 A 连通 -> 无解
    payload = {
        "reference": "A",
        "nodes": ["A", "C"],
        "observations": [],
        "targets": [{"node": "C", "max_width": 1}],
        "candidates": [],
    }
    data = client.post("/api/v1/diagnose/calibration-plan", json=payload).json()
    assert data["status"] == "no_solution"
    assert data["selected_observation_ids"] == []
    assert data["total_cost"] == 0
    assert data["diagnosis"] is None


def test_calibration_plan_contradictory_candidate_unselectable():
    # 候选与基础观测矛盾（B-A ∈ [11,20] ∩ [0,10] 为空），无解
    payload = {
        "reference": "A",
        "observations": [
            {"id": "base", "source": "A", "target": "B", "min": 0, "max": 10}
        ],
        "targets": [{"node": "B", "max_width": 3}],
        "candidates": [
            {"id": "bad", "source": "B", "target": "A",
             "min": 11, "max": 20, "cost": 0}
        ],
    }
    data = client.post("/api/v1/diagnose/calibration-plan", json=payload).json()
    assert data["status"] == "no_solution"
    assert data["diagnosis"] is None


def test_calibration_plan_final_diagnosis_excludes_unselected_endpoints():
    payload = {
        "reference": "A",
        "observations": [
            {"id": "base", "source": "A", "target": "B", "min": 0, "max": 10}
        ],
        "targets": [{"node": "B", "max_width": 3}],
        "candidates": [
            {"id": "c1", "source": "A", "target": "B",
             "min": 0, "max": 1, "cost": 1},
            {"id": "unused", "source": "C", "target": "D",
             "min": 0, "max": 0, "cost": 0},
        ],
    }
    data = client.post("/api/v1/diagnose/calibration-plan", json=payload).json()
    assert data["status"] == "optimal"
    assert data["selected_observation_ids"] == ["c1"]
    # C/D 仅出现在未选中的候选中，最终诊断不包含它们
    assert [b["node"] for b in data["diagnosis"]["node_bounds"]] == ["A", "B"]


def test_calibration_plan_duplicate_target_rejected():
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 2}
        ],
        "targets": [
            {"node": "B", "max_width": 1},
            {"node": "B", "max_width": 2},
        ],
        "candidates": [],
    }
    resp = client.post("/api/v1/diagnose/calibration-plan", json=payload)
    assert resp.status_code == 422


def test_calibration_plan_empty_targets_rejected():
    payload = {
        "reference": "A",
        "observations": [],
        "targets": [],
        "candidates": [],
    }
    resp = client.post("/api/v1/diagnose/calibration-plan", json=payload)
    assert resp.status_code == 422


def test_calibration_plan_duplicate_candidate_id_rejected():
    payload = {
        "reference": "A",
        "observations": [],
        "targets": [{"node": "B", "max_width": 1}],
        "candidates": [
            {"id": "x", "source": "A", "target": "B",
             "min": 0, "max": 1, "cost": 1},
            {"id": "x", "source": "A", "target": "B",
             "min": 0, "max": 1, "cost": 2},
        ],
    }
    resp = client.post("/api/v1/diagnose/calibration-plan", json=payload)
    assert resp.status_code == 422


def test_calibration_plan_candidate_id_collides_base_rejected():
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 5}
        ],
        "targets": [{"node": "B", "max_width": 1}],
        "candidates": [
            {"id": "o1", "source": "A", "target": "B",
             "min": 0, "max": 1, "cost": 1}
        ],
    }
    resp = client.post("/api/v1/diagnose/calibration-plan", json=payload)
    assert resp.status_code == 422


def test_calibration_plan_negative_cost_rejected():
    payload = {
        "reference": "A",
        "observations": [],
        "targets": [{"node": "B", "max_width": 1}],
        "candidates": [
            {"id": "c1", "source": "A", "target": "B",
             "min": 0, "max": 1, "cost": -1}
        ],
    }
    resp = client.post("/api/v1/diagnose/calibration-plan", json=payload)
    assert resp.status_code == 422


def test_calibration_plan_non_integer_cost_rejected():
    payload = {
        "reference": "A",
        "observations": [],
        "targets": [{"node": "B", "max_width": 1}],
        "candidates": [
            {"id": "c1", "source": "A", "target": "B",
             "min": 0, "max": 1, "cost": 1.5}
        ],
    }
    resp = client.post("/api/v1/diagnose/calibration-plan", json=payload)
    assert resp.status_code == 422


def test_calibration_plan_negative_max_width_rejected():
    payload = {
        "reference": "A",
        "observations": [],
        "targets": [{"node": "A", "max_width": -1}],
        "candidates": [],
    }
    resp = client.post("/api/v1/diagnose/calibration-plan", json=payload)
    assert resp.status_code == 422


def test_calibration_plan_too_many_candidates_rejected():
    payload = {
        "reference": "A",
        "observations": [],
        "targets": [{"node": "B", "max_width": 1}],
        "candidates": [
            {"id": f"c{i:02d}", "source": "A", "target": "B",
             "min": 0, "max": 1, "cost": 1}
            for i in range(13)
        ],
    }
    resp = client.post("/api/v1/diagnose/calibration-plan", json=payload)
    assert resp.status_code == 422


def test_calibration_plan_unknown_field_rejected():
    payload = {
        "reference": "A",
        "observations": [],
        "targets": [{"node": "A", "max_width": 1}],
        "candidates": [],
        "bogus": 1,
    }
    resp = client.post("/api/v1/diagnose/calibration-plan", json=payload)
    assert resp.status_code == 422


def test_calibration_plan_candidate_connects_new_target():
    # C 原本不存在于任何基础观测；通过显式 nodes 声明目标，
    # 候选 A->C 将其连通并收窄
    payload = {
        "reference": "A",
        "nodes": ["A", "C"],
        "observations": [],
        "targets": [{"node": "C", "max_width": 2}],
        "candidates": [
            {"id": "link", "source": "A", "target": "C",
             "min": 1, "max": 3, "cost": 4}
        ],
    }
    data = client.post("/api/v1/diagnose/calibration-plan", json=payload).json()
    assert data["status"] == "optimal"
    assert data["selected_observation_ids"] == ["link"]
    assert data["total_cost"] == 4
    bounds = {b["node"]: b for b in data["diagnosis"]["node_bounds"]}
    assert bounds["C"]["lower"] == 1.0 and bounds["C"]["upper"] == 3.0


# ---------------------------------------------------------------------------
# 窗口峰值
# ---------------------------------------------------------------------------


def _window_payload(**overrides):
    payload = {
        "reference": "A",
        "observations": [
            {"id": "sync", "source": "A", "target": "B", "min": 0, "max": 100}
        ],
        "events": [
            {"id": "e1", "node": "B", "timestamp": 1000},
            {"id": "e2", "node": "B", "timestamp": 1010},
        ],
        "window": {"start": 990, "end": 995},
    }
    payload.update(overrides)
    return payload


def test_window_peak_all_events_fit():
    payload = _window_payload(
        events=[
            {"id": "e1", "node": "B", "timestamp": 1000},
            {"id": "e2", "node": "B", "timestamp": 1003},
        ]
    )
    resp = client.post("/api/v1/events/window-peak", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert data["max_in_window"] == 2
    assert data["selected_event_ids"] == ["e1", "e2"]
    assert data["diagnosis"] is None
    offsets = {o["node"]: o["offset"] for o in data["offsets"]}
    assert offsets["A"] == 0.0
    assert 990 <= 1000 - offsets["B"] <= 995
    assert 990 <= 1003 - offsets["B"] <= 995


def test_window_peak_shared_clock_cannot_cooccur():
    # 两条事件共享同一时钟偏移 x_B，不能同时入窗；并列时选 ID 字典序最小者
    resp = client.post("/api/v1/events/window-peak", json=_window_payload())
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert data["max_in_window"] == 1
    assert data["selected_event_ids"] == ["e1"]
    offsets = {o["node"]: o["offset"] for o in data["offsets"]}
    assert 990 <= 1000 - offsets["B"] <= 995
    assert not (990 <= 1010 - offsets["B"] <= 995)


def test_window_peak_witness_in_window_exactly_selected():
    # 三事件两两可同窗、三者不可同窗；见证中入窗事件须恰为所选集合
    payload = _window_payload(
        events=[
            {"id": "e3", "node": "B", "timestamp": 1010},
            {"id": "e1", "node": "B", "timestamp": 1000},
            {"id": "e2", "node": "B", "timestamp": 1005},
        ],
        window={"start": 990, "end": 997},
    )
    data = client.post("/api/v1/events/window-peak", json=payload).json()
    assert data["max_in_window"] == 2
    assert data["selected_event_ids"] == ["e1", "e2"]
    offsets = {o["node"]: o["offset"] for o in data["offsets"]}
    in_window = [
        e["id"]
        for e in payload["events"]
        if 990 <= e["timestamp"] - offsets["B"] <= 997
    ]
    assert in_window == ["e1", "e2"]


def test_window_peak_result_independent_of_event_order():
    events = [
        {"id": "e1", "node": "B", "timestamp": 1000},
        {"id": "e2", "node": "B", "timestamp": 1005},
        {"id": "e3", "node": "B", "timestamp": 1010},
    ]
    results = [
        client.post(
            "/api/v1/events/window-peak",
            json=_window_payload(events=order, window={"start": 990, "end": 997}),
        ).json()
        for order in [events, events[::-1], [events[1], events[2], events[0]]]
    ]
    for r in results[1:]:
        assert r["selected_event_ids"] == results[0]["selected_event_ids"] == ["e1", "e2"]
        assert r["offsets"] == results[0]["offsets"]


def test_window_peak_empty_events_returns_zero():
    payload = _window_payload(
        observations=[{"id": "o1", "source": "A", "target": "B", "min": 1, "max": 5}],
        events=[],
        window={"start": 0, "end": 10},
    )
    data = client.post("/api/v1/events/window-peak", json=payload).json()
    assert data["status"] == "ok"
    assert data["max_in_window"] == 0
    assert data["selected_event_ids"] == []
    offsets = {o["node"]: o["offset"] for o in data["offsets"]}
    assert set(offsets) == {"A", "B"}
    assert 1 <= offsets["B"] - offsets["A"] <= 5


def test_window_peak_base_contradiction_returns_cycle():
    payload = _window_payload(
        observations=[
            {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 10},
            {"id": "o2", "source": "B", "target": "A", "min": 3, "max": 4},
        ],
        events=[{"id": "e1", "node": "B", "timestamp": 100}],
        window={"start": 90, "end": 110},
    )
    resp = client.post("/api/v1/events/window-peak", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "base_contradiction"
    assert data["max_in_window"] is None
    assert data["selected_event_ids"] == []
    assert data["offsets"] is None
    diag = data["diagnosis"]
    assert diag["feasible"] is False
    assert diag["cycle"]["accumulated_bound"] == -3.0
    assert diag["cycle"]["contradiction"] == "0 <= -3.0"


def test_window_peak_event_node_merged_into_offsets():
    # Z 只出现在事件中：并入求解节点集，见证偏移须覆盖它
    payload = _window_payload(
        observations=[],
        events=[{"id": "e1", "node": "Z", "timestamp": 7}],
        window={"start": 5, "end": 10},
    )
    data = client.post("/api/v1/events/window-peak", json=payload).json()
    assert data["max_in_window"] == 1
    offsets = {o["node"]: o["offset"] for o in data["offsets"]}
    assert set(offsets) == {"A", "Z"}
    assert 5 <= 7 - offsets["Z"] <= 10


def test_window_peak_offsets_cover_explicit_nodes_and_satisfy_observations():
    payload = _window_payload(
        nodes=["A", "B", "C"],
        observations=[
            {"id": "o1", "source": "A", "target": "B", "min": 1, "max": 5},
            {"id": "o2", "source": "B", "target": "C", "min": 2, "max": 4},
        ],
        events=[{"id": "e1", "node": "C", "timestamp": 100}],
        window={"start": 90, "end": 200},
    )
    data = client.post("/api/v1/events/window-peak", json=payload).json()
    assert data["selected_event_ids"] == ["e1"]
    offsets = {o["node"]: o["offset"] for o in data["offsets"]}
    assert set(offsets) == {"A", "B", "C"}
    assert offsets["A"] == 0.0
    assert 1 <= offsets["B"] - offsets["A"] <= 5
    assert 2 <= offsets["C"] - offsets["B"] <= 4
    assert 90 <= 100 - offsets["C"] <= 200


def test_window_peak_name_echoed():
    payload = _window_payload(name="peak-1")
    data = client.post("/api/v1/events/window-peak", json=payload).json()
    assert data["name"] == "peak-1"


def test_window_peak_too_many_events_rejected():
    payload = _window_payload(
        events=[
            {"id": f"e{i:02d}", "node": "B", "timestamp": 1000 + i}
            for i in range(13)
        ]
    )
    resp = client.post("/api/v1/events/window-peak", json=payload)
    assert resp.status_code == 422


def test_window_peak_duplicate_event_id_rejected():
    payload = _window_payload(
        events=[
            {"id": "x", "node": "B", "timestamp": 1},
            {"id": "x", "node": "B", "timestamp": 2},
        ]
    )
    resp = client.post("/api/v1/events/window-peak", json=payload)
    assert resp.status_code == 422


def test_window_peak_non_finite_timestamp_rejected():
    body = (
        '{"reference":"A","observations":[],"window":{"start":0,"end":1},'
        '"events":[{"id":"x","node":"B","timestamp":NaN}]}'
    )
    resp = client.post(
        "/api/v1/events/window-peak",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422


def test_window_peak_inverted_window_rejected():
    payload = _window_payload(window={"start": 995, "end": 990})
    resp = client.post("/api/v1/events/window-peak", json=payload)
    assert resp.status_code == 422


def test_window_peak_non_finite_window_rejected():
    body = (
        '{"reference":"A","observations":[],"events":[],'
        '"window":{"start":0,"end":Infinity}}'
    )
    resp = client.post(
        "/api/v1/events/window-peak",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422


def test_window_peak_missing_window_rejected():
    payload = _window_payload()
    del payload["window"]
    resp = client.post("/api/v1/events/window-peak", json=payload)
    assert resp.status_code == 422


def test_window_peak_unknown_field_rejected():
    payload = _window_payload(bogus=1)
    resp = client.post("/api/v1/events/window-peak", json=payload)
    assert resp.status_code == 422


def test_window_peak_inherited_validation_still_applies():
    # 复用诊断请求校验：非法观测区间 / 重复观测 ID / reference 不在 nodes 中
    base = _window_payload(events=[])
    resp = client.post(
        "/api/v1/events/window-peak",
        json=base | {"observations": [
            {"id": "o", "source": "A", "target": "B", "min": 9, "max": 1}
        ]},
    )
    assert resp.status_code == 422
    resp = client.post(
        "/api/v1/events/window-peak",
        json=base | {"observations": [
            {"id": "x", "source": "A", "target": "B", "min": 0, "max": 1},
            {"id": "x", "source": "B", "target": "A", "min": 0, "max": 1},
        ]},
    )
    assert resp.status_code == 422
    resp = client.post(
        "/api/v1/events/window-peak",
        json=base | {"reference": "Z", "nodes": ["A", "B"]},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 冗余审计
# ---------------------------------------------------------------------------


def _redundancy_payload(**overrides):
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 1, "max": 5},
            {"id": "o2", "source": "B", "target": "C", "min": 2, "max": 4},
            {"id": "o3", "source": "A", "target": "C", "min": 3, "max": 9},
        ],
    }
    payload.update(overrides)
    return payload


def test_redundancy_audit_marks_chain_implied_observation():
    resp = client.post("/api/v1/diagnose/redundancy-audit", json=_redundancy_payload())
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert data["diagnosis"] is None
    # 结果按观测 ID 排序
    assert [r["observation_id"] for r in data["results"]] == ["o1", "o2", "o3"]
    by_id = {r["observation_id"]: r for r in data["results"]}
    # o3: A->C ∈ [3,9] 恰被 o1+o2 蕴含 -> 冗余，附替代证据链
    o3 = by_id["o3"]
    assert o3["redundant"] is True
    assert o3["lower"] == 3.0 and o3["upper"] == 9.0
    assert o3["upper_proof"] == [
        {"observation_id": "o1", "constraint": "upper",
         "from": "A", "to": "B", "weight": 5.0},
        {"observation_id": "o2", "constraint": "upper",
         "from": "B", "to": "C", "weight": 4.0},
    ]
    assert o3["lower_proof"] == [
        {"observation_id": "o2", "constraint": "lower",
         "from": "C", "to": "B", "weight": -2.0},
        {"observation_id": "o1", "constraint": "lower",
         "from": "B", "to": "A", "weight": -1.0},
    ]
    # 替代证据链不含被审计观测自身的 ID
    assert all(s["observation_id"] != "o3" for s in o3["upper_proof"])
    assert all(s["observation_id"] != "o3" for s in o3["lower_proof"])
    # 链上的 o1/o2 移除后界会变宽 -> 非冗余，且不附界值与证据链
    for obs_id in ("o1", "o2"):
        item = by_id[obs_id]
        assert item["redundant"] is False
        assert item["lower"] is None and item["upper"] is None
        assert item["lower_proof"] is None and item["upper_proof"] is None


def test_redundancy_audit_base_contradiction_returns_cycle():
    payload = _redundancy_payload(
        observations=[
            {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 10},
            {"id": "o2", "source": "B", "target": "A", "min": 3, "max": 4},
        ]
    )
    resp = client.post("/api/v1/diagnose/redundancy-audit", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "base_contradiction"
    assert data["results"] == []
    diag = data["diagnosis"]
    assert diag["feasible"] is False
    assert diag["cycle"]["accumulated_bound"] == -3.0
    assert diag["cycle"]["contradiction"] == "0 <= -3.0"


def test_redundancy_audit_single_observation_not_redundant():
    # 唯一观测被暂移后两方向均不可达 -> 非冗余
    payload = _redundancy_payload(
        observations=[{"id": "o1", "source": "A", "target": "B", "min": 0, "max": 5}]
    )
    data = client.post("/api/v1/diagnose/redundancy-audit", json=payload).json()
    assert data["status"] == "ok"
    assert data["results"] == [
        {
            "observation_id": "o1",
            "redundant": False,
            "lower": None,
            "upper": None,
            "lower_proof": None,
            "upper_proof": None,
        }
    ]


def test_redundancy_audit_empty_observations_returns_empty_results():
    payload = _redundancy_payload(observations=[])
    data = client.post("/api/v1/diagnose/redundancy-audit", json=payload).json()
    assert data["status"] == "ok"
    assert data["results"] == []
    assert data["diagnosis"] is None


def test_redundancy_audit_results_sorted_and_order_independent():
    observations = [
        {"id": "o1", "source": "A", "target": "B", "min": 1, "max": 5},
        {"id": "o2", "source": "B", "target": "C", "min": 2, "max": 4},
        {"id": "o3", "source": "A", "target": "C", "min": 3, "max": 9},
    ]
    orders = [
        observations,
        observations[::-1],
        [observations[1], observations[2], observations[0]],
    ]
    results = [
        client.post(
            "/api/v1/diagnose/redundancy-audit",
            json=_redundancy_payload(observations=order),
        ).json()["results"]
        for order in orders
    ]
    for r in results:
        assert [item["observation_id"] for item in r] == ["o1", "o2", "o3"]
    for r in results[1:]:
        assert r == results[0]


def test_redundancy_audit_name_echoed():
    payload = _redundancy_payload(name="audit-1")
    data = client.post("/api/v1/diagnose/redundancy-audit", json=payload).json()
    assert data["name"] == "audit-1"


def test_redundancy_audit_too_many_observations_rejected():
    payload = _redundancy_payload(
        observations=[
            {"id": f"o{i:02d}", "source": "A", "target": "B", "min": 0, "max": 1}
            for i in range(31)
        ]
    )
    resp = client.post("/api/v1/diagnose/redundancy-audit", json=payload)
    assert resp.status_code == 422


def test_redundancy_audit_exactly_30_observations_accepted():
    payload = _redundancy_payload(
        observations=[
            {"id": f"o{i:02d}", "source": "A", "target": "B", "min": 0, "max": 100}
            for i in range(30)
        ]
    )
    resp = client.post("/api/v1/diagnose/redundancy-audit", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert len(data["results"]) == 30
    # 30 条完全相同的 [0,100]：每条都被其余 29 条蕴含 -> 全部冗余
    assert all(item["redundant"] for item in data["results"])


def test_redundancy_audit_unknown_field_rejected():
    payload = _redundancy_payload(bogus=1)
    resp = client.post("/api/v1/diagnose/redundancy-audit", json=payload)
    assert resp.status_code == 422


def test_redundancy_audit_inherited_validation_still_applies():
    # 复用诊断请求校验：非法观测区间 / 重复观测 ID / reference 不在 nodes 中
    resp = client.post(
        "/api/v1/diagnose/redundancy-audit",
        json=_redundancy_payload(observations=[
            {"id": "o", "source": "A", "target": "B", "min": 9, "max": 1}
        ]),
    )
    assert resp.status_code == 422
    resp = client.post(
        "/api/v1/diagnose/redundancy-audit",
        json=_redundancy_payload(observations=[
            {"id": "x", "source": "A", "target": "B", "min": 0, "max": 1},
            {"id": "x", "source": "B", "target": "A", "min": 0, "max": 1},
        ]),
    )
    assert resp.status_code == 422
    resp = client.post(
        "/api/v1/diagnose/redundancy-audit",
        json=_redundancy_payload(reference="Z", nodes=["A", "B"]),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 时钟漂移
# ---------------------------------------------------------------------------


def _drift_payload(**overrides):
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 10, "time": 0},
            {"id": "o2", "source": "A", "target": "B", "min": 90, "max": 100, "time": 10},
        ],
        "rates": [{"node": "B", "max_rate": 10}],
        "queries": [0, 5, 10],
    }
    payload.update(overrides)
    return payload


def test_drift_feasible_returns_sorted_query_bounds():
    resp = client.post("/api/v1/diagnose/drift", json=_drift_payload())
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["feasible"] is True
    assert data["reference"] == "A"
    assert [(b["time"], b["node"]) for b in data["query_bounds"]] == [
        (0, "A"), (0, "B"), (5, "A"), (5, "B"), (10, "A"), (10, "B")
    ]
    by_key = {(b["time"], b["node"]): b for b in data["query_bounds"]}
    assert by_key[(0, "A")]["lower"] == 0.0 and by_key[(0, "A")]["upper"] == 0.0
    assert (by_key[(0, "B")]["lower"], by_key[(0, "B")]["upper"]) == (0.0, 10.0)
    # t=5 为两观测区间与漂移锥的交：[40,60]
    assert (by_key[(5, "B")]["lower"], by_key[(5, "B")]["upper"]) == (40.0, 60.0)
    assert (by_key[(10, "B")]["lower"], by_key[(10, "B")]["upper"]) == (90.0, 100.0)


def test_drift_feasible_no_query_fields_cycle_absent():
    data = client.post(
        "/api/v1/diagnose/drift", json=_drift_payload(queries=[])
    ).json()
    assert data["feasible"] is True
    assert data["query_bounds"] == []
    assert "cycle" not in data


def test_drift_infeasible_returns_cycle_with_segment_ids_and_no_bounds():
    payload = _drift_payload(
        rates=[{"node": "B", "max_rate": 5}],
    )
    resp = client.post("/api/v1/diagnose/drift", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["feasible"] is False
    assert "query_bounds" not in data
    cycle = data["cycle"]
    assert cycle["accumulated_bound"] < 0
    assert cycle["contradiction"] == f"0 <= {cycle['accumulated_bound']}"
    ids = {s["observation_id"] for s in cycle["steps"]}
    assert {"o1", "o2"} <= ids
    assert any(sid.startswith("drift:B:") for sid in ids)
    assert any(sid.startswith("ref:") for sid in ids)
    for s in cycle["steps"]:
        assert set(s) == {
            "observation_id", "edge_kind", "node",
            "from_node", "to_node", "from_time", "to_time",
            "constraint", "weight", "bound",
        }
        if s["edge_kind"] == "drift":
            assert s["node"] == "B"
            assert s["from_node"] == s["to_node"] == "B"
        else:
            assert s["node"] is None
    # 环沿 (节点, 时刻) 闭合
    steps = cycle["steps"]
    for i, s in enumerate(steps):
        nxt = steps[(i + 1) % len(steps)]
        assert (s["to_node"], s["to_time"]) == (nxt["from_node"], nxt["from_time"])


def test_drift_infeasible_same_time_observation_cycle():
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 10, "time": 0},
            {"id": "o2", "source": "B", "target": "A", "min": 3, "max": 4, "time": 0},
        ],
        "rates": [{"node": "B", "max_rate": 1}],
        "queries": [],
    }
    data = client.post("/api/v1/diagnose/drift", json=payload).json()
    assert data["feasible"] is False
    assert {s["observation_id"] for s in data["cycle"]["steps"]} == {"o1", "o2"}
    assert all(s["edge_kind"] == "observation" for s in data["cycle"]["steps"])


def test_drift_unreachable_direction_is_null():
    # Z 显式声明且有速率，但与 A 无任何观测连接：两个方向均不可达
    payload = {
        "reference": "A",
        "nodes": ["A", "Z"],
        "observations": [],
        "rates": [{"node": "Z", "max_rate": 1}],
        "queries": [2],
    }
    data = client.post("/api/v1/diagnose/drift", json=payload).json()
    assert data["feasible"] is True
    z = next(b for b in data["query_bounds"] if b["node"] == "Z")
    assert z["lower"] is None and z["upper"] is None
    a = next(b for b in data["query_bounds"] if b["node"] == "A")
    assert a["lower"] == 0.0 and a["upper"] == 0.0


def test_drift_name_echoed():
    data = client.post(
        "/api/v1/diagnose/drift", json=_drift_payload(name="timeline-1")
    ).json()
    assert data["name"] == "timeline-1"


def test_drift_nodes_default_to_observation_endpoints():
    data = client.post("/api/v1/diagnose/drift", json=_drift_payload()).json()
    nodes_at_5 = {b["node"] for b in data["query_bounds"] if b["time"] == 5}
    assert nodes_at_5 == {"A", "B"}


def test_drift_zero_rate_pins_offset():
    payload = _drift_payload(
        observations=[
            {"id": "o1", "source": "A", "target": "B", "min": 1, "max": 5, "time": 0}
        ],
        rates=[{"node": "B", "max_rate": 0}],
        queries=[8],
    )
    data = client.post("/api/v1/diagnose/drift", json=payload).json()
    b = next(x for x in data["query_bounds"] if x["node"] == "B")
    assert b["lower"] == 1.0 and b["upper"] == 5.0


def test_drift_duplicate_observation_id_rejected():
    payload = _drift_payload(
        observations=[
            {"id": "x", "source": "A", "target": "B", "min": 0, "max": 1, "time": 0},
            {"id": "x", "source": "B", "target": "A", "min": 0, "max": 1, "time": 1},
        ]
    )
    assert client.post("/api/v1/diagnose/drift", json=payload).status_code == 422


def test_drift_missing_node_rate_rejected():
    # C 出现在观测中但未给速率
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "B", "target": "C", "min": 0, "max": 1, "time": 0}
        ],
        "rates": [{"node": "B", "max_rate": 1}],
        "queries": [],
    }
    assert client.post("/api/v1/diagnose/drift", json=payload).status_code == 422


def test_drift_rate_for_reference_rejected():
    payload = _drift_payload(
        rates=[{"node": "A", "max_rate": 1}, {"node": "B", "max_rate": 1}]
    )
    assert client.post("/api/v1/diagnose/drift", json=payload).status_code == 422


def test_drift_duplicate_rate_node_rejected():
    payload = _drift_payload(
        rates=[{"node": "B", "max_rate": 1}, {"node": "B", "max_rate": 2}]
    )
    assert client.post("/api/v1/diagnose/drift", json=payload).status_code == 422


def test_drift_rate_for_unknown_node_rejected():
    payload = _drift_payload(rates=[{"node": "Z", "max_rate": 1}])
    assert client.post("/api/v1/diagnose/drift", json=payload).status_code == 422


def test_drift_negative_rate_rejected():
    payload = _drift_payload(rates=[{"node": "B", "max_rate": -0.1}])
    assert client.post("/api/v1/diagnose/drift", json=payload).status_code == 422


def test_drift_non_integer_time_rejected():
    for bad_time in (1.5, -1):
        payload = _drift_payload(
            observations=[
                {"id": "o9", "source": "A", "target": "B",
                 "min": 0, "max": 1, "time": bad_time}
            ]
        )
        assert client.post("/api/v1/diagnose/drift", json=payload).status_code == 422


def test_drift_boolean_time_rejected():
    # JSON 中布尔以 true/false 提交，严格整数模式必须拒绝
    body = (
        '{"reference":"A","queries":[],'
        '"observations":[{"id":"o1","source":"A","target":"B",'
        '"min":0,"max":1,"time":true}],'
        '"rates":[{"node":"B","max_rate":1}]}'
    )
    resp = client.post(
        "/api/v1/diagnose/drift",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422


def test_drift_too_many_queries_rejected():
    payload = _drift_payload(queries=list(range(9)))
    assert client.post("/api/v1/diagnose/drift", json=payload).status_code == 422


def test_drift_duplicate_query_rejected():
    payload = _drift_payload(queries=[1, 1])
    assert client.post("/api/v1/diagnose/drift", json=payload).status_code == 422


def test_drift_negative_query_rejected():
    payload = _drift_payload(queries=[-1])
    assert client.post("/api/v1/diagnose/drift", json=payload).status_code == 422


def test_drift_more_than_40_instants_rejected():
    payload = {
        "reference": "A",
        "observations": [
            {"id": f"o{i}", "source": "A", "target": "B",
             "min": 0, "max": 1000, "time": i}
            for i in range(41)
        ],
        "rates": [{"node": "B", "max_rate": 1000}],
        "queries": [],
    }
    assert client.post("/api/v1/diagnose/drift", json=payload).status_code == 422


def test_drift_exactly_40_instants_accepted():
    payload = {
        "reference": "A",
        "observations": [
            {"id": f"o{i}", "source": "A", "target": "B",
             "min": 0, "max": 1000, "time": i}
            for i in range(38)
        ],
        "rates": [{"node": "B", "max_rate": 1000}],
        "queries": [38, 39],
    }
    resp = client.post("/api/v1/diagnose/drift", json=payload)
    assert resp.status_code == 200, resp.text
    assert resp.json()["feasible"] is True


def test_drift_invalid_interval_rejected():
    payload = _drift_payload(
        observations=[
            {"id": "o9", "source": "A", "target": "B",
             "min": 9, "max": 1, "time": 0}
        ]
    )
    assert client.post("/api/v1/diagnose/drift", json=payload).status_code == 422


def test_drift_reserved_id_prefix_rejected():
    payload = _drift_payload(
        observations=[
            {"id": "drift:B:0:1", "source": "A", "target": "B",
             "min": 0, "max": 1, "time": 0}
        ]
    )
    assert client.post("/api/v1/diagnose/drift", json=payload).status_code == 422


def test_drift_reference_not_in_nodes_rejected():
    payload = _drift_payload(nodes=["B", "C"])
    assert client.post("/api/v1/diagnose/drift", json=payload).status_code == 422


def test_drift_unknown_field_rejected():
    payload = _drift_payload(bogus=1)
    assert client.post("/api/v1/diagnose/drift", json=payload).status_code == 422
