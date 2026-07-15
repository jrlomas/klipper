#!/usr/bin/env python3
"""Atlas read-only Moonraker job-history grounding tests."""

import os
import pathlib
import sqlite3
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from atlas.jobs import JobHistoryReader  # noqa: E402


def test_completed_history_is_authoritative_and_newest_first():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "moonraker-sql.db")
        with sqlite3.connect(path) as connection:
            connection.execute("""CREATE TABLE job_history (
                job_id INTEGER PRIMARY KEY, filename TEXT, status TEXT,
                start_time REAL, end_time REAL, print_duration REAL,
                total_duration REAL, filament_used REAL)""")
            connection.executemany(
                "INSERT INTO job_history VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
                    (1, "first.gcode", "completed", 10, 20, 8, 10, 100),
                    (2, "failed.gcode", "cancelled", 30, 40, 3, 10, 20),
                    (3, "latest.gcode", "completed", 50, 60, 9, 10, 110),
                ])
        reader = JobHistoryReader(path)
        completed = reader.recent(status="completed")
        assert [job["filename"] for job in completed] == [
            "latest.gcode", "first.gcode"]
        context = reader.context()
        assert context.index("latest.gcode") < context.index("first.gcode")
        assert "failed.gcode" in context
        assert "completed_utc=1970-01-01T00:01:00+00:00" in context
        assert reader.status()["available"] is True
        answer = reader.answer_if_authoritative(
            "What's the last print that succeeded on this printer?")
        assert "latest.gcode (job 3)" in answer
        assert "9.0 seconds" in answer
        assert reader.answer_if_authoritative(
            "What temperature is the bed?") is None
        print("PASS: authoritative completed jobs are newest-first and "
              "prompt-ready")


def test_missing_or_wrong_schema_fails_to_unavailable_context():
    with tempfile.TemporaryDirectory() as tmp:
        missing = JobHistoryReader(os.path.join(tmp, "missing.db"))
        assert "unavailable" in missing.context()
        assert missing.last_error
        wrong = os.path.join(tmp, "wrong.db")
        sqlite3.connect(wrong).close()
        reader = JobHistoryReader(wrong)
        assert "unavailable" in reader.context()
        assert reader.last_error
        print("PASS: missing or incompatible history fails closed")


def main():
    test_completed_history_is_authoritative_and_newest_first()
    test_missing_or_wrong_schema_fails_to_unavailable_context()
    print("ALL PASS")


if __name__ == "__main__":
    main()
