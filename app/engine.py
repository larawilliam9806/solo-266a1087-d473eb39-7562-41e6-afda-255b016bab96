"""Routing and answer-path logic for the orchard equipment survey.

This module is deliberately framework free so the rules can be tested in
isolation.

The questionnaire is an ordered list of questions::

    {"id": "A1", "title": "设备来源", "type": "single",
     "options": ["自购", "租赁"], "jumps": {"租赁": "A4"}}

Only single-choice questions route.  When the chosen option has a jump target
the walk continues at that question, otherwise at the next question in order.
A blank (or invalid) answer stops the walk.

Answers are stored per *path*: the ordered sequence of question ids visited
before the answered question.  Two routes that reach the same question
therefore keep independent answers, and a value stored on one route is
restored as soon as the walk returns to that route.
"""

from __future__ import annotations

import re

SINGLE = "single"
TEXT = "text"
NUMBER = "number"
TYPES = (SINGLE, TEXT, NUMBER)

# A refusal cell looks like ``Q1>Q5``: Q1 was refused (left blank) and the
# interview continued at Q5.  A full-width greater-than sign is accepted too.
REFUSAL_PATTERN = re.compile(r"[>＞]")

_JUMP_KEYS = ("jumps", "jump", "跳转", "跳题", "跳题表", "跳转表")
_ANSWER_KEYS = ("answer", "option", "value", "label", "选项", "答案")
_TARGET_KEYS = ("jump", "target", "goto", "skip_to", "跳转", "跳至", "跳到", "next")

META_COLUMNS = {
    "序号", "行号", "提交时间", "填写时间", "开始时间", "结束时间", "用时",
    "用时(秒)", "用时（秒）", "调查员", "审核状态", "备注", "ip", "IP",
    "id", "ID",
}


class DefinitionError(ValueError):
    """The questionnaire definition or the submitted answers are unusable."""


def is_blank(value):
    return value is None or str(value).strip() == ""


def _first(mapping, keys, default=None):
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def _normalize_type(value):
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("single", "radio", "select", "单选", "单选题", "单选题目"):
        return SINGLE
    if text in ("number", "int", "integer", "float", "numeric",
                "数字", "数值", "数值题", "数字题"):
        return NUMBER
    if text in ("text", "string", "input", "fill", "fill_in",
                "文本", "填空", "填空题", "问答", "开放题"):
        return TEXT
    raise DefinitionError("不支持的题型: %s" % value)


def normalize_question(raw, position=0):
    if not isinstance(raw, dict):
        raise DefinitionError("第 %d 个题目格式不正确" % (position + 1))
    qid = _first(raw, ("id", "qid", "question", "code", "题号", "题目编号"))
    if is_blank(qid):
        raise DefinitionError("第 %d 个题目缺少题号" % (position + 1))
    qid = str(qid).strip()
    title = _first(raw, ("title", "label", "name", "题目", "标题", "题干"), qid)
    title = str(title).strip() or qid

    options = []
    jumps = {}
    raw_options = _first(raw, ("options", "choices", "items", "选项"))
    if isinstance(raw_options, dict):
        raw_options = [{"value": key, "jump": target}
                       for key, target in raw_options.items()]
    if raw_options is not None and not isinstance(raw_options, (list, tuple)):
        raise DefinitionError("题目 %s 的选项格式不正确" % qid)
    for item in raw_options or []:
        if isinstance(item, dict):
            value = _first(item, _ANSWER_KEYS)
            target = _first(item, _TARGET_KEYS)
            if is_blank(value):
                raise DefinitionError("题目 %s 有空选项" % qid)
            value = str(value).strip()
            options.append(value)
            if not is_blank(target):
                jumps[value] = str(target).strip()
        elif is_blank(item):
            raise DefinitionError("题目 %s 有空选项" % qid)
        else:
            options.append(str(item).strip())

    raw_jumps = _first(raw, _JUMP_KEYS)
    if raw_jumps is not None and not isinstance(raw_jumps, dict):
        raise DefinitionError("题目 %s 的跳题规则格式不正确" % qid)
    for answer, target in (raw_jumps or {}).items():
        if is_blank(answer) or is_blank(target):
            continue
        answer = str(answer).strip()
        jumps[answer] = str(target).strip()
        if answer not in options:
            options.append(answer)

    declared = _normalize_type(_first(raw, ("type", "kind", "题型", "类型")))
    if declared is None:
        declared = SINGLE if options else TEXT
    if declared == SINGLE and not options:
        raise DefinitionError("单选题 %s 缺少选项" % qid)

    return {
        "id": qid,
        "title": title,
        "type": declared,
        "options": list(dict.fromkeys(options)),
        "jumps": jumps,
    }


def validate(definition):
    if not definition:
        raise DefinitionError("题目列表为空")
    index = {}
    for position, question in enumerate(definition):
        qid = question["id"]
        if qid in index:
            raise DefinitionError("题号重复: %s" % qid)
        index[qid] = position
        if question["type"] not in TYPES:
            raise DefinitionError("题 %s 的题型无效" % qid)
        if question["type"] == SINGLE and not question["options"]:
            raise DefinitionError("单选题 %s 缺少选项" % qid)
    for position, question in enumerate(definition):
        for answer, target in question["jumps"].items():
            if answer not in question["options"]:
                raise DefinitionError(
                    "题 %s 的跳转答案 %s 不在选项中" % (question["id"], answer))
            if target not in index:
                raise DefinitionError(
                    "题 %s 的跳转目标 %s 不存在" % (question["id"], target))
            if index[target] <= position:
                raise DefinitionError(
                    "题 %s 的跳转目标 %s 不是后面的题目"
                    % (question["id"], target))
    return definition


def _extract_full_questions(data):
    if isinstance(data, list):
        if data and all(isinstance(item, dict) and
                        any(key in item for key in
                            ("id", "qid", "question", "题号")) and
                        not any(key in item for key in
                                ("answer", "option", "选项", "答案"))
                        for item in data):
            return data
        return None
    if isinstance(data, dict):
        questions = _first(data, ("questions", "题目", "题"))
        if isinstance(questions, list):
            return questions
        if isinstance(questions, dict):
            return [dict(value, id=key) for key, value in questions.items()]
    return None


def _rule_list(data):
    """``[{"question": "A1", "answer": "租赁", "target": "A4"}, ...]``."""
    if not (isinstance(data, list) and data):
        return None
    table = {}
    order = []
    for item in data:
        if not isinstance(item, dict):
            return None
        qid = _first(item, ("question", "id", "qid", "题号"))
        answer = _first(item, ("answer", "option", "选项", "答案"))
        target = _first(item, _TARGET_KEYS)
        if is_blank(qid) or is_blank(answer) or is_blank(target):
            return None
        qid = str(qid).strip()
        target = str(target).strip()
        table.setdefault(qid, {})[str(answer).strip()] = target
        for q in (qid, target):
            if q not in order:
                order.append(q)
    return {"jumps": table, "order": order}


def _jump_table(data):
    """Extract ``{qid: {answer: target}}`` from any supported JSON shape."""
    if not isinstance(data, dict):
        return None
    jumps = _first(data, _JUMP_KEYS)
    if jumps is not None:
        return jumps
    if data and all(isinstance(value, dict) for value in data.values()):
        return data
    return None


def json_columns(data):
    """Question columns implied by the JSON, or ``None``.

    ``None`` means the JSON only carries jump rules and the CSV columns define
    the question order (a bare jump table).
    """
    full = _extract_full_questions(data)
    if full is not None:
        return [{"id": str(_first(item, ("id", "qid", "question", "题号"),
                                "")).strip(),
                 "title": str(_first(
                     item, ("title", "label", "name", "题目", "标题", "题干"),
                     "")).strip()}
                for item in full]
    if isinstance(data, dict):
        order = _first(data, ("order", "题序", "question_order")) or []
        if order:
            if isinstance(order, (str, int)):
                order = [order]
            titles = _first(data, ("titles", "title_map", "题目名称"), {}) or {}
            return [{"id": str(qid).strip(),
                     "title": str(titles.get(str(qid), "")).strip()}
                    for qid in order if not is_blank(qid)]
    return None


def _unique_ids(order):
    ids = []
    for qid in order:
        qid = str(qid).strip()
        if qid and qid not in ids:
            ids.append(qid)
    return ids


def build_definition(data, columns=None, observed=None):
    """Build a questionnaire from a JSON definition or jump table.

    ``columns`` is the ordered CSV question columns::

        [{"id": "A1", "title": "设备来源"}, ...]

    ``observed`` maps question id to values seen in the CSV; it completes the
    option list of single questions when the JSON only names jumping answers.

    Returns ``(definition, warnings)``.
    """
    columns = columns or []
    observed = observed or {}
    warnings = []

    if data is None:
        raise DefinitionError("缺少 JSON 跳题表")

    if isinstance(data, list) and _extract_full_questions(data) is None:
        rules = _rule_list(data)
        if rules is not None:
            data = rules

    full = _extract_full_questions(data)
    if full is not None:
        definition = [normalize_question(item, pos)
                      for pos, item in enumerate(full)]
        validate(definition)
        known = {question["id"] for question in definition}
        for column in columns:
            if column["id"] not in known:
                warnings.append(
                    "CSV 列「%s」不在题目定义中，已忽略" % column["id"])
        unknown = sorted(known - {column["id"] for column in columns})
        if columns and unknown:
            warnings.append("题目 %s 没有对应的 CSV 列" % "、".join(unknown))
        return definition, warnings

    jump_table = _jump_table(data)
    if jump_table is None:
        raise DefinitionError("JSON 中找不到题目（questions）或跳题表（jumps）")
    if not isinstance(jump_table, dict):
        raise DefinitionError("jumps 必须是 {题号: {答案: 目标题号}} 形式")
    config = data if isinstance(data, dict) else {}

    titles = _first(config, ("titles", "title_map", "题目名称"), {}) or {}
    types = _first(config, ("types", "type_map", "类型"), {}) or {}
    options_map = _first(config, ("options", "选项"), {}) or {}
    singles = _first(config, ("singles", "single", "单选"), []) or []
    if isinstance(singles, (str, int)):
        singles = [singles]
    singles = {str(item).strip() for item in singles}
    order = _first(config, ("order", "题序", "question_order"), []) or []
    if isinstance(order, (str, int)):
        order = [order]
    order = [str(item).strip() for item in order if not is_blank(item)]

    column_ids = [column["id"] for column in columns]
    if order:
        ids = _unique_ids(order)
        ignored = [qid for qid in column_ids if qid not in ids]
        for qid in ignored:
            warnings.append("CSV 列 %s 不在 order 中，已忽略" % qid)
        for qid in ids:
            if qid not in column_ids:
                warnings.append("order 中的题 %s 没有对应的 CSV 列" % qid)
    else:
        ids = list(column_ids)
    for qid in jump_table:
        if str(qid) not in ids and str(qid) in column_ids:
            ids.append(str(qid))
    for source in jump_table:
        targets = jump_table[source]
        if not isinstance(targets, dict):
            raise DefinitionError(
                "题 %s 的跳题规则必须是 {答案: 目标题号}" % source)
        for target in targets.values():
            if is_blank(target):
                continue
            target = str(target).strip()
            if target not in ids:
                raise DefinitionError(
                    "题 %s 的跳转目标 %s 既不在 order 也不在 CSV 列中"
                    % (source, target))

    column_titles = {column["id"]: column.get("title") for column in columns}
    definition = []
    for raw_qid in ids:
        qid = str(raw_qid).strip()
        raw_jumps = jump_table.get(qid) or {}
        if not isinstance(raw_jumps, dict):
            raise DefinitionError(
                "题 %s 的跳题规则必须是 {答案: 目标题号}" % qid)
        jumps = {}
        for answer, target in raw_jumps.items():
            if is_blank(answer) or is_blank(target):
                continue
            jumps[str(answer).strip()] = str(target).strip()

        options = [str(item).strip() for item in (options_map.get(qid) or [])
                   if not is_blank(item)]
        for answer in jumps:
            if answer not in options:
                options.append(answer)
        is_single = (qid in singles or qid in options_map or
                     bool(jump_table.get(qid)))
        if is_single and not options:
            # The JSON names no options for this single question: derive the
            # choice list from the values actually observed in the CSV.
            for value in observed.get(qid, []):
                value = str(value).strip()
                if value and value not in options:
                    options.append(value)

        declared = _normalize_type(types.get(qid))
        if declared is None:
            declared = SINGLE if is_single else TEXT
        if declared == SINGLE and not options:
            raise DefinitionError(
                "单选题 %s 缺少选项，请在 JSON 的 options 中给出" % qid)

        title = titles.get(qid) or column_titles.get(qid) or qid
        definition.append({
            "id": qid,
            "title": str(title).strip() or qid,
            "type": declared,
            "options": list(dict.fromkeys(options)),
            "jumps": jumps,
        })
        if column_titles and qid not in column_titles:
            warnings.append("题目 %s 没有对应的 CSV 列，不会有导入答案" % qid)

    validate(definition)
    return definition, warnings


def build_definition_from_columns(columns):
    """Fallback questionnaire: one text question per CSV column."""
    definition = [normalize_question(
        {"id": column["id"], "title": column.get("title") or column["id"],
         "type": TEXT}, pos) for pos, column in enumerate(columns)]
    validate(definition)
    return definition


def index_of(definition):
    return {question["id"]: position
            for position, question in enumerate(definition)}


def _jump_target(question, value):
    if question.get("type") != SINGLE or is_blank(value):
        return None
    return (question.get("jumps") or {}).get(str(value).strip())


def walk(definition, getter, start=0, refusals=None):
    """Walk the questionnaire using ``getter(question_id, prefix)``.

    A blank/missing answer stops the walk; the stopping question is included
    in the returned steps with ``answered`` false.  Any other single-choice
    value continues the walk: with the option's jump target when the jump
    table specifies one, otherwise with the next question in order.
    ``invalid`` flags single answers outside the option list (dirty data, kept
    for review).

    ``refusals`` maps ``(prefix, qid)`` to ``(source_qid, target_qid)``: when
    the walk reaches such a blank single-choice question it does not stop but
    resumes at the recorded target, marking the step with ``refused`` true.
    """
    index = index_of(definition)
    steps = []
    prefix = []
    position = start
    while 0 <= position < len(definition):
        question = definition[position]
        qid = question["id"]
        if qid in prefix:
            break
        value = getter(qid, tuple(prefix))
        value = "" if is_blank(value) else str(value).strip()
        options = question.get("options") or []
        answered = bool(value)
        step = {
            "qid": qid,
            "pos": position,
            "prefix": tuple(prefix),
            "title": question.get("title", qid),
            "type": question["type"],
            "options": list(options),
            "jumps": dict(question.get("jumps") or {}),
            "value": value,
            "answered": answered,
            "invalid": (answered and question["type"] == SINGLE
                        and value not in options),
            "jump_origin": False,
            "jump_target": None,
            "refused": False,
            "refusal_target": None,
        }
        steps.append(step)
        if not answered:
            # A recorded refusal only applies to this exact path question: the
            # blank single-choice question is skipped and the walk resumes at
            # the recorded next question, instead of stopping like a plain
            # blank answer.
            refusal = (refusals or {}).get((tuple(prefix), qid))
            if refusal is not None and question.get("type") == SINGLE:
                target = refusal[1]
                if target in index:
                    step["refused"] = True
                    step["refusal_target"] = target
                    position = index[target]
                    prefix.append(qid)
                    continue
            break
        target = _jump_target(question, value)
        if target:
            step["jump_origin"] = True
            step["jump_target"] = target
            position = index[target]
        else:
            position += 1
        prefix.append(qid)
    return steps


def walk_plain(definition, answers, refusals=None):
    return walk(definition, lambda qid, prefix: (answers or {}).get(qid),
                refusals=refusals)


def walk_stored(definition, stored, refusals=None):
    """Walk using answers keyed by ``(prefix, question_id)``."""
    return walk(definition,
                lambda qid, prefix: (stored or {}).get((prefix, qid)),
                refusals=refusals)


def route(steps):
    return [step["qid"] for step in steps]


def first_missing(steps):
    # A refused question is intentionally blank: it never counts as the first
    # unanswered question (the walk continues past it).
    for step in steps:
        if not step["answered"] and not step.get("refused"):
            return step
    return None


def parse_refusal(text):
    """Parse a ``Q1>Q5`` cell into ``("Q1", "Q5")`` or raise.

    Blank cells mean "no refusal" and return ``None``.
    """
    if is_blank(text):
        return None
    cleaned = str(text).strip()
    parts = [part.strip() for part in REFUSAL_PATTERN.split(cleaned)]
    if len(parts) != 2 or not parts[0] or not parts[1] or \
            any(REFUSAL_PATTERN.search(part) for part in parts):
        raise DefinitionError("拒答续答格式应为「题号>题号」，如 Q1>Q5")
    return parts[0], parts[1]


def validate_refusal(definition, answers, pair):
    """Validate one ``(source_qid, target_qid)`` refusal for a CSV row.

    The source must be a single-choice question that the normal walk actually
    reaches with a blank answer, and the target must be the next question of
    one of its legal options (an explicit jump target, or the following
    question when the option has no jump).

    Returns the ``(prefix_tuple, source_qid)`` storage key of the refusal.
    """
    source, target = pair
    index = index_of(definition)
    if source not in index:
        raise DefinitionError("拒答题 %s 不在问卷中" % source)
    question = definition[index[source]]
    if question.get("type") != SINGLE:
        raise DefinitionError("拒答题 %s 不是单选题" % source)
    if target not in index:
        raise DefinitionError("拒答续答的去向 %s 不在问卷中" % target)
    if not is_blank(answers.get(source)):
        raise DefinitionError("拒答题 %s 的答案格必须留空" % source)

    steps = walk_plain(definition, answers)
    reached = next((step for step in steps if step["qid"] == source), None)
    if reached is None or reached["answered"]:
        raise DefinitionError("拒答题 %s 不是按原规则走到的题" % source)

    legal = set(question.get("jumps") or {})
    position = index[source]
    next_in_order = (definition[position + 1]["id"]
                     if position + 1 < len(definition) else None)
    for option in question.get("options") or []:
        legal.add((question.get("jumps") or {}).get(option)
                  or next_in_order)
    legal.discard(None)
    if target not in legal:
        raise DefinitionError(
            "拒答续答 %s>%s 的去向不是 %s 合法选项的下一题"
            % (source, target, source))
    return reached["prefix"], source


def import_row(definition, answers, refusals=None):
    """Store answers for one imported CSV row.

    Only questions actually reached by the walk are kept.  The walk ends at
    the first blank answer, so values after it (and on questions skipped by
    a jump) are not imported at all.  When a refusal record applies, the
    refused question is left blank and the walk resumes at its recorded next
    question; later plain blank answers still stop the walk as usual.
    """
    steps = walk_plain(definition, answers, refusals=refusals)
    stored = {}
    for step in steps:
        if not step["answered"]:
            # A refused question stays blank but does not end the import: the
            # walk has already resumed at the recorded next question.
            if step.get("refused"):
                continue
            break
        stored[(step["prefix"], step["qid"])] = step["value"]
    return steps, stored


def plan_household_changes(definition, stored, submitted, refusals=None,
                           clear_refusals=None):
    """Storage changes for one submitted edit form, including refusals.

    ``submitted`` is an ordered list of ``(question_id, prefix, value)``
    triples, where ``prefix`` is the question sequence the form was rendered
    with.  A value is accepted only while the re-walk reaches that question
    with exactly that prefix, so:

    * changing an earlier single answer routes the form onto the other
      branch, and fields of the branch just left are ignored (their answers
      stay saved under their own path and return when it is walked again);
    * a question shared by two paths (e.g. the common cost question) keeps
      one answer per path, and submitting the old path's value never
      overwrites the other path's saved value.

    The walk is recomputed after every accepted value, using the edited
    answers.  A blank value deletes the answer for that exact path only.

    ``refusals`` maps ``(prefix, qid)`` to ``(source_qid, target_qid)`` and
    carries the recorded refusals currently stored for the household.  A
    refused question is rendered blank; filling its radio (or submitting any
    non-blank value) consumes the refusal and routes by the chosen option.
    Keys listed in ``clear_refusals`` delete the record while leaving the
    question blank, so the walk stops there.  Refusals on paths the current
    walk does not reach stay untouched (and reappear when the path returns).

    Returns ``(changes, ignored, refusal_changes)`` where ``changes`` maps
    ``(prefix_tuple, qid)`` to a value (or ``None`` for a deletion),
    ``refusal_changes`` maps refusal keys to ``None`` for deletions, and
    ``ignored`` lists the question ids submitted but not reached.
    """
    staged = dict(stored)
    staged_refusals = dict(refusals or {})
    pending = {}
    for qid, prefix, value in submitted:
        prefix = tuple(prefix)
        pending[(prefix, str(qid))] = "" if value is None else str(value)
    clears = {(tuple(prefix), qid)
              for prefix, qid in (clear_refusals or ())}
    accepted = set()
    changes = {}
    refusal_changes = {}

    prefix = []
    position = 0
    visited = set()
    index = index_of(definition)
    while 0 <= position < len(definition):
        question = definition[position]
        qid = question["id"]
        if qid in visited:
            break
        visited.add(qid)
        key = (tuple(prefix), qid)
        if key in pending:
            value = pending[key].strip()
            old = staged.get(key)
            if value == "":
                if old is not None:
                    del staged[key]
                    changes[key] = None
            else:
                if question["type"] == SINGLE and \
                        value not in (question.get("options") or []) and \
                        value != old:
                    raise DefinitionError(
                        "题 %s 的答案「%s」不在选项中" % (qid, value))
                if old != value:
                    staged[key] = value
                    changes[key] = value
            if value != "":
                # Filling the previously refused question consumes the
                # refusal and routes by the option actually chosen.
                if key in staged_refusals:
                    del staged_refusals[key]
                    refusal_changes[key] = None
            elif key in clears and key in staged_refusals:
                # The explicit "clear record" checkbox: the question stays
                # blank, so the walk stops here from now on.
                del staged_refusals[key]
                refusal_changes[key] = None
            accepted.add(key)
        elif key in clears and key in staged_refusals:
            del staged_refusals[key]
            refusal_changes[key] = None
        value = staged.get(key)
        value = "" if is_blank(value) else str(value).strip()
        if not value:
            refusal = staged_refusals.get(key)
            if refusal is not None and question.get("type") == SINGLE \
                    and refusal[1] in index:
                position = index[refusal[1]]
                prefix.append(qid)
                continue
            break
        target = _jump_target(question, value)
        position = index[target] if target else position + 1
        prefix.append(qid)

    # Refusal records on paths the current walk does not reach are never
    # touched: they reappear, with their later answers, when the walk returns
    # to that path.
    ignored = [key[1] for key in pending if key not in accepted]
    return changes, ignored, refusal_changes


def plan_changes(definition, stored, submitted):
    """Backwards-compatible wrapper without refusal handling."""
    changes, ignored, _ = plan_household_changes(
        definition, stored, submitted)
    return changes, ignored


def other_path_answers(definition, stored, active_steps, refusals=None):
    """Saved answers that are not on the active path (for transparency).

    Dormant refusal records (on paths the current walk does not reach) are
    listed too, so an interviewer switching branches can see that a recorded
    refusal still exists and will come back when the path returns.
    """
    active = {(step["prefix"], step["qid"]) for step in active_steps}
    by_qid = {question["id"]: question for question in definition}
    out = []
    for (prefix, qid), value in stored.items():
        if (prefix, qid) in active or qid not in by_qid:
            continue
        out.append({
            "qid": qid,
            "title": by_qid[qid].get("title", qid),
            "prefix": prefix,
            "value": value,
            "refusal_target": None,
        })
    for key, pair in (refusals or {}).items():
        prefix, qid = key
        if (prefix, qid) in active or qid not in by_qid:
            continue
        out.append({
            "qid": qid,
            "title": by_qid[qid].get("title", qid),
            "prefix": prefix,
            "value": "",
            "refusal_target": pair[1],
        })
    out.sort(key=lambda item: (item["qid"], item["prefix"]))
    return out
