import pytest

import engine


def definition():
    """Two entry routes that converge on the shared cost question Q7.

    self-buy:  Q1 → Q2 → Q3 → Q4 → Q7
    rental:    Q1 → Q5 → Q6 → Q7
    """
    return [
        {"id": "Q1", "title": "设备来源", "type": "single",
         "options": ["自购", "租赁", "两者都有"],
         "jumps": {"自购": "Q2", "租赁": "Q5"}},
        {"id": "Q2", "title": "自购设备类型", "type": "single",
         "options": ["喷药机", "采摘平台"], "jumps": {"采摘平台": "Q7"}},
        {"id": "Q3", "title": "自购台数", "type": "number",
         "options": [], "jumps": {}},
        {"id": "Q4", "title": "是否享受补贴", "type": "single",
         "options": ["有", "无"], "jumps": {"有": "Q7", "无": "Q7"}},
        {"id": "Q5", "title": "租赁方式", "type": "single",
         "options": ["按年", "按季"], "jumps": {}},
        {"id": "Q6", "title": "租赁台数", "type": "number",
         "options": [], "jumps": {}},
        {"id": "Q7", "title": "年费用", "type": "number",
         "options": [], "jumps": {}},
    ]


SELF_PREFIX = ("Q1", "Q2", "Q3", "Q4")
RENT_PREFIX = ("Q1", "Q5", "Q6")


def routes(defn, answers):
    return engine.route(engine.walk_plain(defn, answers))


def test_jump_then_fall_through():
    assert routes(definition(), {"Q1": "自购"})[:2] == ["Q1", "Q2"]


def test_unrouted_option_falls_to_next_question():
    assert routes(definition(), {"Q1": "两者都有"})[:2] == ["Q1", "Q2"]


def test_jump_skips_questions():
    assert routes(definition(), {"Q1": "租赁"}) == ["Q1", "Q5"]


def test_blank_stops_walk():
    defn = definition()
    assert routes(defn, {"Q1": "租赁", "Q5": "按年"}) == ["Q1", "Q5", "Q6"]


def test_both_routes_converge_on_shared_cost_question():
    defn = definition()
    self_route = routes(defn, {"Q1": "自购", "Q2": "喷药机", "Q3": "2",
                               "Q4": "无"})
    rent_route = routes(defn, {"Q1": "租赁", "Q5": "按年", "Q6": "3"})
    assert self_route == ["Q1", "Q2", "Q3", "Q4", "Q7"]
    assert rent_route == ["Q1", "Q5", "Q6", "Q7"]


def test_import_keeps_only_reached_answers():
    defn = definition()
    _, stored = engine.import_row(defn, {
        "Q1": "租赁", "Q2": "喷药机", "Q3": "9", "Q4": "有",
        "Q5": "按年", "Q6": "", "Q7": "100"})
    assert stored == {
        ((), "Q1"): "租赁",
        (("Q1",), "Q5"): "按年",
    }
    assert (RENT_PREFIX, "Q7") not in stored


def test_import_stops_at_first_blank():
    _, stored = engine.import_row(definition(), {
        "Q1": "自购", "Q2": "", "Q3": "9", "Q7": "100"})
    assert stored == {((), "Q1"): "自购"}


def test_parallel_paths_keep_separate_answers():
    defn = definition()
    stored = {
        ((), "Q1"): "自购",
        (("Q1",), "Q2"): "喷药机",
        (("Q1", "Q2"), "Q3"): "2",
        (SELF_PREFIX, "Q7"): "12000",
        (("Q1",), "Q5"): "按年",
        (RENT_PREFIX, "Q7"): "8000",
    }
    steps = engine.walk_stored(defn, stored)
    assert [s["qid"] for s in steps] == ["Q1", "Q2", "Q3", "Q4"]
    assert engine.first_missing(steps)["qid"] == "Q4"
    others = engine.other_path_answers(defn, stored, steps)
    assert {(o["qid"], o["value"]) for o in others} == {
        ("Q5", "按年"), ("Q7", "12000"), ("Q7", "8000")}


def test_switch_back_restores_path_answers():
    defn = definition()
    stored = {
        ((), "Q1"): "自购",
        (("Q1",), "Q5"): "按季",
        (RENT_PREFIX, "Q7"): "8000",
    }
    steps = engine.walk_stored(defn, stored)
    assert [(s["qid"], s["value"]) for s in steps] == [
        ("Q1", "自购"), ("Q2", "")]


def test_plan_changes_ignores_off_path_fields():
    defn = definition()
    stored = {((), "Q1"): "自购", (("Q1",), "Q2"): "喷药机"}
    changes, ignored = engine.plan_changes(defn, stored, [
        ("Q1", (), "租赁"),
        ("Q2", ("Q1",), "喷药机"),
        ("Q5", ("Q1",), "按年"),
        ("Q6", ("Q1", "Q5"), "3"),
        ("Q7", RENT_PREFIX, "8000"),
    ])
    assert changes == {
        ((), "Q1"): "租赁",
        (("Q1",), "Q5"): "按年",
        (("Q1", "Q5"), "Q6"): "3",
        (RENT_PREFIX, "Q7"): "8000",
    }
    assert ignored == ["Q2"]
    merged = dict(stored)
    merged.update(changes)
    assert merged[(("Q1",), "Q2")] == "喷药机"


def test_plan_changes_preserves_old_branch_when_switching():
    defn = definition()
    stored = {((), "Q1"): "租赁", (("Q1",), "Q5"): "按年"}
    changes, ignored = engine.plan_changes(defn, stored, [("Q1", (), "自购")])
    assert changes == {((), "Q1"): "自购"}
    assert ignored == []
    merged = dict(stored)
    merged.update(changes)
    assert (("Q1",), "Q5") in merged


def test_plan_changes_clears_only_current_path():
    defn = definition()
    stored = {((), "Q1"): "自购", (("Q1",), "Q2"): "喷药机",
              (("Q1",), "Q5"): "按年"}
    changes, _ = engine.plan_changes(defn, stored, [
        ("Q1", (), "自购"), ("Q2", ("Q1",), "")])
    assert changes == {(("Q1",), "Q2"): None}
    merged = dict(stored)
    for key, value in changes.items():
        if value is None:
            del merged[key]
    assert (("Q1",), "Q5") in merged


def test_shared_cost_question_is_stored_per_path():
    defn = definition()
    stored = {
        ((), "Q1"): "自购",
        (("Q1",), "Q2"): "喷药机",
        (("Q1", "Q2"), "Q3"): "2",
        (("Q1", "Q2", "Q3"), "Q4"): "无",
        (SELF_PREFIX, "Q7"): "12000",
        (("Q1",), "Q5"): "按年",
        (("Q1", "Q5"), "Q6"): "3",
        (RENT_PREFIX, "Q7"): "8000",
    }
    changes, ignored = engine.plan_changes(defn, stored, [
        ("Q1", (), "自购"), ("Q7", SELF_PREFIX, "15000")])
    assert changes == {(SELF_PREFIX, "Q7"): "15000"}
    assert ignored == []
    merged = dict(stored)
    merged.update(changes)
    # The rental path's cost is untouched.
    assert merged[(RENT_PREFIX, "Q7")] == "8000"


def test_plan_changes_uses_prefix_reported_on_form():
    defn = definition()
    stored = {((), "Q1"): "自购"}
    # The form was rendered on the rental route, but Q1 now says 自购, so the
    # rental-only Q5 field (with the rental prefix) is stale.
    changes, ignored = engine.plan_changes(defn, stored, [
        ("Q1", (), "自购"), ("Q5", ("Q1",), "按年")])
    assert changes == {}
    assert ignored == ["Q5"]


def test_invalid_option_is_rejected():
    with pytest.raises(engine.DefinitionError):
        engine.plan_changes(definition(), {}, [("Q1", (), "丢弃")])


def test_first_missing():
    defn = definition()
    steps = engine.walk_stored(defn, {((), "Q1"): "租赁",
                                      (("Q1",), "Q5"): "按年",
                                      (("Q1", "Q5"), "Q6"): "3"})
    assert engine.first_missing(steps)["qid"] == "Q7"


def test_json_jump_table_with_order_and_titles():
    data = {"order": ["Q1", "Q2"], "titles": {"Q1": "来源", "Q2": "费用"},
            "jumps": {"Q1": {"租赁": "Q2"}},
            "options": {"Q1": ["自购", "租赁"]}}
    defn, _ = engine.build_definition(data, [], {})
    assert [q["id"] for q in defn] == ["Q1", "Q2"]
    assert defn[0]["jumps"] == {"租赁": "Q2"}
    assert defn[1]["type"] == "text"


def test_json_backward_jump_is_rejected():
    data = {"order": ["Q1", "Q2"], "jumps": {"Q2": {"x": "Q1"}},
            "options": {"Q1": ["a"], "Q2": ["x"]}}
    with pytest.raises(engine.DefinitionError):
        engine.build_definition(data, [], {})


def test_json_unknown_jump_target_is_rejected():
    data = {"order": ["Q1"], "jumps": {"Q1": {"a": "Q9"}},
            "options": {"Q1": ["a"]}}
    with pytest.raises(engine.DefinitionError):
        engine.build_definition(data, [], {})
