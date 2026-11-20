"""Flask app: import survey CSV + JSON jump table, edit per household, export.

Key behaviours:

* Import walks the routing rules for every CSV row and keeps only the values
  on the questions the walk actually reached (stopping at the first blank).
* Every answer is stored under the path (question-id sequence) that reached
  it, so two routes into the same question keep separate answers and the
  earlier route's answers reappear when the walk returns to it.
* Exports show each household's current path, its effective answers, and its
  first unanswered question.
* Saves happen in one transaction; if anything fails, both the web page and
  the download keep showing the previously saved answers.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3

from flask import (Flask, abort, flash, redirect, render_template, request,
                   send_file, url_for)

import engine
import importer
import storage

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024
app.secret_key = os.environ.get("SURVEY_SECRET", "orchard-survey-dev")

DB_PATH = storage.db_path_from_env()


def get_db():
    directory = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(directory, exist_ok=True)
    return storage.connect(DB_PATH)


@app.errorhandler(sqlite3.Error)
def database_unavailable(error):
    """A storage failure never exposes half-written answers.

    Saves run in a transaction, so the previously committed survey is still
    intact; both the pages and the CSV download keep serving it once storage
    is reachable again.
    """
    app.logger.exception("database error")
    return render_template(
        "error.html",
        title="数据库暂时不可用",
        message="本次操作没有保存，网页和下载都会继续使用上次成功保存的答卷。",
        detail=str(error)), 503


@app.errorhandler(413)
def too_large(error):
    flash("上传文件过大（上限 32 MB）", "error")
    return redirect(url_for("index"))


def load_batch_or_404(connection, batch_id):
    info = storage.get_batch(connection, batch_id)
    if info is None:
        abort(404)
    return info


@app.get("/")
def index():
    connection = get_db()
    batches = storage.list_batches(connection)
    connection.close()
    return render_template("index.html", batches=batches)


@app.post("/upload")
def upload():
    csv_file = request.files.get("csv_file")
    json_file = request.files.get("json_file")
    if csv_file is None or not csv_file.filename:
        flash("请选择答卷 CSV 文件", "error")
        return redirect(url_for("index"))
    if json_file is None or not json_file.filename:
        flash("请选择 JSON 跳题表", "error")
        return redirect(url_for("index"))

    try:
        definition, warnings, rows, _, _, has_refusal_col = importer.parse(
            csv_file.read(), json_file.read())
    except (importer.ImportError_, engine.DefinitionError) as error:
        flash("导入失败：%s" % error, "error")
        return redirect(url_for("index"))

    answers = importer.to_import_answers(definition, rows)
    refusals = importer.to_import_refusals(rows)
    name = request.form.get("batch_name", "").strip() or \
        os.path.splitext(os.path.basename(csv_file.filename))[0]

    connection = get_db()
    try:
        batch_id = storage.create_batch(
            connection, name, definition,
            [(row["household_no"], row["row_number"]) for row in rows],
            answers, refusals_by_household=refusals,
            has_refusals=has_refusal_col)
    finally:
        connection.close()

    flash("已导入 %d 户，%d 道题" % (len(rows), len(definition)), "success")
    if warnings:
        flash("；".join(dict.fromkeys(warnings)), "warning")
    return redirect(url_for("batch", batch_id=batch_id))


@app.get("/batch/<int:batch_id>")
def batch(batch_id):
    connection = get_db()
    info = load_batch_or_404(connection, batch_id)
    definition = info["definition"]
    households = storage.list_households(connection, batch_id)

    summaries = []
    for household in households:
        stored = storage.load_answers(connection, household["id"])
        refusals = storage.load_refusals(connection, household["id"])
        steps = engine.walk_stored(definition, stored, refusals=refusals)
        missing = engine.first_missing(steps)
        summaries.append({
            "id": household["id"],
            "household_no": household["household_no"],
            "route": engine.route(steps),
            "missing": missing["qid"] if missing else None,
            "answered": sum(1 for step in steps if step["answered"]),
        })
    connection.close()
    return render_template("batch.html", batch=info, households=summaries,
                           question_count=len(definition))


@app.get("/batch/<int:batch_id>/household/<int:household_id>")
def household(batch_id, household_id):
    connection = get_db()
    info = load_batch_or_404(connection, batch_id)
    record = storage.get_household(connection, batch_id, household_id)
    if record is None:
        connection.close()
        abort(404)
    stored = storage.load_answers(connection, household_id)
    refusals = storage.load_refusals(connection, household_id)
    connection.close()
    return _render_household(info, record, stored, refusals)


@app.post("/batch/<int:batch_id>/household/<int:household_id>")
def save_household(batch_id, household_id):
    connection = get_db()
    info = load_batch_or_404(connection, batch_id)
    record = storage.get_household(connection, batch_id, household_id)
    if record is None:
        connection.close()
        abort(404)

    submitted, clear_refusals = collect_submitted(request.form)
    stored = storage.load_answers(connection, household_id)
    refusals = storage.load_refusals(connection, household_id)
    try:
        changes, ignored, refusal_changes = engine.plan_household_changes(
            info["definition"], stored, submitted,
            refusals=refusals, clear_refusals=clear_refusals)
        storage.save_answers(connection, household_id, changes,
                             refusal_changes=refusal_changes)
    except engine.DefinitionError as error:
        connection.close()
        flash("保存失败：%s（网页和下载仍显示上次保存的答卷）" % error, "error")
        return redirect(url_for("household", batch_id=batch_id,
                                household_id=household_id))
    except Exception as error:  # transaction rolled back; nothing changed
        connection.close()
        app.logger.exception("save failed")
        flash("保存失败：%s（网页和下载仍显示上次保存的答卷）" % error, "error")
        return redirect(url_for("household", batch_id=batch_id,
                                household_id=household_id))

    current = storage.load_answers(connection, household_id)
    current_refusals = storage.load_refusals(connection, household_id)
    connection.close()
    if ignored:
        flash("题 %s 不在当前路径上，未保存" % "、".join(dict.fromkeys(ignored)),
              "warning")
    if refusal_changes:
        flash("拒答记录已处理，后答按当前路径保留或停用", "success")
    saved = sum(1 for value in changes.values() if value is not None)
    if saved:
        flash("已保存 %d 处修改" % saved, "success")
    elif not refusal_changes:
        flash("没有需要保存的修改", "success")
    return _render_household(info, record, current, current_refusals)


def collect_submitted(form):
    """Turn the edit form into ``(entries, clear_refusals)``.

    ``entries`` is ``[(qid, prefix, value)]`` in page order; ``clear_refusals``
    is the set of prefixes whose refusal record was explicitly cleared.
    """
    entries = []
    clear_refusals = set()
    index = 1
    while ("k_qid_%d" % index) in form:
        qid = form["k_qid_%d" % index]
        prefix = form.get("k_prefix_%d" % index, "[]")
        try:
            parsed = json.loads(prefix)
            prefix = tuple(str(item) for item in parsed)
        except (TypeError, ValueError):
            prefix = ()
        entries.append((qid, prefix, form.get("v_%d" % index, "")))
        if form.get("r_clear_%d" % index):
            clear_refusals.add((prefix, qid))
        index += 1
    return entries, clear_refusals


def _render_household(info, record, stored, refusals=None):
    definition = info["definition"]
    steps = engine.walk_stored(definition, stored, refusals=refusals)
    return render_template(
        "household.html",
        batch=info,
        household=record,
        steps=steps,
        missing=engine.first_missing(steps),
        others=engine.other_path_answers(
            definition, stored, steps, refusals=refusals),
    )


@app.get("/batch/<int:batch_id>/export.csv")
def export_csv(batch_id):
    connection = get_db()
    info = load_batch_or_404(connection, batch_id)
    definition = info["definition"]
    households = storage.list_households(connection, batch_id)

    qids = [question["id"] for question in definition]
    titles = {question["id"]: question["title"] for question in definition}

    output = io.StringIO()
    writer = csv.writer(output)
    header = ["户号", "当前路径", "已答题数"]
    header += ["%s %s" % (qid, titles[qid]) for qid in qids]
    header += ["首个漏答题", "漏答题干"]
    if info.get("has_refusals"):
        header.append(importer.REFUSAL_HEADER)
    writer.writerow(header)

    for household in households:
        stored = storage.load_answers(connection, household["id"])
        refusals = (storage.load_refusals(connection, household["id"])
                    if info.get("has_refusals") else None)
        steps = engine.walk_stored(definition, stored, refusals=refusals)
        values = {step["qid"]: step["value"] for step in steps
                  if step["answered"]}
        missing = engine.first_missing(steps)
        row = [household["household_no"], " → ".join(engine.route(steps)),
               len(values)]
        row += [values.get(qid, "") for qid in qids]
        row += [missing["qid"] if missing else "",
                missing["title"] if missing else ""]
        if info.get("has_refusals"):
            # Only refusals still effective on the current path are exported;
            # dormant (bypassed) and cleared records stay blank.
            active = next((step for step in steps if step.get("refused")),
                          None)
            row.append("%s>%s" % (active["qid"], active["refusal_target"])
                       if active else "")
        writer.writerow(row)

    connection.close()
    data = output.getvalue().encode("utf-8-sig")
    return send_file(io.BytesIO(data), mimetype="text/csv",
                     as_attachment=True,
                     download_name="survey_%d_export.csv" % batch_id)


@app.get("/batch/<int:batch_id>/definition.json")
def export_definition(batch_id):
    connection = get_db()
    info = load_batch_or_404(connection, batch_id)
    connection.close()
    return info["definition"]


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")),
            debug=os.environ.get("SURVEY_DEBUG") == "1")
