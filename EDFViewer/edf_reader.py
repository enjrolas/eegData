#!/usr/bin/env python
"""Standalone EDF reader script. Called by Django views via subprocess
to avoid Python version mismatch between Apache's WSGI (3.8) and the
virtualenv (3.9) where numpy/pyedflib are installed.

Usage:
    python edf_reader.py channels <edf_path>
    python edf_reader.py signals <edf_path> <channel_indices> <start> <length>
"""
import sys
import json
import pyedflib
from pyedflib import highlevel


def cmd_channels(edf_path):
    f = pyedflib.EdfReader(edf_path)
    labels = f.getSignalLabels()
    n_channels = f.signals_in_file
    channels = []
    for i in range(n_channels):
        channels.append({
            'index': i,
            'label': labels[i],
            'sampleRate': f.getSampleFrequency(i),
            'nSamples': int(f.getNSamples()[i]),
        })
    duration = f.getFileDuration()
    f.close()
    return {'duration': duration, 'channels': channels}


def cmd_signals(edf_path, channel_str, start_str, length_str):
    start = float(start_str)
    length = float(length_str)
    channel_indices = [int(c) for c in channel_str.split(',')]

    signals, signal_headers, header = highlevel.read_edf(edf_path)

    result = {}
    for idx in channel_indices:
        if idx < 0 or idx >= len(signals):
            continue
        fs = signal_headers[idx]['sample_frequency']
        start_sample = int(start * fs)
        end_sample = int((start + length) * fs)
        start_sample = max(0, start_sample)
        end_sample = min(len(signals[idx]), end_sample)

        data = signals[idx][start_sample:end_sample]

        # Downsample if too many points for the browser
        max_points = 4000
        if len(data) > max_points:
            factor = len(data) // max_points
            data = data[::factor]
            effective_fs = fs / factor
        else:
            effective_fs = fs

        result[str(idx)] = {
            'label': signal_headers[idx]['label'],
            'sampleRate': effective_fs,
            'physicalMin': float(signal_headers[idx].get('physical_min', -1)),
            'physicalMax': float(signal_headers[idx].get('physical_max', 1)),
            'dimension': signal_headers[idx].get('dimension', 'uV'),
            'data': data.tolist(),
        }

    return {'start': start, 'length': length, 'signals': result}


def cmd_allsignals(edf_path, channel_str):
    """Return full-duration signal data for the given channels.

    Keeps full resolution (no downsampling) so the client can slice
    any window and render it instantly without another server round-trip.
    """
    channel_indices = [int(c) for c in channel_str.split(',')]
    signals, signal_headers, header = highlevel.read_edf(edf_path)

    result = {}
    for idx in channel_indices:
        if idx < 0 or idx >= len(signals):
            continue
        fs = signal_headers[idx]['sample_frequency']
        result[str(idx)] = {
            'label': signal_headers[idx]['label'],
            'sampleRate': fs,
            'dimension': signal_headers[idx].get('dimension', 'uV'),
            'data': signals[idx].tolist(),
        }

    return {'signals': result}


if __name__ == '__main__':
    command = sys.argv[1]
    if command == 'channels':
        output = cmd_channels(sys.argv[2])
    elif command == 'signals':
        output = cmd_signals(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
    elif command == 'allsignals':
        output = cmd_allsignals(sys.argv[2], sys.argv[3])
    else:
        print(f'Unknown command: {command}', file=sys.stderr)
        sys.exit(1)
    json.dump(output, sys.stdout)
