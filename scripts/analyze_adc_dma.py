#!/usr/bin/env python3
"""Regenerate the DMA/ADC qualification figures from the archived CSV."""

import csv
import html
import pathlib
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "docs" / "data" / "adc_dma_qualification.csv"
DEFAULT_OUT = ROOT / "docs" / "img"


LABELS = {
    "legacy_8x_thermistor": "F072 legacy thermistor",
    "dma_1ksps_osr8": "F072 DMA 1 kscan/s",
    "dma_distributed_8x_thermistor": "F072 DMA thermistor",
    "dma_mcu_adc_thermistor_restart": "RP2040 migrated thermistor",
    "dma_hw_osr16_1ksps": "H723 DMA HW-OSR16",
    "dma_hw_osr16_with_solver_load": "H723 DMA + solver load",
}


def number(row, key):
    value = row.get(key, "")
    return float(value) if value else 0.0


def load_rows(path):
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def write_bar_svg(path, title, subtitle, rows, value_fn, unit,
                  color="#14b8a6", second_fn=None,
                  second_label="task/deferred work"):
    values = [value_fn(row) for row in rows]
    second = [second_fn(row) if second_fn else 0.0 for row in rows]
    maximum = max([a + b for a, b in zip(values, second)] + [1e-12])
    width, left, right = 940, 245, 70
    bar_width = width - left - right
    height = 112 + len(rows) * 54
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d"'
        ' viewBox="0 0 %d %d" role="img">' % (width, height, width, height),
        '<rect width="100%%" height="100%%" fill="#0f172a"/>',
        '<style>text{font-family:system-ui,sans-serif;fill:#e2e8f0}'
        '.title{font-size:22px;font-weight:700}'
        '.sub{font-size:13px;fill:#94a3b8}'
        '.label{font-size:13px}.value{font-size:12px;font-weight:650}'
        '.grid{stroke:#334155;stroke-width:1}</style>',
        '<text class="title" x="24" y="34">%s</text>' % html.escape(title),
        '<text class="sub" x="24" y="57">%s</text>' % html.escape(subtitle),
    ]
    for tick in range(5):
        x = left + bar_width * tick / 4
        value = maximum * tick / 4
        parts.append('<line class="grid" x1="%.1f" y1="75" x2="%.1f"'
                     ' y2="%d"/>' % (x, x, height - 28))
        parts.append('<text class="sub" text-anchor="middle" x="%.1f"'
                     ' y="%d">%.3g</text>' % (x, height - 10, value))
    for index, row in enumerate(rows):
        y = 82 + index * 54
        first, extra = values[index], second[index]
        first_width = first / maximum * bar_width
        extra_width = extra / maximum * bar_width
        label = LABELS.get(row["workload"], row["workload"])
        parts.append('<text class="label" x="24" y="%d">%s</text>'
                     % (y + 18, html.escape(label)))
        parts.append('<rect x="%d" y="%d" width="%.2f" height="24"'
                     ' rx="3" fill="%s"/>'
                     % (left, y, first_width, color))
        if extra:
            parts.append('<rect x="%.2f" y="%d" width="%.2f" height="24"'
                         ' rx="3" fill="#f59e0b"/>'
                         % (left + first_width, y, extra_width))
        parts.append('<text class="value" x="%.2f" y="%d">%.4g %s</text>'
                     % (left + first_width + extra_width + 7, y + 17,
                        first + extra, html.escape(unit)))
    if second_fn:
        parts.extend([
            '<rect x="%d" y="43" width="12" height="12" fill="%s"/>'
            % (width - 300, color),
            '<text class="sub" x="%d" y="54">IRQ/publication</text>'
            % (width - 282),
            '<rect x="%d" y="43" width="12" height="12" fill="#f59e0b"/>'
            % (width - 165),
            '<text class="sub" x="%d" y="54">%s</text>'
            % (width - 147, html.escape(second_label)),
        ])
    parts.append("</svg>\n")
    path.write_text("\n".join(parts), encoding="utf-8")


def main():
    data = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DATA
    out = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUT
    out.mkdir(parents=True, exist_ok=True)
    rows = load_rows(data)

    def event_rate(row):
        events = number(row, "timer_invocations") or number(row, "blocks")
        return events / number(row, "duration_s")

    def hard_cpu(row):
        return (100.0 * number(row, "hardpath_ticks")
                / number(row, "clock_hz") / number(row, "duration_s"))

    def task_cpu(row):
        return (100.0 * number(row, "task_ticks")
                / number(row, "clock_hz") / number(row, "duration_s"))

    def hard_mean_us(row):
        count = (number(row, "timer_invocations")
                 or number(row, "blocks"))
        return (1e6 * number(row, "hardpath_ticks")
                / number(row, "clock_hz") / count)

    write_bar_svg(
        out / "adc-dma-event-rate.svg",
        "Scheduler/IRQ event rate",
        "Legacy counts timer callbacks; DMA counts completed-block "
        "publications.",
        rows, event_rate, "events/s")
    write_bar_svg(
        out / "adc-dma-measured-cpu.svg",
        "Measured firmware CPU slices",
        "Legacy task/report work was not instrumented; DMA task work includes "
        "filtering and message enqueue.",
        rows, hard_cpu, "% CPU", second_fn=task_cpu)
    write_bar_svg(
        out / "adc-dma-hardpath-latency.svg",
        "Mean hard-path service time",
        "Instrumentation is internal timer accounting; no diagnostic GPIO is "
        "toggled in the measured path.",
        rows, hard_mean_us, "us/event", color="#60a5fa")


if __name__ == "__main__":
    main()
