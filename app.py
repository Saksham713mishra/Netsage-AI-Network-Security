"""
app.py
------
NetSage AI - main Flask app.

Routes:
  /                      dashboard / home
  /cases                 list all cases
  /cases/new             add a case by hand
  /cases/import          import cases from a CSV (the cases.csv deliverable)
  /cases/<id>             case detail: evidence, rule check, AI diagnosis, review
  /cases/<id>/diagnose     (POST) run the AI on this case
  /cases/<id>/rule-check    (POST) run the deterministic checker on this case
  /cases/<id>/review        (POST) log a human review decision
  /dashboard               charts + AI-vs-human agreement rate
  /responsible-ai-log        list of cases where AI needed correction
  /pkt-upload                upload a .pkt file to inspect

Run with: python app.py
Then open http://127.0.0.1:5000
"""

import os
import io
import csv
import json
from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

import database as db
import rule_checker
import ai_diagnose
import pkt_parser

def _normalize_env_file():
    """Handle Windows-created .env files that begin with a UTF-8 BOM."""
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8-sig") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip().lstrip("\ufeff")
                value = value.strip().strip('"').strip("'")
                if key and value and key not in os.environ:
                    os.environ[key] = value

    for key in list(os.environ):
        if key.startswith("\ufeff"):
            fixed = key.lstrip("\ufeff")
            if fixed not in os.environ and os.environ.get(key):
                os.environ[fixed] = os.environ[key]


load_dotenv()
_normalize_env_file()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

with app.app_context():
    db.init_db()


# ---------------------------------------------------------------- home

@app.route("/")
def home():
    stats = db.dashboard_stats()
    recent_cases = db.list_cases()[:6]
    return render_template("index.html", stats=stats, recent_cases=recent_cases)


# ---------------------------------------------------------------- cases

@app.route("/cases")
def cases_list():
    all_cases = db.list_cases()
    return render_template("cases_list.html", cases=all_cases)


@app.route("/cases/new", methods=["GET", "POST"])
def cases_new():
    if request.method == "POST":
        case_id = db.insert_case(
            case_ref=request.form.get("case_ref", "").strip(),
            symptom=request.form.get("symptom", "").strip(),
            topology_note=request.form.get("topology_note", "").strip(),
            show_output=request.form.get("show_output", "").strip(),
            expected_fault=request.form.get("expected_fault", "").strip(),
            osi_layer=request.form.get("osi_layer", "").strip(),
            concept_tag=request.form.get("concept_tag", "").strip(),
            severity=request.form.get("severity", "Medium").strip(),
        )
        flash("Case added.", "success")
        return redirect(url_for("case_detail", case_id=case_id))
    return render_template("case_form.html")


@app.route("/cases/import", methods=["GET", "POST"])
def cases_import():
    """Import cases.csv. Expected headers (order doesn't matter):
    case_ref, symptom, topology_note, show_output, expected_fault,
    osi_layer, concept_tag, severity
    """
    if request.method == "POST":
        file = request.files.get("csv_file")
        if not file or file.filename == "":
            flash("Choose a CSV file first.", "error")
            return redirect(url_for("cases_import"))

        stream = io.StringIO(file.stream.read().decode("utf-8-sig"), newline=None)
        reader = csv.DictReader(stream)
        imported = 0
        skipped = 0
        for row in reader:
            symptom = (row.get("symptom") or "").strip()
            show_output = (row.get("show_output") or row.get("show output") or "").strip()
            if not symptom or not show_output:
                skipped += 1
                continue
            db.insert_case(
                case_ref=(row.get("case_ref") or row.get("case ref") or "").strip(),
                symptom=symptom,
                topology_note=(row.get("topology_note") or row.get("topology note") or "").strip(),
                show_output=show_output,
                expected_fault=(row.get("expected_fault") or row.get("expected fault") or "").strip(),
                osi_layer=(row.get("osi_layer") or row.get("osi layer") or "").strip(),
                concept_tag=(row.get("concept_tag") or row.get("concept") or "").strip(),
                severity=(row.get("severity") or "Medium").strip(),
            )
            imported += 1

        flash(f"Imported {imported} cases." + (f" Skipped {skipped} rows missing symptom/show_output." if skipped else ""), "success")
        return redirect(url_for("cases_list"))

    return render_template("case_import.html")


@app.route("/cases/<int:case_id>")
def case_detail(case_id):
    case = db.get_case(case_id)
    if not case:
        flash("Case not found.", "error")
        return redirect(url_for("cases_list"))

    latest_diagnosis = db.get_latest_diagnosis(case_id)
    review = db.get_review_for_diagnosis(latest_diagnosis["id"]) if latest_diagnosis else None
    rule_check = db.get_latest_rule_check(case_id)
    rule_issues = json.loads(rule_check["issues_json"]) if rule_check else None

    return render_template(
        "case_detail.html",
        case=case,
        diagnosis=latest_diagnosis,
        review=review,
        rule_issues=rule_issues,
    )


@app.route("/cases/<int:case_id>/rule-check", methods=["POST"])
def run_rule_check(case_id):
    case = db.get_case(case_id)
    if not case:
        flash("Case not found.", "error")
        return redirect(url_for("cases_list"))

    issues = rule_checker.run_checks(
        case["show_output"], case["expected_fault"], case["concept_tag"], case["symptom"]
    )
    db.insert_rule_check(case_id, json.dumps(issues))
    flash(f"Rule checker found {len(issues)} issue(s).", "success")
    return redirect(url_for("case_detail", case_id=case_id))


@app.route("/cases/<int:case_id>/diagnose", methods=["POST"])
def run_diagnosis(case_id):
    case = db.get_case(case_id)
    if not case:
        flash("Case not found.", "error")
        return redirect(url_for("cases_list"))

    try:
        parsed, raw_text, model_used = ai_diagnose.diagnose_case(
            case["symptom"], case["topology_note"], case["show_output"]
        )
    except RuntimeError as e:
        flash(str(e), "error")
        return redirect(url_for("case_detail", case_id=case_id))
    except Exception as e:
        flash(f"AI call failed: {e}", "error")
        return redirect(url_for("case_detail", case_id=case_id))

    if parsed:
        db.insert_diagnosis(
            case_id=case_id,
            root_cause=parsed.get("root_cause", ""),
            confidence=parsed.get("confidence", ""),
            evidence=parsed.get("evidence", ""),
            next_command=parsed.get("next_command", ""),
            fix_steps=parsed.get("fix_steps", ""),
            raw_json=raw_text,
            model_used=model_used,
        )
        flash("AI diagnosis complete - review it below before accepting.", "success")
    else:
        db.insert_diagnosis(
            case_id=case_id,
            root_cause="(could not parse model response as JSON)",
            confidence="", evidence="", next_command="", fix_steps="",
            raw_json=raw_text,
            model_used=model_used,
        )
        flash("Model response wasn't valid JSON - raw output saved, check it manually.", "error")

    return redirect(url_for("case_detail", case_id=case_id))


@app.route("/cases/<int:case_id>/review", methods=["POST"])
def submit_review(case_id):
    diagnosis_id = request.form.get("diagnosis_id")
    if not diagnosis_id:
        flash("No diagnosis to review yet - run the AI diagnosis first.", "error")
        return redirect(url_for("case_detail", case_id=case_id))

    db.insert_review(
        diagnosis_id=diagnosis_id,
        status=request.form.get("status"),
        reviewer_name=request.form.get("reviewer_name", "").strip(),
        corrected_root_cause=request.form.get("corrected_root_cause", "").strip(),
        notes=request.form.get("notes", "").strip(),
    )
    flash("Review logged.", "success")
    return redirect(url_for("case_detail", case_id=case_id))


# ---------------------------------------------------------------- dashboard

@app.route("/dashboard")
def dashboard():
    stats = db.dashboard_stats()
    return render_template("dashboard.html", stats=stats)


# ---------------------------------------------------------------- responsible AI log

@app.route("/responsible-ai-log")
def responsible_ai_log():
    corrected = db.list_corrected_reviews()
    return render_template("responsible_ai_log.html", corrected=corrected)


# ---------------------------------------------------------------- .pkt upload

@app.route("/pkt-upload", methods=["GET", "POST"])
def pkt_upload():
    result = None
    if request.method == "POST":
        file = request.files.get("pkt_file")
        if not file or file.filename == "":
            flash("Choose a .pkt or .pka file first.", "error")
            return redirect(url_for("pkt_upload"))

        filename = secure_filename(file.filename)
        save_path = os.path.join(UPLOAD_DIR, filename)
        file.save(save_path)

        result = pkt_parser.analyze_pkt_file(save_path)
        extracted_text = result.get("xml_excerpt") or "\n".join(result.get("strings_found", []))
        db.insert_pkt_upload(filename, result["method_used"], extracted_text)

    return render_template("pkt_upload.html", result=result)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
