"""时间漂移诊断接口（POST /api/v1/diagnose/drift）测试。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

PATH = "/api/v1/diagnose/drift"


def test_drift_api_feasible_returns_bounds_sorted_by_second_then_node():
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o0", "source": "A", "target": "B", "second": 0, "min": 0, "max": 10}
        ],
        "rates": [{"node": "B", "rate": 1}],
        "query_seconds": [10, 0],  # 乱序提交，响应必须按秒数升序
    }
    resp = client.post(PATH, json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["feasible"] is True
    assert data["reference"] == "A"
    assert data["timeline"] == [0, 10]
    assert [q["second"] for q in data["queries"]] == [0, 10]

    at10 = {b["node"]: b for b in data["queries"][1]["node_bounds"]}
    assert list(at10) == ["A", "B"]  # 节点按字典序
    assert at10["A"] == {"node": "A", "lower": 0.0, "upper": 0.0}
    assert at10["B"]["lower"] == -10.0 and at10["B"]["upper"] == 20.0


def test_drift_api_name_echoed():
    payload = {
        "reference": "A",
        "observations": [],
        "rates": [],
        "query_seconds": [],
        "name": "sweep-1",
    }
    data = client.post(PATH, json=payload).json()
    assert data["feasible"] is True and data["name"] == "sweep-1"
    assert data["timeline"] == [] and data["queries"] == []


def test_drift_api_infeasible_cycle_mixes_observation_and_drift_steps():
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o0", "source": "A", "target": "B", "second": 0, "min": 0, "max": 2},
            {"id": "o1", "source": "A", "target": "B", "second": 1, "min": 10, "max": 12},
        ],
        "rates": [{"node": "B", "rate": 5}],
        "query_seconds": [1],
    }
    resp = client.post(PATH, json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["feasible"] is False
    assert "queries" not in data  # 不可行时不给查询界
    cycle = data["cycle"]
    assert cycle["accumulated_bound"] == -3.0
    assert cycle["contradiction"] == "0 <= -3.0"
    by_type = {s["type"]: s for s in cycle["steps"]}
    assert set(by_type) == {"observation", "drift"}

    obs_step = next(s for s in cycle["steps"] if s["type"] == "observation")
    assert set(obs_step) == {
        "type", "observation_id", "from", "to", "constraint", "weight", "bound"
    }
    assert obs_step["from"] == {"node": "A", "second": 0}

    drift_step = next(s for s in cycle["steps"] if s["type"] == "drift")
    assert set(drift_step) == {
        "type", "segment_id", "node", "from_second", "to_second",
        "constraint", "weight", "bound",
    }
    assert drift_step["segment_id"].startswith("drift:")
    assert drift_step["constraint"] in ("forward", "backward")

    # 每步时间点首尾闭合
    def endpoint_to(step):
        if step["type"] == "drift":
            return step["node"], step["to_second"]
        return step["to"]["node"], step["to"]["second"]

    def endpoint_from(step):
        if step["type"] == "drift":
            return step["node"], step["from_second"]
        return step["from"]["node"], step["from"]["second"]

    steps = cycle["steps"]
    for i, step in enumerate(steps):
        assert endpoint_from(step) == endpoint_to(steps[i - 1])


def test_drift_api_infeasible_pure_observation_cycle():
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "second": 0, "min": 0, "max": 10},
            {"id": "o2", "source": "B", "target": "A", "second": 0, "min": 3, "max": 4},
        ],
        "rates": [{"node": "B", "rate": 1}],
        "query_seconds": [],
    }
    data = client.post(PATH, json=payload).json()
    assert data["feasible"] is False
    assert {s["type"] for s in data["cycle"]["steps"]} == {"observation"}


def test_drift_api_unreachable_direction_is_null():
    payload = {
        "reference": "A",
        "nodes": ["A", "B", "C"],
        "observations": [
            {"id": "o0", "source": "A", "target": "B", "second": 0, "min": 0, "max": 10}
        ],
        "rates": [{"node": "B", "rate": 1}, {"node": "C", "rate": 2}],
        "query_seconds": [5],
    }
    data = client.post(PATH, json=payload).json()
    bounds = {b["node"]: b for b in data["queries"][0]["node_bounds"]}
    assert bounds["C"]["lower"] is None and bounds["C"]["upper"] is None
    assert bounds["B"]["lower"] == -5.0 and bounds["B"]["upper"] == 15.0


def _expect_422(payload, fragment=None):
    resp = client.post(PATH, json=payload)
    assert resp.status_code == 422, resp.text
    if fragment is not None:
        text = resp.text
        assert fragment in text, (fragment, text)
    return resp


def test_drift_api_422_duplicate_observation_id():
    _expect_422(
        {
            "reference": "A",
            "observations": [
                {"id": "x", "source": "A", "target": "B", "second": 0, "min": 0, "max": 1},
                {"id": "x", "source": "A", "target": "B", "second": 1, "min": 0, "max": 1},
            ],
            "rates": [{"node": "B", "rate": 1}],
            "query_seconds": [],
        },
        "重复",
    )


def test_drift_api_422_missing_node_rate():
    _expect_422(
        {
            "reference": "A",
            "observations": [
                {"id": "o1", "source": "A", "target": "C", "second": 0, "min": 0, "max": 1}
            ],
            "rates": [],
            "query_seconds": [],
        },
        "速率",
    )


def test_drift_api_422_rate_for_reference_rejected():
    _expect_422(
        {
            "reference": "A",
            "observations": [],
            "rates": [{"node": "A", "rate": 1}],
            "query_seconds": [],
        },
        "参考",
    )


def test_drift_api_422_non_integer_or_negative_seconds():
    base = {
        "reference": "A",
        "observations": [
            {"id": "o0", "source": "A", "target": "B", "second": 0, "min": 0, "max": 1}
        ],
        "rates": [{"node": "B", "rate": 1}],
        "query_seconds": [],
    }
    for bad_second in (-1, 1.5, True, "0"):
        payload = {**base, "observations": [dict(base["observations"][0], second=bad_second)]}
        _expect_422(payload)
    _expect_422({**base, "query_seconds": [-1]})
    _expect_422({**base, "query_seconds": [2.0]})
    _expect_422({**base, "query_seconds": [False]})
    _expect_422({**base, "query_seconds": ["1"]})


def test_drift_api_422_more_than_eight_queries():
    base = {
        "reference": "A",
        "observations": [],
        "rates": [],
    }
    _expect_422({**base, "query_seconds": list(range(9))})


def test_drift_api_422_duplicate_query_seconds():
    _expect_422(
        {
            "reference": "A",
            "observations": [],
            "rates": [],
            "query_seconds": [1, 1],
        }
    )


def test_drift_api_422_more_than_forty_moments():
    # 33 个观测秒 + 8 个查询秒 = 41 个不同时刻（查询数仍在上限内）
    obs = [
        {"id": f"o{i}", "source": "A", "target": "B", "second": i, "min": 0, "max": 1000}
        for i in range(33)
    ]
    _expect_422(
        {
            "reference": "A",
            "observations": obs,
            "rates": [{"node": "B", "rate": 100}],
            "query_seconds": list(range(33, 41)),
        },
        "40",
    )


def test_drift_api_422_invalid_rates_and_intervals():
    common = {
        "reference": "A",
        "observations": [
            {"id": "o0", "source": "A", "target": "B", "second": 0, "min": 0, "max": 1}
        ],
        "query_seconds": [],
    }
    _expect_422({**common, "rates": [{"node": "B", "rate": -0.1}]})
    _expect_422(
        {**common, "rates": [{"node": "B", "rate": 1}, {"node": "B", "rate": 2}]}
    )
    _expect_422(
        {
            "reference": "A",
            "observations": [
                {"id": "o0", "source": "A", "target": "B", "second": 0, "min": 5, "max": 1}
            ],
            "rates": [{"node": "B", "rate": 1}],
            "query_seconds": [],
        }
    )
    # 未知节点（不在 nodes ∪ 观测端点 ∪ reference 中）给速率 -> 422
    _expect_422({**common, "rates": [{"node": "Z", "rate": 1}]})


def test_drift_api_422_reference_not_in_nodes():
    _expect_422(
        {
            "reference": "A",
            "nodes": ["B"],
            "observations": [],
            "rates": [{"node": "B", "rate": 1}],
            "query_seconds": [],
        }
    )


def test_drift_api_422_unknown_field():
    _expect_422(
        {
            "reference": "A",
            "observations": [],
            "rates": [],
            "query_seconds": [],
            "bogus": 1,
        }
    )


def test_drift_api_422_nan_rate_does_not_500():
    # 与既有接口一致：NaN 等非有限浮点不能让服务崩成 500，必须 422。
    body = (
        '{"reference":"A","observations":[],"rates":[{"node":"B","rate":NaN}],'
        '"query_seconds":[]}'
    )
    resp = client.post(PATH, content=body, headers={"content-type": "application/json"})
    assert resp.status_code == 422


def test_drift_api_exactly_forty_moments_accepted():
    obs = [
        {"id": f"o{i}", "source": "A", "target": "B", "second": i, "min": 0, "max": 1000}
        for i in range(32)
    ]
    payload = {
        "reference": "A",
        "observations": obs,
        "rates": [{"node": "B", "rate": 100}],
        "query_seconds": list(range(32, 40)),
    }
    resp = client.post(PATH, json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["timeline"]) == 40
    assert [q["second"] for q in data["queries"]] == list(range(32, 40))


def test_drift_api_effective_nodes_default_to_endpoints():
    # 不显式给 nodes：节点宇宙取观测端点 ∪ reference；C 仅出现在第二条观测，
    # 仍必须给出速率（缺失则 422，给齐则 200）。
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o0", "source": "A", "target": "B", "second": 0, "min": 0, "max": 5},
            {"id": "o1", "source": "B", "target": "C", "second": 0, "min": 0, "max": 5},
        ],
        "rates": [{"node": "B", "rate": 1}, {"node": "C", "rate": 1}],
        "query_seconds": [0],
    }
    resp = client.post(PATH, json=payload)
    assert resp.status_code == 200, resp.text
    nodes = [b["node"] for b in resp.json()["queries"][0]["node_bounds"]]
    assert nodes == ["A", "B", "C"]
