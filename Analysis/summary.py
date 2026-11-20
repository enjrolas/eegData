"""Reading the JSON summary the analysis saves in Trial.analysisData (no heavy imports)."""
import json


def load_summary(trial):
    """The JSON summary written by the analysis, or None."""
    if not trial.analysisData:
        return None
    try:
        with trial.analysisData.open("rb") as f:
            return json.loads(f.read().decode("utf-8"))
    except (OSError, ValueError):
        return None


def status_of(trial, summary=None):
    """(css class, label) for a trial's analysis state."""
    if not trial.edfFile:
        return "muted", "no EDF"
    if trial.analysisError:
        return "bad", "error"
    if not trial.analysisStarted:
        return "muted", "not analysed"
    summary = summary if summary is not None else load_summary(trial)
    if not summary:
        return "muted", "analysed (legacy)"
    st = summary.get("status", "?")
    return ("good" if st == "ok" else "warn" if st.startswith("weak") else "bad"), st
