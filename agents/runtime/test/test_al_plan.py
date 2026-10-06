import pytest

from units.al_plan import PlanInvalid, heuristic_plan, make_plan


def test_heuristic_splits_and_chains():
    plan = heuristic_plan("A 를 한다; B 를 한다 → C 를 한다", 12)
    assert [s["title"] for s in plan["steps"]] == ["A 를 한다", "B 를 한다", "C 를 한다"]
    assert [s["depends_on"] for s in plan["steps"]] == [[], ["s1"], ["s2"]]
    assert plan["source"] == "heuristic" and plan["dod"][0]["type"] == "human"


def test_max_steps_caps_plan():
    assert len(heuristic_plan(". ".join(f"단계{i}" for i in range(20)), 5)["steps"]) == 5


def test_plan_without_dod_is_invalid():
    with pytest.raises(PlanInvalid, match="DoD"):
        make_plan("g", [{"title": "x"}], [], "test", 12)


def test_forward_dependencies_are_dropped():
    plan = make_plan("g", [{"title": "a", "depends_on": [2]}, {"title": "b", "depends_on": [1]}], [{"type": "test", "spec": "ok"}], "t", 12)
    assert plan["steps"][0]["depends_on"] == [] and plan["steps"][1]["depends_on"] == ["s1"]
