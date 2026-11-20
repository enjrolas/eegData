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
KEYPRESS_SHOW = ["O1", "O2", "T5", "T6"]   # always drawn on the keypress-locked average
RP_LATENCY_MIN_EPOCHS = 10
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

from Analysis.labels import IMAGES, RP_WINDOW_MS, PEAK_SEARCH_MS, SITE_DOMAIN  # noqa: E402

# image name -> Trial model field
FIELDS = {name: field for name, field, _title, _note in IMAGES}

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
    # calibration burst: half-period from the trial (the model default of 60 ms
    # predates the setting being recorded — the page's default is 1000 ms), and
    # from v1.3 the JS time of every calibration flip relative to trial start
    period = t.calibrationPeriod if t.calibrationPeriod and t.calibrationPeriod >= 200 else 1000
    cal_js = None
    if t.calibrationTimestamps and t.trialStartMillis:
        try:
            ms = [float(v) for v in t.calibrationTimestamps.split(",")[1::2]]
            cal_js = np.array(ms) - float(t.trialStartMillis)
        except ValueError:
            cal_js = None
    trial.update({
        "pk": t.pk,
        "edf": t.edfFile.path,
        "subject": str(t.subject),
        "timestamp": t.trialTimestamp,
        "cal_half_period_ms": period / 2.0,
        "cal_js": cal_js,
        "delay_ms": float(t.delayBetweenImages or 300),
        "cal_flips_model": int(round(2 * t.calibrationFlashes)) if t.calibrationFlashes else None,
        "total_images": int(t.totalImages) if t.totalImages else None,
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


CAL_TOL = 0.15          # calibration flip intervals must be within ±15% of the half-period
CAL_MIN_INTERVALS = 5   # at least this many evenly spaced flips in a row
# An anchor is checked against the first/last 10 images: each needs a flip within this
# distance.  It only has to tell the right image from a neighbour (~316 ms away), and
# some sessions' JS log runs 40-80 ms behind the screen, so allow under half an interval.
CAL_FIRST_IMAGE_TOL = 120.0
FRAME_MS = 1000 / 60    # one display frame


def all_transitions(trace, fs, start_ms=None, end_ms=None):
    """
    Every state change on a sync trace (both directions), timed at the
    transition.  With start/end, spikes are thresholded on that stretch alone
    (the calibration flips can be smaller than the image flips).  A biphasic
    flip is detected on both polarities a few ms apart; those are merged.
    """
    a = 0 if start_ms is None else max(int(start_ms * fs / 1000), 0)
    b = len(trace) if end_ms is None else min(int(end_ms * fs / 1000), len(trace))
    seg = trace[a:b]
    if len(seg) < fs:
        return np.array([])
    pos, neg = detect_spikes(seg, fs), detect_spikes(-seg, fs)
    peaks = np.sort(np.concatenate([pos, neg])) + a / fs * 1000.0
    tt, _ = transition_times(trace, fs, peaks)
    tt = np.sort(tt)
    if len(tt) > 1:                               # merge double detections of one flip
        keep = np.concatenate([[True], np.diff(tt) > 60.0])
        tt = tt[keep]
    return tt


def _anchor_score(tt, t0, first_images_js):
    """How well an anchor predicts the first images: (fraction matched, median error ms)."""
    pred = t0 + np.asarray(first_images_js, dtype=float)
    k = np.clip(np.searchsorted(tt, pred), 1, len(tt) - 1)
    err = np.where(np.abs(tt[k - 1] - pred) < np.abs(tt[k] - pred), tt[k - 1] - pred, tt[k] - pred)
    hit = np.abs(err) < CAL_FIRST_IMAGE_TOL
    return float(hit.mean()), (float(np.median(err[hit])) if hit.any() else None)


def find_calibration(tt, first_images_js, approx_first_image_edf, half_period_ms=500.0,
                     cal_js=None, delay_ms=300.0):
    """
    Burst anchor — the sync gold standard.  Finds the calibration burst before
    the first image and returns the EDF time of JS time zero, or None.

    Every page version flips the sync patch every calibrationPeriod/2 for
    calibrationFlashes cycles, then shows the first image once `delay` has
    passed since the last calibration flip (v1.2+ adds a short trial-start
    blip in between).  So the first flip after the burst is image 1 — which
    pins down which flip is which.  (Before v1.2 JS time zero was the start of
    the burst, from v1.2 its end; pinning image 1 works for both.)  From v1.3
    the JS times of the calibration flips are recorded and fitted directly.

    Bursts faster than ~150 ms per flip (early 60 ms-period sessions) cannot be
    resolved at 250 Hz; those trials rely on the end anchor.
    """
    if half_period_ms < 150:
        return None
    first_images_js = np.asarray(first_images_js, dtype=float)[:10]
    iv = np.diff(tt)
    good = np.abs(iv - half_period_ms) < CAL_TOL * half_period_ms
    best = None
    for e in range(2, len(tt) - 1):              # e = index of last calibration flip
        if good[e]:
            continue                              # the run continues past e
        r = 0
        while e - 1 - r >= 0 and good[e - 1 - r]:
            r += 1
        if r < 2:                                 # at least 3 evenly spaced flips
            continue
        b = e - r                                 # first flip of the run
        if r < CAL_MIN_INTERVALS and b > 0 and tt[b] - tt[b - 1] < 1.5 * half_period_ms:
            continue                              # a short run must stand alone
        n = e + 1
        blip = tt[n] - tt[e] < 120
        if blip:
            n += 1                                # v1.2+ trial-start blip
            if n >= len(tt):
                continue
        gap = tt[n] - tt[e]
        if delay_ms - 20 <= gap <= delay_ms + 100:
            # image 1 is the flip that follows the burst
            t0_pin = float(tt[n] - first_images_js[0])
            how = "first image pinned to the flip after the burst"
        else:
            # image 1's flip not detected: the page shows it one `delay` (plus up
            # to a frame) after the last calibration flip
            t0_pin = float(tt[e] + delay_ms + FRAME_MS / 2 - first_images_js[0])
            how = "first image timed one delay after the last calibration flip"
        cand = {"t0_ms": t0_pin, "how": how, "fit_spread_ms": None}
        if cal_js is not None and len(cal_js) >= 2:
            k = min(len(cal_js), r + 1)
            fit = tt[e - k + 1:e + 1] - np.asarray(cal_js[-k:], dtype=float)
            if abs(np.median(fit) - t0_pin) < CAL_FIRST_IMAGE_TOL:
                cand = {"t0_ms": float(np.median(fit)), "how": "fitted on %d calibration flips "
                        "with recorded JS times" % k, "fit_spread_ms": float(np.std(fit))}
        frac, err = _anchor_score(tt, cand["t0_ms"], first_images_js)
        dist = abs(approx_first_image_edf - (cand["t0_ms"] + first_images_js[0]))
        if dist > 2500 or frac < 0.6:
            continue
        cand.update({"kind": "burst", "n_flips": r + 1, "half_period_ms": float(np.median(iv[b:e])),
                     "start_ms": float(tt[b]), "end_ms": float(tt[e]), "start_blip_seen": bool(blip),
                     "first_images_matched": frac, "first_image_err_ms": err, "_dist": dist})
        if best is None or dist < best["_dist"]:
            best = cand
    if best:
        best.pop("_dist")
    return best


def find_end_anchor(tt, stim_ms, approx_t0, slope, record_end_ms, delay_ms=300.0):
    """
    End anchor: count back from the end of the image train.  When the trial
    completes the page stops drawing the patch; if it was white, that adds one
    more flip ~200 ms after the last image — sooner than any image interval —
    which is dropped.  The remaining last flip is the last logged image.  Only
    valid if the recording carries on for a second afterwards with no flips.
    """
    if len(tt) < 3:
        return None
    pred_last = approx_t0 + stim_ms[-1] * (1 + slope)
    near = np.where(np.abs(tt - pred_last) < 2500)[0]
    if not len(near):
        return None
    k = near.max()
    if k < len(tt) - 1 or record_end_ms - tt[k] < 1000:
        return None                               # flips continue, or recording stops too soon
    removal = tt[k] - tt[k - 1] < delay_ms - 60
    if removal:
        k -= 1                                    # patch-removal flip after the last image
    t0 = float(tt[k] - stim_ms[-1] * (1 + slope))
    last_js = stim_ms[-10:]
    frac, err = _anchor_score(tt, t0 + last_js[0] * slope, last_js)
    if frac < 0.6:
        return None
    return {"kind": "end", "t0_ms": t0, "last_flip_ms": float(tt[k]),
            "how": "last flip of the image train = last image%s" % (
                " (patch-removal flip after it dropped)" if removal else ""),
            "removal_flip": bool(removal), "last_images_matched": frac, "last_image_err_ms": err}


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
    def match(t0, slope=0.0):
        """Pair each image-era flip with its nearest logged image for this clock."""
        s = t0 + stim_ms * (1.0 + slope)
        j = np.clip(np.searchsorted(s, spikes), 1, len(s) - 1)
        j = np.where(np.abs(spikes - s[j - 1]) <= np.abs(spikes - s[j]), j - 1, j)
        resid = spikes - s[j]
        return j, resid, (np.abs(resid) < 2 * PHOTODIODE_MATCH_MS) & mask[j]

    # image-only alignment: best offset from the flips, plus linear drift.
    # Image flips are nearly periodic, so this can lock on one image early or
    # late — the anchors below settle which flip is which.
    j, resid, ok = match(pd["offset"])
    a1, a0 = np.polyfit(stim_ms[j[ok]], resid[ok], 1)
    slope_img = float(a1)
    t0_img = float(pd["offset"] + a0)                               # EDF time of JS time 0

    # anchors: calibration burst (gold standard), else the end of the image train
    approx_first = t0_img + stim_ms[0]
    tt_start = all_transitions(trace, fs, approx_first - 20000, approx_first + 3000)
    cal = find_calibration(tt_start, stim_ms[:10], approx_first,
                           trial.get("cal_half_period_ms", 500.0), trial.get("cal_js"),
                           trial.get("delay_ms", 300.0))
    approx_last = t0_img + stim_ms[-1] * (1 + slope_img)
    tt_end = all_transitions(trace, fs, approx_last - 15000, approx_last + 5000)
    end = find_end_anchor(tt_end, stim_ms, t0_img, slope_img, len(trace) / fs * 1000.0,
                          trial.get("delay_ms", 300.0))
    anchored = cal is not None
    if anchored:
        # gold standard: JS time zero fixed by the burst; fit only the drift
        t0 = cal["t0_ms"]
        j, resid, ok = match(t0, slope_img)
        x, r = stim_ms[j[ok]], (spikes[ok] - (t0 + stim_ms[j[ok]]))
        slope = float((x * r).sum() / (x * x).sum()) if ok.any() else slope_img
        clock_map = lambda js: t0 + js * (1.0 + slope)
    elif end is not None:
        # which flip is which from the end of the train; then fit offset and drift
        j, resid, ok = match(end["t0_ms"], slope_img)
        a1, a0 = np.polyfit(stim_ms[j[ok]], spikes[ok] - stim_ms[j[ok]], 1)
        t0, slope = float(a0), float(a1)
        clock_map = lambda js: t0 + js * (1.0 + slope)
    else:
        t0, slope = t0_img, slope_img
        clock_map = lambda js: t0 + js * (1.0 + slope)
    drift_ppm = slope * 1e6
    ref = cal if cal is not None else end
    match_frac = float(len(np.unique(j[ok])) / max(mask.sum(), 1))

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
        "offset_ms": float(clock_map(0.0)), "drift_ppm": drift_ppm,
        "match": match_frac, "typical": pd["typical"],
        "calibration": cal, "end_anchor": end, "anchored": anchored,
        "crosscheck_ms": float(t0_img - ref["t0_ms"]) if ref else None,
        "end_vs_burst_ms": float(end["t0_ms"] - cal["t0_ms"]) if (cal and end) else None,
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


def extract_epochs(x, fs, times_ms, window_ms=EPOCH_MS, baseline_ms=BASELINE_MS, reject_uv=None):
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
        if np.ptp(seg) > (REJECT_UV if reject_uv is None else reject_uv):
            rejected += 1
            continue
        kept.append(seg)
    return np.array(kept).reshape(-1, len(tax)), tax, rejected


def rp_latency(Ec, Es, tax, sign, n_boot=2000, seed=0):
    """
    Image-to-RP latency, measured on the average: the peak (of the RP's
    polarity) of the clear − scrambled difference wave within the peak-search
    window.  It must be a real local peak, not the window edge.  Error bars:
    the clear and scrambled images are resampled n_boot times and the peak
    re-measured; the result is the median of those latencies with their 95%
    interval.  Returns None if the average has no peak inside the window or
    fewer than half the resamples do.

    (Single-image peak picking was tried first and is not usable: on single
    epochs the largest bump is mostly noise, so the median lands near the
    middle of the window whatever the real latency is.)
    """
    srch = (tax >= PEAK_SEARCH_MS[0]) & (tax <= PEAK_SEARCH_MS[1])
    ts = tax[srch]
    C, S = Ec[:, srch], Es[:, srch]

    def peak(d):
        k = int(np.argmax(sign * d))
        return float(ts[k]) if 0 < k < len(ts) - 1 else np.nan

    observed = peak(C.mean(0) - S.mean(0))
    if np.isnan(observed) or len(C) < RP_LATENCY_MIN_EPOCHS:
        return None
    rng = np.random.default_rng(seed)
    boots = np.array([peak(C[rng.integers(0, len(C), len(C))].mean(0)
                           - S[rng.integers(0, len(S), len(S))].mean(0)) for _ in range(n_boot)])
    ok = boots[~np.isnan(boots)]
    if len(ok) < n_boot / 2:
        return None
    lo, hi = np.percentile(ok, [2.5, 97.5])
    q1, q3 = np.percentile(ok, [25, 75])
    return {"peak_ms": observed, "boot_median_ms": float(np.median(ok)),
            "ci_low_ms": float(lo), "ci_high_ms": float(hi), "q1_ms": float(q1), "q3_ms": float(q3),
            "n": int(len(C)), "n_scrambled": int(len(S)), "boot_peak_frac": float(len(ok) / n_boot),
            "polarity": "positive" if sign > 0 else "negative", "method": "bootstrap of the average"}


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


def render(fig, hi_dpi=150):
    """(web PNG at 90 dpi, report PNG at hi_dpi); closes the figure.  Tall
    stacked figures use a lower hi_dpi to keep memory down."""
    hi = to_png(fig, dpi=hi_dpi, close=False)
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
EEG_ORDER = ["FP1", "FP2", "F7", "F3", "Fz", "F4", "F8", "T3", "C3", "Cz", "C4", "T4",
             "T5", "P3", "Pz", "P4", "T6", "O1", "Oz", "O2", "A1", "A2"]


def extra_image(trial):
    """The jigsaw page logs and shows one image more than requested (its end
    check runs after logging), drawn on the frame that ends the trial.  That
    last image N+1 is not counted anywhere in the analysis."""
    n = trial.get("total_images")
    return bool(n) and len(trial["stim_ms"]) == n + 1


def count_image_pulses(trace, fs, onsets):
    """Sync flips while images were shown: from image 1 to the last image (the
    calibration burst before and the patch-removal flip after are excluded)."""
    tt = all_transitions(trace, fs, onsets[0] - 1500, onsets[-1] + 1500)
    return int(((tt >= onsets[0] - 60) & (tt <= onsets[-1] + 60)).sum())


def sync_proof_figure(pk, trace, fs, al, onsets, trial):
    """
    Proof of sync: the sync trace across the calibration burst with every
    measured calibration flip numbered, the trial-start blip and the first
    images marked (v1.3: where the recorded JS times put each flip), and the
    end of the image train counted back from the last image.
    """
    c = clean_sync(trace, fs)
    t = np.arange(len(c)) / fs * 1000.0
    cal, end = al.get("calibration"), al.get("end_anchor")
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(18, 8.5), gridspec_kw={"height_ratios": [3, 2]})

    def panel(ax, w0, w1):
        m = (t >= w0) & (t <= w1)
        ax.plot(t[m] / 1000, c[m], color="0.35", lw=0.8)
        lo, hi = (c[m].min(), c[m].max()) if m.any() else (-1, 1)
        return lo, hi

    # --- start: from before the first sync activity until 3 image pulses are through
    pre = all_transitions(trace, fs, onsets[0] - 15000, onsets[0] + 200)
    pre = pre[pre < onsets[0] - 60]
    first_act = min([v for v in ([pre.min()] if len(pre) else []) +
                     ([cal["start_ms"]] if cal else [])] or [onsets[0] - 2000])
    w0, w1 = first_act - 1000, onsets[min(2, len(onsets) - 1)] + 250
    lo, hi = panel(a1, w0, w1)
    tt = all_transitions(trace, fs, w0 - 2000, w1 + 2000)
    tt = tt[(tt >= w0) & (tt <= w1)]
    if cal:
        flips = tt[(tt >= cal["start_ms"] - 5) & (tt <= cal["end_ms"] + 5)]
        for i, f in enumerate(flips, 1):
            a1.axvline(f / 1000, color="#2f5bea", lw=1)
            a1.annotate(str(i), (f / 1000, hi), xytext=(0, 4), textcoords="offset points",
                        ha="center", fontsize=10, fontweight="bold", color="#2f5bea")
            if i > 1:
                a1.annotate("%d ms" % round(f - flips[i - 2]), ((f + flips[i - 2]) / 2000, lo),
                            ha="center", va="bottom", fontsize=7, color="#2f5bea")
        t0 = cal["t0_ms"]
        a1.axvline(t0 / 1000, color="#c2185b", lw=1.2, ls="--")
        a1.annotate("JS t=0", (t0 / 1000, lo), xytext=(3, 12), textcoords="offset points",
                    fontsize=8, color="#c2185b")
        if trial.get("cal_js") is not None:
            for v in trial["cal_js"]:
                a1.plot([(t0 + v) / 1000], [hi * 0.82], marker="v", color="#c2185b", ms=6)
        if trial.get("cal_js") is not None:
            expect = "%d recorded" % len(trial["cal_js"])
        elif trial.get("cal_flips_model"):
            expect = "model says %d" % trial["cal_flips_model"]
        else:
            expect = "not recorded"
        status = ("%d calibration flips found (%s) at %.0f ms; %s"
                  % (len(flips), expect, cal["half_period_ms"], cal["how"]))
    else:
        status = "no calibration burst found before the first image"
    for i in range(min(3, len(onsets))):
        a1.axvline(onsets[i] / 1000, color="#c0392b", lw=1.6)
        a1.annotate("img %d" % (i + 1), (onsets[i] / 1000, hi), xytext=(0, 4),
                    textcoords="offset points", ha="center", fontsize=9, color="#c0392b")
    a1.set_title("Start of trial — %s" % status, fontsize=10, loc="left")
    a1.set_xlabel("s in EDF")

    # --- end: last images counted back
    n = len(onsets)
    w0, w1 = onsets[max(n - 5, 0)] - 400, onsets[-1] + 1500
    lo, hi = panel(a2, w0, w1)
    for i in range(max(n - 5, 0), n):
        a2.axvline(onsets[i] / 1000, color="#c0392b", lw=1.6)
        a2.annotate("img %d" % (i + 1), (onsets[i] / 1000, hi), xytext=(0, 4),
                    textcoords="offset points", ha="center", fontsize=9, color="#c0392b")
    last_shown = onsets[-1]
    if al.get("dropped_onset") is not None:
        last_shown = al["dropped_onset"]
        a2.axvline(last_shown / 1000, color="0.5", lw=1.4)
        a2.annotate("img %d (not counted)" % (n + 1), (last_shown / 1000, hi), xytext=(0, 4),
                    textcoords="offset points", ha="center", fontsize=9, color="0.4")
    if end and end.get("removal_flip"):
        tt = all_transitions(trace, fs, w0 - 2000, w1 + 2000)
        after = tt[(tt > last_shown + 60) & (tt <= w1)]
        if len(after):
            a2.axvline(after[0] / 1000, color="0.5", lw=1, ls=":")
            a2.annotate("patch removed", (after[0] / 1000, lo), xytext=(3, 6),
                        textcoords="offset points", fontsize=8, color="0.4")
    a2.set_title("End of trial — %s" % (end["how"] if end else "no clean end of the image train"),
                 fontsize=10, loc="left")
    a2.set_xlabel("s in EDF")
    fig.suptitle("Trial #%d — proof of sync on %s: %d image sync pulses counted for %d images; %s"
                 % (pk, al["channel"], al.get("image_sync_pulses", 0), n,
                    "anchored on the calibration burst" if cal else
                    "anchored on the end of the image train" if end else "NOT anchored"),
                 fontsize=11)
    fig.tight_layout()
    return render(fig)


LEGACY_CHANNELS = ["O1", "O2", "T5", "T6"]
LEGACY_OFFSET_MS = 500        # ± window of the original peri-stimulus averages
LEGACY_PROMPT_MS = 1000       # original "promptDelay"


def legacy_average(x, fs, times_ms, before_ms, after_ms):
    """The original periStimulusAverage(): plain mean of the notch-filtered
    signal from -before to +after ms around each event (no bandpass, no
    baseline, no rejection) — with the epoch indexing fixed (the original
    wrapped the pre-stimulus half around to the end of the array)."""
    n0, n1 = int(round(-before_ms * fs / 1000)), int(round(after_ms * fs / 1000))
    segs = [x[int(t * fs / 1000) + n0:int(t * fs / 1000) + n1] for t in times_ms
            if int(t * fs / 1000) + n0 >= 0 and int(t * fs / 1000) + n1 <= len(x)]
    tax = np.arange(n0, n1) * 1000.0 / fs
    return (np.mean(segs, axis=0) if segs else np.full(len(tax), np.nan)), tax, len(segs)


def legacy_figure(pk, signals, labels, fs, times_ms, before_ms, after_ms, title, line_label,
                  xlabel="ms before/after stimulus presentation", marks=(), rug=None):
    """Same layout as the original figures: one wide panel, O1/O2/T5/T6 overlaid."""
    nb, na = ss.iirnotch(LINE_FREQ, 30.0, fs)
    fig, ax = plt.subplots(figsize=(20, 3))
    n = 0
    for c in LEGACY_CHANNELS:
        if c not in labels:
            continue
        x = ss.filtfilt(nb, na, signals[labels.index(c)])
        avg, tax, n = legacy_average(x, fs, times_ms, before_ms, after_ms)
        ax.plot(tax, avg, label="%s %s" % (c, line_label))
    for xv, lab in marks:
        ax.axvline(xv, color="k", lw=0.8, ls="--")
        if lab:
            ax.annotate(lab, (xv, 1), xycoords=("data", "axes fraction"), xytext=(3, -12),
                        textcoords="offset points", fontsize=8)
    if rug is not None and len(rug):
        ax.plot(rug, np.full(len(rug), ax.get_ylim()[0]), "|", color="0.3", ms=8, alpha=0.6)
    ax.set_title(title)
    ax.set_xlabel(xlabel)
    ax.legend(loc="upper right")
    fig.tight_layout()
    return render(fig)


def electrode_rows(signals, labels, fs, exclude):
    """Every EEG electrode in the recording (sync inputs excluded), filtered once."""
    names = [c for c in EEG_ORDER if c in labels and c not in exclude]
    names += [c for c in labels if c not in names and c not in exclude and c not in SYNC_CHANNELS
              and not c.upper().startswith(("EKG", "CH", "EVENT", "STATUS"))]
    rows = []
    for c in names:
        x = filter_channel(signals[labels.index(c)], fs)
        noise = float(np.std(x))
        rows.append({"c": c, "x": x, "noise": noise, "clean": noise < CONNECTED_MAX_UV})
    return rows


def stacked_figure(pk, rows, fs, sets, title, window_ms=EPOCH_MS, baseline_ms=BASELINE_MS,
                   diff=False, stats=None, marks=(), rug=None, xlabel="ms after image onset",
                   shade_rp=True):
    """
    One row per electrode on a shared time axis.  `sets` is a list of
    (event times, colour, label); each is averaged ±2 SE.  diff=True adds the
    difference of the first two (black).  Clean electrodes share one amplitude
    scale; noisy ones are drawn on their own scale without artefact rejection
    and greyed.  `marks`: (x, label) vertical lines; `rug`: per-epoch x
    positions drawn as ticks (e.g. each epoch's image onset).
    """
    if not rows:
        return placeholder(pk, title, "No EEG electrodes in this recording")
    stats = stats or {}
    data = []
    for r in rows:
        means = []
        for times, col, lab in sets:
            E, tax, _ = extract_epochs(r["x"], fs, times, window_ms, baseline_ms,
                                       reject_uv=None if r["clean"] else np.inf)
            means.append((E, col, lab))
        data.append((r, means, tax))
    peaks = [np.abs(E.mean(0)).max() for r, means, tax in data if r["clean"]
             for E, col, lab in means if len(E) >= 5]
    shared = 1.15 * max(peaks) if peaks else None
    fig, axes = plt.subplots(len(data), 1, figsize=(16, 1.15 * len(data) + 1.8), sharex=True,
                             squeeze=False)
    for ax, (r, means, tax) in zip(axes[:, 0], data):
        for E, col, lab in means:
            if len(E):
                m, se = E.mean(0), E.std(0) / np.sqrt(len(E))
                ax.plot(tax, m, color=col, lw=1.2, label="%s (n=%d)" % (lab, len(E)))
                ax.fill_between(tax, m - 2 * se, m + 2 * se, color=col, alpha=0.15, lw=0)
        if diff and len(means) >= 2 and len(means[0][0]) and len(means[1][0]):
            ax.plot(tax, means[0][0].mean(0) - means[1][0].mean(0), color="k", lw=0.9,
                    label="difference")
        ax.axhline(0, color="k", lw=0.3)
        for x, lab in marks:
            ax.axvline(x, color="k", lw=0.8, ls="--" if lab else "-")
        if shade_rp:
            ax.axvspan(*RP_WINDOW_MS, color="gold", alpha=0.18)
        if rug is not None and len(rug):
            y0 = ax.get_ylim()[0] if not (r["clean"] and shared) else -shared
            ax.plot(rug, np.full(len(rug), y0), "|", color="#6a1b9a", ms=6, alpha=0.6)
        if r["clean"] and shared:
            ax.set_ylim(-shared, shared)
        ax.tick_params(labelsize=7)
        c = r["c"]
        if c in stats:
            tag = "%+.1f µV p=%.3f" % (stats[c]["mean"], stats[c]["p"])
        elif r["clean"]:
            tag = "clean %.0f µV" % r["noise"]
        else:
            tag = "noisy %.0f µV — own scale" % r["noise"]
        ax.set_ylabel("%s\n%s" % (c, tag), rotation=0, ha="right", va="center", fontsize=8,
                      color="k" if r["clean"] else "0.55",
                      fontweight="bold" if (c in stats and stats[c]["p"] < 0.05) else "normal")
        if not r["clean"]:
            ax.set_facecolor("#f4f4f4")
    axes[0, 0].legend(fontsize=7, loc="upper right", ncol=4)
    for x, lab in marks:
        if lab:
            axes[0, 0].annotate(lab, (x, 1), xycoords=("data", "axes fraction"), xytext=(3, 3),
                                textcoords="offset points", fontsize=8)
    axes[-1, 0].set_xlabel(xlabel)
    axes[-1, 0].set_xlim(*window_ms)
    fig.suptitle("Trial #%d — %s%s" % (pk, title, "; clean electrodes share one scale (±%.0f µV)"
                                          % shared if shared else ""), fontsize=10)
    fig.tight_layout(rect=(0.06, 0, 1, 0.97))
    return render(fig, hi_dpi=100)


def press_locked_peak(Ek, tk, expected_ms, polarity, half_width=100, n_boot=1000, seed=0):
    """
    Where the RP peaks in a keypress-locked average.  Reaction times vary, so
    the image-locked RP is smeared and lands at a slightly different place on
    each channel; there is no exact conversion from image time.  So it is
    measured in this average itself: the peak of the RP's polarity within
    ±half_width ms of where the image-locked latency puts it (median image time
    + latency), bootstrapped over the key presses.  None if no real peak.
    """
    sign = 1.0 if polarity == "positive" else -1.0
    m = (tk >= expected_ms - half_width) & (tk <= expected_ms + half_width)
    ts, X = tk[m], Ek[:, m]

    def peak(d):
        k = int(np.argmax(sign * d))
        return float(ts[k]) if 0 < k < len(ts) - 1 else np.nan

    observed = peak(X.mean(0))
    if np.isnan(observed):
        return None
    rng = np.random.default_rng(seed)
    boots = np.array([peak(X[rng.integers(0, len(X), len(X))].mean(0)) for _ in range(n_boot)])
    ok = boots[~np.isnan(boots)]
    if len(ok) < n_boot / 2:
        return None
    lo, hi = np.percentile(ok, [2.5, 97.5])
    return {"peak_ms": observed, "boot_median_ms": float(np.median(ok)),
            "ci_low_ms": float(lo), "ci_high_ms": float(hi), "n": int(len(X))}


def keypress_figure(pk, filtered, kp_chans, fs, press_times, rts, median_rt, stats, noise):
    """
    Keypress-locked averages, one panel per electrode, reaching back past the
    earliest possible image.  Marks the key press, the median image
    presentation, each image presentation, and — where measured — the RP:
    median image→RP latency placed after the median image presentation, with
    its 95% CI as error bars.
    """
    win = (-(MAX_REACTION_MS + 200), 400)          # before the earliest possible image
    bl = (win[0], -MAX_REACTION_MS)
    img_x = -median_rt
    fig, axes = plt.subplots(len(kp_chans), 1, figsize=(16, 1.0 + 2.1 * len(kp_chans)),
                             sharex=True, squeeze=False)
    axes = axes[:, 0]
    for ax, c in zip(axes, kp_chans):
        Ek, tk, _ = extract_epochs(filtered[c], fs, press_times, window_ms=win, baseline_ms=bl)
        note = ""
        if len(Ek) < 5:   # not connected: every epoch fails rejection — show it unrejected
            Ek, tk, _ = extract_epochs(filtered[c], fs, press_times, window_ms=win, baseline_ms=bl,
                                       reject_uv=np.inf)
            note = " — no epochs pass the %g µV rejection; average without rejection" % REJECT_UV
        connected = noise.get(c, np.inf) < CONNECTED_MAX_UV
        color = "#2F5BEA" if connected else "#8796A0"
        if len(Ek):
            ax.plot(tk, Ek.mean(0), lw=1.4, color=color)
        ax.axhline(0, color="k", lw=0.4)
        ax.axvline(0, color="k", lw=0.9)
        ax.axvline(img_x, color="#c0392b", lw=1.2, ls="--")
        ax.text(0.003, 0.95, "%s  n=%d  (noise %.0f µV%s)%s" % (
                    c, len(Ek), noise.get(c, float("nan")), "" if connected else ", not connected", note),
                transform=ax.transAxes, va="top", fontsize=9, weight="bold",
                color="#1D2A33" if connected else "#5C6B75")
        ax.set_ylabel("µV")
        L = stats.get(c, {}).get("latency")
        P = press_locked_peak(Ek, tk, img_x + L["peak_ms"], L["polarity"]) if L and len(Ek) >= 5 else None
        if P:
            stats[c]["latency"]["press_locked"] = P
            x = P["peak_ms"]
            lo, hi = ax.get_ylim()
            yy = hi - 0.12 * (hi - lo)
            ax.errorbar([x], [yy], xerr=[[max(0, x - P["ci_low_ms"])], [max(0, P["ci_high_ms"] - x)]],
                        fmt="v" if L["polarity"] == "negative" else "^", color="#C2185B", ms=8,
                        capsize=5, lw=1.6, zorder=5)
            ax.axvline(x, color="#C2185B", lw=0.8, ls=":")
            ann = ax.annotate("RP %.0f ms before the press (95%% CI %.0f–%.0f)\n%.0f ms after the "
                              "image (95%% CI %.0f–%.0f)" % (-x, -P["ci_high_ms"], -P["ci_low_ms"],
                                                            L["peak_ms"], L["ci_low_ms"], L["ci_high_ms"]),
                              (x, yy), xytext=(10, -4), textcoords="offset points", fontsize=8.5,
                              color="#C2185B", va="top")
            ann.set_in_layout(False)
        elif L:
            ax.text(0.997, 0.95, "RP %.0f ms after the image; no matching peak in this average"
                    % L["peak_ms"], transform=ax.transAxes, ha="right", va="top", fontsize=8.5,
                    color="#C2185B")
    ax = axes[0]
    ax.annotate("key press", (0, 1), xycoords=("data", "axes fraction"), xytext=(4, 4),
                textcoords="offset points", fontsize=9)
    ax.annotate("image presented (median, %.0f ms before)" % median_rt, (img_x, 1),
                xycoords=("data", "axes fraction"), xytext=(4, 4),
                textcoords="offset points", fontsize=9, color="#c0392b")
    ax = axes[-1]
    y0 = ax.get_ylim()[0]
    ax.plot(-np.asarray(rts), np.full(len(rts), y0), "|", color="#c0392b", ms=9, alpha=0.6)
    ax.set_xlim(*win)
    ax.set_xlabel("ms relative to key press   (red ticks: each image presentation; "
                  "pink: RP peak in this average, bars = 95% CI)")
    fig.suptitle("Trial #%d — keypress-locked average, back to image presentation (baseline before "
                 "the earliest image)" % pk, fontsize=12, x=0.01, ha="left")
    fig.tight_layout()
    return render(fig, hi_dpi=100 if len(kp_chans) > 4 else 150)


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
    images["sync"], hires["sync"] = images["alignment"], hires["alignment"]
    for name, _field, title, _note in IMAGES:
        if name not in images:
            images[name], hires[name] = placeholder(pk, title.lower(), reason)
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
        "n_images": int(len(trial["stim_ms"]) - extra_image(trial)),
        "n_clear": int(clear[:len(clear) - extra_image(trial)].sum()),
        "n_scrambled": int((~clear[:len(clear) - extra_image(trial)]).sum()), "n_presses": int(len(trial["presses_ms"])),
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
    log("  %.0f Hz, %.0f s; %d images logged%s; %d presses"
        % (fs, signals.shape[1] / fs, len(stim_ms),
           " (%d requested — the last is not counted)" % trial["total_images"]
           if extra_image(trial) else " (%d clear, %d scrambled)" % (clear.sum(), (~clear).sum()),
           len(trial["presses_ms"])))

    # --- Alignment ----------------------------------------------------------
    onsets, clock_map, al = align(trial, signals, labels, fs, header, photodiode)
    log("  alignment: %s" % al["method"])
    if not al["ok"]:
        return no_sync_result(trial, signals, labels, fs, al, log)
    presses = clock_map(trial["presses_ms"])
    dropped = None
    if extra_image(trial):
        dropped = float(onsets[-1])
        onsets, stim_ms, clear = onsets[:-1], stim_ms[:-1], clear[:-1]
        log("    image %d (one more than the %d requested) is not counted" % (len(stim_ms) + 1, len(stim_ms)))
    al["dropped_onset"] = dropped
    al["image_sync_pulses"] = count_image_pulses(al["trace"], fs, onsets)
    log("    image sync pulses counted: %d for %d images" % (al["image_sync_pulses"], len(stim_ms)))
    if al["ok"]:
        log("    offset %.0f ms (EDF header says %.0f); %.0f%% of images matched "
            "(typical %.0f%%); drift %.0f ppm; jitter %.1f ms"
            % (al["offset_ms"], al["header_offset_ms"], 100 * al["match"],
               100 * al["typical"], al["drift_ppm"], al["residuals_ms"].std()))
        if al.get("peak_lag_ms") is not None:
            log("    flips timed at the change of state (%.0f%% refined); spike peaks lag it by %.1f ms"
                % (100 * al["refined_frac"], al["peak_lag_ms"]))
        log("    " + anchor_text(al))

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
    rts, press_for_clear, clear_pressed = [], [], []
    for t in onsets[clear]:
        cand = presses[(presses > t + MIN_REACTION_MS) & (presses < t + MAX_REACTION_MS)]
        if len(cand):
            rts.append(cand[0] - t)
            press_for_clear.append(cand[0])
            clear_pressed.append(t)
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
    # one polarity for the trial: the best channel's RP-window difference
    if chans:
        best_c = min(chans, key=lambda c: stats[c]["p"])
        rp_sign = 1.0 if stats[best_c]["mean"] >= 0 else -1.0
        log("  RP polarity: %s (from %s)" % ("positive" if rp_sign > 0 else "negative", best_c))
    for c in chans:
        stats[c]["latency"] = rp_latency(E_clear[c], E_scr[c], tax, rp_sign)
        L = stats[c]["latency"]
        if L:
            log("  %s: image→RP latency %.0f ms (peak of the average; bootstrap 95%% CI %.0f–%.0f)"
                % (c, L["peak_ms"], L["ci_low_ms"], L["ci_high_ms"]))
        else:
            log("  %s: no RP peak inside %d–%d ms" % ((c,) + tuple(PEAK_SEARCH_MS)))

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

    # IMAGE: proof of sync  -> syncProof
    images["sync"], hires["sync"] = sync_proof_figure(pk, al["trace"], fs, al, onsets, trial)

    # stacked all-electrode figures (each electrode filtered once, shared)
    rows = electrode_rows(signals, labels, fs, al.get("members", ()))
    on_c, on_s = onsets[clear], onsets[~clear]
    images["comparison"], hires["comparison"] = stacked_figure(
        pk, rows, fs, [(on_c, COLORS["clear"], "clear"), (on_s, COLORS["scr"], "scrambled")],
        "clear vs scrambled, every electrode (RP window shaded); status: %s" % status,
        diff=True, stats=stats, marks=[(0, "")])
    # --- the original four figures (as designed in the legacy command) ------
    L = LEGACY_OFFSET_MS
    images["clear"], hires["clear"] = legacy_figure(
        pk, signals, labels, fs, on_c, L, L,
        "Trial #%d, peri-stimulus averages over time, averaged across %d stimuli and %d "
        "non-stimuli" % (pk, len(on_c), len(on_s)), "peri-stimulus average", marks=[(0, "")])
    images["scrambled"], hires["scrambled"] = legacy_figure(
        pk, signals, labels, fs, on_s, L, L,
        "Trial #%d, peri-non-stimulus average over time, averaged across %d non-stimuli"
        % (pk, len(on_s)), "peri-non-stimulus average", marks=[(0, "")])
    # keypresses that came within promptDelay of a clear image (the original filteredImages)
    prompt_presses = []
    for t in on_c:
        cand = presses[(presses > t) & (presses < t + LEGACY_PROMPT_MS)]
        if len(cand):
            prompt_presses.append(cand[0])
    # clear images with a keypress 200-1000 ms later (the original associatedKeypresses)
    assoc = [(t, p) for t, p in zip(clear_pressed, press_for_clear) if p - t < LEGACY_PROMPT_MS]
    if prompt_presses:
        images["clear_pressed"], hires["clear_pressed"] = legacy_figure(
            pk, signals, labels, fs, np.array(prompt_presses), L, L,
            "Trial #%d, filtered peri-stimulus averages over time, around %d key presses made "
            "within %d ms of a clear image" % (pk, len(prompt_presses), LEGACY_PROMPT_MS),
            "filtered peri-stimulus average", xlabel="ms before/after key press",
            marks=[(0, "key press")])
    else:
        images["clear_pressed"], hires["clear_pressed"] = placeholder(
            pk, "filtered peri-stimulus average", "No key press followed a clear image")
    if assoc:
        img_t = np.array([t for t, p in assoc]); prs = np.array([p for t, p in assoc])
        rt_a = prs - img_t
        images["prompt"], hires["prompt"] = legacy_figure(
            pk, signals, labels, fs, prs, LEGACY_PROMPT_MS + 100, 300,
            "Trial #%d, filtered peri-prompt-stimulus averages over time, averaged across %d "
            "prompt (within %d ms of presentation) presses — locked to the key press, back to "
            "image presentation" % (pk, len(prs), LEGACY_PROMPT_MS),
            "filtered peri-prompt-stimulus average", xlabel="ms before/after key press",
            marks=[(0, "key press"), (-float(np.median(rt_a)), "image presentation (median)")],
            rug=-rt_a)
    else:
        images["prompt"], hires["prompt"] = placeholder(
            pk, "filtered peri-prompt-stimulus average", "No prompt key press after a clear image")

    if chans:
        # IMAGE: difference waves  -> filteredPeriStimulusAverage
        fig, ax = plt.subplots(figsize=(16, 4.5))
        for c in chans:
            ax.plot(tax, stats[c]["diff"], lw=1.6, label="%s (p=%.3f)" % (c, stats[c]["p"]))
        decorate(ax)
        ax.set_title("Trial #%d — difference wave: clear − scrambled (RP window shaded)" % pk)
        fig.tight_layout()
        images["difference"], hires["difference"] = render(fig)
    else:
        images["difference"], hires["difference"] = placeholder(pk, "difference wave", no_erp)

    # IMAGE: keypress-locked, reaching back to image presentation  -> keypressLockedAverage
    kp_chans = [c for c in KEYPRESS_SHOW if c in filtered] + [c for c in chans if c not in KEYPRESS_SHOW]
    if press_for_clear and kp_chans:
        images["keypress"], hires["keypress"] = keypress_figure(
            pk, filtered, kp_chans, fs, press_for_clear, rts, median_rt, stats, noise)
    else:
        images["keypress"], hires["keypress"] = placeholder(
            pk, "keypress-locked average",
            "No keypresses followed a clear image" if kp_chans else no_erp)

    best = min(stats.items(), key=lambda kv: kv[1]["p"]) if stats else None
    summary = {
        "pk": pk, "subject": trial["subject"], "timestamp": trial["timestamp"],
        "dataset": trial["meta"].get("dataset", "?"),
        "alignment_ok": al["ok"], "alignment": al["method"],
        "alignment_match": float(al["match"]) if al["ok"] else None,
        "sync_channel": al.get("channel"), "sync_members": list(al.get("members", ())),
        "sync_peak_lag_ms": al.get("peak_lag_ms"),
        "calibration_anchor": bool(al.get("anchored")),
        "calibration": ({k: v for k, v in al["calibration"].items()} if al.get("calibration") else None),
        "calibration_crosscheck_ms": al.get("crosscheck_ms"),
        "image_sync_pulses": al.get("image_sync_pulses"),
        "end_anchor": al.get("end_anchor"),
        "end_vs_burst_ms": al.get("end_vs_burst_ms"),
        "electrode_noise_uv": noise,
        "channels": chans,
        "stats": {c: {k: v for k, v in s.items() if k != "diff"} for c, s in stats.items()},
        "n_clear_with_press": len(rts), "median_rt_ms": median_rt if len(rts) else None,
        "best_channel": best[0] if best else None,
        "best_p": best[1]["p"] if best else None,
        "best_mean_uv": best[1]["mean"] if best else None,
        "best_peak_ms": best[1]["peak_ms"] if best else None,
        # the key number: image->RP latency on the best channel
        "rp_latency": (dict(best[1]["latency"], channel=best[0])
                       if best and best[1].get("latency") else None),
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
def anchor_text(al):
    """One sentence on how image 1 was pinned down, and the cross-checks."""
    cal, end = al.get("calibration"), al.get("end_anchor")
    if not cal and not end:
        return ("No calibration burst or clean end of the image train — alignment from the image "
                "flips only, which can lock on one image early or late")
    x = al.get("crosscheck_ms")
    shift = round(x / 316.0) if x is not None else 0
    verdict = ("image flips alone would have been %+.0f ms off (%+d image%s) — corrected"
               % (x, shift, "" if abs(shift) == 1 else "s") if abs(x) > 150 else
               "image flips alone agree within %.0f ms" % abs(x))
    if cal:
        txt = ("Anchored on the calibration burst (%d flips at %.0f ms; %s%s; first 10 images within "
               "%.0f ms, median %+.0f ms)"
               % (cal["n_flips"], cal["half_period_ms"], cal["how"],
                  ", spread %.1f ms" % cal["fit_spread_ms"] if cal.get("fit_spread_ms") is not None else "",
                  CAL_FIRST_IMAGE_TOL, cal["first_image_err_ms"] or 0.0))
        if end:
            d = al["end_vs_burst_ms"]
            txt += ("; the end of the image train agrees (%+.0f ms)" % d if abs(d) < 150 else
                    "; the end of the image train DISAGREES by %+.0f ms" % d)
        else:
            txt += "; no clean end of the image train to cross-check"
    else:
        txt = ("No usable calibration burst; anchored on the end of the image train (last flip = "
               "last image; last 10 images matched %.0f%%)" % (100 * end["last_images_matched"]))
    return txt + "; " + verdict


def alignment_text(al, n_images):
    if not al["ok"]:
        return al["method"]
    lag = ("; peaks lag the change of state by %.1f ms" % al["peak_lag_ms"]
           if al.get("peak_lag_ms") is not None else "")
    return ("%s; %s; clock offset %.0f ms (EDF header would give %.0f ms); %d/%d images matched; "
            "drift %.0f ppm; jitter %.1f ms%s"
            % (al["method"], anchor_text(al), al["offset_ms"], al["header_offset_ms"],
               al["n_matched"], n_images, al["drift_ppm"], al["residuals_ms"].std(), lag))


def report_warnings(al, stats):
    warn = []
    if not al["ok"]:
        warn.append("No sync signal: none of the sync inputs recorded the screen changing, so the "
                    "image onsets can't be located in the EEG. This trial has no usable data.")
        return warn
    if al["ok"] and not (al.get("calibration") or al.get("end_anchor")):
        warn.append(anchor_text(al) + ". Without the calibration anchor a one-image shift "
                    "(~316 ms) cannot be ruled out; treat these averages with caution.")
    if al["ok"] and al.get("end_vs_burst_ms") is not None and abs(al["end_vs_burst_ms"]) > 150:
        warn.append("The calibration burst and the end of the image train disagree by %+.0f ms "
                    "about which flip is which; the burst was used. Check the alignment plot."
                    % al["end_vs_burst_ms"])
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
            ("RP latency", ("%.0f ms from image presentation to the RP on %s (95%% CI "
                            "%.0f–%.0f ms): peak of the clear − scrambled average within %d–%d ms, "
                            "bootstrapped over %d clear and %d scrambled images" % (
                                s["rp_latency"]["peak_ms"], s["rp_latency"]["channel"],
                                s["rp_latency"]["ci_low_ms"], s["rp_latency"]["ci_high_ms"],
                                PEAK_SEARCH_MS[0], PEAK_SEARCH_MS[1],
                                s["rp_latency"]["n"], s["rp_latency"]["n_scrambled"]))
                           if s.get("rp_latency") else "not measured"),
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
            if name not in hires:
                continue
            from PIL import Image
            img = np.asarray(Image.open(io.BytesIO(hires[name])).convert("RGB"))  # uint8: 1/4 of float
            tall = img.shape[0] > 0.9 * img.shape[1]
            fig = plt.figure(figsize=(PAGE[1], PAGE[0]) if tall else PAGE)
            fig.text(0.05, 0.95, title, fontsize=15, weight="bold", va="top")
            y = _wrap(fig, 0.05, 0.92 if tall else 0.905, note, 105 if tall else 150,
                      0.02 if tall else 0.026, fontsize=9.5, color="#555")
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
