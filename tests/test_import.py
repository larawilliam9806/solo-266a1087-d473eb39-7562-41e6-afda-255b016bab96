import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "app"))

import engine  # noqa: E402
import importer  # noqa: E402

CSV_TEXT = """户号,Q1 设备来源,Q2 自购设备类型,Q3 自购台数,Q4 租赁方式,Q5 年费用
H1,自购,喷药机,2,,,
H2,租赁,喷药机,2,按年,8000
H3,自购,,,,,
"""

JUMPS = {
    "order": ["Q1", "Q2", "Q3", "Q4", "Q5"],
    "types": {"Q1": "single", "Q2": "single", "Q4": "single",
              "Q3": "number", "Q5": "number"},
    "options": {"Q1": ["自购", "租赁"], "Q2": ["喷药机", "采摘平台"],
                "Q4": ["按年", "按季"]},
    "jumps": {"Q1": {"自购": "Q2", "租赁": "Q4"},
              "Q2": {"采摘平台": "Q5"}},
}


def parse(csv_text=CSV_TEXT, jumps=JUMPS, encoding="utf-8"):
    return importer.parse(csv_text.encode(encoding),
                          json.dumps(jumps).encode("utf-8"))


def test_import_uses_only_reached_answers():
    definition, warnings, rows, _, _ = parse()
    stored = importer.to_import_answers(definition, rows)
    assert [q["id"] for q in definition] == ["Q1", "Q2", "Q3", "Q4", "Q5"]
    h2 = stored["H2"]
    assert h2 == {
        ((), "Q1"): "租赁",
        (("Q1",), "Q4"): "按年",
        (("Q1", "Q4"), "Q5"): "8000",
    }
    assert (("Q1",), "Q2") not in h2
    assert (("Q1", "Q4"), "Q3") not in h2


def test_import_stops_at_blank():
    definition, _, rows, _, _ = parse()
    stored = importer.to_import_answers(definition, rows)
    assert stored["H3"] == {((), "Q1"): "自购"}  # Q2 blank stops the walk


def test_import_tolerates_bom_and_gbk():
    definition, _, rows, _, _ = parse(encoding="gb18030")
    assert [row["household_no"] for row in rows] == ["H1", "H2", "H3"]
    assert rows[0]["answers"]["Q1"] == "自购"


def test_household_header_is_found_by_name():
    csv_text = "调查员,户号,Q1 设备来源\n张三,H9,租赁\n"
    definition, _, rows, _, _ = parse(csv_text)
    assert rows[0]["household_no"] == "H9"


def test_duplicate_households_keep_first():
    csv_text = "户号,Q1 设备来源\nH1,自购\nH1,租赁\n"
    definition, warnings, rows, _, _ = parse(csv_text)
    assert len(rows) == 1
    assert any("重复" in warning for warning in warnings)


def test_missing_column_is_reported():
    csv_text = "户号,Q1 设备来源,Q2 自购设备类型\nH1,自购,喷药机\n"
    definition, warnings, rows, _, _ = parse(csv_text)
    assert any("Q4" in warning for warning in warnings)


def test_unknown_option_is_kept_but_flagged():
    jumps = {"order": ["Q1"], "types": {"Q1": "single"},
             "options": {"Q1": ["自购", "租赁"]}, "jumps": {}}
    definition, _, rows, _, _ = parse("户号,Q1 设备来源\nH1,丢弃\n", jumps)
    # The value cannot be routed, so it stays on Q1 for the editor to fix.
    stored = importer.to_import_answers(definition, rows)
    assert stored["H1"] == {((), "Q1"): "丢弃"}
    steps = engine.walk_stored(definition, stored["H1"])
    assert steps[0]["invalid"] is True


def test_rule_list_shape():
    rules = [{"question": "Q1", "answer": "租赁", "target": "Q2"},
             {"question": "Q2", "answer": "x", "target": "Q3"}]
    data = {"jumps": {"Q1": {"自购": "Q2", "租赁": "Q3"},
                      "Q2": {"x": "Q3"}, "Q3": {}},
            "order": ["Q1", "Q2", "Q3"],
            "options": {"Q1": ["自购", "租赁"], "Q2": ["x"]}}
    del rules
    definition, _ = engine.build_definition(data, [], {})
    assert definition[0]["jumps"] == {"自购": "Q2", "租赁": "Q3"}


def test_bare_jump_table_without_order_uses_csv_columns():
    jumps = {"Q1": {"租赁": "Q3"}}
    csv_text = "户号,Q1 来源,Q2 型号,Q3 费用\nH1,租赁,喷药机,100\n"
    definition, _, rows, _, _ = parse(csv_text, jumps)
    assert [q["id"] for q in definition] == ["Q1", "Q2", "Q3"]
    stored = importer.to_import_answers(definition, rows)["H1"]
    assert stored == {((), "Q1"): "租赁", (("Q1",), "Q3"): "100"}
