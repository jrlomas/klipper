#!/usr/bin/env python3
"""Measure ADC noise, correlation, and effective resolution from a CSV.

DC mode reports RMS-noise and peak-to-peak effective bits.  Sine mode fits the
fundamental and treats every residual (noise, distortion, drift) as error,
yielding a SINAD-equivalent ENOB without requiring NumPy.
"""

import argparse, csv, json, math


def mean(values):
    return sum(values) / len(values)


def dc_metrics(values, full_scale, representation_bits):
    avg = mean(values)
    centered = [value - avg for value in values]
    variance = sum(value * value for value in centered) / len(values)
    sigma = math.sqrt(variance)
    peak_to_peak = max(values) - min(values)
    covariance = sum(centered[i] * centered[i - 1]
                     for i in range(1, len(values)))
    denominator = sum(value * value for value in centered[:-1])
    lag1 = covariance / denominator if denominator else 0.
    # A stuck DC code is not evidence of perfect resolution: without enough
    # natural or deliberate dither there is no information about sub-code
    # position. Report the noise-derived figures as unresolved in that case.
    rms_bits = (math.log(full_scale / (math.sqrt(12.) * sigma), 2)
                if sigma else None)
    noise_free = (math.log(full_scale / peak_to_peak, 2)
                  if peak_to_peak else None)
    return {
        'samples': len(values), 'mean_code': avg, 'sigma_codes': sigma,
        'peak_to_peak_codes': peak_to_peak, 'lag1_correlation': lag1,
        'dc_codes_exercised': len(set(values)),
        'dc_resolution_resolved': bool(sigma and peak_to_peak),
        'rms_noise_limited_bits': (None if rms_bits is None else
                                   min(representation_bits, rms_bits)),
        'noise_free_bits': (None if noise_free is None else
                            min(representation_bits, noise_free)),
    }


def _solve_3x3(matrix, vector):
    rows = [list(matrix[i]) + [vector[i]] for i in range(3)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda row: abs(rows[row][col]))
        if abs(rows[pivot][col]) < 1.e-18:
            raise ValueError('sine fit is singular')
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


def sine_metrics(values, sample_rate, frequency):
    rows = []
    for pos in range(len(values)):
        phase = 2. * math.pi * frequency * pos / sample_rate
        rows.append((1., math.sin(phase), math.cos(phase)))
    matrix = [[sum(row[i] * row[j] for row in rows)
               for j in range(3)] for i in range(3)]
    vector = [sum(row[i] * value for row, value in zip(rows, values))
              for i in range(3)]
    offset, sine, cosine = _solve_3x3(matrix, vector)
    fitted = [offset + sine * row[1] + cosine * row[2] for row in rows]
    residual = [value - fit for value, fit in zip(values, fitted)]
    signal_rms = math.sqrt((sine * sine + cosine * cosine) / 2.)
    residual_rms = math.sqrt(sum(value * value for value in residual)
                             / len(residual))
    sinad_db = (20. * math.log10(signal_rms / residual_rms)
                if residual_rms else float('inf'))
    return {
        'fundamental_amplitude_codes': math.hypot(sine, cosine),
        'offset_code': offset, 'residual_rms_codes': residual_rms,
        'sinad_db': sinad_db,
        'sinad_enob': ((sinad_db - 1.76) / 6.02
                       if math.isfinite(sinad_db) else float('inf')),
    }


def analyze(values, resolution_bits, oversample, shift,
            sample_rate=None, frequency=None):
    full_scale = ((1 << resolution_bits) - 1) * oversample / (1 << shift)
    representation_bits = int(math.ceil(math.log(full_scale + 1., 2)))
    result = {
        'nominal_bits': resolution_bits,
        'oversample': oversample,
        'hardware_shift': shift,
        'full_scale_code': full_scale,
        'representation_bits': representation_bits,
        'ideal_oversample_gain_bits': .5 * math.log(oversample, 2),
        'ideal_enob_ceiling': min(
            representation_bits,
            resolution_bits + .5 * math.log(oversample, 2)),
        'dc': dc_metrics(values, full_scale, representation_bits),
    }
    if sample_rate is not None and frequency is not None:
        result['sine'] = sine_metrics(values, sample_rate, frequency)
    return result


def load_column(path, column):
    with open(path, newline='') as stream:
        rows = csv.DictReader(stream)
        if column not in (rows.fieldnames or []):
            raise ValueError("CSV has no column '%s'" % (column,))
        values = [float(row[column]) for row in rows
                  if row.get(column, '').strip()]
    if len(values) < 8:
        raise ValueError('at least eight samples are required')
    return values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('csv')
    parser.add_argument('--column', default='value')
    parser.add_argument('--resolution-bits', type=int, default=12)
    parser.add_argument('--oversample', type=int, default=1)
    parser.add_argument('--shift', type=int, default=0)
    parser.add_argument('--sample-rate', type=float)
    parser.add_argument('--frequency', type=float)
    args = parser.parse_args()
    if (args.sample_rate is None) != (args.frequency is None):
        parser.error('--sample-rate and --frequency must be used together')
    if args.oversample < 1 or args.oversample & (args.oversample - 1):
        parser.error('--oversample must be a positive power of two')
    if args.shift < 0 or args.shift > int(math.log(args.oversample, 2)):
        parser.error('--shift must be between zero and log2(oversample)')
    values = load_column(args.csv, args.column)
    print(json.dumps(analyze(
        values, args.resolution_bits, args.oversample, args.shift,
        args.sample_rate, args.frequency), indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
