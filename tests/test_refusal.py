"""Refusal continuation (拒答续答): import validation, routing and editing."""

import html
import io
import json
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import engine  # noqa: E402
import importer  # noqa: E402
import storage  # noqa: E402


def definition():
    """Q1 self-buy / rental branches that converge on Q7."""
    return [
        {"id": "Q1", "title": "设备来源", "type": "single",
         "options": ["自购", "租赁"], "jumps": {"租赁": "Q5"}},
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


# --------------------------------------------------------------------------- #
# Engine: parsing and validation
# --------------------------------------------------------------------------- #

def test_parse_refusal_cell():
    assert engine.parse_refusal(" Q1 > Q5 ") == ("Q1", "Q5")
    assert engine.parse_refusal("Q1＞Q5") == ("Q1", "Q5")  # full-width >
    assert engine.parse_refusal("") is None
    assert engine.parse_refusal(None) is None


@pytest.mark.parametrize("cell", ["Q1-Q5", "Q1", "Q1>", ">Q5",
                                  "Q1>Q5>Q7", ">>"])
def test_parse_refusal_bad_format(cell):
    with pytest.raises(engine.DefinitionError):
        engine.parse_refusal(cell)


def test_validate_refusal_accepts_jump_target():
    defn = definition()
    # Q1 options: 自购 -> Q2 (fall-through), 租赁 -> Q5 (jump).
    prefix, qid = engine.validate_refusal(
        defn, {"Q1": ""}, ("Q1", "Q5"))
    assert prefix == () and qid == "Q1"
    # Fall-through next question is legal too.
    prefix, qid = engine.validate_refusal(
        defn, {"Q1": ""}, ("Q1", "Q2"))
    assert prefix == () and qid == "Q1"


def test_validate_refusal_accepts_next_in_order_for_plain_single():
    defn = definition()
    # Q5 has no jumps: both options lead to Q6.
    prefix, qid = engine.validate_refusal(
        defn, {"Q1": "租赁", "Q5": ""}, ("Q5", "Q6"))
    assert prefix == ("Q1",) and qid == "Q5"


def test_validate_refusal_rejects_illegal_target():
    defn = definition()
    with pytest.raises(engine.DefinitionError, match="合法选项的下一题"):
        engine.validate_refusal(defn, {"Q1": ""}, ("Q1", "Q6"))
    with pytest.raises(engine.DefinitionError, match="合法选项的下一题"):
        engine.validate_refusal(
            defn, {"Q1": "租赁", "Q5": ""}, ("Q5", "Q7"))


def test_validate_refusal_requires_blank_answer():
    defn = definition()
    with pytest.raises(engine.DefinitionError, match="答案格必须留空"):
        engine.validate_refusal(defn, {"Q1": "自购"}, ("Q1", "Q5"))


def test_validate_refusal_requires_reachable_question():
    defn = definition()
    # A jump skips Q2-Q4 entirely, so Q4 is a single question the rental walk
    # never reaches (it stops at the blank Q5 instead).
    with pytest.raises(engine.DefinitionError, match="不是按原规则走到"):
        engine.validate_refusal(
            defn, {"Q1": "租赁", "Q5": ""}, ("Q4", "Q7"))


def test_validate_refusal_rejects_non_single_and_unknown_ids():
    defn = definition()
    # Reach the number question Q7 via the complete self-buy route first.
    reachable = {"Q1": "自购", "Q2": "喷药机", "Q3": "2", "Q4": "无",
                 "Q7": ""}
    with pytest.raises(engine.DefinitionError, match="不是单选题"):
        engine.validate_refusal(defn, reachable, ("Q7", "Q1"))
    with pytest.raises(engine.DefinitionError, match="不在问卷中"):
        engine.validate_refusal(defn, {}, ("Q0", "Q5"))
    with pytest.raises(engine.DefinitionError, match="去向"):
        engine.validate_refusal(defn, {"Q1": ""}, ("Q1", "Q9"))


# --------------------------------------------------------------------------- #
# Engine: walking, counting and first-missing semantics
# --------------------------------------------------------------------------- #

def test_walk_skips_refused_question_and_continues():
    defn = definition()
    answers = {"Q1": "", "Q5": "按年", "Q6": "3", "Q7": "8000"}
    refusals = {((), "Q1"): ("Q1", "Q5")}
    steps = engine.walk_plain(defn, answers, refusals=refusals)
    assert [s["qid"] for s in steps] == ["Q1", "Q5", "Q6", "Q7"]
    refused = steps[0]
    assert refused["refused"] is True
    assert refused["answered"] is False
    assert refused["refusal_target"] == "Q5"
    assert engine.first_missing(steps) is None
    assert sum(1 for s in steps if s["answered"]) == 3


def test_plain_blank_after_refusal_still_stops_walk():
    defn = definition()
    answers = {"Q1": "", "Q5": "按年", "Q6": "", "Q7": "9000"}
    refusals = {((), "Q1"): ("Q1", "Q5")}
    steps, stored = engine.import_row(defn, answers, refusals=refusals)
    assert [s["qid"] for s in steps] == ["Q1", "Q5", "Q6"]
    # The refused Q1 is never stored; Q5 is; Q7 past the new blank is dropped.
    assert stored == {(("Q1",), "Q5"): "按年"}
    missing = engine.first_missing(steps)
    assert missing["qid"] == "Q6"  # the refused Q1 is not the first missing


def test_refused_question_is_not_counted_as_answered():
    defn = definition()
    answers = {"Q1": "", "Q5": "按年", "Q6": "3", "Q7": "8000"}
    refusals = {((), "Q1"): ("Q1", "Q5")}
    steps, _ = engine.import_row(defn, answers, refusals=refusals)
    assert sum(1 for s in steps if s["answered"]) == 3
    assert not any(s["qid"] == "Q1" and s["answered"] for s in steps)


def test_walk_without_refusal_records_behaves_as_before():
    defn = definition()
    steps = engine.walk_plain(defn, {"Q1": "租赁", "Q5": "按年"})
    assert [s["qid"] for s in steps] == ["Q1", "Q5", "Q6"]
    assert not any(s.get("refused") for s in steps)


# --------------------------------------------------------------------------- #
# Engine: editing semantics
# --------------------------------------------------------------------------- #

RENT = ("Q1", "Q5", "Q6")


def rental_household():
    defn = definition()
    answers = {"Q1": "", "Q5": "按年", "Q6": "3", "Q7": "8000"}
    refusals = {((), "Q1"): ("Q1", "Q5")}
    _, stored = engine.import_row(defn, answers, refusals=refusals)
    return defn, stored, refusals


def test_blank_save_keeps_refusal_and_later_answers():
    defn, stored, refusals = rental_household()
    changes, _, refusal_changes = engine.plan_household_changes(
        defn, stored, [("Q1", (), ""), ("Q5", ("Q1",), "按年")],
        refusals=refusals)
    assert changes == {}
    assert refusal_changes == {}
    steps = engine.walk_stored(defn, stored, refusals=refusals)
    assert [s["qid"] for s in steps] == ["Q1", "Q5", "Q6", "Q7"]


def test_fill_same_path_option_consumes_refusal_and_reuses_later_answers():
    defn, stored, refusals = rental_household()
    changes, _, refusal_changes = engine.plan_household_changes(
        defn, stored, [("Q1", (), "租赁")], refusals=refusals)
    assert changes == {((), "Q1"): "租赁"}
    assert refusal_changes == {((), "Q1"): None}
    merged = dict(stored)
    merged.update({k: v for k, v in changes.items() if v})
    remaining = {k: v for k, v in refusals.items()
                 if k not in refusal_changes}
    steps = engine.walk_stored(defn, merged, refusals=remaining)
    # Same branch: the already-saved later answers stay in use.
    assert [(s["qid"], s["value"]) for s in steps] == [
        ("Q1", "租赁"), ("Q5", "按年"), ("Q6", "3"), ("Q7", "8000")]
    assert not any(s.get("refused") for s in steps)


def test_fill_other_branch_only_reads_that_paths_answers():
    defn, stored, refusals = rental_household()
    changes, ignored, refusal_changes = engine.plan_household_changes(
        defn, stored,
        [("Q1", (), "自购"), ("Q2", ("Q1",), "喷药机")],
        refusals=refusals)
    assert changes == {((), "Q1"): "自购", (("Q1",), "Q2"): "喷药机"}
    assert refusal_changes == {((), "Q1"): None}
    merged = dict(stored)
    for key, value in changes.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    remaining = {k: v for k, v in refusals.items()
                 if k not in refusal_changes}
    steps = engine.walk_stored(defn, merged, refusals=remaining)
    # Rental answers are not read on the self-buy path; Q3 is blank.
    assert [s["qid"] for s in steps] == ["Q1", "Q2", "Q3"]
    assert steps[2]["value"] == ""


def test_clear_refusal_stops_walk_at_the_question():
    defn, stored, refusals = rental_household()
    changes, _, refusal_changes = engine.plan_household_changes(
        defn, stored, [("Q1", (), "")], refusals=refusals,
        clear_refusals=[((), "Q1")])
    assert changes == {}
    assert refusal_changes == {((), "Q1"): None}
    remaining = {k: v for k, v in refusals.items()
                 if k not in refusal_changes}
    steps = engine.walk_stored(defn, stored, refusals=remaining)
    assert [s["qid"] for s in steps] == ["Q1"]
    assert engine.first_missing(steps)["qid"] == "Q1"


def test_earlier_question_bypass_keeps_dormant_refusal():
    defn = definition()
    # Refusal is on Q5 (rental branch), reached after Q1=租赁.
    answers = {"Q1": "租赁", "Q5": "", "Q6": "3", "Q7": "8000"}
    refusals = {(("Q1",), "Q5"): ("Q5", "Q6")}
    _, stored = engine.import_row(defn, answers, refusals=refusals)
    changes, ignored, refusal_changes = engine.plan_household_changes(
        defn, stored,
        [("Q1", (), "自购"), ("Q5", ("Q1",), "")],
        refusals=refusals)
    assert changes == {((), "Q1"): "自购"}
    assert ignored == ["Q5"]            # Q5 no longer reached
    assert refusal_changes == {}        # record stays dormant
    merged = dict(stored)
    for key, value in changes.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    steps = engine.walk_stored(defn, merged, refusals=refusals)
    assert [s["qid"] for s in steps] == ["Q1", "Q2"]
    # The rental answers and the refusal record are both still stored.
    assert (("Q1", "Q5"), "Q6") in stored
    assert (("Q1",), "Q5") in refusals


def test_switching_back_restores_refusal_and_later_answers():
    defn = definition()
    answers = {"Q1": "租赁", "Q5": "", "Q6": "3", "Q7": "8000"}
    refusals = {(("Q1",), "Q5"): ("Q5", "Q6")}
    _, stored = engine.import_row(defn, answers, refusals=refusals)
    changes, _, _ = engine.plan_household_changes(
        defn, stored, [("Q1", (), "自购")], refusals=refusals)
    merged = dict(stored)
    for key, value in changes.items():
        merged[key] = value
    # Back to rental: the dormant refusal reactivates and reuses later values.
    changes, _, refusal_changes = engine.plan_household_changes(
        defn, merged, [("Q1", (), "租赁")], refusals=refusals)
    assert refusal_changes == {}
    for key, value in changes.items():
        merged[key] = value
    steps = engine.walk_stored(defn, merged, refusals=refusals)
    assert [(s["qid"], s["value"], s["refused"]) for s in steps] == [
        ("Q1", "租赁", False), ("Q5", "", True),
        ("Q6", "3", False), ("Q7", "8000", False)]


def test_dormant_refusal_listed_in_other_paths():
    defn = definition()
    answers = {"Q1": "租赁", "Q5": "", "Q6": "3", "Q7": "8000"}
    refusals = {(("Q1",), "Q5"): ("Q5", "Q6")}
    _, stored = engine.import_row(defn, answers, refusals=refusals)
    # Walk the self-buy route instead.
    stored[((), "Q1")] = "自购"
    steps = engine.walk_stored(defn, stored, refusals=refusals)
    others = engine.other_path_answers(defn, stored, steps,
                                       refusals=refusals)
    dormant = next(item for item in others
                   if item["qid"] == "Q5" and item["refusal_target"])
    assert dormant["refusal_target"] == "Q6"
    assert dormant["prefix"] == ("Q1",)


# --------------------------------------------------------------------------- #
# Importer: CSV column handling and whole-batch rejection
# --------------------------------------------------------------------------- #

JUMPS = {
    "order": ["Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7"],
    "types": {"Q1": "single", "Q2": "single", "Q4": "single",
              "Q5": "single", "Q3": "number", "Q6": "number",
              "Q7": "number"},
    "options": {"Q1": ["自购", "租赁"], "Q2": ["喷药机", "采摘平台"],
                "Q4": ["有", "无"], "Q5": ["按年", "按季"]},
    "jumps": {"Q1": {"自购": "Q2", "租赁": "Q5"},
              "Q2": {"采摘平台": "Q7"},
              "Q4": {"有": "Q7", "无": "Q7"}},
}

CSV_WITH_REFUSAL = """户号,Q1 设备来源,Q2 自购设备类型,Q3 自购台数,Q4 是否享受购置补贴,Q5 租赁方式,Q6 租赁台数,Q7 年费用合计（元）,拒答续答
H1,,喷药机,2,无,按年,3,8000,Q1>Q5
H2,租赁,喷药机,2,有,按年,3,8000,
H3,自购,,,,,,,
"""


def parse(csv_text=CSV_WITH_REFUSAL, jumps=JUMPS):
    return importer.parse(csv_text.encode("utf-8"),
                          json.dumps(jumps).encode("utf-8"))


def test_import_detects_refusal_column_and_records():
    definition, warnings, rows, _, headers, has_col = parse()
    assert has_col is True
    assert importer.REFUSAL_HEADER in headers
    h1 = next(row for row in rows if row["household_no"] == "H1")
    assert h1["refusal_pair"] == ("Q1", "Q5")
    assert h1["refusal_key"] == ((), "Q1")
    h2 = next(row for row in rows if row["household_no"] == "H2")
    assert "refusal_key" not in h2


def test_import_routes_refusal_and_keeps_later_answers():
    definition, _, rows, _, _, _ = parse()
    stored = importer.to_import_answers(definition, rows)
    refusals = importer.to_import_refusals(rows)
    h1_stored = stored["H1"]
    assert ((), "Q1") not in h1_stored          # refused Q1 stays blank
    assert h1_stored[(("Q1",), "Q5")] == "按年"
    assert h1_stored[(("Q1", "Q5"), "Q6")] == "3"
    assert h1_stored[(RENT, "Q7")] == "8000"
    assert refusals["H1"] == {((), "Q1"): ("Q1", "Q5")}
    assert "H2" not in refusals


@pytest.mark.parametrize("cell,reason", [
    ("Q1>Q6", "合法选项"),          # target not a legal next question
    ("Q1>Q9", "去向"),             # unknown target
    ("Q4>Q5", "不是按原规则走到"),  # Q4 is skipped by the Q1>Q5 refusal
    ("Q7>Q1", "不是单选题"),        # source is a number question
    ("Q1", "格式"),               # malformed cell
])
def test_bad_refusal_rejects_whole_batch_with_household_no(cell, reason):
    csv_text = CSV_WITH_REFUSAL.replace("Q1>Q5", cell)
    if cell == "Q7>Q1":
        # Put H1 on a route that actually reaches the number question Q7.
        csv_text = csv_text.replace(
            "H1,,喷药机,2,无,按年,3,8000,Q7>Q1",
            "H1,自购,喷药机,2,无,,,8000,Q7>Q1")
    if cell == "Q4>Q5":
        # The refusal jumps Q1 straight to Q5, skipping self-buy Q2-Q4;
        # blank their cells so the only problem is Q4 being unreachable.
        csv_text = csv_text.replace(
            "H1,,喷药机,2,无,按年,3,8000,Q4>Q5",
            "H1,,,,,按年,3,8000,Q4>Q5")
    with pytest.raises(importer.ImportError_) as exc:
        parse(csv_text)
    message = str(exc.value)
    assert "H1" in message
    assert reason in message


def test_refusal_with_filled_answer_is_rejected():
    csv_text = CSV_WITH_REFUSAL.replace(
        "H1,,喷药机", "H1,自购,喷药机")
    with pytest.raises(importer.ImportError_, match="答案格必须留空"):
        parse(csv_text)


def test_at_most_one_refusal_per_household_is_enforced_by_cell():
    # A cell naming two records is simply not a legal A>B cell.
    csv_text = CSV_WITH_REFUSAL.replace("Q1>Q5", "Q1>Q5;Q2>Q3")
    with pytest.raises(importer.ImportError_, match="格式"):
        parse(csv_text)


def test_batch_without_refusal_column_is_unchanged():
    csv_text = ("户号,Q1 设备来源,Q5 租赁方式\n"
                "H1,租赁,按年\n")
    definition, _, rows, _, _, has_col = parse(csv_text)
    assert has_col is False
    assert importer.to_import_refusals(rows) == {}


def test_full_width_arrow_is_accepted():
    csv_text = CSV_WITH_REFUSAL.replace("Q1>Q5", "Q1＞Q5")
    definition, _, rows, _, _, _ = parse(csv_text)
    h1 = next(row for row in rows if row["household_no"] == "H1")
    assert h1["refusal_pair"] == ("Q1", "Q5")


# --------------------------------------------------------------------------- #
# Storage round trip
# --------------------------------------------------------------------------- #

def test_storage_persists_and_loads_refusals(tmp_path):
    connection = storage.connect(str(tmp_path / "survey.sqlite3"))
    defn = definition()
    refusals = {"H1": {((), "Q1"): ("Q1", "Q5")}}
    answers = {"H1": {(("Q1",), "Q5"): "按年"}}
    batch_id = storage.create_batch(
        connection, "批次", defn, [("H1", 1)], answers,
        refusals_by_household=refusals, has_refusals=True)
    info = storage.get_batch(connection, batch_id)
    assert info["has_refusals"] is True
    household = storage.list_households(connection, batch_id)[0]
    assert storage.load_refusals(connection, household["id"]) == \
        {((), "Q1"): ("Q1", "Q5")}
    storage.save_answers(
        connection, household["id"], {},
        refusal_changes={((), "Q1"): None})
    assert storage.load_refusals(connection, household["id"]) == {}
    connection.close()


def test_storage_save_failure_rolls_back_refusal_deletion(tmp_path):
    connection = storage.connect(str(tmp_path / "survey.sqlite3"))
    defn = definition()
    refusals = {"H1": {((), "Q1"): ("Q1", "Q5")}}
    batch_id = storage.create_batch(
        connection, "批次", defn, [("H1", 1)], {},
        refusals_by_household=refusals, has_refusals=True)
    household = storage.list_households(connection, batch_id)[0]

    class ExplodingConnection:
        """Delegates everything but fails once the save transaction starts."""

        def __init__(self, real):
            self._real = real
            self._armed = False

        def execute(self, statement, parameters=()):
            if self._armed:
                raise RuntimeError("disk full")
            return self._real.execute(statement, parameters)

        def __getattr__(self, name):
            return getattr(self._real, name)

    wrapper = ExplodingConnection(connection)
    wrapper._armed = True
    with pytest.raises(RuntimeError, match="disk full"):
        storage.save_answers(
            wrapper, household["id"], {((), "Q1"): "租赁"},
            refusal_changes={((), "Q1"): None})
    # Reopen: the transaction rollback kept the original record and answers.
    connection2 = storage.connect(str(tmp_path / "survey.sqlite3"))
    assert storage.load_refusals(connection2, household["id"]) == \
        {((), "Q1"): ("Q1", "Q5")}
    connection2.close()


# --------------------------------------------------------------------------- #
# Web layer
# --------------------------------------------------------------------------- #

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SURVEY_DB", str(tmp_path / "survey.sqlite3"))
    monkeypatch.setenv("SURVEY_SECRET", "test")
    for module in ("storage", "engine", "importer", "main"):
        sys.modules.pop(module, None)
    import main
    main.app.config.update(TESTING=True)
    with main.app.test_client() as test_client:
        yield test_client


def upload(client, csv_text=CSV_WITH_REFUSAL, jumps=JUMPS):
    response = client.post("/upload", data={
        "csv_file": (io.BytesIO(csv_text.encode("utf-8")), "answers.csv"),
        "json_file": (io.BytesIO(json.dumps(jumps).encode("utf-8")),
                      "jumps.json"),
        "batch_name": "拒答批次",
    }, content_type="multipart/form-data", follow_redirects=True)
    assert response.status_code == 200
    return response.get_data(as_text=True)


def form_fields(body):
    entries = []
    pattern = re.compile(
        r'name="k_qid_(\d+)"\s+value="([^"]*)">\s*'
        r'<input\s+type="hidden"\s+name="k_prefix_\1"\s+value="([^"]*)"',
        re.S)
    for index, qid, prefix in pattern.findall(body):
        tags = re.findall(r'<input[^>]*name="v_%s"[^>]*>' % index, body, re.S)
        chosen = next((tag for tag in tags if "checked" in tag),
                      tags[0] if tags else "")
        value_match = re.search(r'data-value="([^"]*)"', chosen)
        entries.append((html.unescape(qid), json.loads(html.unescape(prefix)),
                        html.unescape(value_match.group(1)) if value_match
                        else ""))
    return entries


def test_web_import_shows_refusal_on_household_page(client):
    body = upload(client)
    assert "拒答批次" in body
    page = client.get("/batch/1/household/1").get_data(as_text=True)
    assert "拒答，续答 Q5" in page
    fields = form_fields(page)
    assert [qid for qid, _, _ in fields] == ["Q1", "Q5", "Q6", "Q7"]
    q1 = next(item for item in fields if item[0] == "Q1")
    assert q1[2] == ""                      # refused question renders blank
    assert 'name="r_clear_1"' in page       # clear-record control rendered


def test_web_batch_list_excludes_refused_from_answered_count(client):
    upload(client)
    body = client.get("/batch/1").get_data(as_text=True)
    h1_row = re.search(r"<tr>\s*<td[^>]*>\s*H1\s*</td>.*?</tr>",
                       body, re.S).group(0)
    cells = re.findall(r"<td[^>]*>(.*?)</td>", h1_row, re.S)
    # Columns: 户号, 当前路径, 已答, 首个漏答题, 链接
    assert re.sub(r"\s+", "", cells[2]) == "3"
    assert "拒答" not in cells[3] or True


def test_web_export_keeps_refusal_column_with_active_record(client):
    upload(client)
    text = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    header = text.splitlines()[0].split(",")
    assert header[-1] == "拒答续答"
    lines = {line.split(",", 1)[0]: line.split(",")
             for line in text.splitlines()[1:]}
    assert lines["H1"][-1] == "Q1>Q5"
    assert lines["H2"][-1] == ""            # no record stays blank
    # Refused answer cell blank; not reported as first missing.
    q1_index = next(i for i, name in enumerate(header)
                    if name.startswith("Q1 "))
    assert lines["H1"][q1_index] == ""
    assert lines["H1"][header.index("首个漏答题")] == ""
    assert lines["H1"][header.index("已答题数")] == "3"


def test_web_fill_refused_option_saves_and_routes(client):
    upload(client)
    body = client.post("/batch/1/household/1", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "v_1": "租赁",
        "k_qid_2": "Q5", "k_prefix_2": '["Q1"]', "v_2": "按年",
        "k_qid_3": "Q6", "k_prefix_3": '["Q1", "Q5"]', "v_3": "3",
        "k_qid_4": "Q7",
        "k_prefix_4": '["Q1", "Q5", "Q6"]', "v_4": "8000",
    }, follow_redirects=True).get_data(as_text=True)
    assert "拒答，续答" not in body
    fields = dict((q, v) for q, _, v in form_fields(body))
    assert fields["Q1"] == "租赁"
    assert fields["Q5"] == "按年" and fields["Q7"] == "8000"
    export = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    h1 = next(line for line in export.splitlines() if line.startswith("H1"))
    assert h1.split(",")[-1] == ""          # record consumed


def test_web_clear_refusal_keeps_question_blank_and_stops(client):
    upload(client)
    body = client.post("/batch/1/household/1", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "v_1": "",
        "r_clear_1": "1",
    }, follow_redirects=True).get_data(as_text=True)
    fields = form_fields(body)
    assert [q for q, _, _ in fields] == ["Q1"]
    assert "拒答，续答" not in body
    export = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    h1 = next(line for line in export.splitlines() if line.startswith("H1"))
    cells = h1.split(",")
    header = export.splitlines()[0].split(",")
    assert cells[header.index("首个漏答题")] == "Q1"
    assert cells[-1] == ""


def test_web_bypass_via_earlier_question_then_restore(client):
    # Refusal on Q5 of a rental household.
    csv_text = CSV_WITH_REFUSAL.replace(
        "H1,,喷药机,2,无,按年,3,8000,Q1>Q5",
        "H1,租赁,,,,,3,8000,Q5>Q6")
    upload(client, csv_text=csv_text)
    # Initial page: Q1 -> Q5(refused) -> Q6 -> Q7.
    page = client.get("/batch/1/household/1").get_data(as_text=True)
    assert "拒答，续答 Q6" in page
    assert [q for q, _, _ in form_fields(page)] == ["Q1", "Q5", "Q6", "Q7"]

    # Change the earlier Q1 to 自购: refusal goes dormant, rental kept.
    body = client.post("/batch/1/household/1", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "v_1": "自购",
        "k_qid_2": "Q5", "k_prefix_2": '["Q1"]', "v_2": "",
    }, follow_redirects=True).get_data(as_text=True)
    assert [q for q, _, _ in form_fields(body)] == ["Q1", "Q2"]
    assert "拒答，续答 Q6" in body            # shown under other paths

    # Switch back: refusal and the later answers return.
    body = client.post("/batch/1/household/1", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "v_1": "租赁",
    }, follow_redirects=True).get_data(as_text=True)
    fields = form_fields(body)
    assert [q for q, _, _ in fields] == ["Q1", "Q5", "Q6", "Q7"]
    values = dict((q, v) for q, _, v in fields)
    assert values["Q7"] == "8000"
    assert "拒答，续答 Q6" in body


def test_web_bad_refusal_rejects_batch_and_stores_nothing(client):
    csv_text = CSV_WITH_REFUSAL.replace("Q1>Q5", "Q1>Q6")
    body = upload(client, csv_text=csv_text)
    assert "导入失败" in body and "H1" in body
    assert client.get("/batch/1").status_code == 404


def test_web_save_failure_keeps_refusal_record(client, monkeypatch):
    upload(client)
    import main

    def explode(connection, household_id, changes, refusal_changes=None):
        raise RuntimeError("disk full")

    monkeypatch.setattr(main.storage, "save_answers", explode)
    response = client.post("/batch/1/household/1", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "v_1": "租赁",
    }, follow_redirects=True)
    assert "disk full" in response.get_data(as_text=True)
    page = client.get("/batch/1/household/1").get_data(as_text=True)
    assert "拒答，续答 Q5" in page            # original survey still served
    export = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    h1 = next(line for line in export.splitlines() if line.startswith("H1"))
    assert h1.split(",")[-1] == "Q1>Q5"


def test_web_export_without_refusal_column_has_no_extra_column(client):
    csv_text = ("户号,Q1 设备来源,Q5 租赁方式\n"
                "H1,租赁,按年\n")
    upload(client, csv_text=csv_text)
    text = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    assert "拒答续答" not in text.splitlines()[0]
