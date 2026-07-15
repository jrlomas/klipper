#!/usr/bin/env python3
"""Generate dependency-free SVG figures for the machine-time white paper."""

import csv
import math
import os
import statistics
from xml.sax.saxutils import escape


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(
    ROOT, 'docs/evidence/machine_time/scope_comparison_edges.csv')
SYNC_LINE_DATA = os.path.join(
    ROOT, 'docs/evidence/machine_time/sync_line_edges.csv')
USB_SOF_DATA = os.path.join(
    ROOT, 'docs/evidence/machine_time/usb_sof_edges.csv')
OUT = os.path.join(ROOT, 'docs/img')

COLORS = {
    'blue': '#2563eb', 'cyan': '#0891b2', 'violet': '#7c3aed',
    'green': '#059669', 'orange': '#ea580c', 'red': '#dc2626',
    'slate': '#475569', 'ink': '#0f172a', 'muted': '#64748b',
    'grid': '#dbe4ee', 'band': '#dcfce7', 'paper': '#ffffff',
}


class Svg:
    def __init__(self, width, height, title, description):
        self.width, self.height = width, height
        self.parts = [
            '<svg xmlns="http://www.w3.org/2000/svg" '
            'viewBox="0 0 %d %d" role="img">' % (width, height),
            '<title>%s</title>' % escape(title),
            '<desc>%s</desc>' % escape(description),
            '<rect width="100%%" height="100%%" fill="%s"/>'
            % COLORS['paper'],
            '<style>text{font-family:Inter,system-ui,sans-serif;fill:%s}'
            '.title{font-size:22px;font-weight:700}.subtitle{font-size:13px;'
            'fill:%s}.axis{font-size:12px;fill:%s}.legend{font-size:12px}'
            '</style>' % (COLORS['ink'], COLORS['muted'], COLORS['muted']),
        ]

    def rect(self, x, y, width, height, fill='none', stroke='none',
             stroke_width=1, opacity=1., rx=0):
        self.parts.append(
            '<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f" '
            'fill="%s" stroke="%s" stroke-width="%.2f" opacity="%.3f" '
            'rx="%.2f"/>' % (x, y, width, height, fill, stroke,
                              stroke_width, opacity, rx))

    def line(self, x1, y1, x2, y2, stroke, width=1., dash=None,
             opacity=1.):
        dashed = '' if dash is None else ' stroke-dasharray="%s"' % dash
        self.parts.append(
            '<line x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f" '
            'stroke="%s" stroke-width="%.2f" opacity="%.3f"%s/>'
            % (x1, y1, x2, y2, stroke, width, opacity, dashed))

    def circle(self, x, y, radius, fill, stroke='none', width=1.):
        self.parts.append(
            '<circle cx="%.2f" cy="%.2f" r="%.2f" fill="%s" '
            'stroke="%s" stroke-width="%.2f"/>'
            % (x, y, radius, fill, stroke, width))

    def polyline(self, points, stroke, width=2., dash=None, opacity=1.):
        dashed = '' if dash is None else ' stroke-dasharray="%s"' % dash
        coords = ' '.join('%.2f,%.2f' % point for point in points)
        self.parts.append(
            '<polyline points="%s" fill="none" stroke="%s" '
            'stroke-width="%.2f" stroke-linejoin="round" '
            'stroke-linecap="round" opacity="%.3f"%s/>'
            % (coords, stroke, width, opacity, dashed))

    def text(self, x, y, value, size_class='axis', anchor='start',
             weight=None, rotate=None):
        attrs = ' class="%s" text-anchor="%s"' % (size_class, anchor)
        if weight is not None:
            attrs += ' font-weight="%s"' % weight
        if rotate is not None:
            attrs += ' transform="rotate(%s %.2f %.2f)"' % (rotate, x, y)
        self.parts.append('<text x="%.2f" y="%.2f"%s>%s</text>'
                          % (x, y, attrs, escape(str(value))))

    def save(self, name):
        self.parts.append('</svg>')
        path = os.path.join(OUT, name)
        with open(path, 'w', encoding='utf-8') as output:
            output.write('\n'.join(self.parts) + '\n')
        return path


def load_data():
    grouped = {}
    with open(DATA, newline='', encoding='utf-8') as source:
        for row in csv.DictReader(source):
            grouped.setdefault(row['dataset'], []).append({
                'sample': int(row['sample']),
                'scope': float(row['scope_delta_us']),
                'isr': float(row['isr_delta_us']),
                'mapping': float(row['mapping_delta_us']),
            })
    return grouped


def load_sync_line_data():
    grouped = {}
    with open(SYNC_LINE_DATA, newline='', encoding='utf-8') as source:
        for row in csv.DictReader(source):
            grouped.setdefault(row['dataset'], []).append({
                'sample': int(row['sample']),
                'primary': int(row['primary_actual_ticks']),
                'secondary': int(row['secondary_capture_ticks']),
                'map_error': float(row['map_error_us']),
            })
    return grouped


def load_usb_sof_data():
    grouped = {}
    with open(USB_SOF_DATA, newline='', encoding='utf-8') as source:
        for row in csv.DictReader(source):
            grouped.setdefault(row['dataset'], []).append({
                'sample': int(row['sample']),
                'error': float(row['error_us']),
            })
    return grouped


def summarize(values):
    return {
        'count': len(values), 'mean': statistics.fmean(values),
        'stdev': statistics.pstdev(values), 'min': min(values),
        'max': max(values), 'max_abs': max(abs(value) for value in values),
    }


def add_header(svg, title, subtitle):
    svg.text(70, 38, title, 'title')
    svg.text(70, 61, subtitle, 'subtitle')


def chart_timeseries(data):
    width, height = 1100, 620
    svg = Svg(width, height, 'Helix and original Klipper physical skew',
              'Logic-analyzer edge skew by captured sample after restart.')
    add_header(svg, 'Physical skew after restart',
               'Same Pico GPIO24 / EBB36 PB8 rig at 24 MHz; green band is '
               'the original ±10 µs design objective')
    left, top, plot_w, plot_h = 95, 95, 720, 430
    x_min, x_max, y_min, y_max = 1., 30., -12., 30.
    sx = lambda value: left + (value - x_min) / (x_max - x_min) * plot_w
    sy = lambda value: top + (y_max - value) / (y_max - y_min) * plot_h
    svg.rect(left, sy(10), plot_w, sy(-10) - sy(10), COLORS['band'])
    for value in range(-10, 31, 5):
        svg.line(left, sy(value), left + plot_w, sy(value), COLORS['grid'])
        svg.text(left - 12, sy(value) + 4, value, anchor='end')
    for value in (1, 5, 10, 15, 20, 25, 30):
        svg.line(sx(value), top, sx(value), top + plot_h, COLORS['grid'])
        svg.text(sx(value), top + plot_h + 22, value, anchor='middle')
    svg.line(left, sy(0), left + plot_w, sy(0), COLORS['slate'], 1.5)
    svg.text(left + plot_w / 2, height - 48, 'Successful captured edge',
             anchor='middle')
    svg.text(25, top + plot_h / 2, 'Pico minus EBB36 edge skew (µs)',
             anchor='middle', rotate=-90)

    sessions = [
        ('helix-sched-rr-r1', 'Helix RR session 1', COLORS['blue']),
        ('helix-sched-rr-r2', 'Helix RR session 2', COLORS['cyan']),
        ('helix-sched-rr-r3', 'Helix RR session 3', COLORS['violet']),
        ('helix-sched-rr-r4', 'Helix RR session 4', COLORS['green']),
        ('klipper-legacy-r1', 'Original Klipper comparator',
         COLORS['orange']),
    ]
    for key, _label, color in sessions:
        points = [(sx(row['sample']), sy(row['scope'])) for row in data[key]]
        svg.polyline(points, color, 2.2 if 'legacy' in key else 1.8,
                     dash='7 4' if 'legacy' in key else None,
                     opacity=.95)
        for x, y in points:
            svg.circle(x, y, 2.2, color)
    legend_x, legend_y = 845, 112
    for index, (_key, label, color) in enumerate(sessions):
        y = legend_y + index * 29
        svg.line(legend_x, y, legend_x + 30, y, color, 3,
                 dash='7 4' if 'Klipper' in label else None)
        svg.text(legend_x + 40, y + 4, label, 'legend')
    svg.text(845, 295, 'Interpretation', weight='700')
    notes = [
        'Helix pooled: +1.12 µs mean',
        '2.75 µs population σ',
        '−5.17 to +10.88 µs observed',
        '',
        'Original Klipper: +8.36 µs mean',
        '7.60 µs population σ',
        '+1.50 to +26.67 µs observed',
        '',
        'Both mappings settle statistically;',
        'neither exposes a hard USB bound.',
    ]
    for index, note in enumerate(notes):
        svg.text(845, 322 + index * 20, note, 'legend')
    return svg.save('machine_time_restart_comparison.svg')


def chart_distributions(data):
    # The result annotation includes mean, deviation, and sample count. Keep a
    # wide right margin so it remains visible in rendered documentation.
    width, height = 1350, 560
    svg = Svg(width, height, 'Cross-MCU skew distributions',
              'Observed range, population standard deviation, and mean.')
    add_header(svg, 'Measured skew: center, variation, and tails',
               'Thin line = observed range; thick line = mean ±1σ; dot = '
               'mean. These are measurements, not guaranteed limits.')
    helix = []
    for key in ('helix-sched-rr-r1', 'helix-sched-rr-r2',
                'helix-sched-rr-r3', 'helix-sched-rr-r4'):
        helix.extend(row['scope'] for row in data[key])
    rows = [
        ('Helix robust + SCHED_RR (4 sessions)', helix, COLORS['blue']),
        ('Original Klipper print_time',
         [row['scope'] for row in data['klipper-legacy-r1']],
         COLORS['orange']),
        ('Helix adverse SCHED_OTHER startup',
         [row['scope'] for row in data['helix-sched-other-adverse']],
         COLORS['red']),
        ('Rejected freeze-biased experiment',
         [row['scope'] for row in
          data['helix-phase-continuous-rejected']], COLORS['violet']),
    ]
    left, top, plot_w, plot_h = 330, 105, 690, 340
    x_min, x_max = -60., 70.
    sx = lambda value: left + (value - x_min) / (x_max - x_min) * plot_w
    svg.rect(sx(-10), top, sx(10) - sx(-10), plot_h, COLORS['band'])
    for value in range(-60, 71, 10):
        svg.line(sx(value), top, sx(value), top + plot_h, COLORS['grid'])
        svg.text(sx(value), top + plot_h + 25, value, anchor='middle')
    svg.line(sx(0), top, sx(0), top + plot_h, COLORS['slate'], 1.5)
    for index, (label, values, color) in enumerate(rows):
        stat = summarize(values)
        y = top + 55 + index * 78
        svg.text(left - 18, y + 4, label, anchor='end')
        svg.line(sx(stat['min']), y, sx(stat['max']), y, color, 2,
                 opacity=.65)
        svg.line(sx(stat['mean'] - stat['stdev']), y,
                 sx(stat['mean'] + stat['stdev']), y, color, 10)
        svg.circle(sx(stat['mean']), y, 6, COLORS['paper'], color, 3)
        svg.text(left + plot_w + 14, y + 4,
                 'μ=%+.2f  σ=%.2f  n=%d' % (
                     stat['mean'], stat['stdev'], stat['count']), 'legend')
    svg.text(left + plot_w / 2, height - 48,
             'Physical edge skew (µs)', anchor='middle')
    return svg.save('machine_time_distribution_comparison.svg')


def chart_print_impact(data):
    # The legend deliberately names the provenance of every reference line;
    # allow enough width for those labels without clipping them.
    width, height = 1350, 620
    svg = Svg(width, height, 'Clock skew translated to path phase',
              'Path distance corresponding to time skew over print speed.')
    add_header(svg, 'What the timing numbers mean along a printed path',
               'Distance = speed × |skew|. This is a phase shift, not '
               'cumulative position or extrusion loss.')
    left, top, plot_w, plot_h = 95, 100, 720, 420
    x_min, x_max, y_min, y_max = 0., 500., 0., .05
    sx = lambda value: left + value / x_max * plot_w
    sy = lambda value: top + (y_max - value) / y_max * plot_h
    for value in range(0, 501, 100):
        svg.line(sx(value), top, sx(value), top + plot_h, COLORS['grid'])
        svg.text(sx(value), top + plot_h + 23, value, anchor='middle')
    for step in range(0, 51, 5):
        value = step / 1000.
        svg.line(left, sy(value), left + plot_w, sy(value), COLORS['grid'])
        svg.text(left - 12, sy(value) + 4, '%.3f' % value, anchor='end')
    svg.text(left + plot_w / 2, height - 48, 'Toolhead speed (mm/s)',
             anchor='middle')
    svg.text(24, top + plot_h / 2, 'Equivalent path-phase distance (mm)',
             anchor='middle', rotate=-90)
    series = [
        (1.121759, 'Helix pooled mean: 1.12 µs', COLORS['blue'], None),
        (2.751439, 'Helix pooled 1σ: 2.75 µs', COLORS['cyan'], None),
        (10.875, 'Helix observed maximum: 10.88 µs',
         COLORS['green'], None),
        (26.666667, 'Original Klipper maximum: 26.67 µs',
         COLORS['orange'], '7 4'),
        (58.791667, 'Largest measured campaign bias: 58.79 µs',
         COLORS['violet'], '7 4'),
        (88., 'Symmetry-free RTT envelope example: 88 µs',
         COLORS['red'], '3 4'),
    ]
    speeds = list(range(0, 501, 10))
    for skew_us, _label, color, dash in series:
        points = [(sx(speed), sy(speed * skew_us * 1.e-6))
                  for speed in speeds]
        svg.polyline(points, color, 2.4, dash=dash)
    legend_x, legend_y = 842, 118
    for index, (_skew, label, color, dash) in enumerate(series):
        y = legend_y + index * 36
        svg.line(legend_x, y, legend_x + 28, y, color, 3, dash=dash)
        svg.text(legend_x + 38, y + 4, label, 'legend')
    svg.rect(842, 360, 225, 112, '#f8fafc', COLORS['grid'], rx=8)
    svg.text(855, 384, 'At 300 mm/s', weight='700')
    svg.text(855, 408, 'Helix mean: 0.00034 mm', 'legend')
    svg.text(855, 430, 'Helix max: 0.00326 mm', 'legend')
    svg.text(855, 452, '58.79 µs bias: 0.01764 mm', 'legend')
    return svg.save('machine_time_print_domain_impact.svg')


def chart_error_source(data):
    width, height = 1000, 520
    svg = Svg(width, height, 'ISR and clock mapping variation',
              'Logarithmic comparison of standard deviation sources.')
    add_header(svg, 'The GPIO interrupt is not the variable term',
               'Population standard deviation across 90 retained SCHED_RR '
               'edges; logarithmic scale')
    records = []
    for key in ('helix-sched-rr-r1', 'helix-sched-rr-r2',
                'helix-sched-rr-r3', 'helix-sched-rr-r4'):
        records.extend(data[key])
    entries = [
        ('MCU ISR differential', statistics.pstdev(
            row['isr'] for row in records), COLORS['green']),
        ('Host/mapping term', statistics.pstdev(
            row['mapping'] for row in records), COLORS['orange']),
        ('Physical edge skew', statistics.pstdev(
            row['scope'] for row in records), COLORS['blue']),
    ]
    left, top, plot_w, plot_h = 120, 105, 700, 310
    y_min, y_max = .01, 10.
    sy = lambda value: top + (
        math.log10(y_max) - math.log10(value)) / (
            math.log10(y_max) - math.log10(y_min)) * plot_h
    for value in (.01, .03, .1, .3, 1., 3., 10.):
        svg.line(left, sy(value), left + plot_w, sy(value), COLORS['grid'])
        svg.text(left - 14, sy(value) + 4, '%g' % value, anchor='end')
    bar_w = 130
    for index, (label, value, color) in enumerate(entries):
        x = left + 90 + index * 210
        y = sy(value)
        svg.rect(x, y, bar_w, top + plot_h - y, color, rx=4)
        svg.text(x + bar_w / 2, y - 12, '%.3f µs' % value,
                 anchor='middle', weight='700')
        svg.text(x + bar_w / 2, top + plot_h + 28, label,
                 anchor='middle')
    ratio = entries[1][1] / entries[0][1]
    svg.text(850, 180, 'Mapping variation is', weight='700')
    svg.text(850, 218, '%.0f×' % ratio, 'title', anchor='middle')
    svg.text(850, 244, 'the ISR variation', anchor='middle')
    svg.text(33, top + plot_h / 2, 'Standard deviation (µs, log scale)',
             anchor='middle', rotate=-90)
    return svg.save('machine_time_error_source.svg')


def affine_residual_sigma(records):
    x0, y0 = records[0]['primary'], records[0]['secondary']
    xs = [row['primary'] - x0 for row in records]
    ys = [row['secondary'] - y0 for row in records]
    xmean = statistics.fmean(xs)
    ymean = statistics.fmean(ys)
    denom = sum((x - xmean) ** 2 for x in xs)
    slope = sum((x - xmean) * (y - ymean)
                for x, y in zip(xs, ys)) / denom
    intercept = ymean - slope * xmean
    residuals_us = [
        (y - intercept - slope * x) / 64.
        for x, y in zip(xs, ys)]
    return statistics.pstdev(residuals_us)


def chart_sync_line(sync_data):
    width, height = 1300, 640
    svg = Svg(width, height, 'Direct sync line and USB clock-map error',
              'Physical edge-fit residual compared with USB mapping error.')
    add_header(svg, 'A shared edge exposes the USB mapping error directly',
               'Pico GPIO24 drives EBB36 PB8; the secondary timestamps the '
               'edge in its EXTI interrupt')
    left, top, plot_w, plot_h = 90, 105, 700, 380
    sx = lambda value: left + (value - 1.) / 29. * plot_w
    y_min, y_max = -5., 4.5
    sy = lambda value: top + (y_max - value) / (y_max - y_min) * plot_h
    for value in range(-5, 5):
        svg.line(left, sy(value), left + plot_w, sy(value), COLORS['grid'])
        svg.text(left - 12, sy(value) + 4, value, anchor='end')
    svg.line(left, sy(0), left + plot_w, sy(0), COLORS['slate'], 1.5)
    for value in (1, 5, 10, 15, 20, 25, 30):
        svg.line(sx(value), top, sx(value), top + plot_h, COLORS['grid'])
        svg.text(sx(value), top + plot_h + 23, value, anchor='middle')
    series = [
        ('sync-line-rt-r1', 'Run 1', COLORS['blue']),
        ('sync-line-rt-r2', 'Run 2', COLORS['green']),
        ('sync-line-rt-r3', 'Run 3', COLORS['violet']),
    ]
    for key, _label, color in series:
        points = [(sx(row['sample']), sy(row['map_error']))
                  for row in sync_data[key]]
        svg.polyline(points, color, 2.4)
        for x, y in points:
            svg.circle(x, y, 2.2, color)
    svg.text(left + plot_w / 2, height - 58, 'Captured edge',
             anchor='middle')
    svg.text(25, top + plot_h / 2, 'USB-map prediction error (µs)',
             anchor='middle', rotate=-90)

    panel_x = 965
    svg.text(panel_x, 116, 'Measured variation', weight='700')
    svg.text(panel_x, 140, 'Population σ, logarithmic scale', 'subtitle')
    entries = []
    for key, label, color in series:
        physical = affine_residual_sigma(sync_data[key])
        usb = statistics.pstdev(
            row['map_error'] for row in sync_data[key])
        entries.extend([
            ('%s physical fit' % label, physical, color),
            ('%s USB map' % label, usb, COLORS['orange']),
        ])
    bar_left, bar_top, bar_w, bar_h = panel_x, 174, 260, 285
    lo, hi = .001, 10.
    bx = lambda value: bar_left + (
        math.log10(value) - math.log10(lo)) / (
            math.log10(hi) - math.log10(lo)) * bar_w
    for value in (.001, .01, .1, 1., 10.):
        svg.line(bx(value), bar_top, bx(value), bar_top + bar_h,
                 COLORS['grid'])
        svg.text(bx(value), bar_top + bar_h + 22, '%g' % value,
                 anchor='middle')
    for index, (label, value, color) in enumerate(entries):
        y = bar_top + 25 + index * 43
        svg.text(bar_left - 8, y + 4, label, 'legend', anchor='end')
        svg.line(bx(lo), y, bx(value), y, color, 9)
        svg.circle(bx(value), y, 5, color)
        svg.text(bx(value) + 9, y + 4, '%.4f µs' % value, 'legend')
    ratios = []
    for key, _label, _color in series:
        ratios.append(statistics.pstdev(
            row['map_error'] for row in sync_data[key])
            / affine_residual_sigma(sync_data[key]))
    svg.text(panel_x, 525, 'Physical residual is %.0f–%.0f× smaller' % (
                 min(ratios), max(ratios)),
             'legend', weight='700')
    svg.text(panel_x, 547, 'than the USB-map σ in these runs.', 'legend')
    return svg.save('machine_time_sync_line.svg')


def chart_sof_discipline(sof_data):
    width, height = 1200, 620
    svg = Svg(width, height, 'USB SOF phase and disciplined clock map',
              'Direct-wire-calibrated phase for matching USB SOF frames and '
              'the resulting machine-time mapping error.')
    add_header(svg, 'USB SOF turns phase observation into a hardware event',
               'Same-frame timestamps and the disciplined map, both '
               'calibrated against the direct GPIO edge')
    left, top, plot_w, plot_h = 90, 105, 720, 390
    sx = lambda value: left + (value - 1.) / 49. * plot_w
    y_min, y_max = -.75, .85
    sy = lambda value: top + (y_max - value) / (y_max - y_min) * plot_h
    for step in range(-6, 9, 2):
        value = step / 10.
        svg.line(left, sy(value), left + plot_w, sy(value), COLORS['grid'])
        svg.text(left - 12, sy(value) + 4, '%+.1f' % value, anchor='end')
    for value in (1, 10, 20, 30, 40, 50):
        svg.line(sx(value), top, sx(value), top + plot_h, COLORS['grid'])
        svg.text(sx(value), top + plot_h + 23, value, anchor='middle')
    svg.line(left, sy(0), left + plot_w, sy(0), COLORS['slate'], 1.5)
    series = [
        ('usb-sof-rt-r1', 'Matched SOF phase', COLORS['violet']),
        ('sof-discipline-rt-r1', 'Discipline acquisition', COLORS['cyan']),
        ('sof-discipline-rt-r2', 'Steady discipline', COLORS['green']),
    ]
    for key, _label, color in series:
        points = [(sx(row['sample']), sy(row['error']))
                  for row in sof_data[key]]
        svg.polyline(points, color, 2.2)
        for x, y in points:
            svg.circle(x, y, 2.1, color)
    svg.text(left + plot_w / 2, height - 58, 'Matched frame / captured edge',
             anchor='middle')
    svg.text(25, top + plot_h / 2, 'Direct-wire-calibrated error (us)',
             anchor='middle', rotate=-90)

    panel_x = 850
    svg.text(panel_x, 116, 'Measured distributions', weight='700')
    svg.text(panel_x, 140, 'Mean and population sigma', 'subtitle')
    for index, (key, label, color) in enumerate(series):
        values = [row['error'] for row in sof_data[key]]
        stat = summarize(values)
        y = 190 + index * 105
        svg.line(panel_x, y, panel_x + 28, y, color, 4)
        svg.text(panel_x + 40, y + 4, label, 'legend', weight='700')
        svg.text(panel_x + 40, y + 27,
                 'mean %+.4f us' % stat['mean'], 'legend')
        svg.text(panel_x + 40, y + 48,
                 'sigma %.4f us' % stat['stdev'], 'legend')
        svg.text(panel_x + 40, y + 69,
                 'range %+.4f to %+.4f us' % (
                     stat['min'], stat['max']), 'legend')
    svg.text(panel_x, 515, 'Steady map jitter: 15 ns RMS',
             'legend', weight='700')
    svg.text(panel_x, 538, 'Fixed phase remains measurable and stable.',
             'legend')
    return svg.save('machine_time_sof_discipline.svg')


def main():
    data = load_data()
    sync_data = load_sync_line_data()
    sof_data = load_usb_sof_data()
    paths = [chart_timeseries(data), chart_distributions(data),
             chart_print_impact(data), chart_error_source(data),
             chart_sync_line(sync_data), chart_sof_discipline(sof_data)]
    for path in paths:
        print(path)


if __name__ == '__main__':
    main()
