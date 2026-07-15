#!/usr/bin/env python3
"""Render the direct endstop edge-to-stop comparison using only stdlib."""

import csv
import html
import pathlib
import statistics


ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / "docs/data/interrupt_polling_direct.csv"
OUTPUT = ROOT / "docs/img/interrupt-polling-direct.svg"
CLOCK_MHZ = 12.0


def load_rows():
    rows = []
    with DATA.open(newline="") as f:
        for row in csv.DictReader(f):
            row["contact"] = int(row["contact"])
            row["speed_mm_s"] = float(row["speed_mm_s"])
            row["edge_clock"] = int(row["edge_clock"])
            row["stop_clock"] = int(row["stop_clock"])
            row["delta_ticks"] = int(row["delta_ticks"])
            actual_ticks = ((row["stop_clock"] - row["edge_clock"])
                            & 0xffffffff)
            if actual_ticks != row["delta_ticks"]:
                raise ValueError("clock delta mismatch at contact %d"
                                 % (row["contact"],))
            row["latency_us"] = row["delta_ticks"] / CLOCK_MHZ
            row["overrun_um"] = (
                row["speed_mm_s"] * row["latency_us"] / 1000.0)
            rows.append(row)
    return rows


def esc(value):
    return html.escape(str(value), quote=True)


def text(x, y, value, size=14, weight=400, anchor="start", fill="#263238"):
    return (f'<text x="{x}" y="{y}" font-size="{size}" '
            f'font-weight="{weight}" text-anchor="{anchor}" '
            f'fill="{fill}">{esc(value)}</text>')


def panel(rows, x, y, width, height, metric, ymax, ticks, title, ylabel):
    plot_left = x + 65
    plot_top = y + 55
    plot_width = width - 85
    plot_height = height - 125
    groups = [("isr", "fast"), ("poll", "fast"),
              ("isr", "slow"), ("poll", "slow")]
    labels = [("ISR", "20 mm/s"), ("poll", "20 mm/s"),
              ("ISR", "3 mm/s"), ("poll", "3 mm/s")]
    colors = {"isr": "#087f8c", "poll": "#e76f51"}
    out = [f'<rect x="{x}" y="{y}" width="{width}" height="{height}" '
           'rx="10" fill="#ffffff" stroke="#d8e0e3"/>',
           text(x + 20, y + 30, title, 18, 700)]

    for tick in ticks:
        py = plot_top + plot_height * (1.0 - tick / ymax)
        out.append(f'<line x1="{plot_left}" y1="{py:.2f}" '
                   f'x2="{plot_left + plot_width}" y2="{py:.2f}" '
                   'stroke="#e5eaec" stroke-width="1"/>')
        label = f"{tick:g}"
        out.append(text(plot_left - 9, py + 5, label, 12, 400, "end", "#53636a"))

    out.append(f'<line x1="{plot_left}" y1="{plot_top}" '
               f'x2="{plot_left}" y2="{plot_top + plot_height}" '
               'stroke="#607078" stroke-width="1.5"/>')
    out.append(f'<line x1="{plot_left}" y1="{plot_top + plot_height}" '
               f'x2="{plot_left + plot_width}" y2="{plot_top + plot_height}" '
               'stroke="#607078" stroke-width="1.5"/>')

    spacing = plot_width / len(groups)
    for gi, ((mode, pass_name), (label1, label2)) in enumerate(zip(groups, labels)):
        values = [r[metric] for r in rows
                  if r["mode"] == mode and r["pass"] == pass_name]
        gx = plot_left + spacing * (gi + 0.5)
        for i, value in enumerate(values):
            # Deterministic horizontal spread; no random plot movement.
            jitter = ((i * 7) % 17 - 8) * 1.8
            py = plot_top + plot_height * (1.0 - value / ymax)
            out.append(f'<circle cx="{gx + jitter:.2f}" cy="{py:.2f}" r="3.5" '
                       f'fill="{colors[mode]}" fill-opacity="0.62"/>')
        mean = statistics.mean(values)
        my = plot_top + plot_height * (1.0 - mean / ymax)
        out.append(f'<line x1="{gx - 25}" y1="{my:.2f}" '
                   f'x2="{gx + 25}" y2="{my:.2f}" '
                   f'stroke="{colors[mode]}" stroke-width="5"/>')
        out.append(text(gx, plot_top + plot_height + 24, label1,
                        13, 700, "middle", colors[mode]))
        out.append(text(gx, plot_top + plot_height + 41, label2,
                        11, 400, "middle", "#53636a"))
        out.append(text(gx, plot_top + plot_height + 62,
                        f"mean {mean:.2f}", 11, 600, "middle", "#263238"))

    # Rotated y label.
    lx = x + 18
    ly = plot_top + plot_height / 2
    out.append(f'<text x="{lx}" y="{ly}" font-size="12" font-weight="600" '
               f'text-anchor="middle" fill="#53636a" '
               f'transform="rotate(-90 {lx} {ly})">{esc(ylabel)}</text>')
    return out


def main():
    rows = load_rows()
    if len(rows) != 64:
        raise SystemExit(f"expected 64 contacts, found {len(rows)}")
    for mode in ("isr", "poll"):
        for pass_name in ("fast", "slow"):
            count = sum(r["mode"] == mode and r["pass"] == pass_name
                        for r in rows)
            if count != 16:
                raise SystemExit(f"expected 16 {mode}/{pass_name} contacts,"
                                 f" found {count}")

    svg = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="720" '
        'viewBox="0 0 1080 720" role="img" '
        'aria-labelledby="title desc">',
        '<title id="title">Repeatability of interrupt versus polling endstop response</title>',
        '<desc id="desc">Scatter plots compare edge-to-stop latency and physical '
        'overrun for interrupt and polling detection at twenty and three millimeters '
        'per second.</desc>',
        '<rect width="1080" height="720" fill="#f5f7f8"/>',
        text(540, 36, "Same switch edge: bounded ISR vs sampling phase", 25,
             700, "middle", "#16343d"),
        text(540, 61,
             "32 physical contacts per mode; horizontal bars are means",
             14, 400, "middle", "#53636a"),
    ]
    svg += panel(rows, 35, 82, 495, 535, "latency_us", 500,
                 [0, 100, 200, 300, 400, 500],
                 "Timing: edge to trajectory halt", "latency (microseconds)")
    svg += panel(rows, 550, 82, 495, 535, "overrun_um", 2.2,
                 [0, 0.5, 1.0, 1.5, 2.0],
                 "Motion after contact", "overrun (micrometers)")
    svg += [
        '<circle cx="365" cy="661" r="5" fill="#087f8c"/>',
        text(378, 666, "active GPIO ISR", 13, 600),
        '<circle cx="535" cy="661" r="5" fill="#e76f51"/>',
        text(548, 666, "legacy poller + passive edge timestamp", 13, 600),
        text(540, 697,
             "Both modes stopped every run; the ISR result is the narrow distribution, not merely the smaller mean.",
             13, 500, "middle", "#3d4e55"),
        '</svg>',
    ]
    OUTPUT.write_text("\n".join(svg) + "\n")
    print(OUTPUT)


if __name__ == "__main__":
    main()
