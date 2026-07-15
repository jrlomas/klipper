"""Read-only deterministic grounding from Moonraker's job history."""

import datetime
import os
import sqlite3
import urllib.parse
import re


class JobHistoryReader:
    def __init__(self, path):
        self.path = (os.path.abspath(os.path.expanduser(path))
                     if path else None)
        self.last_error = ""

    def _connect(self):
        if self.path is None or not os.path.isfile(self.path):
            raise FileNotFoundError("Moonraker job database is unavailable")
        uri = "file:%s?mode=ro" % urllib.parse.quote(self.path)
        return sqlite3.connect(uri, uri=True, timeout=1.0)

    @staticmethod
    def _job(row):
        filename = os.path.basename(str(row[1] or ""))[:256]
        filename = "".join(char for char in filename
                           if char.isprintable() and char not in "\r\n")
        return {
            "job_id": int(row[0]), "filename": filename,
            "status": str(row[2]), "start_time": float(row[3]),
            "end_time": (float(row[4]) if row[4] is not None else None),
            "print_duration": float(row[5]),
            "total_duration": float(row[6]),
            "filament_used": float(row[7]),
        }

    def recent(self, limit=5, status=None):
        limit = max(1, min(int(limit), 20))
        query = ("SELECT job_id, filename, status, start_time, end_time, "
                 "print_duration, total_duration, filament_used "
                 "FROM job_history")
        params = []
        if status is not None:
            query += " WHERE status=?"
            params.append(status)
        query += " ORDER BY COALESCE(end_time, start_time) DESC, job_id DESC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as connection:
                rows = connection.execute(query, params).fetchall()
        except (OSError, sqlite3.Error) as exc:
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            return []
        self.last_error = ""
        return [self._job(row) for row in rows]

    def context(self):
        completed = self.recent(limit=5, status="completed")
        recent = self.recent(limit=5)
        if not completed and not recent:
            return "(Moonraker job history unavailable)"

        def line(job):
            ended = job["end_time"]
            ended_text = (datetime.datetime.fromtimestamp(
                ended, datetime.timezone.utc).isoformat()
                          if ended is not None else "not-ended")
            return ("job_id=%d status=%s filename=%s completed_utc=%s "
                    "print_duration_s=%.3f total_duration_s=%.3f "
                    "filament_used_mm=%.3f" % (
                        job["job_id"], job["status"], job["filename"],
                        ended_text, job["print_duration"],
                        job["total_duration"], job["filament_used"]))

        return ("Last successful prints (newest first):\n%s\n"
                "Recent print attempts (newest first):\n%s" % (
                    "\n".join("- " + line(job) for job in completed)
                    or "- none",
                    "\n".join("- " + line(job) for job in recent)
                    or "- none"))

    def answer_if_authoritative(self, question):
        """Answer narrow job-history facts without asking the model."""
        terms = set(re.findall(r"[a-z]+", question.lower()))
        asks_last = bool(terms & {"last", "latest", "recent"})
        asks_print = bool(terms & {"print", "job"})
        asks_success = bool(terms & {
            "success", "successful", "succeeded", "completed"})
        if not (asks_last and asks_print and asks_success):
            return None
        jobs = self.recent(limit=1, status="completed")
        if not jobs:
            return "insufficient evidence: no completed print is recorded"
        job = jobs[0]
        ended = job["end_time"]
        ended_text = (datetime.datetime.fromtimestamp(
            ended, datetime.timezone.utc).isoformat()
                      if ended is not None else "an unknown time")
        return ("The last successful print was %s (job %d), completed at "
                "%s after %.1f seconds of printing and %.1f mm of filament."
                % (job["filename"], job["job_id"], ended_text,
                   job["print_duration"], job["filament_used"]))

    def status(self):
        return {
            "configured": self.path is not None,
            "available": bool(self.path and os.path.isfile(self.path)),
            "last_error": self.last_error,
        }
