#!/usr/bin/env python3
"""Replay HELIX wire intentions and reconcile them with the MCU flight log."""

import argparse
import json
import sys


def signed32(value):
    value = int(value) & 0xffffffff
    return value - (1 << 32) if value & 0x80000000 else value


def _smul_shr(a, t, shift):
    value = (abs(a) * t) >> shift
    return -value if a < 0 else value


def _poly_term(coeff, t, nmul, nsh, divisor):
    value = coeff
    for index in range(nmul):
        value = _smul_shr(value, t, 16 if index < nsh else 0)
    value = abs(value) // divisor * (-1 if value < 0 else 1)
    return value


def end_delta(duration, velocity, accel, jerk=0, snap=0, crackle=0):
    delta = (velocity * duration) << 16
    product = accel * duration
    delta += (abs(product) * duration >> 1) * (-1 if product < 0 else 1)
    delta += _poly_term(jerk, duration, 3, 1, 6)
    delta += _poly_term(snap, duration, 4, 2, 24)
    delta += _poly_term(crackle, duration, 5, 3, 120)
    return delta


def segment_pos(fields, ticks):
    return fields["start_acc_q32"] + end_delta(
        ticks, fields.get("velocity", 0), fields.get("accel", 0),
        fields.get("jerk", 0), fields.get("snap", 0),
        fields.get("crackle", 0))


def replay_pulses(fields, mpos):
    """Return expected (absolute clock, physical mpos) GPIO step edges."""
    duration = fields["duration"]
    start = fields["start_acc_q32"]
    finish = fields["end_acc_q32"]
    if finish == start:
        return [], mpos
    direction = 1 if finish > start else -1
    pulses = []
    # A monotonic span cannot cross more whole-step boundaries than its
    # endpoint displacement plus the two fractional endpoint boundaries.
    # Fail closed if a corrupt position seed would otherwise make replay
    # iterate billions of times.
    max_pulses = (abs(finish - start) >> 48) + 2
    previous_t = 0
    while True:
        boundary = ((2 * mpos + direction) << 47)
        if direction > 0:
            if finish < boundary:
                break
        elif finish > boundary:
            break
        if len(pulses) >= max_pulses:
            raise ValueError("pulse replay exceeds endpoint displacement")
        low, high = previous_t, duration
        while low < high:
            middle = (low + high) // 2
            pos = segment_pos(fields, middle)
            crossed = pos >= boundary if direction > 0 else pos <= boundary
            if crossed:
                high = middle
            else:
                low = middle + 1
        previous_t = low
        mpos += direction
        pulses.append((fields["start_clock"] + low, mpos))
    return pulses, mpos


def load_records(path, start=None, end=None, actuator=None):
    records = []
    with open(path, "r", encoding="utf-8") as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except (ValueError, TypeError):
                continue
            mtime = record.get("machine_time")
            if start is not None and (mtime is None or mtime < start):
                continue
            if end is not None and (mtime is None or mtime > end):
                continue
            if (actuator is not None and record.get("kind") == "intention"
                    and record.get("fields", {}).get("actuator") != actuator):
                continue
            records.append(record)
    return records


def audit(records):
    intentions = [r for r in records if r.get("kind") == "intention"]
    executions = [r for r in records if r.get("kind") == "execution"]
    errors = []
    summaries = []
    expected_ends = set()
    expected_fields = {}
    rebases = {}
    segments_by_oid = {}

    def checked_replay(fields, mpos, context):
        try:
            return replay_pulses(fields, mpos)
        except ValueError as exc:
            errors.append("%s: %s" % (context, exc))
            return [], mpos

    by_actuator = {}
    for record in intentions:
        fields = record["fields"]
        by_actuator.setdefault(fields["actuator"], []).append(fields)
    for actuator, stream in sorted(by_actuator.items()):
        stream.sort(key=lambda f: (f["start_clock"], f["event"] != "rebase"))
        mpos = None
        last_clock = last_acc = None
        pulse_clocks = []
        terminal_holds = 0
        oid = stream[0]["oid"]
        for fields in stream:
            event = fields["event"]
            if event == "rebase":
                if last_clock is not None and stream_prev != "hold":
                    errors.append("%s: path rebased without terminal hold"
                                  % actuator)
                mpos = fields["mcu_position"]
                last_clock = fields["end_clock"]
                last_acc = fields["acc_q32"]
                stream_prev = "rebase"
                rebases[(oid, fields["start_clock"] & 0xffffffff,
                         fields["position_su"])] = fields
                continue
            if last_clock != fields["start_clock"]:
                errors.append("%s: clock discontinuity %s != %s" % (
                    actuator, fields["start_clock"], last_clock))
            if last_acc != fields["start_acc_q32"]:
                errors.append("%s: accumulator discontinuity" % actuator)
            want_end = fields["start_acc_q32"] + end_delta(
                fields["duration"], fields.get("velocity", 0),
                fields.get("accel", 0), fields.get("jerk", 0),
                fields.get("snap", 0), fields.get("crackle", 0))
            if want_end != fields["end_acc_q32"]:
                errors.append("%s: coefficient endpoint mismatch at %d" % (
                    actuator, fields["start_clock"]))
            if mpos is None:
                errors.append("%s: segment replay begins without a rebase"
                              % actuator)
                pulses = []
            else:
                pulses, mpos = checked_replay(
                    fields, mpos, "%s intention replay" % actuator)
            pulse_clocks.extend(clock for clock, unused in pulses)
            last_clock = fields["end_clock"]
            last_acc = fields["end_acc_q32"]
            stream_prev = event
            terminal_holds += event == "hold"
            wire_end = signed32(last_acc >> 32)
            expected_ends.add((oid, last_clock & 0xffffffff, wire_end))
            expected_fields[(oid, last_clock & 0xffffffff,
                             wire_end)] = fields
            segments_by_oid.setdefault(oid, []).append(fields)
        if stream and stream_prev != "hold":
            errors.append("%s: recorded path does not end in a hold" % actuator)
        intervals = [b - a for a, b in zip(pulse_clocks, pulse_clocks[1:])]
        summaries.append(
            "%s oid=%d segments=%d holds=%d pulses=%d min_interval_ticks=%s"
            % (actuator, oid, sum(f["event"] == "segment" for f in stream),
               terminal_holds, len(pulse_clocks),
               min(intervals) if intervals else "n/a"))

    # A reliable dump can overlap the live stream; sequence is per MCU.
    unique = {}
    intended_oids = set(segments_by_oid)
    for record in executions:
        fields = dict(record["fields"])
        fields["position_su"] = signed32(fields["position_su"])
        if intended_oids and fields["src_oid"] not in intended_oids:
            continue
        unique[(record.get("source"), fields["seq"])] = fields
    if intentions and not unique:
        errors.append("no MCU execution records for recorded intentions")
    matched = 0
    triggers = 0
    executed_pulses = dict((oid, []) for oid in intended_oids)
    executed_mpos = {}
    for fields in unique.values():
        event = fields["event"]
        key = (fields["src_oid"], fields["mcu_clock"],
               fields["position_su"])
        if event == "rebase":
            rebase = rebases.get(key)
            if rebase is None:
                errors.append("unmatched execution rebase oid=%d clock=%d"
                              " pos=%d" % key)
            else:
                executed_mpos[fields["src_oid"]] = rebase["mcu_position"]
        elif event in ("segment_done", "hold"):
            if key in expected_ends:
                matched += 1
                if event == "segment_done":
                    oid = fields["src_oid"]
                    mpos = executed_mpos.get(oid)
                    if mpos is None:
                        errors.append("execution endpoint without rebase"
                                      " oid=%d clock=%d" % (
                                          oid, fields["mcu_clock"]))
                    else:
                        pulses, mpos = checked_replay(
                            expected_fields[key], mpos,
                            "oid=%d execution replay" % oid)
                        executed_pulses[oid].extend(pulses)
                        executed_mpos[oid] = mpos
            else:
                errors.append("unmatched execution endpoint oid=%d clock=%d"
                              " pos=%d" % key)
        elif event == "underrun":
            errors.append("MCU recorded trajectory underrun oid=%d clock=%d"
                          % (fields["src_oid"], fields["mcu_clock"]))
        elif event == "trigger":
            triggers += 1
            candidates = segments_by_oid.get(fields["src_oid"], ())
            containing = []
            for segment in candidates:
                elapsed = ((fields["mcu_clock"]
                            - (segment["start_clock"] & 0xffffffff))
                           & 0xffffffff)
                if elapsed <= segment["duration"]:
                    containing.append((segment, elapsed))
            if not containing:
                errors.append("trigger outside recorded intention oid=%d"
                              " clock=%d" % (fields["src_oid"],
                                             fields["mcu_clock"]))
            else:
                segment, elapsed = min(containing, key=lambda item: item[1])
                expected = signed32(segment_pos(segment, elapsed) >> 32)
                if expected != fields["position_su"]:
                    errors.append("trigger position mismatch oid=%d:"
                                  " expected=%d executed=%d" % (
                                      fields["src_oid"], expected,
                                      fields["position_su"]))
                oid = fields["src_oid"]
                mpos = executed_mpos.get(oid)
                if mpos is None:
                    errors.append("execution trigger without rebase oid=%d"
                                  " clock=%d" % (oid,
                                                  fields["mcu_clock"]))
                else:
                    partial = dict(segment, duration=elapsed,
                                   end_acc_q32=segment_pos(segment, elapsed))
                    pulses, mpos = checked_replay(
                        partial, mpos, "oid=%d trigger replay" % oid)
                    executed_pulses[oid].extend(pulses)
                    executed_mpos[oid] = mpos
                    nearest = (fields["position_su"] + 32768) // 65536
                    if (mpos - nearest) & 0xffff:
                        errors.append("trigger physical-step mismatch oid=%d:"
                                      " replayed=%d recorded-position=%d" % (
                                          oid, mpos, nearest))
    actuator_by_oid = dict((stream[0]["oid"], actuator)
                           for actuator, stream in by_actuator.items())
    for oid, pulses in sorted(executed_pulses.items()):
        clocks = [clock for clock, unused in pulses]
        intervals = [b - a for a, b in zip(clocks, clocks[1:]) if b > a]
        summaries.append(
            "%s oid=%d executed_pulses=%d min_executed_interval_ticks=%s"
            % (actuator_by_oid.get(oid, "oid%d" % oid), oid, len(pulses),
               min(intervals) if intervals else "n/a"))
    return summaries, matched, triggers, len(unique), errors


def main():
    parser = argparse.ArgumentParser(
        description="Replay Atlas HELIX intention/execution JSONL")
    parser.add_argument("path")
    parser.add_argument("--start", type=float)
    parser.add_argument("--end", type=float)
    parser.add_argument("--actuator")
    args = parser.parse_args()
    result = audit(load_records(
        args.path, args.start, args.end, args.actuator))
    summaries, matched, triggers, executed, errors = result
    for summary in summaries:
        print(summary)
    print("execution_records=%d matched_boundaries=%d triggers=%d errors=%d"
          % (executed, matched, triggers, len(errors)))
    for error in errors:
        print("ERROR:", error)
    return 1 if errors or not summaries or not executed else 0


if __name__ == "__main__":
    sys.exit(main())
