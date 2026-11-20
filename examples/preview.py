"""Sample data: a self-buy / rental branch survey with a shared cost question."""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import engine  # noqa: E402
import importer  # noqa: E402


def load():
    with open(os.path.join(ROOT, "examples", "answers.csv"), "rb") as handle:
        raw_csv = handle.read()
    with open(os.path.join(ROOT, "examples", "jumps.json"), "rb") as handle:
        raw_json = handle.read()
    definition, warnings, rows, _, _, _ = importer.parse(raw_csv, raw_json)
    return definition, warnings, rows


def main():
    definition, warnings, rows = load()
    print("题目：")
    for question in definition:
        print("  %-3s %-8s %s  jumps=%s" % (
            question["id"], question["type"], question["title"],
            question["jumps"] or "-"))
    if warnings:
        print("\n提示：")
        for warning in warnings:
            print("  -", warning)

    print("\n各户当前路径：")
    answers = importer.to_import_answers(definition, rows)
    refusals = importer.to_import_refusals(rows)
    for row in rows:
        stored = answers[row["household_no"]]
        row_refusals = refusals.get(row["household_no"])
        steps = engine.walk_stored(definition, stored, refusals=row_refusals)
        missing = engine.first_missing(steps)
        refused = [step["qid"] for step in steps if step.get("refused")]
        print("  %-5s %-32s 首个漏答: %-4s 拒答: %s" % (
            row["household_no"], " → ".join(engine.route(steps)),
            missing["qid"] if missing else "无",
            "、".join(refused) if refused else "无"))

    print("\n示例 JSON：")
    print(json.dumps(
        {"order": [q["id"] for q in definition],
         "jumps": {q["id"]: q["jumps"] for q in definition if q["jumps"]}},
        ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
