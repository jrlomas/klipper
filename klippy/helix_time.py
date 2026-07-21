"""Transport-independent four-timestamp machine-time discipline."""

import collections
import dataclasses
import math
import statistics
import time


@dataclasses.dataclass(frozen=True)
class FourTimestampSample:
    epoch: int
    t1: int
    t2: int
    t3: int
    t4: int
    quality: int = 0

    @property
    def delay(self):
        return (self.t4 - self.t1) - (self.t3 - self.t2)

    @property
    def offset(self):
        return ((self.t2 - self.t1) + (self.t3 - self.t4)) / 2.

    @property
    def midpoint(self):
        return (self.t1 + self.t4) / 2.


class FourTimestampDiscipline:
    """Robust offset/drift estimate driven by exact MAC-boundary stamps.

    Times use an arbitrary but common integer unit (normally nanoseconds).
    Samples with impossible geometry, excessive path delay, stale epochs,
    reordered host timestamps, or median/MAD outliers are rejected.  The
    estimate remains usable for a bounded holdover interval after link loss.
    """
    def __init__(self, window=64, min_samples=8, max_delay=2_000_000,
                 holdover=5_000_000_000, clock=None):
        if window < min_samples or min_samples < 2:
            raise ValueError('invalid discipline window')
        self.window = window
        self.min_samples = min_samples
        self.max_delay = max_delay
        self.holdover = holdover
        self.clock = clock or time.monotonic_ns
        self.samples = collections.deque(maxlen=window)
        self.epoch = None
        self.offset = 0.
        self.drift = 0.
        self.reference = 0.
        self.last_host_time = None
        self.last_update = None
        self.generation = 0
        self.accepted = 0
        self.rejected = collections.Counter()

    def reset(self, epoch):
        self.samples.clear()
        self.epoch = epoch
        self.offset = self.drift = self.reference = 0.
        self.last_host_time = self.last_update = None
        self.generation += 1

    def _reject(self, reason):
        self.rejected[reason] += 1
        return False

    def add(self, sample, observed_at=None):
        if not sample.epoch:
            return self._reject('invalid_epoch')
        if self.epoch is None:
            self.reset(sample.epoch)
        elif sample.epoch != self.epoch:
            self.reset(sample.epoch)
        if not (sample.t1 <= sample.t4 and sample.t2 <= sample.t3):
            return self._reject('invalid_geometry')
        if sample.delay < 0 or sample.delay > self.max_delay:
            return self._reject('path_delay')
        if self.last_host_time is not None and sample.t1 <= self.last_host_time:
            return self._reject('reordered')
        if len(self.samples) >= self.min_samples:
            offsets = [item.offset for item in self.samples]
            median = statistics.median(offsets)
            mad = statistics.median(abs(value - median) for value in offsets)
            limit = max(250., 8. * mad)
            if abs(sample.offset - median) > limit:
                return self._reject('offset_outlier')
            delays = [item.delay for item in self.samples]
            dmedian = statistics.median(delays)
            dmad = statistics.median(abs(value - dmedian) for value in delays)
            if sample.delay > dmedian + max(1000., 8. * dmad):
                return self._reject('delay_outlier')
        self.samples.append(sample)
        self.last_host_time = sample.t1
        self.last_update = self.clock() if observed_at is None else observed_at
        self.accepted += 1
        self._fit()
        return True

    def _fit(self):
        ranked = sorted(self.samples, key=lambda item: item.delay)
        selected = ranked[:max(self.min_samples, (len(ranked) + 1) // 2)]
        if not selected:
            return
        reference = statistics.fmean(item.midpoint for item in selected)
        offsets = [item.offset for item in selected]
        xs = [item.midpoint - reference for item in selected]
        mean_offset = statistics.fmean(offsets)
        denom = sum(value * value for value in xs)
        drift = (sum(x * (offset - mean_offset)
                     for x, offset in zip(xs, offsets)) / denom
                 if denom else 0.)
        if not math.isfinite(drift) or abs(drift) > 0.001:
            self._reject('drift_bound')
            return
        self.reference = reference
        self.offset = mean_offset
        self.drift = drift
        self.generation += 1

    def estimate_remote(self, host_time):
        if not self.usable():
            raise RuntimeError('machine-time discipline is not usable')
        return host_time + self.offset + self.drift * (
            host_time - self.reference)

    def usable(self, now=None):
        if len(self.samples) < self.min_samples or self.last_update is None:
            return False
        now = self.clock() if now is None else now
        return now - self.last_update <= self.holdover

    def status(self, now=None):
        now = self.clock() if now is None else now
        age = None if self.last_update is None else now - self.last_update
        state = ('converged' if self.usable(now) else
                 'holdover_expired' if self.last_update is not None
                 and len(self.samples) >= self.min_samples else 'acquiring')
        delays = [sample.delay for sample in self.samples]
        return {
            'schema_version': 1, 'state': state, 'epoch': self.epoch,
            'generation': self.generation, 'samples': len(self.samples),
            'accepted': self.accepted, 'rejected': dict(self.rejected),
            'offset': self.offset, 'drift_ppm': self.drift * 1_000_000.,
            'delay_median': statistics.median(delays) if delays else None,
            'delay_max': max(delays) if delays else None,
            'age': age, 'holdover': self.holdover,
            'timestamp_source': 'simulated_or_mac_hardware',
        }
