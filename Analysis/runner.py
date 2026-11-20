"""
Run the ERP analysis for one trial from the web process.

The analysis needs ~200 MB (numpy/scipy/matplotlib + the EDF), so it runs as a
separate `manage.py analysis --trialId N` process under the project virtualenv
instead of inside the mod_wsgi daemon.  The command writes its results (or the
error) to the Trial row; the caller re-reads the row afterwards.
"""
import os
import subprocess
import sys
import tempfile

from django.conf import settings

BASE_DIR = str(settings.BASE_DIR)
VENV_PYTHON = os.path.join(BASE_DIR, ".env", "bin", "python")
TIMEOUT_S = 600


def run_analysis(pk):
    """Returns (process_ok, output_tail).  Trial row holds the real outcome."""
    python = VENV_PYTHON if os.path.exists(VENV_PYTHON) else sys.executable
    env = dict(os.environ)
    # www-data has no writable home; give matplotlib a cache dir that persists
    env["MPLCONFIGDIR"] = os.path.join(tempfile.gettempdir(), "eegdata-matplotlib")
    try:
        p = subprocess.run([python, os.path.join(BASE_DIR, "manage.py"), "analysis",
                            "--trialId", str(pk)],
                           cwd=BASE_DIR, env=env, capture_output=True, text=True,
                           timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return False, "analysis took longer than %d s and was stopped" % TIMEOUT_S
    except OSError as e:
        return False, "could not start the analysis: %s" % e
    out = (p.stdout + p.stderr).strip().splitlines()
    return p.returncode == 0, "\n".join(out[-15:])
