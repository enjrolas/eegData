"""
ERP analysis of the recognition potential (RP) for the visual recognition experiment.

Ported from /home/japhy/eeg-analysis.artiswrong.com/run_analysis.py.  This
module only computes: it reads the EDF and the Trial's JSON fields and returns
PNG bytes, a summary dict and a text log.  It never writes to the database or
to disk — the `analysis` management command decides what to do with the output.

Pipeline:
  1. Photodiode alignment.  The display toggles a patch with each image, so a
     photodiode channel spikes on every image or every other image.  Which EDF
     channel carries it differs between sessions (EKG1, CH21, A2, ...), so every
     channel (both polarities) is tried against the stimulus log and the best
     match wins.  A linear clock drift is fitted and each onset is snapped to
     its spike.  The EDF header start time is only a fallback (flagged).
  2. Connected-electrode detection.  Floating electrodes show ~1000 uV of noise;
     only candidate channels below CONNECTED_MAX_UV after filtering are used.
  3. 60 Hz notch + 0.5-15 Hz zero-phase bandpass.
  4. Epochs -200..800 ms, baseline -200..0 ms, reject > REJECT_UV peak-to-peak.
  5. Clear (target) vs scrambled averages, difference wave, permutation test
     on the mean amplitude in the RP window (two-sided: the RP's sign depends on
     the reference), keypress-locked check.

The four PNGs map onto the Trial model's existing image fields:
  periStimulusAverage                -> ERPs: clear vs scrambled per channel
  filteredPeriStimulusAverage        -> difference wave (clear - scrambled)
  filteredPeriPromptStimulusAverage  -> keypress-locked average
  periNonStimulusAverage             -> alignment check + signal overview
"""

import io
import json
from datetime import datetime

import numpy as np
import scipy.signal as ss
import matplotlib
matplotlib.use("Agg")  # headless backend — no display needed
import matplotlib.pyplot as plt
from pyedflib import highlevel

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Electrodes considered for the ERP, in order of RP relevance (occipital and
# posterior-temporal first).  Only the ones that look connected in a given
# recording are used.
CANDIDATE_CHANNELS = ["O1", "O2", "Oz", "T5", "T6", "P3", "P4", "Pz", "Cz"]
RP_SITES = ("O1", "O2", "Oz", "T5", "T6")
CONNECTED_MAX_UV = 50.0       # std of filtered signal; good ones ~10-20, floating 100+

LINE_FREQ = 60.0
BANDPASS = (0.5, 15.0)        # Hz
EPOCH_MS = (-200, 800)
BASELINE_MS = (-200, 0)
REJECT_UV = 100.0
MIN_REACTION_MS = 200
MAX_REACTION_MS = 1200
N_PERMUTATIONS = 2000

PHOTODIODE_MATCH_MS = 40      # tolerance when matching spikes to images
PHOTODIODE_MIN_GAIN = 0.25    # best match must beat typical match by this much
PHOTODIODE_GOOD_MATCH = 0.7   # below this, alignment is flagged as weak
OFFSET_STEP_MS = 20.0         # coarse search step (refined to 1 ms after)

# image name -> Trial model field
FIELDS = {
    "erp": "periStimulusAverage",
    "difference": "filteredPeriStimulusAverage",
    "keypress": "filteredPeriPromptStimulusAverage",
    "alignment": "periNonStimulusAverage",
}

from Analysis.labels import IMAGES, RP_WINDOW_MS, PEAK_SEARCH_MS, SITE_DOMAIN  # noqa: E402

COLORS = {"clear": "#c0392b", "scr": "#2c6fbb"}


class Log:
    """Collects lines for Trial.analysisOutput and echoes them to stdout."""

    def __init__(self, echo=True):
        self.lines, self.echo = [], echo

    def __call__(self, msg=""):
        self.lines.append(msg)
        if self.echo:
            print(msg, flush=True)

    def text(self):
        return "\n".join(self.lines)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def _number(v):
    try:
        f = float(v)
        return f if np.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def parse_trial_json(trial_data, presses):
    """trial_data: dict with a 'data' key as stored by the jigsaw page."""
    data = trial_data["data"]
    keys = sorted((k for k in data if k.isdigit()), key=int)
    rows = [data[k].split(",") for k in keys]
    if not rows:
        raise ValueError("trialData contains no image rows")
    # presses can contain nulls (see the javascript null-value fix); drop them
    presses = [p for p in (_number(v) for v in (presses or [])) if p is not None]
    return {
        "stim_ms": np.array([float(r[0]) for r in rows]),
        "clear": np.array([r[2].strip() == "false" for r in rows]),
        "meta": {k: v for k, v in data.items() if not k.isdigit()},
        "presses_ms": np.array(presses, dtype=float),
    }


def trial_from_model(t):
    """Build the analysis input from a Trial model instance (read-only)."""
    if not t.edfFile:
        raise ValueError("Trial #%d has no EDF file attached" % t.pk)
    td = json.loads(t.trialData)
    presses = json.loads(t.recognitionPresses) if t.recognitionPresses else []
    trial = parse_trial_json(td, presses)
    trial.update({
        "pk": t.pk,
        "edf": t.edfFile.path,
        "subject": str(t.subject),
        "timestamp": t.trialTimestamp,
    })
    return trial


def parse_timestamp(s):
    for fmt in ("%m/%d/%Y %H:%M:%S.%f", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except (ValueError, AttributeError):
            pass
    return None


# ---------------------------------------------------------------------------
# Photodiode alignment
# ---------------------------------------------------------------------------
def detect_spikes(x, fs):
    """Spike times (ms) of the large, sharp peaks in x."""
    lo, hi = np.median(x), np.percentile(x, 99.9)
    if hi - lo <= 0:
        return np.array([])
    peaks, _ = ss.find_peaks(x, height=lo + 0.5 * (hi - lo), distance=int(0.2 * fs))
    return peaks / fs * 1000.0


def match_matrix(spikes, times, offsets):
    """Boolean [n_offsets x n_times]: is there a spike within tolerance?

    Computed in chunks of offsets: the float temporaries for a full trial
    (~10k offsets x 500 images) otherwise peak at several hundred MB, enough
    to push this small server into swap and get mysqld OOM-killed.
    """
    out = np.empty((len(offsets), len(times)), dtype=bool)
    for a in range(0, len(offsets), 500):
        M = times[None, :] + offsets[a:a + 500, None]
        i = np.clip(np.searchsorted(spikes, M), 1, len(spikes) - 1)
        d = np.minimum(np.abs(spikes[i - 1] - M), np.abs(spikes[i] - M))
        out[a:a + 500] = d < PHOTODIODE_MATCH_MS
    return out


def find_photodiode(signals, labels, fs, stim_ms, only=None):
    """
    Try every channel as the photodiode and return (best, closest); best is
    None if nothing matches the stimulus log.

    For each channel, spike sets tried are: positive peaks, negative peaks, and
    both together (the patch turning on and off gives spikes of opposite sign
    on alternate images).  Each set is scored against every image, even
    images and odd images.
    """
    duration = signals.shape[1] / fs * 1000
    span = stim_ms[-1] - stim_ms[0]
    offsets = np.arange(-stim_ms[0] - 0.2 * span,
                        duration - stim_ms[-1] + 0.2 * span, OFFSET_STEP_MS)
    idx = np.arange(len(stim_ms))
    parities = {"every image": idx >= 0, "even images": idx % 2 == 0,
                "odd images": idx % 2 == 1}
    n_img = len(stim_ms)
    best = None
    for ch, x in zip(labels, signals):
        if only and ch != only:
            continue
        pos, neg = detect_spikes(x, fs), detect_spikes(-x, fs)
        sets = {"positive spikes": pos, "negative spikes": neg,
                "positive and negative spikes": np.sort(np.concatenate([pos, neg]))}
        for sname, spikes in sets.items():
            # a photodiode gives roughly one spike per image or per two images
            if not (0.3 * n_img <= len(spikes) <= 3 * n_img):
                continue
            hit = match_matrix(spikes, stim_ms, offsets)
            for pname, pmask in parities.items():
                frac = hit[:, pmask].mean(1)
                k = np.argmax(frac)
                gain = frac[k] - np.median(frac)
                # rank by images matched beyond chance, so a set covering
                # every image beats one covering only half
                score = gain * pmask.sum()
                if best is None or score > best["score"] * 1.02:
                    best = {"channel": ch, "spike_set": sname, "parity": pname,
                            "mask": pmask, "spikes": spikes,
                            "offset": offsets[k], "match": frac[k],
                            "typical": np.median(frac), "gain": gain, "score": score}
    if best is None or best["gain"] < PHOTODIODE_MIN_GAIN:
        return None, best
    # refine the offset at 1 ms resolution
    fine = best["offset"] + np.arange(-OFFSET_STEP_MS, OFFSET_STEP_MS + 1, 1.0)
    frac = match_matrix(best["spikes"], stim_ms[best["mask"]], fine).mean(1)
    best["offset"] = fine[np.argmax(frac)]
    best["match"] = frac.max()
    return best, best


# Inputs the screen sensors have been wired to.  Solar cells (pre-v1.2) and the
# v1.2+ photoresistors went into these; with double sync the two photoresistors
# watch patches of opposite colour, so a pair gives mirror-image signals.
SYNC_CHANNELS = ["CH21", "CH23", "CH24", "EKG1", "EKG2"]
SYNC_PAIRS = [("EKG1", "EKG2"), ("CH21", "Cz")]
TRANSITION_SEARCH_MS = 60     # how far before a spike's peak to look for the state change


def sync_candidates(signals, labels, scalp=False):
    """{name: (trace, member channels)} for the sync search.

    Dedicated inputs and the double-sync pairs (as differences) by default;
    with scalp=True, every other channel (fallback for sessions where the
    sync leaked into the cap)."""
    out = {}
    if not scalp:
        for c in SYNC_CHANNELS:
            if c in labels:
                out[c] = (signals[labels.index(c)], (c,))
        for a, b in SYNC_PAIRS:
            if a in labels and b in labels:
                out["%s/%s" % (a, b)] = (signals[labels.index(a)] - signals[labels.index(b)], (a, b))
    else:
        for c in labels:
            if c not in SYNC_CHANNELS:
                out[c] = (signals[labels.index(c)], (c,))
    return out


def clean_sync(x, fs):
    """Remove the sensor's offset, mains hum and fast flicker, keeping the flips."""
    b, a = ss.butter(2, 0.5, "high", fs=fs)
    x = ss.filtfilt(b, a, x)
    for f0 in (LINE_FREQ, 2 * LINE_FREQ):
        if f0 < 0.95 * fs / 2:
            nb, na = ss.iirnotch(f0, 10.0, fs)
            x = ss.filtfilt(nb, na, x)
    if 80 < 0.9 * fs / 2:
        b, a = ss.butter(4, 80, "low", fs=fs)
        x = ss.filtfilt(b, a, x)
    return x


def transition_times(trace, fs, peaks_ms):
    """
    Screen-change time for each detected spike: the moment the sensor changes
    state, not the peak.  On the cleaned trace, find the steepest part of the
    edge leading into the peak, take the level of the old state just before it
    and of the new state just after it, and return the instant the signal
    crosses halfway between them (interpolated between samples).  For a
    mirrored pair (A − B) that midpoint is the crossover of A and B.
    Returns (times_ms, refined_mask).
    """
    c = clean_sync(trace, fs)
    dc = np.diff(c)
    ms = lambda v: int(round(v * fs / 1000))
    w, pre0, pre1, post = ms(TRANSITION_SEARCH_MS), ms(40), ms(12), ms(30)
    out = np.array(peaks_ms, dtype=float)
    refined = np.zeros(len(out), dtype=bool)
    for n, p in enumerate(peaks_ms):
        i = int(round(p * fs / 1000))
        lo = i - w
        if lo - pre0 < 0 or i + post >= len(c):
            continue
        sgn = 1.0 if c[i] >= np.median(c[lo:i]) else -1.0
        e = lo + int(np.argmax(sgn * dc[lo:i]))          # steepest point of the edge
        old = np.median(c[e - pre0:e - pre1])             # level of the state being left
        new = c[e:e + post].max() if sgn > 0 else c[e:e + post].min()
        if sgn * (new - old) <= 0:
            continue
        mid = (old + new) / 2.0
        y = sgn * (c[e - pre1:e + post] - mid)            # crosses 0 upward at the transition
        up = np.where((y[:-1] <= 0) & (y[1:] > 0))[0]
        if not len(up):
            continue
        k = up[np.argmin(np.abs(up + e - pre1 - e))]      # crossing nearest the steepest point
        frac = -y[k] / (y[k + 1] - y[k])
        out[n] = (e - pre1 + k + frac) / fs * 1000.0
        refined[n] = True
    return out, refined


def align(trial, signals, labels, fs, header, photodiode=None):
    """
    Returns (onsets_ms, clock_map, info).  clock_map converts JS time to EDF
    time (used for keypresses).

    The sync is searched for on the dedicated sync inputs and double-sync
    pairs only.  Spikes find which flip is which; each flip is then timed at
    its state transition.  If there is no sync, there is no usable data:
    returns (None, None, info) with info["ok"] False — no fallback to scalp
    electrodes or the EDF header clock.
    """
    stim_ms = trial["stim_ms"]
    if photodiode:   # explicit override: any channel or pair may be named
        allc = {**sync_candidates(signals, labels), **sync_candidates(signals, labels, scalp=True)}
        cands = {photodiode: allc[photodiode]} if photodiode in allc else {}
    else:
        cands = sync_candidates(signals, labels)
    names = list(cands)
    pd, closest = (find_photodiode(np.array([cands[n][0] for n in names]), names, fs, stim_ms)
                   if names else (None, None))

    ts = parse_timestamp(trial["timestamp"])
    header_offset = ((ts - header["startdate"]).total_seconds() * 1000
                     if ts else np.nan)
    info = {"header_offset_ms": header_offset, "candidates": names}

    if pd is None:
        info.update({"method": "no sync signal on the sync inputs (%s)" % (", ".join(names) or "none recorded"),
                     "ok": False, "residuals_ms": np.array([]), "closest": closest})
        return None, None, info

    trace, members = cands[pd["channel"]]
    peaks, mask = pd["spikes"], pd["mask"]
    # time each flip at its change of state, not at the spike's peak
    spikes, refined = transition_times(trace, fs, peaks)
    s = stim_ms + pd["offset"]
    j = np.clip(np.searchsorted(s, spikes), 1, len(s) - 1)
    j = np.where(np.abs(spikes - s[j - 1]) <= np.abs(spikes - s[j]), j - 1, j)
    resid = spikes - s[j]
    ok = (np.abs(resid) < 2 * PHOTODIODE_MATCH_MS) & mask[j]
    drift = np.polyfit(s[j[ok]], resid[ok], 1)
    off = pd["offset"]
    clock_map = lambda js: (js + off) + np.polyval(drift, js + off)

    onsets = clock_map(stim_ms)
    corr = np.full(len(stim_ms), np.nan)
    corr[j[ok]] = spikes[ok] - onsets[j[ok]]
    idx = np.arange(len(stim_ms))
    have = ~np.isnan(corr)
    corr = np.interp(idx, idx[have], corr[have])

    pair = len(members) == 2
    info.update({
        "method": "%s on %s (%s, matched to %s; timed at the %s)"
                  % ("sync crossover" if pair else "sync", pd["channel"], pd["spike_set"],
                     pd["parity"], "crossover of the pair" if pair else "state transition"),
        "ok": True, "channel": pd["channel"], "members": members, "trace": trace,
        "offset_ms": float(clock_map(0.0)), "drift_ppm": drift[0] * 1e6,
        "match": pd["match"], "typical": pd["typical"],
        "n_matched": int(ok.sum()), "residuals_ms": corr[have],
        "refined_frac": float(refined[ok].mean()) if ok.any() else 0.0,
        "peak_lag_ms": float(np.median((peaks - spikes)[ok & refined])) if (ok & refined).any() else None,
    })
    return onsets + corr, clock_map, info


# ---------------------------------------------------------------------------
# Signal processing
# ---------------------------------------------------------------------------
def filter_channel(x, fs):
    nb, na = ss.iirnotch(LINE_FREQ, 30.0, fs)
    bb, ba = ss.butter(3, BANDPASS, btype="band", fs=fs)
    return ss.filtfilt(bb, ba, ss.filtfilt(nb, na, x))


def extract_epochs(x, fs, times_ms, window_ms=EPOCH_MS, baseline_ms=BASELINE_MS):
    """Baseline-corrected epochs [n_kept x n_samples], time axis, n_rejected."""
    n0 = int(round(window_ms[0] * fs / 1000))
    n1 = int(round(window_ms[1] * fs / 1000))
    tax = np.arange(n0, n1) * 1000.0 / fs
    bl = (tax >= baseline_ms[0]) & (tax < baseline_ms[1])
    kept, rejected = [], 0
    for t in times_ms:
        i = int(round(t * fs / 1000))
        if i + n0 < 0 or i + n1 > len(x):
            rejected += 1
            continue
        seg = x[i + n0:i + n1]
        seg = seg - seg[bl].mean()
        if np.ptp(seg) > REJECT_UV:
            rejected += 1
            continue
        kept.append(seg)
    return np.array(kept).reshape(-1, len(tax)), tax, rejected


def permutation_test(a, b, mask, n=N_PERMUTATIONS, seed=0):
    """Two-sided permutation test on mean(a) - mean(b) within mask."""
    obs = a[:, mask].mean() - b[:, mask].mean()
    pooled = np.concatenate([a[:, mask].mean(1), b[:, mask].mean(1)])
    rng = np.random.default_rng(seed)
    na = len(a)
    null = np.empty(n)
    for k in range(n):
        p = rng.permutation(pooled)
        null[k] = p[:na].mean() - p[na:].mean()
    return obs, (np.sum(np.abs(null) >= abs(obs)) + 1) / (n + 1)


# ---------------------------------------------------------------------------
# Plot helpers
# ---------------------------------------------------------------------------
def shade_mean(ax, tax, E, color, label):
    m = E.mean(0)
    se = E.std(0) / np.sqrt(len(E))
    ax.plot(tax, m, color=color, lw=1.6, label="%s (n=%d)" % (label, len(E)))
    ax.fill_between(tax, m - 2 * se, m + 2 * se, color=color, alpha=0.18, lw=0)


def decorate(ax, xlabel="ms after image onset"):
    ax.axvline(0, color="k", lw=0.8)
    ax.axhline(0, color="k", lw=0.4)
    ax.axvspan(*RP_WINDOW_MS, color="gold", alpha=0.18)
    ax.set_xlabel(xlabel)
    ax.set_ylabel("µV")
    ax.legend(fontsize=8, loc="upper left")


def to_png(fig, dpi=90, close=True):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi)
    if close:
        plt.close(fig)
    return buf.getvalue()


def render(fig):
    """(web PNG at 90 dpi, report PNG at 150 dpi); closes the figure."""
    hi = to_png(fig, dpi=150, close=False)
    return to_png(fig), hi


def placeholder(pk, title, reason):
    """Stand-in image so every Trial field still gets a picture."""
    fig, ax = plt.subplots(figsize=(16, 3))
    ax.axis("off")
    ax.text(0.5, 0.6, "Trial #%d — %s" % (pk, title), ha="center", fontsize=14,
            transform=ax.transAxes)
    ax.text(0.5, 0.3, reason, ha="center", fontsize=11, color="#a04000",
            transform=ax.transAxes)
    return render(fig)


# ---------------------------------------------------------------------------
# Analysis of one trial
# ---------------------------------------------------------------------------
def no_sync_result(trial, signals, labels, fs, al, log):
    """Result for a trial whose sync inputs show no screen changes: no usable data."""
    pk, clear = trial["pk"], trial["clear"]
    reason = "No sync signal: image onsets can't be located in the EEG, so this trial has no usable data."
    log("  status: no sync")
    # show what the sync inputs did record, so the absence can be checked by eye
    cands = sync_candidates(signals, labels)
    n = max(len(cands), 1)
    fig, axes = plt.subplots(n, 1, figsize=(18, 1.6 + 1.3 * n), sharex=True, squeeze=False)
    t = np.arange(signals.shape[1]) / fs
    m = (t >= t[-1] / 2) & (t <= t[-1] / 2 + 10)
    for ax, (name, (x, _members)) in zip(axes[:, 0], cands.items()):
        ax.plot(t[m], clean_sync(x, fs)[m], lw=0.7, color="0.3")
        ax.set_ylabel(name, rotation=0, ha="right", va="center", fontsize=9)
        ax.tick_params(labelsize=8)
    if not cands:
        axes[0, 0].axis("off")
        axes[0, 0].text(0.5, 0.5, "No sync inputs (%s) in this recording" % ", ".join(SYNC_CHANNELS),
                        ha="center", transform=axes[0, 0].transAxes)
    axes[-1, 0].set_xlabel("s in EDF (10 s from mid-recording; mains and offset removed)")
    fig.suptitle("Trial #%d — NO SYNC: the sync inputs show no screen changes "
                 "(a usable trial shows a step every ~330 ms)" % pk)
    fig.tight_layout()
    images, hires = {}, {}
    images["alignment"], hires["alignment"] = render(fig)
    for name, title in (("erp", "ERPs clear vs scrambled"), ("difference", "difference wave"),
                        ("keypress", "keypress-locked average")):
        images[name], hires[name] = placeholder(pk, title, reason)
    summary = {
        "pk": pk, "subject": trial["subject"], "timestamp": trial["timestamp"],
        "dataset": trial["meta"].get("dataset", "?"),
        "alignment_ok": False, "alignment": al["method"], "alignment_match": None,
        "sync_channel": None, "sync_members": [], "sync_peak_lag_ms": None,
        "electrode_noise_uv": {}, "channels": [], "stats": {},
        "n_clear_with_press": 0, "median_rt_ms": None,
        "best_channel": None, "best_p": None, "best_mean_uv": None, "best_peak_ms": None,
        "status": "no sync",
        "config": {"bandpass_hz": BANDPASS, "epoch_ms": EPOCH_MS, "baseline_ms": BASELINE_MS,
                   "rp_window_ms": RP_WINDOW_MS, "peak_search_ms": PEAK_SEARCH_MS,
                   "reject_uv": REJECT_UV, "connected_max_uv": CONNECTED_MAX_UV},
        "edf": trial["edf"].rsplit("/", 1)[-1],
        "n_images": int(len(trial["stim_ms"])), "n_clear": int(clear.sum()),
        "n_scrambled": int((~clear).sum()), "n_presses": int(len(trial["presses_ms"])),
        "used_channels": [], "alignment_detail": al["method"],
        "warnings": report_warnings(al, {}),
    }
    log_text = log.text()
    return {"images": images, "summary": summary, "log": log_text,
            "pdf": build_pdf(summary, hires, log_text)}


def analyze(trial, photodiode=None, channels=None, log=None):
    """
    Returns {"images": {name: png_bytes}, "summary": dict, "log": str}.
    `images` always has the four keys in FIELDS.
    """
    log = log or Log()
    pk = trial["pk"]
    stim_ms, clear = trial["stim_ms"], trial["clear"]
    log("=" * 60)
    log("Trial #%d — subject %s, %s" % (pk, trial["subject"], trial["timestamp"]))
    log("  EDF: %s" % trial["edf"])

    signals, signal_headers, header = highlevel.read_edf(trial["edf"])
    labels = [h["label"] for h in signal_headers]
    fs = float(signal_headers[0]["sample_frequency"])
    log("  %.0f Hz, %.0f s; %d images (%d clear, %d scrambled); %d presses"
        % (fs, signals.shape[1] / fs, len(stim_ms), clear.sum(), (~clear).sum(),
           len(trial["presses_ms"])))

    # --- Alignment ----------------------------------------------------------
    onsets, clock_map, al = align(trial, signals, labels, fs, header, photodiode)
    log("  alignment: %s" % al["method"])
    if not al["ok"]:
        return no_sync_result(trial, signals, labels, fs, al, log)
    presses = clock_map(trial["presses_ms"])
    if al["ok"]:
        log("    offset %.0f ms (EDF header says %.0f); %.0f%% of images matched "
            "(typical %.0f%%); drift %.0f ppm; jitter %.1f ms"
            % (al["offset_ms"], al["header_offset_ms"], 100 * al["match"],
               100 * al["typical"], al["drift_ppm"], al["residuals_ms"].std()))
        if al.get("peak_lag_ms") is not None:
            log("    flips timed at the change of state (%.0f%% refined); spike peaks lag it by %.1f ms"
                % (100 * al["refined_frac"], al["peak_lag_ms"]))

    # --- Connected electrodes ----------------------------------------------
    filtered, noise = {}, {}
    for c in CANDIDATE_CHANNELS:
        if c in labels and c not in al.get("members", ()):
            filtered[c] = filter_channel(signals[labels.index(c)], fs)
            noise[c] = float(np.std(filtered[c]))
    if channels:
        use = [c for c in channels if c in filtered]
    else:
        use = [c for c in CANDIDATE_CHANNELS if noise.get(c, np.inf) < CONNECTED_MAX_UV]
    log("  electrode noise (µV): %s" % ", ".join("%s %.0f" % kv for kv in noise.items()))
    log("  using: %s" % (", ".join(use) or "NONE"))

    # --- Behaviour ----------------------------------------------------------
    rts, press_for_clear = [], []
    for t in onsets[clear]:
        cand = presses[(presses > t + MIN_REACTION_MS) & (presses < t + MAX_REACTION_MS)]
        if len(cand):
            rts.append(cand[0] - t)
            press_for_clear.append(cand[0])
    rts = np.array(rts)
    median_rt = float(np.median(rts)) if len(rts) else float("nan")
    log("  %d of %d clear images followed by a keypress (median RT %.0f ms)"
        % (len(rts), clear.sum(), median_rt))

    # --- ERPs & stats -------------------------------------------------------
    E_clear, E_scr, stats = {}, {}, {}
    tax = None
    for c in use:
        E_clear[c], tax, _ = extract_epochs(filtered[c], fs, onsets[clear])
        E_scr[c], _, _ = extract_epochs(filtered[c], fs, onsets[~clear])
        if len(E_clear[c]) < 5 or len(E_scr[c]) < 5:
            log("  %s: too few clean epochs, skipped" % c)
            continue
        win = (tax >= RP_WINDOW_MS[0]) & (tax <= RP_WINDOW_MS[1])
        diff = E_clear[c].mean(0) - E_scr[c].mean(0)
        obs, p = permutation_test(E_clear[c], E_scr[c], win)
        # largest difference of either sign in the search window
        srch = (tax >= PEAK_SEARCH_MS[0]) & (tax <= PEAK_SEARCH_MS[1])
        k = np.argmax(np.abs(diff[srch]))
        stats[c] = {"diff": diff, "mean": float(obs), "p": float(p),
                    "peak_ms": float(tax[srch][k]),
                    "peak_uv": float(diff[srch][k]),
                    "n_clear": len(E_clear[c]), "n_scr": len(E_scr[c])}
        log("  %s: clear−scrambled %d–%d ms = %+.2f µV, p=%.3f, peak %+.1f µV @ %d ms"
            % (c, RP_WINDOW_MS[0], RP_WINDOW_MS[1], obs, p,
               stats[c]["peak_uv"], stats[c]["peak_ms"]))
    chans = list(stats)

    status = ("no connected electrodes" if not chans else
              "weak alignment (%.0f%% matched)" % (100 * al["match"])
              if al["match"] < PHOTODIODE_GOOD_MATCH else "ok")
    no_erp = ("No connected electrodes among %s (all above %g µV noise after filtering)"
              % (", ".join(CANDIDATE_CHANNELS), CONNECTED_MAX_UV))

    images, hires = {}, {}
    t_axis = np.arange(signals.shape[1]) / fs * 1000

    # IMAGE: alignment check + signal overview  -> periNonStimulusAverage
    fig = plt.figure(figsize=(18, 8))
    gs = fig.add_gridspec(2, 2, width_ratios=[3, 1])
    ax = fig.add_subplot(gs[0, 0])
    w0, w1 = onsets[0] - 500, onsets[0] + 4500
    m = (t_axis >= w0) & (t_axis <= w1)
    if al["ok"]:
        pdx = al["trace"]
        ax.plot(t_axis[m] / 1000, pdx[m], color="0.4", lw=0.8,
                label="%s (sync%s)" % (al["channel"], " pair, difference" if len(al["members"]) == 2 else ""))
    ylo, yhi = ax.get_ylim() if al["ok"] else (0, 1)
    sel = (onsets >= w0) & (onsets <= w1)
    ax.vlines(onsets[sel & clear] / 1000, ylo, yhi, color=COLORS["clear"], lw=1.5, label="clear onset")
    ax.vlines(onsets[sel & ~clear] / 1000, ylo, yhi, color=COLORS["scr"], lw=0.8, ls="--",
              label="scrambled onset")
    ax.set_xlabel("s in EDF")
    ax.set_title("Sync signal vs. aligned image onsets (first 5 s; onsets at each change of state)"
                 if al["ok"] else "NO PHOTODIODE FOUND — onsets from EDF header time")
    ax.legend(fontsize=8, loc="upper right")
    ax = fig.add_subplot(gs[0, 1])
    if al["ok"]:
        ax.hist(al["residuals_ms"], bins=40, color="0.5")
        ax.set_title("Screen change − JS log after drift fit\n(std %.1f ms)" % al["residuals_ms"].std())
        ax.set_xlabel("ms")
    else:
        ax.axis("off")
        ax.text(0.5, 0.5, "alignment unreliable", ha="center", transform=ax.transAxes)
    ax = fig.add_subplot(gs[1, :])
    c0 = chans[0] if chans else next(iter(filtered), None)
    if c0:
        w0, w1 = onsets[0] - 500, onsets[0] + 15000
        m = (t_axis >= w0) & (t_axis <= w1)
        y = filtered[c0][m]
        lo, hi = (y.min(), y.max()) if len(y) else (-1, 1)
        ax.plot(t_axis[m] / 1000, y, color="0.3", lw=0.7, label="%s (filtered)" % c0)
        ax.vlines(onsets[clear] / 1000, lo, hi, color=COLORS["clear"], lw=1.5, label="clear")
        ax.vlines(onsets[~clear] / 1000, lo / 2, hi / 2, color=COLORS["scr"], lw=0.6, label="scrambled")
        if len(presses):
            ax.vlines(presses / 1000, lo, hi, color="purple", ls="--", lw=1, label="keypress")
        ax.set_xlim(w0 / 1000, w1 / 1000)
        ax.set_xlabel("s in EDF")
        ax.set_ylabel("µV")
        ax.legend(fontsize=8, loc="upper right")
        ax.set_title("First 15 s of trial: filtered %s with events%s"
                     % (c0, "" if chans else "  (electrode NOT connected)"))
    else:
        ax.axis("off")
    fig.suptitle("Trial #%d — alignment check (%s)   status: %s" % (pk, al["method"], status))
    fig.tight_layout()
    images["alignment"], hires["alignment"] = render(fig)

    if chans:
        # IMAGE: ERPs per channel  -> periStimulusAverage
        ncol = 2 if len(chans) > 1 else 1
        nrow = int(np.ceil(len(chans) / ncol))
        fig, axes = plt.subplots(nrow, ncol, figsize=(16, 4.5 * nrow), sharex=True, squeeze=False)
        for ax in axes.flat[len(chans):]:
            ax.axis("off")
        for ax, c in zip(axes.flat, chans):
            shade_mean(ax, tax, E_clear[c], COLORS["clear"], "clear (target)")
            shade_mean(ax, tax, E_scr[c], COLORS["scr"], "scrambled")
            decorate(ax)
            ax.set_title("%s   (clear − scrambled %d–%d ms: %+.1f µV, p=%.3f)"
                         % (c, RP_WINDOW_MS[0], RP_WINDOW_MS[1],
                            stats[c]["mean"], stats[c]["p"]))
        fig.suptitle("Trial #%d — ERPs clear vs scrambled, %g–%g Hz, ±2 SE shaded; status: %s"
                     % (pk, BANDPASS[0], BANDPASS[1], status))
        fig.tight_layout()
        images["erp"], hires["erp"] = render(fig)

        # IMAGE: difference waves  -> filteredPeriStimulusAverage
        fig, ax = plt.subplots(figsize=(16, 4.5))
        for c in chans:
            ax.plot(tax, stats[c]["diff"], lw=1.6, label="%s (p=%.3f)" % (c, stats[c]["p"]))
        decorate(ax)
        ax.set_title("Trial #%d — difference wave: clear − scrambled (RP window shaded)" % pk)
        fig.tight_layout()
        images["difference"], hires["difference"] = render(fig)

        # IMAGE: keypress-locked  -> filteredPeriPromptStimulusAverage
        if press_for_clear:
            fig, ax = plt.subplots(figsize=(16, 4.5))
            for c in chans:
                Ek, tk, _ = extract_epochs(filtered[c], fs, press_for_clear,
                                           window_ms=(-800, 400), baseline_ms=(-800, -600))
                if len(Ek):
                    ax.plot(tk, Ek.mean(0), lw=1.4, label="%s (n=%d)" % (c, len(Ek)))
            ax.axvline(0, color="k", lw=0.8)
            ax.axhline(0, color="k", lw=0.4)
            ax.set_xlabel("ms relative to keypress")
            ax.set_ylabel("µV")
            ax.legend(fontsize=8, loc="upper left")
            ax.set_title("Trial #%d — keypress-locked average (median RT %.0f ms); a response "
                         "driven by the press peaks near 0" % (pk, median_rt))
            fig.tight_layout()
            images["keypress"], hires["keypress"] = render(fig)
        else:
            images["keypress"], hires["keypress"] = placeholder(pk, "keypress-locked average",
                                             "No keypresses followed a clear image")
    else:
        images["erp"], hires["erp"] = placeholder(pk, "ERPs clear vs scrambled", no_erp)
        images["difference"], hires["difference"] = placeholder(pk, "difference wave", no_erp)
        images["keypress"], hires["keypress"] = placeholder(pk, "keypress-locked average", no_erp)

    best = min(stats.items(), key=lambda kv: kv[1]["p"]) if stats else None
    summary = {
        "pk": pk, "subject": trial["subject"], "timestamp": trial["timestamp"],
        "dataset": trial["meta"].get("dataset", "?"),
        "alignment_ok": al["ok"], "alignment": al["method"],
        "alignment_match": float(al["match"]) if al["ok"] else None,
        "sync_channel": al.get("channel"), "sync_members": list(al.get("members", ())),
        "sync_peak_lag_ms": al.get("peak_lag_ms"),
        "electrode_noise_uv": noise,
        "channels": chans,
        "stats": {c: {k: v for k, v in s.items() if k != "diff"} for c, s in stats.items()},
        "n_clear_with_press": len(rts), "median_rt_ms": median_rt if len(rts) else None,
        "best_channel": best[0] if best else None,
        "best_p": best[1]["p"] if best else None,
        "best_mean_uv": best[1]["mean"] if best else None,
        "best_peak_ms": best[1]["peak_ms"] if best else None,
        "status": status,
        "config": {"bandpass_hz": BANDPASS, "epoch_ms": EPOCH_MS, "baseline_ms": BASELINE_MS,
                   "rp_window_ms": RP_WINDOW_MS, "peak_search_ms": PEAK_SEARCH_MS, "reject_uv": REJECT_UV,
                   "connected_max_uv": CONNECTED_MAX_UV},
        # extra details for the report
        "edf": trial["edf"].rsplit("/", 1)[-1],
        "n_images": int(len(stim_ms)), "n_clear": int(clear.sum()),
        "n_scrambled": int((~clear).sum()), "n_presses": int(len(trial["presses_ms"])),
        "used_channels": use,
        "alignment_detail": alignment_text(al, len(stim_ms)),
        "warnings": report_warnings(al, stats),
    }
    log("  status: %s" % status)
    log_text = log.text()
    return {"images": images, "summary": summary, "log": log_text,
            "pdf": build_pdf(summary, hires, log_text)}


# ---------------------------------------------------------------------------
# Report (PDF)
# ---------------------------------------------------------------------------
def alignment_text(al, n_images):
    if not al["ok"]:
        return al["method"]
    lag = ("; peaks lag the change of state by %.1f ms" % al["peak_lag_ms"]
           if al.get("peak_lag_ms") is not None else "")
    return ("%s; clock offset %.0f ms (EDF header would give %.0f ms); %d/%d images matched; "
            "drift %.0f ppm; jitter %.1f ms%s"
            % (al["method"], al["offset_ms"], al["header_offset_ms"], al["n_matched"],
               n_images, al["drift_ppm"], al["residuals_ms"].std(), lag))


def report_warnings(al, stats):
    warn = []
    if not al["ok"]:
        warn.append("No sync signal: none of the sync inputs recorded the screen changing, so the "
                    "image onsets can't be located in the EEG. This trial has no usable data.")
        return warn
    if al["ok"] and al["match"] < PHOTODIODE_GOOD_MATCH:
        warn.append("Only %.0f%% of images matched a photodiode spike on %s, so the alignment "
                    "may be wrong. Check the alignment plot before trusting these averages."
                    % (100 * al["match"], al["channel"]))
    if not stats:
        warn.append("No connected electrodes among %s (all above %g µV noise)."
                    % (", ".join(CANDIDATE_CHANNELS), CONNECTED_MAX_UV))
    elif not any(c in stats for c in RP_SITES):
        warn.append("No occipital or posterior-temporal electrode (%s) was connected; the "
                    "recognition potential is normally largest there." % ", ".join(RP_SITES))
    return warn


def _wrap(fig, x, y, text, width, line_h, **kw):
    """Draw wrapped text from (x, y) downwards in figure coords; returns the new y."""
    import textwrap
    for para in text.split("\n"):
        for line in textwrap.wrap(para, width) or [""]:
            fig.text(x, y, line, va="top", **kw)
            y -= line_h
    return y


def build_pdf(s, hires, log_text):
    """Multi-page PDF report (letter landscape) — summary page, one page per plot, log."""
    from matplotlib.backends.backend_pdf import PdfPages
    PAGE = (11, 8.5)
    buf = io.BytesIO()
    footer = "Trial #%d • generated %s • %s" % (
        s["pk"], datetime.now().strftime("%Y-%m-%d %H:%M"), SITE_DOMAIN)
    with PdfPages(buf, metadata={"Title": "Trial #%d recognition potential analysis" % s["pk"],
                                 "Subject": "EEG analysis report"}) as pdf:
        # --- page 1: summary --------------------------------------------------
        fig = plt.figure(figsize=PAGE)
        fig.text(0.05, 0.94, "Trial #%d — Recognition Potential Analysis" % s["pk"], fontsize=20,
                 weight="bold", va="top")
        ok = s["status"] == "ok"
        fig.text(0.95, 0.94, s["status"].upper(), fontsize=11, weight="bold", va="top", ha="right",
                 color="white", bbox=dict(boxstyle="round,pad=0.4", ec="none",
                                          fc="#2e7d32" if ok else "#d68910"
                                          if s["status"].startswith("weak") else "#c0392b"))
        rt = ("median RT %.0f ms" % s["median_rt_ms"]) if s["median_rt_ms"] else "no RT"
        y = 0.875
        meta = [
            ("Subject", "%s   •   recorded %s   •   dataset %s" % (s["subject"], s["timestamp"], s["dataset"])),
            ("Stimuli", "%d images: %d clear, %d scrambled   •   %d keypresses (%d matched to clear "
                        "images, %s)" % (s["n_images"], s["n_clear"], s["n_scrambled"],
                                         s["n_presses"], s["n_clear_with_press"], rt)),
            ("EDF file", s["edf"]),
            ("Alignment", s["alignment_detail"]),
            ("Electrodes", "noise after filtering — " + ", ".join(
                "%s %.0f µV%s" % (c, v, " ✓" if c in s["used_channels"] else "")
                for c, v in s["electrode_noise_uv"].items())
                + "   (✓ = used, threshold %g µV)" % CONNECTED_MAX_UV),
            ("Processing", "%g Hz notch, %g–%g Hz bandpass, epochs %d to %d ms, baseline %d to %d ms, "
                           "reject > %g µV peak-to-peak" % (LINE_FREQ, BANDPASS[0], BANDPASS[1],
                                                           EPOCH_MS[0], EPOCH_MS[1], BASELINE_MS[0],
                                                           BASELINE_MS[1], REJECT_UV)),
        ]
        for label, text in meta:
            fig.text(0.05, y, label, fontsize=9.5, weight="bold", va="top", color="#444")
            y = _wrap(fig, 0.16, y, text, 125, 0.026, fontsize=9.5) - 0.008
        for w in s["warnings"]:
            y -= 0.01
            fig.text(0.05, y, "⚠", fontsize=11, va="top", color="#e67e22")
            y = _wrap(fig, 0.075, y, w, 140, 0.026, fontsize=9.5, color="#a04000") - 0.005
        y -= 0.03
        if s["stats"]:
            fig.text(0.05, y, "Clear − scrambled, %d–%d ms RP window" % RP_WINDOW_MS,
                     fontsize=13, weight="bold", va="top")
            y -= 0.045
            rows = [[c, "%d / %d" % (v["n_clear"], v["n_scr"]), "%+.2f µV" % v["mean"],
                     "%.3f" % v["p"], "%+.1f µV @ %d ms" % (v["peak_uv"], v["peak_ms"])]
                    for c, v in s["stats"].items()]
            h = 0.045 * (len(rows) + 1)
            ax = fig.add_axes([0.05, y - h, 0.9, h])
            ax.axis("off")
            tb = ax.table(cellText=rows, colLabels=["Channel", "Epochs (clear / scrambled)",
                                                    "Mean difference",
                                                    "p (permutation, %d)" % N_PERMUTATIONS,
                                                    "Largest difference %d–%d ms" % PEAK_SEARCH_MS],
                          loc="upper center", cellLoc="center", bbox=[0, 0, 1, 1],
                          colWidths=[0.12, 0.26, 0.19, 0.22, 0.21])
            tb.auto_set_font_size(False)
            tb.set_fontsize(10)
            for (r, c), cell in tb.get_celld().items():
                cell.set_edgecolor("#ddd")
                if r == 0:
                    cell.set_facecolor("#f0f0f0")
                    cell.set_text_props(weight="bold")
                elif float(rows[r - 1][3]) < 0.05:
                    cell.set_text_props(weight="bold")
            y -= h + 0.03
            fig.text(0.05, y, "Bold = p < 0.05. Not corrected for testing several channels.",
                     fontsize=8.5, color="#666", va="top")
        fig.text(0.05, 0.03, footer, fontsize=8, color="#888")
        pdf.savefig(fig)
        plt.close(fig)

        # --- one page per plot -------------------------------------------------
        for name, _field, title, note in IMAGES:
            img = plt.imread(io.BytesIO(hires[name]), format="png")
            fig = plt.figure(figsize=PAGE)
            fig.text(0.05, 0.95, title, fontsize=15, weight="bold", va="top")
            y = _wrap(fig, 0.05, 0.905, note, 150, 0.026, fontsize=9.5, color="#555")
            top = y - 0.01
            ax = fig.add_axes([0.03, 0.06, 0.94, top - 0.06])
            ax.imshow(img, interpolation="antialiased")
            ax.axis("off")
            fig.text(0.05, 0.03, footer, fontsize=8, color="#888")
            pdf.savefig(fig)
            plt.close(fig)

        # --- log -----------------------------------------------------------------
        fig = plt.figure(figsize=PAGE)
        fig.text(0.05, 0.95, "Analysis log", fontsize=15, weight="bold", va="top")
        _wrap(fig, 0.05, 0.9, log_text, 140, 0.022, fontsize=8, family="monospace")
        fig.text(0.05, 0.03, footer, fontsize=8, color="#888")
        pdf.savefig(fig)
        plt.close(fig)
    return buf.getvalue()
