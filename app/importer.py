"""CSV parsing and questionnaire building from uploaded files."""

from __future__ import annotations

import csv
import io
import json
import re

import engine

HOUSEHOLD_HEADERS = ("户号", "户编号", "户主编号", "农户编号", "household_no",
                     "household", "household_id")
REFUSAL_HEADER = "拒答续答"
_SEPARATORS = re.compile(r"[\s:：、，,|/\\\-—_（(]+")
_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "gbk")


class ImportError_(ValueError):
    """An uploaded file cannot be imported."""


def decode(raw):
    for encoding in _ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ImportError_("无法识别 CSV 编码，请另存为 UTF-8 或 GBK")


def read_csv(raw):
    text = decode(raw)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows = [row for row in reader]
    if not rows:
        raise ImportError_("CSV 是空的")
    return rows


def _clean_header(header):
    return (header or "").replace("﻿", "").strip()


def find_household_column(headers):
    cleaned = [_clean_header(h) for h in headers]
    for position, header in enumerate(cleaned):
        lowered = header.lower()
        for candidate in HOUSEHOLD_HEADERS:
            if lowered == candidate.lower():
                return position
    for position, header in enumerate(cleaned):
        if "户号" in header or "编号" in header:
            return position
    return 0


def header_key(header):
    """``A1 设备来源`` -> ``("A1", "设备来源")``; ``设备来源`` -> ``(None, 设备来源)``."""
    cleaned = _clean_header(header)
    parts = _SEPARATORS.split(cleaned, maxsplit=1)
    first = parts[0].strip()
    if parts[1:]:
        rest = cleaned[len(first):].strip()
        rest = rest.lstrip("".join(" \t:：、，,|/\\-—_（）()"))
        return (first or None), rest.strip()
    return first or None, cleaned


def question_id_for_header(header, definition):
    """Map a CSV header to a question id.

    Accepts the bare id, ``A1 设备来源``, ``A1：设备来源`` and the title.
    """
    cleaned = _clean_header(header)
    ids = [question["id"] for question in definition]
    if cleaned in ids:
        return cleaned
    first = _SEPARATORS.split(cleaned, maxsplit=1)[0].strip()
    if first in ids:
        return first
    for question in definition:
        if cleaned == question["title"]:
            return question["id"]
    return None


def read_json(raw):
    try:
        return json.loads(decode(raw))
    except (ValueError, TypeError) as error:
        raise ImportError_("JSON 跳题表解析失败: %s" % error)


def parse(raw_csv, raw_json, household_column=None):
    """Parse uploads into ``(definition, warnings, rows)``.

    ``rows`` is a list of ``{household_no, row_number, answers}`` where
    ``answers`` maps question id to the cell text.
    """
    rows = read_csv(raw_csv)
    headers = [_clean_header(h) for h in rows[0]]
    if not any(headers):
        raise ImportError_("CSV 表头为空")

    refusal_position = next(
        (position for position, header in enumerate(headers)
         if header == REFUSAL_HEADER), None)
    household_position = (find_household_column(headers)
                          if household_column in (None, "")
                          else int(household_column))
    if household_position < 0 or household_position >= len(headers):
        raise ImportError_("户号列位置超出范围")
    if len(headers) < 2:
        raise ImportError_("CSV 至少需要户号列和一个题目列")

    data = read_json(raw_json) if raw_json else None

    # Question order and titles come from the JSON when it declares them.
    # A bare jump table leaves the order to the CSV columns.
    json_columns = engine.json_columns(data) if data is not None else None
    if json_columns:
        columns = [{"id": column["id"],
                    "title": column["title"] or column["id"]}
                   for column in json_columns]
    else:
        columns = []
        for position, header in enumerate(headers):
            if position == household_position or \
                    position == refusal_position:
                continue
            key, title = header_key(header)
            columns.append({"id": key or title, "title": title})

    column_map = {}
    for position, header in enumerate(headers):
        if position == household_position or \
                position == refusal_position:
            continue
        qid = question_id_for_header(header, columns)
        if qid is None:
            continue
        column_map[position] = qid

    if not column_map:
        raise ImportError_("CSV 中没有能对应到题号的列")

    parsed_rows = []
    observed = {}
    seen = set()
    warnings = []
    for row_number, raw_row in enumerate(rows[1:], start=1):
        if not any((cell or "").strip() for cell in raw_row):
            continue
        household_no = (raw_row[household_position]
                        if household_position < len(raw_row) else "").strip()
        if not household_no:
            warnings.append("CSV 第 %d 行缺少户号，已跳过" % (row_number + 1))
            continue
        if household_no in seen:
            warnings.append("户号 %s 重复出现，只保留第一次" % household_no)
            continue
        seen.add(household_no)
        answers = {}
        for position, qid in column_map.items():
            cell = (raw_row[position]
                    if position < len(raw_row) else "").strip()
            answers[qid] = cell
            if cell:
                observed.setdefault(qid, [])
                if cell not in observed[qid]:
                    observed[qid].append(cell)
        refusal_cell = ""
        if refusal_position is not None:
            refusal_cell = (raw_row[refusal_position]
                            if refusal_position < len(raw_row)
                            else "").strip()
        parsed_rows.append({
            "household_no": household_no,
            "row_number": row_number,
            "answers": answers,
            "refusal_cell": refusal_cell,
        })

    if not parsed_rows:
        raise ImportError_("CSV 中没有数据行")

    column_definitions = [{"id": qid, "title": next(
        (header for position, header in enumerate(headers)
         if column_map.get(position) == qid), qid)}
        for qid in dict.fromkeys(column_map.values())]

    if data is None:
        definition = engine.build_definition_from_columns(column_definitions)
        warnings.append("未上传 JSON 跳题表：按 CSV 列顺序生成纯文本问卷")
    else:
        try:
            definition, build_warnings = engine.build_definition(
                data, column_definitions, observed)
        except engine.DefinitionError as error:
            raise ImportError_(str(error))
        warnings.extend(build_warnings)

    if refusal_position is not None:
        # Every recorded refusal is checked against the routing rules; one bad
        # record rejects the whole batch and nothing is stored.
        for row in parsed_rows:
            cell = row.get("refusal_cell") or ""
            if not cell:
                continue
            try:
                pair = engine.parse_refusal(cell)
                prefix, source = engine.validate_refusal(
                    definition, row["answers"], pair)
            except engine.DefinitionError as error:
                raise ImportError_(
                    "户号 %s 的拒答续答有误：%s"
                    % (row["household_no"], error))
            row["refusal_key"] = (tuple(prefix), source)
            row["refusal_pair"] = pair

    return (definition, warnings, parsed_rows, household_position,
            headers, refusal_position is not None)


def to_import_answers(definition, rows):
    """Key answers by ``(prefix, qid)`` using the routing rules."""
    stored = {}
    for row in rows:
        refusals = _row_refusals(row)
        _, answers = engine.import_row(
            definition, row["answers"], refusals=refusals)
        stored[row["household_no"]] = answers
    return stored


def _row_refusals(row):
    key = row.get("refusal_key")
    pair = row.get("refusal_pair")
    if not key or not pair:
        return None
    return {(tuple(key[0]), key[1]): (pair[0], pair[1])}


def to_import_refusals(rows):
    """Refusal records keyed by household_no and ``(prefix, qid)``."""
    refusals_by_household = {}
    for row in rows:
        refusals = _row_refusals(row)
        if refusals:
            refusals_by_household[row["household_no"]] = refusals
    return refusals_by_household
