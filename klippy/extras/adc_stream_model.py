# Deterministic host reference for the MCU ADC subscription filter.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.


class FilterModel:
    def __init__(self, input_div=1, osr=1, shift=0, report_div=1):
        if not (1 <= input_div <= 0xffff):
            raise ValueError("input_div must be between 1 and 65535")
        if not (1 <= osr <= 256):
            raise ValueError("osr must be between 1 and 256")
        if not (0 <= shift <= 31):
            raise ValueError("shift must be between 0 and 31")
        if not (1 <= report_div <= 4096):
            raise ValueError("report_div must be between 1 and 4096")
        self.input_div = input_div
        self.osr = osr
        self.shift = shift
        self.report_div = report_div
        self.reset()

    def reset(self, discontinuity=False):
        self.raw_index = 0
        self.accumulator = 0
        self.osr_count = 0
        self.outputs = []
        self.pending_discontinuity = bool(discontinuity)

    def push(self, sample, scan_index):
        if not (0 <= sample <= 0xffff):
            raise ValueError("sample must fit uint16")
        raw_index, self.raw_index = self.raw_index, self.raw_index + 1
        if raw_index % self.input_div:
            return None
        self.accumulator += sample
        self.osr_count += 1
        if self.osr_count != self.osr:
            return None
        value = self.accumulator
        if self.shift:
            value = (value + (1 << (self.shift - 1))) >> self.shift
        value = min(value, 0xffffffff)
        self.accumulator = self.osr_count = 0
        self.outputs.append((scan_index, value))
        if len(self.outputs) != self.report_div:
            return None
        summary = {
            "count": len(self.outputs),
            "minimum": min(v for _, v in self.outputs),
            "maximum": max(v for _, v in self.outputs),
            "sum": sum(v for _, v in self.outputs),
            "first_scan": self.outputs[0][0],
            "last_scan": self.outputs[-1][0],
            "flags": 1 if self.pending_discontinuity else 0,
        }
        self.outputs = []
        self.pending_discontinuity = False
        return summary


def run_interleaved(channels, subscriptions):
    """Run [(sample, ...), ...] scans through subscription dictionaries."""
    models = [FilterModel(**s["filter"]) for s in subscriptions]
    reports = [[] for _ in subscriptions]
    for scan_index, scan in enumerate(channels):
        for index, subscription in enumerate(subscriptions):
            result = models[index].push(
                scan[subscription["channel"]], scan_index)
            if result is not None:
                reports[index].append(result)
    return reports
