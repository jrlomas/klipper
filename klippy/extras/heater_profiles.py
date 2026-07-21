# Versioned characterization storage and bounded gain scheduling for Helix.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import hashlib, json, math, os, stat, tempfile, time


STORE_VERSION = 1
MODEL_VERSION = 1
GAIN_NAMES = ('kp', 'ki', 'kd')
THERMAL_MODEL_NAMES = ('gain', 'tau', 'delay')


def _median(values):
    values = sorted(values)
    size = len(values)
    if size & 1:
        return values[size // 2]
    return .5 * (values[size // 2 - 1] + values[size // 2])


def _run_id(run):
    payload = json.dumps(run, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _solve_3x3(matrix, vector):
    """Solve a small dense system with pivoting; return None if singular."""
    rows = [list(matrix[i]) + [vector[i]] for i in range(3)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda row: abs(rows[row][col]))
        if abs(rows[pivot][col]) < 1.e-12:
            return None
        rows[col], rows[pivot] = rows[pivot], rows[col]
        divisor = rows[col][col]
        rows[col] = [value / divisor for value in rows[col]]
        for row in range(3):
            if row == col:
                continue
            factor = rows[row][col]
            rows[row] = [rows[row][idx] - factor * rows[col][idx]
                         for idx in range(4)]
    return [rows[row][3] for row in range(3)]


def _cross(origin, first, second):
    return ((first[0] - origin[0]) * (second[1] - origin[1])
            - (first[1] - origin[1]) * (second[0] - origin[0]))


def _convex_hull(points):
    """Return the counter-clockwise hull of unique two-dimensional points."""
    points = sorted(set(points))
    if len(points) <= 1:
        return points
    lower = []
    for point in points:
        while len(lower) >= 2 and _cross(lower[-2], lower[-1], point) <= 0.:
            lower.pop()
        lower.append(point)
    upper = []
    for point in reversed(points):
        while len(upper) >= 2 and _cross(upper[-2], upper[-1], point) <= 0.:
            upper.pop()
        upper.append(point)
    return lower[:-1] + upper[:-1]


def _inside_convex_hull(point, hull):
    """Return true for points inside or on a non-degenerate convex hull."""
    if len(hull) < 3:
        return False
    tolerance = 1.e-9
    signs = [_cross(hull[pos], hull[(pos + 1) % len(hull)], point)
             for pos in range(len(hull))]
    return (all(value >= -tolerance for value in signs)
            or all(value <= tolerance for value in signs))


def fit_first_order_step(samples, power):
    """Fit T=T0+A*(1-exp(-(t-delay)/tau)) to a constant-power step.

    The host may spend computation on a bounded delay/tau grid; the MCU only
    receives the resulting first-order coefficients.  This fitter deliberately
    rejects weak, short, or boundary-limited experiments instead of inventing
    a confident plant model from insufficient heating evidence.
    """
    if not math.isfinite(float(power)) or not 0. < power <= 1.:
        raise ValueError('thermal step power must be in (0, 1]')
    cleaned = []
    for stamp, temp in samples:
        stamp, temp = float(stamp), float(temp)
        if math.isfinite(stamp) and math.isfinite(temp):
            cleaned.append((stamp, temp))
    cleaned.sort()
    if len(cleaned) < 30:
        raise ValueError('thermal step requires at least 30 samples')
    start = cleaned[0][0]
    measured = [(stamp - start, temp) for stamp, temp in cleaned]
    duration = measured[-1][0]
    if duration < 10.:
        raise ValueError('thermal step duration is too short')
    baseline_count = max(3, min(10, len(measured) // 20))
    baseline = _median([temp for unused, temp
                        in measured[:baseline_count]])
    values = [temp - baseline for unused, temp in measured]
    rise = max(values)
    if rise < 2.:
        raise ValueError('thermal step produced less than 2C rise')
    intervals = [measured[pos][0] - measured[pos - 1][0]
                 for pos in range(1, len(measured))
                 if measured[pos][0] > measured[pos - 1][0]]
    sample_period = _median(intervals) if intervals else duration / len(measured)
    delay_limit = min(60., duration * .25)
    delay_candidates = [delay_limit * pos / 24. for pos in range(25)]
    tau_min = max(1., 2. * sample_period)
    tau_max = duration * 20.
    tau_candidates = [tau_min * (tau_max / tau_min) ** (pos / 119.)
                      for pos in range(120)]
    best = None
    for delay in delay_candidates:
        for tau in tau_candidates:
            basis = [(0. if stamp <= delay else
                      1. - math.exp(-(stamp - delay) / tau))
                     for stamp, unused in measured]
            norm = sum(value * value for value in basis)
            if norm < 1.e-12:
                continue
            amplitude = sum(value * observed
                            for value, observed in zip(basis, values)) / norm
            if amplitude <= 0.:
                continue
            residuals = [observed - amplitude * value
                         for value, observed in zip(basis, values)]
            sse = sum(value * value for value in residuals)
            if best is None or sse < best[0]:
                best = (sse, delay, tau, amplitude, residuals)
    if best is None:
        raise ValueError('unable to fit first-order thermal response')
    sse, delay, tau, amplitude, residuals = best
    # A solution pinned to the search ceiling means the experiment observed
    # too little curvature to identify a time constant safely.
    if tau > tau_max * .98:
        raise ValueError(
            'thermal step did not run long enough to identify tau')
    mean_value = sum(values) / len(values)
    total = sum((value - mean_value) ** 2 for value in values)
    r_squared = 1. - sse / total if total > 0. else 0.
    rmse = math.sqrt(sse / len(values))
    if r_squared < .95:
        raise ValueError(
            'thermal step first-order fit is poor (R^2=%.4f)' % r_squared)
    return {
        'gain': amplitude / power,
        'tau': tau,
        'delay': delay,
        'baseline': baseline,
        'asymptote': baseline + amplitude,
        'power': power,
        'samples': len(values),
        'duration': duration,
        'rise': rise,
        'rmse': rmse,
        'r_squared': r_squared,
    }


class HeaterProfileStore:
    """Private, atomic JSON store containing raw heater tune evidence."""
    def __init__(self, path, wall_clock=time.time):
        self.path = os.path.abspath(path)
        self.wall_clock = wall_clock
        self.data = self._load()

    def _empty(self):
        return {'version': STORE_VERSION, 'generation': 0, 'heaters': {}}

    def _load(self):
        if not os.path.exists(self.path):
            return self._empty()
        try:
            with open(self.path, 'r') as stream:
                data = json.load(stream)
        except (OSError, ValueError) as exc:
            raise ValueError("Unable to read heater profile store '%s': %s"
                             % (self.path, exc))
        if data.get('version') != STORE_VERSION:
            raise ValueError("Unsupported heater profile store version %s"
                             % (data.get('version'),))
        if not isinstance(data.get('heaters'), dict):
            raise ValueError('Heater profile store has invalid heaters data')
        return data

    def _save(self):
        directory = os.path.dirname(self.path)
        if not os.path.isdir(directory):
            os.makedirs(directory, mode=0o700)
        fd, tmppath = tempfile.mkstemp(
            prefix='.%s.' % (os.path.basename(self.path),), dir=directory)
        try:
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(fd, 'w') as stream:
                fd = -1
                json.dump(self.data, stream, sort_keys=True, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmppath, self.path)
            dirfd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        finally:
            if fd >= 0:
                os.close(fd)
            if os.path.exists(tmppath):
                os.unlink(tmppath)

    def runs(self, heater):
        return list(self.data['heaters'].get(heater, {}).get('runs', []))

    def thermal_models(self, heater):
        return list(self.data['heaters'].get(heater, {}).get(
            'thermal_models', []))

    def add_run(self, heater, run):
        record = dict(run)
        record.setdefault('created', self.wall_clock())
        record.setdefault('status', 'candidate')
        record['id'] = record.get('id') or _run_id(record)
        records = self.data['heaters'].setdefault(
            heater, {'runs': []})['runs']
        if not any(item.get('id') == record['id'] for item in records):
            records.append(record)
            self.data['generation'] += 1
            self._save()
        return record

    def add_thermal_model(self, heater, model):
        record = dict(model)
        record.setdefault('created', self.wall_clock())
        record.setdefault('status', 'candidate')
        record['id'] = record.get('id') or _run_id(record)
        section = self.data['heaters'].setdefault(heater, {'runs': []})
        records = section.setdefault('thermal_models', [])
        if not any(item.get('id') == record['id'] for item in records):
            records.append(record)
            self.data['generation'] += 1
            self._save()
        return record

    def set_status(self, heater, run_id, status):
        if status not in ('candidate', 'validated', 'rejected'):
            raise ValueError("Invalid heater run status '%s'" % (status,))
        for record in self.data['heaters'].get(heater, {}).get('runs', []):
            if record.get('id') == run_id:
                if record.get('status') != status:
                    record['status'] = status
                    self.data['generation'] += 1
                    self._save()
                return record
        raise KeyError(run_id)

    def set_thermal_model_status(self, heater, model_id, status):
        if status not in ('candidate', 'validated', 'rejected'):
            raise ValueError("Invalid thermal model status '%s'" % (status,))
        records = self.data['heaters'].get(heater, {}).get(
            'thermal_models', [])
        for record in records:
            if record.get('id') == model_id:
                if record.get('status') != status:
                    record['status'] = status
                    self.data['generation'] += 1
                    self._save()
                return record
        raise KeyError(model_id)

    def clear_thermal_models(self, heater):
        section = self.data['heaters'].get(heater)
        if section is None or not section.get('thermal_models'):
            return False
        section['thermal_models'] = []
        self.data['generation'] += 1
        self._save()
        return True

    def clear(self, heater=None):
        if heater is None:
            changed = bool(self.data['heaters'])
            self.data['heaters'] = {}
        else:
            changed = self.data['heaters'].pop(heater, None) is not None
        if changed:
            self.data['generation'] += 1
            self._save()
        return changed

    def remove_except(self, heater, keep_ids):
        section = self.data['heaters'].get(heater)
        if section is None:
            return False
        keep_ids = set(keep_ids)
        old = section.get('runs', [])
        new = [run for run in old if run.get('id') in keep_ids]
        if len(new) == len(old):
            return False
        section['runs'] = new
        self.data['generation'] += 1
        self._save()
        return True


class HeaterGainModel:
    """Bounded interpolation over validated characterization points.

    A target-only data set forms three piecewise-linear gain curves.  When
    validated points span both target and context temperature, a least-squares
    plane is fitted for each gain.  Extrapolation is deliberately forbidden.
    """
    def __init__(self, runs, base_gains, gain_ratio=(.25, 4.0)):
        self.base_gains = dict(zip(GAIN_NAMES, base_gains))
        self.min_ratio, self.max_ratio = gain_ratio
        self.points = self._points(runs)
        self.kind = 'base'
        self.coefficients = {}
        self.hull = None
        self.context_range = None
        self.target_range = None
        self._fit()

    def _points(self, runs):
        grouped = {}
        for run in runs:
            if run.get('status') != 'validated':
                continue
            gains = run.get('gains', {})
            try:
                valid = all(math.isfinite(float(gains.get(name, -1.)))
                            and float(gains.get(name, -1.)) >= 0.
                            for name in GAIN_NAMES)
            except (TypeError, ValueError):
                valid = False
            if not valid:
                continue
            target = float(run['target'])
            context = run.get('context_temp')
            context = None if context is None else float(context)
            grouped.setdefault((target, context), []).append(gains)
        points = []
        order = lambda item: (item[0][0], float('-inf')
                              if item[0][1] is None else item[0][1])
        for (target, context), samples in sorted(grouped.items(), key=order):
            point = {'target': target, 'context_temp': context}
            point.update({name: _median([float(sample[name])
                                        for sample in samples])
                          for name in GAIN_NAMES})
            point['samples'] = len(samples)
            points.append(point)
        return points

    def _fit_plane(self, name, points):
        # Normal equations for gain = a + b*target + c*context.
        rows = [(1., p['target'], p['context_temp']) for p in points]
        matrix = [[sum(row[i] * row[j] for row in rows)
                   for j in range(3)] for i in range(3)]
        vector = [sum(row[i] * point[name]
                      for row, point in zip(rows, points))
                  for i in range(3)]
        return _solve_3x3(matrix, vector)

    def _fit(self):
        if not self.points:
            return
        surface_points = [p for p in self.points
                          if p['context_temp'] is not None]
        curve_points = [p for p in self.points
                        if p['context_temp'] is None]
        if len(surface_points) >= 3:
            context_range = (min(p['context_temp'] for p in surface_points),
                             max(p['context_temp'] for p in surface_points))
            if context_range[1] > context_range[0]:
                planes = {name: self._fit_plane(name, surface_points)
                          for name in GAIN_NAMES}
                if all(value is not None for value in planes.values()):
                    hull = _convex_hull([
                        (p['target'], p['context_temp'])
                        for p in surface_points])
                    if len(hull) < 3:
                        return
                    self.kind = 'surface'
                    self.coefficients = planes
                    self.hull = hull
                    self.context_range = context_range
                    self.target_range = (
                        min(p['target'] for p in surface_points),
                        max(p['target'] for p in surface_points))
                    return
        # Do not silently erase context from underdetermined surface data.
        # A separately measured context-free series may still form a curve;
        # otherwise the model remains at its explicit base fallback.
        self.points = curve_points
        if not self.points:
            return
        self.target_range = (min(p['target'] for p in self.points),
                             max(p['target'] for p in self.points))
        self.kind = 'curve' if len(self.points) >= 2 else 'point'
        self.coefficients = {'points': [
            {key: point[key] for key in
             ('target', 'kp', 'ki', 'kd', 'samples')}
            for point in self.points]}

    def _bounded(self, gains):
        raw = {name: float(gains[name]) for name in GAIN_NAMES}
        bounded = {}
        for name in GAIN_NAMES:
            base = self.base_gains[name]
            lower, upper = base * self.min_ratio, base * self.max_ratio
            bounded[name] = max(lower, min(upper, raw[name]))
        clamped = [name for name in GAIN_NAMES
                   if abs(bounded[name] - raw[name]) > 1.e-12]
        return bounded, raw, clamped

    def _selection(self, gains, source, **metadata):
        bounded, raw, clamped = self._bounded(gains)
        result = {'gains': bounded, 'raw_gains': raw,
                  'clamped_gains': clamped, 'bounded': bool(clamped),
                  'source': source, 'model': self.kind}
        result.update(metadata)
        return result

    def select(self, target, context_temp=None):
        target = float(target)
        fallback = {'gains': dict(self.base_gains), 'source': 'base',
                    'model': self.kind, 'raw_gains': dict(self.base_gains),
                    'clamped_gains': [], 'bounded': False}
        if not self.points or self.target_range is None:
            return fallback
        if self.kind == 'base':
            return fallback
        if target < self.target_range[0] or target > self.target_range[1]:
            return fallback
        if self.kind == 'surface':
            if context_temp is None:
                return fallback
            context_temp = float(context_temp)
            if (context_temp < self.context_range[0]
                    or context_temp > self.context_range[1]):
                return fallback
            if not _inside_convex_hull((target, context_temp), self.hull):
                return fallback
            gains = {name: coef[0] + coef[1] * target
                     + coef[2] * context_temp
                     for name, coef in self.coefficients.items()}
            return self._selection(gains, 'surface')
        points = sorted(self.points, key=lambda point: point['target'])
        exact = [point for point in points
                 if abs(point['target'] - target) < 1.e-9]
        if exact:
            return self._selection(exact[0], 'exact')
        if len(points) < 2:
            return fallback
        for lower, upper in zip(points, points[1:]):
            if lower['target'] <= target <= upper['target']:
                fraction = ((target - lower['target'])
                            / (upper['target'] - lower['target']))
                gains = {name: lower[name]
                         + fraction * (upper[name] - lower[name])
                         for name in GAIN_NAMES}
                return self._selection(
                    gains, 'linear',
                    bracket=[lower['target'], upper['target']])
        return fallback

    def status(self):
        return {
            'version': MODEL_VERSION,
            'kind': self.kind,
            'points': len(self.points),
            'target_range': self.target_range,
            'context_range': self.context_range,
            'coefficients': self.coefficients,
            'surface_hull': self.hull,
            'gain_ratio_bounds': [self.min_ratio, self.max_ratio],
        }


class ThermalPlantModel:
    """Bounded target-indexed first-order thermal plant models.

    Plant identification is deliberately separate from controller tuning.
    Each validated point contains steady full-duty gain and the dominant time
    constant.  Interpolation is permitted only between characterized targets;
    outside that range the explicit printer.cfg model remains authoritative.
    """
    def __init__(self, records, base_model, model_ratio=(.25, 4.0),
                 max_delay=60.):
        self.base_model = {
            name: float(base_model[name]) for name in THERMAL_MODEL_NAMES}
        self.min_ratio, self.max_ratio = model_ratio
        self.max_delay = float(max_delay)
        self.points = self._points(records)
        self.kind = ('curve' if len(self.points) >= 2
                     else 'point' if self.points else 'base')
        self.target_range = (None if not self.points else (
            self.points[0]['target'], self.points[-1]['target']))

    def _points(self, records):
        grouped = {}
        for record in records:
            if record.get('status') != 'validated':
                continue
            # Context-dependent plant surfaces are intentionally not collapsed
            # into a target-only curve.
            if record.get('context_temp') is not None:
                continue
            model = record.get('model', {})
            try:
                target = float(record['target'])
                values = {
                    'gain': float(model['gain']),
                    'tau': float(model['tau']),
                    'delay': float(model.get('delay', 0.)),
                }
            except (KeyError, TypeError, ValueError):
                continue
            if (not math.isfinite(target)
                    or not math.isfinite(values['gain'])
                    or not math.isfinite(values['tau'])
                    or not math.isfinite(values['delay'])
                    or values['gain'] <= 0. or values['tau'] <= 0.
                    or values['delay'] < 0.):
                continue
            grouped.setdefault(target, []).append(values)
        points = []
        for target, samples in sorted(grouped.items()):
            point = {'target': target, 'samples': len(samples)}
            point.update({name: _median([sample[name] for sample in samples])
                          for name in THERMAL_MODEL_NAMES})
            points.append(point)
        return points

    def _bounded(self, model):
        raw = {name: float(model[name]) for name in THERMAL_MODEL_NAMES}
        bounded = {}
        for name in ('gain', 'tau'):
            base = self.base_model[name]
            bounded[name] = max(base * self.min_ratio,
                                min(base * self.max_ratio, raw[name]))
        bounded['delay'] = max(0., min(self.max_delay, raw['delay']))
        clamped = [name for name in THERMAL_MODEL_NAMES
                   if abs(bounded[name] - raw[name]) > 1.e-12]
        return bounded, raw, clamped

    def _selection(self, model, source, **metadata):
        bounded, raw, clamped = self._bounded(model)
        result = {'model': bounded, 'raw_model': raw,
                  'clamped_parameters': clamped, 'bounded': bool(clamped),
                  'source': source, 'kind': self.kind}
        result.update(metadata)
        return result

    def select(self, target):
        target = float(target)
        fallback = {'model': dict(self.base_model),
                    'raw_model': dict(self.base_model),
                    'clamped_parameters': [], 'bounded': False,
                    'source': 'base', 'kind': self.kind}
        if not self.points or self.target_range is None:
            return fallback
        if target < self.target_range[0] or target > self.target_range[1]:
            return fallback
        exact = [point for point in self.points
                 if abs(point['target'] - target) < 1.e-9]
        if exact:
            return self._selection(exact[0], 'exact')
        if len(self.points) < 2:
            return fallback
        for lower, upper in zip(self.points, self.points[1:]):
            if lower['target'] <= target <= upper['target']:
                fraction = ((target - lower['target'])
                            / (upper['target'] - lower['target']))
                model = {name: lower[name] + fraction * (
                    upper[name] - lower[name])
                    for name in THERMAL_MODEL_NAMES}
                return self._selection(
                    model, 'linear',
                    bracket=[lower['target'], upper['target']])
        return fallback

    def status(self):
        return {
            'version': MODEL_VERSION,
            'kind': self.kind,
            'points': len(self.points),
            'target_range': self.target_range,
            'model_ratio_bounds': [self.min_ratio, self.max_ratio],
            'delay_bounds': [0., self.max_delay],
            'models': [dict(point) for point in self.points],
        }
