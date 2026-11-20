# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Django 4.2 web application for collecting and analyzing EEG (electroencephalography) data. Subjects perform visual recognition tasks via a jigsaw puzzle interface (p5.js), while EEG signals are recorded. The analysis pipeline processes EDF files to compute peri-stimulus averaged neural responses.

## Common Commands

```bash
# Activate the project virtualenv (required before any manage.py command)
source .env/bin/activate

# Run development server
python manage.py runserver

# Database migrations
python manage.py migrate

# Run EEG analysis on all unanalyzed trials
python manage.py analysis

# Run analysis on a specific trial
python manage.py analysis --trialId <id>

# Install dependencies
pip install -r requirements.txt
```

`analyzeTrials.sh` is the cron-friendly wrapper that activates `.env/` and pipes output to `/home/japhy/logs/analyzeEEGMeasurements.log`, guarding against concurrent runs.

## Architecture

**Django apps:**

- **Trial** — Core app. Models: `Subject`, `Trial`. Handles the full trial lifecycle: setup form, jigsaw puzzle interface, data recording (EDF upload + JSON metadata), and analysis result viewing. The `Trial` model also stores generated analysis artifacts (peri-stimulus PNGs, raw `analysisData`, `analysisOutput` log) as fields — model state and analysis outputs are coupled.
- **Dataset** — Manages image datasets (original + resized) used as stimuli in the jigsaw puzzle.
- **Analysis** — Stores analysis configuration parameters (filter frequencies, prompt stimulus timing) linked to trials.
- **dataEndpoint** — Lightweight REST endpoint for browsing trial data.
- **EDFViewer** — Browser-based EDF signal viewer. Mounted at `/edfviewer/<trial_id>` with JSON APIs for channel listings and signal slices.

**Key URL routes** (Trial app, mounted at `/`):
- `/` — Setup form (subject/dataset selection, trial config)
- `/jigsaw` — Puzzle interface
- `/data` — POST endpoint for recording trial data
- `/analyses` — List all trials with analysis status
- `/analysis/<id>` — View analysis results
- `/edfviewer/<id>` — Visualize the raw EDF for a trial

**Frontend:** HTML templates with p5.js for interactive puzzle visualization, jQuery for AJAX. Templates are in `Trial/templates/Trial/`. Multiple `.html~` backup files coexist with the live ones — only the unsuffixed file is served.

**Data shape gotchas:**
- `Trial.trialData` and `Trial.recognitionPresses` are `TextField`s containing JSON — always `json.loads()` them before use. `trialData['data']` is a dict where digit-string keys map to `"<ms>,<imageName>,<isScrambled>"` CSV strings.
- `Trial.javascriptVersion` gates timing: v1.1 trials apply a `-300ms` offset in analysis. Newer trials use the recorded `trialStartMillis`/`calibrationTimestamps` for synchronization.

## EEG Analysis Pipeline

The main analysis command (`Trial/management/commands/analysis.py`) performs:
1. Reads EDF files via pyedflib (channels: O1, O2, T5, T6, differenced from CH21/Cz)
2. Applies 60 Hz notch filter and bandpass filtering (configurable via Analysis model)
3. Synchronizes trial event timestamps with EEG device timestamps
4. Computes 4 types of peri-stimulus averages (stimulus, filtered stimulus, filtered prompt-stimulus, non-stimulus control)
5. Generates matplotlib PNG plots saved to `media/analysis/`

**Time synchronization is the most fragile part of the pipeline.** Three clocks must be reconciled: the EDF file's `header["startdate"]` (EEG device clock), `Trial.trialTimestamp` (browser-side, parsed with two fallback formats — with and without sub-second precision), and `Trial.EDFStartTime` (manually entered, used as override when device/browser clocks diverge by more than 60 seconds). Calibration flashes recorded on `CH21 - Cz` provide a fallback alignment via rising-edge detection over a sliding peak-to-peak window.

## Configuration

- Database credentials: `eegdata/settings.ini` (read via python-decouple, MySQL backend)
- Media uploads: `media/trialData/` (EDF/LAY/STAT files), `media/analysis/` (generated plots)
- Logging: `/home/japhy/logs/eegdata-django-log`

## Key Dependencies

Scientific stack: pyedflib (EDF file I/O), scipy (signal processing), numpy, pandas, matplotlib. Django extensions: REST framework, django-filter, django-pandas, dj_notebook.
