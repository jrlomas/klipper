#!/usr/bin/env python3
"""Analyze and plot a HELIX_HEATER_SINE_TEST raw capture."""

import argparse
import csv
import html
import json
import math
import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'klippy'))

from extras.pid_calibrate import thermal_sine_metrics


def load_capture(filename):
    samples = []
    with open(filename, newline='') as stream:
        for row in csv.DictReader(stream):
            samples.append((
                float(row['elapsed_s']), float(row['temperature_c']),
                float(row['commanded_power']),
                row['measurement_window'].strip().lower() == 'true'))
    if not samples:
        raise ValueError('capture is empty')
    return samples


def _polyline(points, xmap, ymap):
    return ' '.join('%.2f,%.2f' % (xmap(x), ymap(y)) for x, y in points)


def write_svg(filename, samples, period, metrics, title):
    width, height = 920, 570
    left, right, top, bottom = 78, 24, 48, 48
    split, gap = 370, 44
    x0, x1 = samples[0][0], samples[-1][0]
    temps = [sample[1] for sample in samples]
    powers = [sample[2] for sample in samples]
    tpad = max(.2, .08 * (max(temps) - min(temps)))
    t0, t1 = min(temps) - tpad, max(temps) + tpad
    ppad = max(.002, .15 * (max(powers) - min(powers)))
    p0, p1 = min(powers) - ppad, max(powers) + ppad
    plot_w = width - left - right
    temp_h = split - top
    power_top = split + gap
    power_h = height - power_top - bottom

    xmap = lambda value: left + (value - x0) / (x1 - x0) * plot_w
    tymap = lambda value: split - (value - t0) / (t1 - t0) * temp_h
    pymap = lambda value: (power_top + power_h
                           - (value - p0) / (p1 - p0) * power_h)

    measured = [sample for sample in samples if sample[3]]
    origin = measured[0][0]
    phase = math.radians(metrics['phase_deg'])
    fit = [(sample[0], metrics['offset_c']
            + metrics['drift_c_per_s'] * (sample[0] - origin)
            + metrics['amplitude_c'] * math.sin(
                2. * math.pi * sample[0] / period + phase))
           for sample in measured]
    boundary = xmap(origin)

    lines = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'viewBox="0 0 %d %d">' % (width, height, width, height),
        '<rect width="100%" height="100%" fill="#fff"/>',
        '<style>text{font-family:system-ui,sans-serif;fill:#17202a}'
        '.axis{stroke:#5d6d7e;stroke-width:1}.grid{stroke:#d5d8dc;'
        'stroke-width:1}.raw{fill:none;stroke:#2471a3;stroke-width:1.5}'
        '.fit{fill:none;stroke:#c0392b;stroke-width:2}'
        '.power{fill:none;stroke:#7d3c98;stroke-width:1.5}</style>',
        '<text x="%d" y="25" text-anchor="middle" font-size="18" '
        'font-weight="600">%s</text>' % (width // 2, html.escape(title)),
        '<rect x="%.2f" y="%d" width="%.2f" height="%d" fill="#eaf2f8"/>'
        % (boundary, top, left + plot_w - boundary, temp_h),
        '<line class="axis" x1="%d" y1="%d" x2="%d" y2="%d"/>'
        % (left, split, left + plot_w, split),
        '<line class="axis" x1="%d" y1="%d" x2="%d" y2="%d"/>'
        % (left, top, left, split),
        '<line class="axis" x1="%d" y1="%d" x2="%d" y2="%d"/>'
        % (left, power_top + power_h, left + plot_w, power_top + power_h),
        '<line class="axis" x1="%d" y1="%d" x2="%d" y2="%d"/>'
        % (left, power_top, left, power_top + power_h),
    ]
    for pos in range(5):
        fraction = pos / 4.
        y = top + fraction * temp_h
        value = t1 - fraction * (t1 - t0)
        lines.extend([
            '<line class="grid" x1="%d" y1="%.2f" x2="%d" y2="%.2f"/>'
            % (left, y, left + plot_w, y),
            '<text x="%d" y="%.2f" text-anchor="end" font-size="12">%.2f</text>'
            % (left - 8, y + 4, value),
        ])
    for pos in range(6):
        fraction = pos / 5.
        x = left + fraction * plot_w
        value = x0 + fraction * (x1 - x0)
        lines.extend([
            '<line class="grid" x1="%.2f" y1="%d" x2="%.2f" y2="%d"/>'
            % (x, top, x, split),
            '<text x="%.2f" y="%d" text-anchor="middle" '
            'font-size="12">%.0f</text>'
            % (x, height - 20, value),
        ])
    lines.extend([
        '<polyline class="raw" points="%s"/>' % _polyline(
            [(s[0], s[1]) for s in samples], xmap, tymap),
        '<polyline class="fit" points="%s"/>' % _polyline(fit, xmap, tymap),
        '<polyline class="power" points="%s"/>' % _polyline(
            [(s[0], s[2]) for s in samples], xmap, pymap),
        '<text x="20" y="%d" transform="rotate(-90 20 %d)" '
        'text-anchor="middle" font-size="13">Temperature (C)</text>'
        % ((top + split) // 2, (top + split) // 2),
        '<text x="20" y="%d" transform="rotate(-90 20 %d)" '
        'text-anchor="middle" font-size="13">PWM duty</text>'
        % ((power_top + power_top + power_h) // 2,
           (power_top + power_top + power_h) // 2),
        '<text x="%d" y="%d" text-anchor="middle" '
        'font-size="13">Time (s)</text>'
        % (left + plot_w // 2, height - 2),
        '<line x1="%d" y1="35" x2="%d" y2="35" class="raw"/>'
        '<text x="%d" y="39" font-size="12">temperature</text>'
        % (left, left + 25, left + 31),
        '<line x1="%d" y1="35" x2="%d" y2="35" class="fit"/>'
        '<text x="%d" y="39" font-size="12">detrended fit</text>'
        % (left + 125, left + 150, left + 156),
        '<text x="%d" y="39" font-size="12">shaded = measured</text>'
        % (left + 300),
        '</svg>',
    ])
    with open(filename, 'w') as stream:
        stream.write('\n'.join(lines) + '\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('csv')
    parser.add_argument('--period', type=float, required=True)
    parser.add_argument('--amplitude', type=float)
    parser.add_argument('--svg')
    parser.add_argument('--title', default='Helix thermal-chain sine response')
    args = parser.parse_args()
    samples = load_capture(args.csv)
    amplitude = args.amplitude
    if amplitude is None:
        powers = [sample[2] for sample in samples]
        amplitude = .5 * (max(powers) - min(powers))
    metrics = thermal_sine_metrics(samples, args.period, amplitude)
    print(json.dumps(metrics, indent=2, sort_keys=True))
    if args.svg:
        write_svg(args.svg, samples, args.period, metrics, args.title)


if __name__ == '__main__':
    main()
