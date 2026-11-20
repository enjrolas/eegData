"""
Trial tags: characteristics found while analysing a trial ("no data file",
"incomplete calibration burst", ...).

Automatic tags are worked out from the trial record and the analysis summary
(Trial.analysisData), so they can be refreshed at any time without re-running
the analysis.  Refreshing only adds/removes *automatic* tags; tags someone
attached by hand in the admin are never touched.

No heavy imports here (used by the admin and views too).
"""
import json

from Analysis.summary import load_summary

# slug -> (name, category, description).  Order = display order.
AUTO_TAGS = {
    # data
    "no-data-file": ("No data file", "data",
                     "No EDF file has been uploaded for this trial."),
    "not-analysed": ("Not analysed", "data",
                     "An EDF file is attached but the analysis has not run yet."),
    "analysis-failed": ("Analysis failed", "data",
                        "The analysis stopped with an error (e.g. unreadable EDF) — see the log."),
    "legacy-analysis": ("Legacy analysis only", "data",
                        "Analysed by the old command; no summary to check against."),
    # sync
    "no-sync": ("No sync", "sync",
                "None of the sync inputs recorded the screen changing; no usable data."),
    "no-calibration-burst": ("No calibration burst", "sync",
                             "The calibration burst was not found; alignment relies on the end "
                             "of the image train or the image flips alone."),
    "calibration-incomplete": ("Incomplete calibration pulses", "sync",
                               "Fewer calibration flips were found than the trial settings call "
                               "for (2 x calibrationFlashes).  Checked from v1.2 only."),
    "calibration-excess": ("Extra calibration pulses", "sync",
                           "More calibration flips were found than the trial settings call for."),
    "anchors-disagree": ("Burst and end anchors disagree", "sync",
                         "The calibration burst and the end of the image train disagree by more "
                         "than 100 ms about where the images are."),
    "unanchored-alignment": ("Unanchored alignment", "sync",
                             "Neither the calibration burst nor the end of the image train could "
                             "anchor the alignment; it rests on the image flips alone."),
    "partial-image-match": ("Images partly matched", "sync",
                            "Fewer than 90% of the images were matched to a flip on the sync "
                            "trace."),
    "image-pulse-mismatch": ("Image count doesn't match sync pulses", "sync",
                             "The number of sync pulses counted while images were shown differs "
                             "from the number of images analysed."),
    # trial
    "image-count-mismatch": ("Image count doesn't match expected", "trial",
                             "The number of images logged differs from totalImages (other than "
                             "the N+1 page bug)."),
    "no-keypresses": ("No keypresses", "trial", "No keypresses or touches were recorded."),
    # result
    "significant-rp": ("Significant RP", "result",
                       "Best channel shows a clear-vs-scrambled difference with p < 0.05 in the "
                       "RP window."),
}

# electrodes: tagged when connected (pass the noise check) — the EDFs always
# carry the full 10-20 montage, so presence alone says nothing
ELECTRODES = ["O1", "O2", "Oz", "T5", "T6", "P3", "P4", "Pz", "Cz"]
for _c in ELECTRODES:
    AUTO_TAGS["channel-" + _c.lower()] = (_c, "channel",
                                          "%s connected: passes the noise check (filtered signal "
                                          "std below 50 µV) and is used in the analysis." % _c)

ANCHOR_DISAGREE_MS = 100
MIN_IMAGE_MATCH = 0.9


def logged_images(trial):
    try:
        return sum(1 for k in json.loads(trial.trialData)["data"] if str(k).isdigit())
    except (ValueError, KeyError, TypeError):
        return None


def page_version(trial):
    """The version the jigsaw page wrote into trialData itself (v1.2 onwards).
    Pre-1.2 pages wrote none; the model field was filled in afterwards."""
    try:
        d = json.loads(trial.trialData)
        v = d.get("data", d).get("javascriptVersion")
        return tuple(int(x) for x in str(v).split(".")[:2]) if v else None
    except (ValueError, TypeError, AttributeError):
        return None


def expected_flips(trial):
    try:
        return int(round(2 * float(trial.calibrationFlashes)))
    except (TypeError, ValueError):
        return None


def compute_tags(trial, summary=None):
    """Automatic tag slugs for this trial."""
    tags = []
    n_logged = logged_images(trial)
    n_total = trial.totalImages
    if n_logged is not None and n_total:
        # n_total + 1 is the page's N+1 bug (every trial; that image isn't counted)
        if n_logged not in (n_total, n_total + 1):
            tags.append("image-count-mismatch")
    try:
        presses = json.loads(trial.recognitionPresses) if trial.recognitionPresses else []
    except ValueError:
        presses = []
    if not [p for p in presses if p is not None]:
        tags.append("no-keypresses")

    if not trial.edfFile:
        return ["no-data-file"] + tags
    if trial.analysisError:
        return ["analysis-failed"] + tags
    if not trial.analysisStarted:
        return ["not-analysed"] + tags
    s = summary if summary is not None else load_summary(trial)
    if not s:
        return ["legacy-analysis"] + tags

    if not s.get("alignment_ok"):
        return ["no-sync"] + tags

    cal = s.get("calibration")
    if not s.get("calibration_anchor") or not cal:
        tags.append("no-calibration-burst")
    else:
        # Pre-1.2: the server saved calibrationFlashes into a misspelt attribute
        # (trial.calibratedFlashes) until Jan 2026, so the model holds the default
        # 5 or a hand-entered value, not what the page did.  Don't compare.
        v = page_version(trial)
        want = expected_flips(trial) if v and v >= (1, 2) else None
        got = cal.get("n_flips")
        if want and got is not None:
            if got < want:
                tags.append("calibration-incomplete")
            elif got > want:
                tags.append("calibration-excess")
    if not s.get("calibration_anchor") and not s.get("end_anchor"):
        tags.append("unanchored-alignment")
    d = s.get("end_vs_burst_ms")
    if d is not None and abs(d) > ANCHOR_DISAGREE_MS:
        tags.append("anchors-disagree")
    m = s.get("alignment_match")
    if m is not None and m < MIN_IMAGE_MATCH:
        tags.append("partial-image-match")
    pulses, n_img = s.get("image_sync_pulses"), s.get("n_images")
    if pulses is not None and n_img is not None and pulses != n_img:
        tags.append("image-pulse-mismatch")
    if s.get("best_p") is not None and s["best_p"] < 0.05:
        tags.append("significant-rp")
    tags += ["channel-" + c.lower() for c in ELECTRODES if c in (s.get("used_channels") or [])]
    return tags


def ensure_tags():
    """Create any missing automatic Tag rows (never overwrites edits made in the admin)."""
    from Trial.models import Tag
    have = set(Tag.objects.filter(slug__in=AUTO_TAGS).values_list("slug", flat=True))
    for order, (slug, (name, cat, desc)) in enumerate(AUTO_TAGS.items()):
        if slug not in have:
            Tag.objects.create(slug=slug, name=name, category=cat, description=desc,
                               automatic=True, order=order)


def apply_tags(trial, summary=None):
    """Refresh the automatic tags on one trial; manual tags are left alone.
    Returns the list of automatic slugs now attached."""
    from Trial.models import Tag
    ensure_tags()
    want = set(compute_tags(trial, summary))
    current = {t.slug: t for t in trial.tags.filter(automatic=True)}
    remove = [t for slug, t in current.items() if slug not in want]
    add = Tag.objects.filter(slug__in=want - set(current))
    if remove:
        trial.tags.remove(*remove)
    if add:
        trial.tags.add(*add)
    return sorted(want, key=list(AUTO_TAGS).index)
