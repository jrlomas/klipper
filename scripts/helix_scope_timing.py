#!/usr/bin/env python3
"""Correlate scoped cross-MCU GPIO edges with Helix timing state.

This commissioning tool drives a configured digital ``[output_pin]`` through
Moonraker, captures D0/D1 with an fx2lafw-compatible analyzer, and records both
the physical edge separation and each MCU's post-write ISR timestamp from
``QUERY_PIN_TIMING``.  It never sends motion or heater commands.
"""

import argparse
import json
import math
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


TIMING_RE = re.compile(
    r"mcu '([^']+)': value=(\d+) dropped=(\d+) scheduled=(\d+)"
    r" actual=(\d+) late=(-?\d+) ticks \(([-+0-9.]+)us\)")


def http_json(url, method='GET', payload=None, timeout=5.):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    request = urllib.request.Request(
        url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.load(response)
    except (urllib.error.URLError, ValueError) as exc:
        raise RuntimeError("Moonraker request failed: %s" % (exc,))
    if 'error' in body:
        raise RuntimeError("Moonraker error: %s" % (body['error'],))
    return body.get('result')


def query_objects(base_url, objects):
    query = '&'.join(urllib.parse.quote(name, safe='') for name in objects)
    result = http_json(
        '%s/printer/objects/query?%s' % (base_url.rstrip('/'), query))
    return result['status']


def run_gcode(base_url, script):
    return http_json('%s/printer/gcode/script' % (base_url.rstrip('/'),),
                     method='POST', payload={'script': script})


def parse_samplerate(value):
    match = re.fullmatch(r'\s*([0-9.]+)\s*([kKmM]?)\s*(?:Hz)?\s*', value)
    if match is None:
        raise ValueError("Invalid samplerate: %s" % (value,))
    scale = {'': 1., 'k': 1000., 'm': 1000000.}[match.group(2).lower()]
    return float(match.group(1)) * scale


def parse_sigrok_csv(path):
    first = transitions = None
    sample = -1
    with open(path, 'r', encoding='utf-8') as csv_file:
        for line in csv_file:
            if not line or line[0] in ';' or line.startswith('logic,'):
                continue
            fields = line.strip().split(',')
            if len(fields) < 2 or fields[0] not in ('0', '1'):
                continue
            sample += 1
            values = (int(fields[0]), int(fields[1]))
            if first is None:
                first = values
                transitions = [None, None]
                continue
            for channel in (0, 1):
                is_new_edge = values[channel] != first[channel]
                if transitions[channel] is None and is_new_edge:
                    transitions[channel] = sample
            if all(value is not None for value in transitions):
                break
    if first is None or transitions is None or any(
            value is None for value in transitions):
        raise RuntimeError("Both channel transitions were not captured")
    return {'initial': first, 'edge_samples': tuple(transitions)}


def parse_timing_messages(messages, expected_value, after_time=0.):
    states = {}
    for entry in reversed(messages):
        if entry.get('time', 0.) < after_time:
            continue
        for match in TIMING_RE.finditer(entry.get('message', '')):
            name = match.group(1)
            value = int(match.group(2))
            if value != expected_value or name in states:
                continue
            states[name] = {
                'value': value,
                'dropped': int(match.group(3)),
                'scheduled': int(match.group(4)),
                'actual': int(match.group(5)),
                'late_ticks': int(match.group(6)),
                'late_us': float(match.group(7)),
            }
    return states


def summarize(values):
    if not values:
        return None
    ordered = sorted(values)
    p99_index = min(len(ordered) - 1, math.ceil(len(ordered) * .99) - 1)
    return {
        'count': len(values),
        'mean': statistics.fmean(values),
        'stdev': statistics.pstdev(values),
        'min': ordered[0],
        'max': ordered[-1],
        'p99_abs': sorted(abs(value) for value in values)[p99_index],
        'max_abs': max(abs(value) for value in values),
    }


def wait_converged(base_url, timeout):
    deadline = time.monotonic() + timeout
    while True:
        status = query_objects(base_url, ['webhooks', 'timesync'])
        if status['webhooks']['state'] != 'ready':
            raise RuntimeError("Printer is not ready: %s" % (
                status['webhooks']['state_message'],))
        mcus = status.get('timesync', {}).get('mcus', {})
        if mcus and all(state.get('converged') for state in mcus.values()):
            return status['timesync']
        if time.monotonic() >= deadline:
            raise RuntimeError("Machine-time discipline did not converge")
        time.sleep(.25)


def capture_once(args, index, current_value, sample_rate_hz):
    value = 0 if current_value else 1
    edge = 'f' if value == 0 else 'r'
    stem = 'edge-%04d-%s' % (index, 'fall' if value == 0 else 'rise')
    session_path = os.path.join(args.output_dir, stem + '.sr')
    csv_path = os.path.join(args.output_dir, stem + '.csv')
    for path in (session_path, csv_path):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    timesync_before = None
    if args.require_converged:
        timesync_before = wait_converged(
            args.moonraker, args.converge_timeout)
    command = [
        args.sigrok, '-d', args.driver,
        '-c', 'samplerate=%s' % (args.samplerate,),
        '-c', 'captureratio=%d' % (args.capture_ratio,),
        '-C', '%s,%s' % (args.channel0, args.channel1),
        '-t', '%s=%s' % (args.channel0, edge),
        '--samples', str(args.samples), '-O', 'srzip', '-o', session_path,
    ]
    capture = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(args.arm_delay)
    command_time = time.time()
    command_name = ('SET_PIN_LEGACY_TIMING'
                    if args.legacy_timing else 'SET_PIN')
    run_gcode(args.moonraker, '%s PIN=%s VALUE=%d' % (
        command_name, args.pin, value))
    try:
        stdout, stderr = capture.communicate(timeout=args.capture_timeout)
    except subprocess.TimeoutExpired:
        capture.terminate()
        try:
            stdout, stderr = capture.communicate(timeout=1.)
        except subprocess.TimeoutExpired:
            capture.kill()
            stdout, stderr = capture.communicate()
        raise RuntimeError("Analyzer trigger timed out: %s" % (stderr.strip(),))
    if capture.returncode or not os.path.isfile(session_path):
        raise RuntimeError("Analyzer capture failed (%d): %s%s" % (
            capture.returncode, stdout, stderr))
    query_time = time.time()
    run_gcode(args.moonraker, 'QUERY_PIN_TIMING PIN=%s' % (args.pin,))
    time.sleep(args.query_delay)
    store = http_json('%s/server/gcode_store?count=100' % (
        args.moonraker.rstrip('/'),))['gcode_store']
    isr = parse_timing_messages(store, value, query_time)
    conversion = subprocess.run(
        [args.sigrok, '-i', session_path, '-O', 'csv', '-o', csv_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if conversion.returncode:
        raise RuntimeError("Trace conversion failed: %s" % (
            conversion.stderr.strip(),))
    parsed = parse_sigrok_csv(csv_path)
    edge0, edge1 = parsed['edge_samples']
    delta_us = (edge1 - edge0) / sample_rate_hz * 1000000.
    result = {
        'index': index,
        'command_unix_time': command_time,
        'value': value,
        'edge': 'fall' if value == 0 else 'rise',
        'session': session_path,
        'sample_rate_hz': sample_rate_hz,
        'edge_samples': {args.channel0: edge0, args.channel1: edge1},
        'scope_delta_us': delta_us,
        'isr': isr,
        'timesync_before': timesync_before,
    }
    state0 = isr.get(args.channel0_mcu)
    state1 = isr.get(args.channel1_mcu)
    if state0 is not None and state1 is not None:
        result['isr_delta_us'] = state1['late_us'] - state0['late_us']
        result['mapping_delta_us'] = (
            delta_us - result['isr_delta_us'])
    return value, result


def build_argparser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--moonraker', default='http://127.0.0.1:7125')
    parser.add_argument('--pin', default='atlas_machine_time_scope')
    parser.add_argument('--sigrok', default='sigrok-cli')
    parser.add_argument('--driver', default='fx2lafw')
    parser.add_argument('--channel0', default='D0')
    parser.add_argument('--channel1', default='D1')
    parser.add_argument('--channel0-mcu', default='ebb36')
    parser.add_argument('--channel1-mcu', default='mcu')
    parser.add_argument('--samplerate', default='24MHz')
    parser.add_argument('--samples', type=int, default=120000)
    parser.add_argument('--capture-ratio', type=int, default=50)
    parser.add_argument('--count', type=int, default=20)
    parser.add_argument('--max-attempts', type=int, default=0)
    parser.add_argument('--arm-delay', type=float, default=.75)
    parser.add_argument('--query-delay', type=float, default=.15)
    parser.add_argument('--attempt-delay', type=float, default=.25,
                        help=('minimum delay between analyzer attempts; '
                              'allows USB drivers to release the device'))
    parser.add_argument('--capture-timeout', type=float, default=8.)
    parser.add_argument('--converge-timeout', type=float, default=30.)
    parser.add_argument('--settle', type=float, default=0.)
    parser.add_argument('--output-dir', default='/tmp/helix-scope-timing')
    parser.add_argument('--label', default='machine-time')
    parser.add_argument('--legacy-timing', action='store_true',
                        help=('use Klipper per-MCU print-time scheduling '
                              'instead of the machine-time fanout'))
    parser.add_argument('--allow-unconverged', dest='require_converged',
                        action='store_false')
    parser.set_defaults(require_converged=True)
    return parser


def main(argv=None):
    args = build_argparser().parse_args(argv)
    if args.count <= 0 or args.samples <= 0:
        raise SystemExit("count and samples must be positive")
    sample_rate_hz = parse_samplerate(args.samplerate)
    os.makedirs(args.output_dir, exist_ok=True)
    if args.settle:
        time.sleep(args.settle)
    object_name = 'output_pin %s' % (args.pin,)
    status = query_objects(args.moonraker, ['webhooks', object_name])
    if status['webhooks']['state'] != 'ready':
        raise SystemExit("Printer is not ready")
    current_value = int(status[object_name]['value'] >= .5)
    records = []
    failures = []
    max_attempts = args.max_attempts or args.count * 3
    attempt = 0
    while len(records) < args.count and attempt < max_attempts:
        if attempt and args.attempt_delay:
            time.sleep(args.attempt_delay)
        attempt += 1
        try:
            current_value, record = capture_once(
                args, attempt, current_value, sample_rate_hz)
        except Exception as exc:
            # SET_PIN may have completed before a capture-side failure. Query
            # the logical output state so the next trigger uses the right edge.
            status = query_objects(args.moonraker, [object_name])
            current_value = int(status[object_name]['value'] >= .5)
            failure = {'attempt': attempt, 'error': str(exc)}
            failures.append(failure)
            print(json.dumps(failure, sort_keys=True), file=sys.stderr)
            continue
        records.append(record)
        print(json.dumps(record, sort_keys=True))
    if len(records) < args.count:
        raise SystemExit("Only %d/%d captures succeeded" % (
            len(records), args.count))
    scope_values = [record['scope_delta_us'] for record in records]
    isr_values = [record['isr_delta_us'] for record in records
                  if 'isr_delta_us' in record]
    mapping_values = [record['mapping_delta_us'] for record in records
                      if 'mapping_delta_us' in record]
    report = {
        'label': args.label,
        'clock_domain': ('legacy-print-time' if args.legacy_timing
                         else 'machine-time'),
        'pin': args.pin,
        'channels': {args.channel0: args.channel0_mcu,
                     args.channel1: args.channel1_mcu},
        'records': records,
        'failures': failures,
        'summary': {
            'scope_delta_us': summarize(scope_values),
            'isr_delta_us': summarize(isr_values),
            'mapping_delta_us': summarize(mapping_values),
        },
    }
    report_path = os.path.join(args.output_dir, 'report.json')
    with open(report_path, 'w', encoding='utf-8') as report_file:
        json.dump(report, report_file, indent=2, sort_keys=True)
        report_file.write('\n')
    print(json.dumps({'report': report_path,
                      'summary': report['summary']}, sort_keys=True))


if __name__ == '__main__':
    main()
