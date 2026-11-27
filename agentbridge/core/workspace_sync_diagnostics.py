"""Read-only aggregate diagnostics for the durable Workspace mirror ledgers."""


def snapshot(db, subject, now):
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "workspace_query_destinations" not in tables:
        return {"available": False}
    if "selection" not in {row[1] for row in db.execute("PRAGMA table_info(workspace_query_destinations)")}:
        return {"available": False, "reason": "legacy_destination_schema"}
    admissions = [dict(row) for row in db.execute("""SELECT host,selection,COUNT(*) AS count
        FROM workspace_query_destinations WHERE subject=? GROUP BY host,selection ORDER BY host,selection""", (subject,))]
    ledgers = {}
    for mode, table in (("workspace_text_v2", "workspace_text_deliveries"),
                        ("workspace_query_v2", "workspace_query_deliveries")):
        if table not in tables:
            ledgers[mode] = {"available": False}
            continue
        rows = db.execute(f"""SELECT p.host,d.state,COUNT(*) AS count,
            SUM(CASE WHEN d.state='sent' AND typeof(d.receipt)='integer' AND d.receipt>0
                AND d.receipt<9007199254740992 THEN 1 ELSE 0 END) AS confirmed_receipts,
            SUM(CASE WHEN d.state='sending' AND d.due<=? THEN 1 ELSE 0 END) AS overdue_sending,
            MIN(CASE WHEN d.state IN ('pending','leased','retry_wait','sending','unknown','blocked')
                THEN d.due ELSE NULL END) AS oldest_outstanding_due_at
            FROM {table} d JOIN workspace_query_destinations p USING(question_id)
            WHERE p.subject=? GROUP BY p.host,d.state ORDER BY p.host,d.state""", (now,subject)).fetchall()
        ledgers[mode] = {"available": True, "states": [dict(row) for row in rows]}
    decisions_available = "workspace_text_projection_decisions" in tables
    decisions = []
    unclassified = None
    if decisions_available:
        decisions = [dict(row) for row in db.execute("""SELECT p.host,d.kind,d.decision,COUNT(*) AS count
            FROM workspace_text_projection_decisions d JOIN workspace_query_destinations p USING(question_id)
            WHERE p.subject=? GROUP BY p.host,d.kind,d.decision ORDER BY p.host,d.kind,d.decision""", (subject,))]
        unclassified = db.execute("""SELECT COUNT(*) FROM workspace_query_destinations p WHERE p.subject=?
            AND p.selection='paired' AND NOT EXISTS (SELECT 1 FROM workspace_text_projection_decisions d
                WHERE d.question_id=p.question_id AND d.entry_id=p.question_id)""", (subject,)).fetchone()[0]
    return {"available": True, "admissions": admissions, "ledgers": ledgers,
        "text_projection": {"available": decisions_available, "decisions": decisions,
            # Existing rows before this diagnostic was installed stay unclassified.
            # This is missing observation, never evidence of sending or failure.
            "paired_questions_without_decision": unclassified}}
