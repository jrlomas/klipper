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


def replay_pulse_stats(fields, mpos, previous_clock=None):
    """Replay edges without retaining a tuple for every physical pulse."""
    duration = fields["duration"]
    start = fields["start_acc_q32"]
    finish = fields["end_acc_q32"]
    if finish == start:
        return 0, mpos, None, previous_clock
    direction = 1 if finish > start else -1
    max_pulses = (abs(finish - start) >> 48) + 2
    pulse_count = 0
    min_interval = None
    previous_t = 0
    last_clock = previous_clock
    while True:
        boundary = ((2 * mpos + direction) << 47)
        if direction > 0:
            if finish < boundary:
                break
        elif finish > boundary:
            break
        if pulse_count >= max_pulses:
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
        clock = fields["start_clock"] + low
        if last_clock is not None and clock > last_clock:
            interval = clock - last_clock
            min_interval = (interval if min_interval is None
                            else min(min_interval, interval))
        last_clock = clock
        pulse_count += 1
    return pulse_count, mpos, min_interval, last_clock


def load_records(path, start=None, end=None, actuator=None, session_id=None,
                 after_line=0):
    if session_id == "latest":
        latest = None
        with open(path, "r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    candidate = json.loads(line).get("session_id")
                except (ValueError, TypeError):
                    continue
                if candidate:
                    latest = candidate
        session_id = latest
        if session_id is None:
            raise ValueError("telemetry has no session identifiers")
    records = []
    with open(path, "r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if line_number <= after_line:
                continue
            try:
                record = json.loads(line)
            except (ValueError, TypeError):
                continue
            if (session_id is not None
                    and record.get("session_id") != session_id):
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
    intention_ranges = {}

    def checked_replay_stats(fields, mpos, previous_clock, context):
        try:
            return replay_pulse_stats(fields, mpos, previous_clock)
        except ValueError as exc:
            errors.append("%s: %s" % (context, exc))
            return 0, mpos, None, previous_clock

    by_actuator = {}
    for record in intentions:
        fields = record["fields"]
        by_actuator.setdefault(fields["actuator"], []).append(fields)
    for actuator, stream in sorted(by_actuator.items()):
        stream.sort(key=lambda f: (f["start_clock"], f["event"] != "rebase"))
        mpos = None
        last_clock = last_acc = None
        pulse_count = 0
        min_interval = None
        last_pulse_clock = None
        terminal_holds = 0
        oid = stream[0]["oid"]
        range_start = None
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
                range_start = fields["start_clock"]
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
                count = 0
            else:
                count, mpos, interval, last_pulse_clock = (
                    checked_replay_stats(
                        fields, mpos, last_pulse_clock,
                        "%s intention replay" % actuator))
                if interval is not None:
                    min_interval = (interval if min_interval is None
                                    else min(min_interval, interval))
            pulse_count += count
            last_clock = fields["end_clock"]
            last_acc = fields["end_acc_q32"]
            stream_prev = event
            terminal_holds += event == "hold"
            wire_end = signed32(last_acc >> 32)
            expected_ends.add((oid, last_clock & 0xffffffff, wire_end))
            expected_fields[(oid, last_clock & 0xffffffff,
                             wire_end)] = fields
            segments_by_oid.setdefault(oid, []).append(fields)
            if event == "hold" and range_start is not None:
                intention_ranges.setdefault(oid, []).append((
                    range_start & 0xffffffff, last_clock - range_start))
                range_start = None
        if stream and stream_prev != "hold":
            errors.append("%s: recorded path does not end in a hold" % actuator)
        summaries.append(
            "%s oid=%d segments=%d holds=%d pulses=%d min_interval_ticks=%s"
            % (actuator, oid, sum(f["event"] == "segment" for f in stream),
               terminal_holds, pulse_count,
               min_interval if min_interval is not None else "n/a"))

    # A reliable dump can overlap the live stream; sequence is per MCU.
    # Establish the current evidence window at the first matched rebase.  This
    # disambiguates stale ring records whose 32-bit MCU clock happens to alias
    # a later intention interval after wrap.
    first_rebase_clock = min(
        (fields["start_clock"] for fields in rebases.values()), default=None)
    sequence_anchors = []
    if first_rebase_clock is not None:
        for record in executions:
            fields = record["fields"]
            key = (fields["src_oid"], fields["mcu_clock"],
                   signed32(fields["position_su"]))
            rebase = rebases.get(key)
            if (fields["event"] == "rebase" and rebase is not None
                    and rebase["start_clock"] == first_rebase_clock):
                sequence_anchors.append(fields["seq"])
    sequence_anchor = min(sequence_anchors) if sequence_anchors else None
    unique = {}
    intended_oids = set(segments_by_oid)
    for record in executions:
        fields = dict(record["fields"])
        fields["position_su"] = signed32(fields["position_su"])
        if intended_oids and fields["src_oid"] not in intended_oids:
            continue
        if (sequence_anchor is not None
                and ((fields["seq"] - sequence_anchor) & 0xffffffff)
                >= 0x80000000):
            continue
        ranges = intention_ranges.get(fields["src_oid"], ())
        if ranges and not any(
                ((fields["mcu_clock"] - start) & 0xffffffff) <= span
                for start, span in ranges):
            # Reliable dumps may repeat pre-window records still resident in
            # the MCU ring.  Their wire clocks prove they are not evidence for
            # (or against) the recorded intention intervals.
            continue
        unique[(record.get("source"), fields["seq"])] = fields
    if intentions and not unique:
        errors.append("no MCU execution records for recorded intentions")
    matched = 0
    triggers = 0
    executed_stats = dict((oid, {
        "count": 0, "min_interval": None, "last_clock": None,
    }) for oid in intended_oids)
    executed_mpos = {}
    missing_rebase_reported = set()
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
                        if oid not in missing_rebase_reported:
                            errors.append(
                                "execution endpoint without rebase"
                                " oid=%d clock=%d" % (
                                    oid, fields["mcu_clock"]))
                            missing_rebase_reported.add(oid)
                    else:
                        stats = executed_stats[oid]
                        count, mpos, interval, last_clock = (
                            checked_replay_stats(
                                expected_fields[key], mpos,
                                stats["last_clock"],
                                "oid=%d execution replay" % oid))
                        stats["count"] += count
                        stats["last_clock"] = last_clock
                        if interval is not None:
                            current = stats["min_interval"]
                            stats["min_interval"] = (
                                interval if current is None
                                else min(current, interval))
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
                    stats = executed_stats[oid]
                    count, mpos, interval, last_clock = (
                        checked_replay_stats(
                            partial, mpos, stats["last_clock"],
                            "oid=%d trigger replay" % oid))
                    stats["count"] += count
                    stats["last_clock"] = last_clock
                    if interval is not None:
                        current = stats["min_interval"]
                        stats["min_interval"] = (
                            interval if current is None
                            else min(current, interval))
                    executed_mpos[oid] = mpos
                    nearest = (fields["position_su"] + 32768) // 65536
                    if (mpos - nearest) & 0xffff:
                        errors.append("trigger physical-step mismatch oid=%d:"
                                      " replayed=%d recorded-position=%d" % (
                                          oid, mpos, nearest))
    actuator_by_oid = dict((stream[0]["oid"], actuator)
                           for actuator, stream in by_actuator.items())
    for oid, stats in sorted(executed_stats.items()):
        summaries.append(
            "%s oid=%d executed_pulses=%d min_executed_interval_ticks=%s"
            % (actuator_by_oid.get(oid, "oid%d" % oid), oid,
               stats["count"],
               (stats["min_interval"]
                if stats["min_interval"] is not None else "n/a")))
    return summaries, matched, triggers, len(unique), errors


def main():
    parser = argparse.ArgumentParser(
        description="Replay Atlas HELIX intention/execution JSONL")
    parser.add_argument("path")
    parser.add_argument("--start", type=float)
    parser.add_argument("--end", type=float)
    parser.add_argument("--actuator")
    parser.add_argument(
        "--session", help="telemetry session id, or 'latest'")
    parser.add_argument(
        "--after-line", type=int, default=0,
        help="ignore records through this one-based input line")
    args = parser.parse_args()
    try:
        records = load_records(
            args.path, args.start, args.end, args.actuator,
            args.session, args.after_line)
    except ValueError as exc:
        print("ERROR:", exc)
        return 2
    result = audit(records)
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
