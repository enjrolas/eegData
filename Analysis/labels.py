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

# (image name, Trial field, title, explanatory note) in report order
IMAGES = [
    ("erp", "periStimulusAverage", "Event-related potentials: clear vs scrambled",
     "The recognition potential (RP) appears about 200–300 ms after a recognisable (clear) image "
     "as a deflection that is absent for scrambled images; its sign depends on the reference. "
     "The RP window is shaded; bands are ±2 standard errors."),
    ("difference", "filteredPeriStimulusAverage", "Difference wave (clear − scrambled)",
     "Images arrive every ~330 ms, so each epoch also contains responses to neighbouring "
     "images. Subtracting the scrambled average removes that shared part and isolates the RP."),
    ("keypress", "filteredPeriPromptStimulusAverage", "Keypress-locked average",
     "Checks whether activity tied to the button press, rather than recognition, drives the "
     "difference between clear and scrambled images."),
    ("alignment", "periNonStimulusAverage", "Alignment check and signal overview",
     "Each image onset is snapped to its photodiode spike. Bottom: the first 15 s of the "
     "filtered signal with image onsets and keypresses."),
]
