# Deterministic per-machine drift baselines (FD-0002 Milestone D floor).

import json
import math
import os
import tempfile


METRICS = {
    "link_stats": ("crc_errors", "retransmits"),
    "timesync": ("error_us",),
}


class BaselineMonitor:
    def __init__(self, path, min_samples=5, sigma=4.0):
        self.path = os.path.abspath(os.path.expanduser(path))
        self.min_samples = min_samples
        self.sigma = sigma
        self.stats = {}
        self._load()

    def _load(self):
        try:
            with open(self.path) as handle:
                data = json.load(handle)
            if data.get("schema_version") == 1:
                self.stats = data.get("metrics", {})
        except FileNotFoundError:
            pass

    def _save(self):
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".atlas-baseline-", dir=directory)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump({"schema_version": 1, "metrics": self.stats},
                          handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    def observe(self, events):
        alerts = []
        changed = False
        for event in events:
            for field in METRICS.get(event.kind, ()):
                value = event.fields.get(field)
                if not isinstance(value, (int, float)):
                    continue
                name = "%s.%s" % (event.source, field)
                stat = self.stats.setdefault(
                    name, {"count": 0, "mean": 0.0, "m2": 0.0})
                count = stat["count"]
                mean = stat["mean"]
                stddev = math.sqrt(stat["m2"] / max(1, count - 1))
                floor = 1.0 if field != "error_us" else 10.0
                threshold = max(floor, self.sigma * stddev)
                if count >= self.min_samples and abs(value - mean) > threshold:
                    alerts.append({
                        "metric": name, "value": value, "baseline": mean,
                        "threshold": threshold, "source": event.source,
                        "mtime": event.mtime})
                    continue
                count += 1
                delta = value - mean
                mean += delta / count
                stat.update(count=count, mean=mean,
                            m2=stat["m2"] + delta * (value - mean))
                changed = True
        if changed:
            self._save()
        return alerts
