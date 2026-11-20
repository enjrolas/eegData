"""Plot titles and notes shared by the analysis, the PDF report and the admin.

Kept free of numpy/matplotlib imports so the web process can use it cheaply.
"""

# Recognition potential (RP; Rudell 1991): a deflection ~200-300 ms after an
# image becomes recognisable, largest over occipital / posterior-temporal sites.
# Its sign depends on the reference, so tests and peaks are polarity-agnostic.
RP_WINDOW_MS = (200, 300)     # mean-amplitude window for the clear - scrambled test
PEAK_SEARCH_MS = (150, 350)   # where the largest |clear - scrambled| is reported
WINDOW_TEXT = "%d–%d ms" % RP_WINDOW_MS

# primary public domain (PDF footer)
SITE_DOMAIN = "eegdata.alexhornstein.com"

# (image name, Trial field, title, explanatory note) in report order.
# The four original fields keep the meaning they were named for.
IMAGES = [
    ("keypress", "keypressLockedAverage", "Keypress-locked average",
     "Averaged around the keypress instead of the image: checks whether activity tied to the "
     "button press, rather than recognition, drives the clear-vs-scrambled difference."),
    ("sync", "syncProof", "Proof of sync",
     "The sync sensor from before the calibration burst until three image pulses have come "
     "through (each calibration flip numbered), and the last images, showing which screen flip "
     "is which image. Dashed line: JS time zero."),
    ("comparison", "clearVsScrambled", "Clear vs scrambled, every electrode",
     "The recognition potential (RP) appears about 200–300 ms after a recognisable (clear) image "
     "as a deflection that is absent for scrambled images; its sign depends on the reference. "
     "Clear (red), scrambled (blue), difference (black), ±2 SE bands; clean electrodes share one "
     "scale, noisy ones are greyed and drawn on their own scale."),
    ("difference", "differenceWave", "Difference wave (clear − scrambled)",
     "Images arrive every ~330 ms, so each epoch also contains responses to neighbouring "
     "images. Subtracting the scrambled average removes that shared part and isolates the RP."),
    ("clear", "periStimulusAverage", "Peri-stimulus average (clear images)",
     "The original design: O1, O2, T5 and T6, 60 Hz notch only, ±500 ms around each clear image."),
    ("scrambled", "periNonStimulusAverage", "Peri-non-stimulus average (scrambled images)",
     "The original design: O1, O2, T5 and T6, 60 Hz notch only, ±500 ms around each scrambled image."),
    ("clear_pressed", "filteredPeriStimulusAverage",
     "Filtered peri-stimulus average (around key presses after clear images)",
     "The original design: ±500 ms around each key press made within 1000 ms of a clear image."),
    ("prompt", "filteredPeriPromptStimulusAverage",
     "Filtered peri-prompt-stimulus average (key press, back to image presentation)",
     "Clear images followed by a key press 200–1000 ms later, locked to the key press and reaching "
     "back to the image presentation (ticks: each image onset; dashed: the median)."),
    ("alignment", "alignmentCheck", "Alignment check and signal overview",
     "Each image onset is timed at its sync flip. Bottom: the first 15 s of the filtered signal "
     "with image onsets and keypresses."),
]
