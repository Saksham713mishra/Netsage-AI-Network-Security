"""
database.py
------------
All the SQLite stuff for NetSage AI lives here. We use plain sqlite3
(no ORM) because the schema is small and it's easier to reason about
for a college project than dragging in SQLAlchemy.

Tables:
  cases        - the 30+ troubleshooting cases (symptom, show output, etc.)
  diagnoses    - one row per AI diagnosis run against a case
  reviews      - human review decision for a diagnosis (Accepted/Edited/Rejected)
  rule_checks  - output of the deterministic Python checker for a case
  pkt_uploads  - log of .pkt files people have uploaded to inspect
"""

import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "netsage.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_ref TEXT,                  -- optional human label like "C-014"
            symptom TEXT NOT NULL,
            topology_note TEXT,
            show_output TEXT NOT NULL,
            expected_fault TEXT,
            osi_layer TEXT,
            concept_tag TEXT,
            severity TEXT DEFAULT 'Medium',
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS diagnoses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
            root_cause TEXT,
            confidence TEXT,
            evidence TEXT,
            next_command TEXT,
            fix_steps TEXT,
            raw_json TEXT,                  -- full model response, kept for debugging
            model_used TEXT,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            diagnosis_id INTEGER NOT NULL REFERENCES diagnoses(id) ON DELETE CASCADE,
            status TEXT NOT NULL,           -- Accepted / Edited / Rejected
            reviewer_name TEXT,
            corrected_root_cause TEXT,
            notes TEXT,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS rule_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
            issues_json TEXT NOT NULL,      -- json list of {type, severity, message}
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS pkt_uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            method_used TEXT,               -- how we got data out ("zlib", "xor+zlib", "strings-only")
            extracted_text TEXT,
            linked_case_id INTEGER REFERENCES cases(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()


def now():
    return datetime.utcnow().isoformat()


# ---------- cases ----------

def insert_case(case_ref, symptom, topology_note, show_output,
                 expected_fault, osi_layer, concept_tag, severity):
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO cases
           (case_ref, symptom, topology_note, show_output, expected_fault,
            osi_layer, concept_tag, severity, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (case_ref, symptom, topology_note, show_output, expected_fault,
         osi_layer, concept_tag, severity, now()),
    )
    conn.commit()
    case_id = cur.lastrowid
    conn.close()
    return case_id


def list_cases():
    conn = get_db()
    rows = conn.execute("SELECT * FROM cases ORDER BY id DESC").fetchall()
    conn.close()
    return rows


def get_case(case_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
    conn.close()
    return row


def case_count():
    conn = get_db()
    n = conn.execute("SELECT COUNT(*) AS n FROM cases").fetchone()["n"]
    conn.close()
    return n


# ---------- diagnoses ----------

def insert_diagnosis(case_id, root_cause, confidence, evidence,
                      next_command, fix_steps, raw_json, model_used):
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO diagnoses
           (case_id, root_cause, confidence, evidence, next_command,
            fix_steps, raw_json, model_used, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (case_id, root_cause, confidence, evidence, next_command,
         fix_steps, raw_json, model_used, now()),
    )
    conn.commit()
    diag_id = cur.lastrowid
    conn.close()
    return diag_id


def get_latest_diagnosis(case_id):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM diagnoses WHERE case_id = ? ORDER BY id DESC LIMIT 1",
        (case_id,),
    ).fetchone()
    conn.close()
    return row


def get_diagnosis(diagnosis_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM diagnoses WHERE id = ?", (diagnosis_id,)).fetchone()
    conn.close()
    return row


def list_diagnoses_for_case(case_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM diagnoses WHERE case_id = ? ORDER BY id DESC", (case_id,)
    ).fetchall()
    conn.close()
    return rows


# ---------- reviews ----------

def insert_review(diagnosis_id, status, reviewer_name, corrected_root_cause, notes):
    conn = get_db()
    conn.execute(
        """INSERT INTO reviews
           (diagnosis_id, status, reviewer_name, corrected_root_cause, notes, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (diagnosis_id, status, reviewer_name, corrected_root_cause, notes, now()),
    )
    conn.commit()
    conn.close()


def get_review_for_diagnosis(diagnosis_id):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM reviews WHERE diagnosis_id = ? ORDER BY id DESC LIMIT 1",
        (diagnosis_id,),
    ).fetchone()
    conn.close()
    return row


def list_all_reviews():
    conn = get_db()
    rows = conn.execute("""
        SELECT r.*, d.case_id, d.root_cause AS ai_root_cause, c.symptom, c.case_ref
        FROM reviews r
        JOIN diagnoses d ON d.id = r.diagnosis_id
        JOIN cases c ON c.id = d.case_id
        ORDER BY r.id DESC
    """).fetchall()
    conn.close()
    return rows


def list_corrected_reviews():
    """Cases where the human edited or rejected the AI's answer -
    this feeds the Responsible AI log."""
    conn = get_db()
    rows = conn.execute("""
        SELECT r.*, d.case_id, d.root_cause AS ai_root_cause, d.confidence,
               c.symptom, c.case_ref
        FROM reviews r
        JOIN diagnoses d ON d.id = r.diagnosis_id
        JOIN cases c ON c.id = d.case_id
        WHERE r.status IN ('Edited', 'Rejected')
        ORDER BY r.id DESC
    """).fetchall()
    conn.close()
    return rows


# ---------- rule checks ----------

def insert_rule_check(case_id, issues_json):
    conn = get_db()
    conn.execute(
        "INSERT INTO rule_checks (case_id, issues_json, created_at) VALUES (?, ?, ?)",
        (case_id, issues_json, now()),
    )
    conn.commit()
    conn.close()


def get_latest_rule_check(case_id):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM rule_checks WHERE case_id = ? ORDER BY id DESC LIMIT 1",
        (case_id,),
    ).fetchone()
    conn.close()
    return row


# ---------- pkt uploads ----------

def insert_pkt_upload(filename, method_used, extracted_text, linked_case_id=None):
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO pkt_uploads (filename, method_used, extracted_text, linked_case_id, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (filename, method_used, extracted_text, linked_case_id, now()),
    )
    conn.commit()
    upload_id = cur.lastrowid
    conn.close()
    return upload_id


def list_pkt_uploads():
    conn = get_db()
    rows = conn.execute("SELECT * FROM pkt_uploads ORDER BY id DESC").fetchall()
    conn.close()
    return rows


# ---------- dashboard aggregates ----------

def dashboard_stats():
    conn = get_db()

    total_cases = conn.execute("SELECT COUNT(*) AS n FROM cases").fetchone()["n"]
    total_diagnoses = conn.execute("SELECT COUNT(*) AS n FROM diagnoses").fetchone()["n"]

    by_fault = conn.execute("""
        SELECT COALESCE(expected_fault, 'Unclassified') AS fault, COUNT(*) AS n
        FROM cases GROUP BY fault ORDER BY n DESC
    """).fetchall()

    by_layer = conn.execute("""
        SELECT COALESCE(osi_layer, 'Unclassified') AS layer, COUNT(*) AS n
        FROM cases GROUP BY layer ORDER BY n DESC
    """).fetchall()

    by_severity = conn.execute("""
        SELECT COALESCE(severity, 'Unclassified') AS severity, COUNT(*) AS n
        FROM cases GROUP BY severity ORDER BY n DESC
    """).fetchall()

    review_counts = conn.execute("""
        SELECT status, COUNT(*) AS n FROM reviews GROUP BY status
    """).fetchall()

    conn.close()

    review_map = {r["status"]: r["n"] for r in review_counts}
    accepted = review_map.get("Accepted", 0)
    edited = review_map.get("Edited", 0)
    rejected = review_map.get("Rejected", 0)
    reviewed_total = accepted + edited + rejected
    agreement_rate = round((accepted / reviewed_total) * 100, 1) if reviewed_total else None

    return {
        "total_cases": total_cases,
        "total_diagnoses": total_diagnoses,
        "by_fault": [dict(r) for r in by_fault],
        "by_layer": [dict(r) for r in by_layer],
        "by_severity": [dict(r) for r in by_severity],
        "accepted": accepted,
        "edited": edited,
        "rejected": rejected,
        "reviewed_total": reviewed_total,
        "agreement_rate": agreement_rate,
    }
