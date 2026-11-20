import os
import json
import subprocess
import sys

from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
from django.conf import settings

from Trial.models import Trial

# Apache's WSGI runs Python 3.8 but numpy/pyedflib are installed for Python 3.9
# in the virtualenv. We shell out to the venv's Python for EDF reading.
VENV_PYTHON = os.path.join(settings.BASE_DIR, '.env', 'bin', 'python')

# Standalone script that reads EDF files and outputs JSON
EDF_READER_SCRIPT = os.path.join(settings.BASE_DIR, 'EDFViewer', 'edf_reader.py')


def _get_edf_path(trial):
    return os.path.join(settings.MEDIA_ROOT, trial.edfFile.name)


def _run_edf_reader(args):
    """Run the EDF reader script in the virtualenv Python and return parsed JSON."""
    result = subprocess.run(
        [VENV_PYTHON, EDF_READER_SCRIPT] + args,
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout)


def viewer(request, trial_id):
    trial = get_object_or_404(Trial, pk=trial_id)
    return render(request, 'EDFViewer/viewer.html', {'trial': trial})


def channel_list(request, trial_id):
    """Return list of channel labels and metadata for this trial's EDF file."""
    trial = get_object_or_404(Trial, pk=trial_id)
    filename = _get_edf_path(trial)
    data = _run_edf_reader(['channels', filename])
    data['trialId'] = trial_id
    return JsonResponse(data)


def signal_data(request, trial_id):
    """Return signal data for requested channels.

    Query params:
        channels: comma-separated channel indices (e.g. "0,1,5")
        start: start time in seconds (default 0)
        length: window length in seconds (default 10)
    """
    trial = get_object_or_404(Trial, pk=trial_id)
    filename = _get_edf_path(trial)

    channels = request.GET.get('channels', '0')
    start = request.GET.get('start', '0')
    length = request.GET.get('length', '10')

    data = _run_edf_reader(['signals', filename, channels, start, length])
    return JsonResponse(data)


def all_signal_data(request, trial_id):
    """Return full-duration signal data for requested channels.

    Query params:
        channels: comma-separated channel indices (e.g. "0,1,5")
    """
    trial = get_object_or_404(Trial, pk=trial_id)
    filename = _get_edf_path(trial)

    channels = request.GET.get('channels', '0')

    data = _run_edf_reader(['allsignals', filename, channels])
    return JsonResponse(data)
