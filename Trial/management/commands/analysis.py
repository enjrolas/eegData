"""
Recognition-potential (RP) ERP analysis of uploaded trials (pipeline in Analysis/erp.py).

    python manage.py analysis                         # every trial not yet analysed (cron)
    python manage.py analysis --trialId 144           # (re)analyse specific trial(s)
    python manage.py analysis --trialId 144 145 --dry-run --output-dir /some/dir
                                                      # test run: PNGs to a folder, DB untouched

Each analysed trial gets four PNGs saved into its existing image fields:
  periStimulusAverage                ERPs: clear vs scrambled per channel
  filteredPeriStimulusAverage        difference wave (clear - scrambled)
  filteredPeriPromptStimulusAverage  keypress-locked average
  periNonStimulusAverage             alignment check + signal overview
plus the JSON summary in analysisData and the text log in analysisOutput.

Nothing is deleted: Django's storage gives a new file a fresh suffixed name if
one already exists, so earlier analysis images stay on disk.

The previous implementation is kept as `manage.py analysis_legacy`.
"""
import html
import json
import os
import re
import traceback
from datetime import datetime

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand

from Analysis import erp
from Trial.models import Trial


class Command(BaseCommand):
    help = "Recognition-potential ERP analysis of uploaded trials (photodiode-aligned)"

    def add_arguments(self, parser):
        parser.add_argument("--trialId", type=int, nargs="+",
                            help="analyse these trial(s) even if already analysed")
        parser.add_argument("--dry-run", action="store_true",
                            help="do not write to the database; requires --output-dir")
        parser.add_argument("--output-dir",
                            help="also write PNGs, summary JSON and log here")
        parser.add_argument("--photodiode", help="force the photodiode channel (default: auto)")
        parser.add_argument("--channels", nargs="+", help="force EEG channels (default: auto)")
        parser.add_argument("--report-only", action="store_true",
                            help="only (re)generate the PDF report and JSON summary; leave the "
                                 "image links, log and status flags as they are")
        parser.add_argument("--missing-reports", action="store_true",
                            help="select every analysed trial that has no PDF report yet")

    def handle(self, *args, **opts):
        # This server is short of memory.  If it runs out mid-analysis, the
        # kernel should kill this process, not mysqld (which takes every site
        # down with it).  Raising our own OOM score needs no privileges.
        try:
            with open("/proc/self/oom_score_adj", "w") as f:
                f.write("1000")
        except OSError:
            pass

        if opts["dry_run"] and not opts["output_dir"]:
            self.stderr.write("--dry-run needs --output-dir (otherwise the results go nowhere)")
            return
        if opts["output_dir"]:
            os.makedirs(opts["output_dir"], exist_ok=True)

        if opts["trialId"]:
            trials = Trial.objects.filter(pk__in=opts["trialId"]).order_by("pk")
        elif opts["missing_reports"]:
            trials = (Trial.objects.exclude(edfFile="").filter(analysisError=False)
                      .exclude(periStimulusAverage="").filter(analysisReport="").order_by("pk"))
        else:
            # same selection as the legacy command
            trials = (Trial.objects.exclude(edfFile="").exclude(EDFStartTime=0)
                      .filter(analysisError=False, analysisStarted=False)
                      .order_by("timestamp"))
        if not trials.exists():
            print("no new trials")
            return

        failures = []
        for trial in trials:
            log = erp.Log()
            try:
                result = erp.analyze(erp.trial_from_model(trial), photodiode=opts["photodiode"],
                                     channels=opts["channels"], log=log)
            except Exception:
                log(traceback.format_exc())
                failures.append(trial.pk)
                result = None

            if opts["output_dir"]:
                self.write_files(trial.pk, result, log.text(), opts["output_dir"])
            if opts["dry_run"]:
                pass
            elif opts["report_only"]:
                if result is not None:
                    self.save_report(trial, result)
            else:
                self.save_to_model(trial, result, log.text())

        if opts["output_dir"]:
            self.write_index(opts["output_dir"])
        if opts["dry_run"]:
            print("\nDRY RUN — database not modified. Output in %s" % opts["output_dir"])
        if failures:
            print("\nFAILED: %s" % ", ".join("#%d" % pk for pk in failures))

    def write_files(self, pk, result, log_text, out):
        with open(os.path.join(out, "trial%d.log" % pk), "w") as f:
            f.write(log_text)
        if result is None:
            return
        for name, png in result["images"].items():
            path = os.path.join(out, "trial%d-%s.png" % (pk, erp.FIELDS[name]))
            with open(path, "wb") as f:
                f.write(png)
            print("  wrote %s" % path)
        with open(os.path.join(out, "trial%d.json" % pk), "w") as f:
            json.dump(result["summary"], f, indent=1)
        with open(os.path.join(out, "trial%d-report.pdf" % pk), "wb") as f:
            f.write(result["pdf"])

    IMAGE_TITLES = [
        ("periStimulusAverage", "ERPs: clear vs scrambled"),
        ("filteredPeriStimulusAverage", "Difference wave (clear − scrambled)"),
        ("filteredPeriPromptStimulusAverage", "Keypress-locked average"),
        ("periNonStimulusAverage", "Alignment check + signal overview"),
    ]

    def write_index(self, out):
        """index.html covering every trial<ID>.log in the output folder."""
        pks = sorted((int(m.group(1)) for m in
                      (re.match(r"trial(\d+)\.log$", f) for f in os.listdir(out)) if m),
                     reverse=True)
        body = []
        toc = []
        for pk in pks:
            summary = {}
            sp = os.path.join(out, "trial%d.json" % pk)
            if os.path.exists(sp):
                with open(sp) as f:
                    summary = json.load(f)
            with open(os.path.join(out, "trial%d.log" % pk)) as f:
                log_text = f.read()
            status = summary.get("status", "ERROR")
            best = ("%s: %+.2f µV, p=%.3f" % (summary["best_channel"], summary["best_mean_uv"],
                                               summary["best_p"])
                    if summary.get("best_channel") else "—")
            toc.append("<tr><td><a href='#t%d'>#%d</a></td><td>%s</td><td>%s</td><td>%s</td>"
                       "<td>%s</td></tr>" % (pk, pk, html.escape(str(summary.get("subject", "?"))),
                                             ", ".join(summary.get("channels", [])) or "—",
                                             best, html.escape(status)))
            body.append("<section id='t%d'><h2>Trial #%d <span class='st'>%s</span></h2>"
                        % (pk, pk, html.escape(status)))
            body.append("<details><summary>log</summary><pre>%s</pre></details>"
                        % html.escape(log_text))
            for field, title in self.IMAGE_TITLES:
                fn = "trial%d-%s.png" % (pk, field)
                if os.path.exists(os.path.join(out, fn)):
                    body.append("<h3>%s <code>%s</code></h3><a href='%s'><img src='%s' alt='%s'></a>"
                                % (title, field, fn, fn, title))
            body.append("</section>")
        page = """<!DOCTYPE html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>EEG Analysis Review</title>
<style>
 body { font-family: sans-serif; max-width: 1300px; margin: 0 auto; padding: 16px; color: #222; }
 img { max-width: 100%%; border: 1px solid #ddd; }
 table { border-collapse: collapse; } td, th { border-bottom: 1px solid #eee; padding: 6px 12px; text-align: left; }
 h2 { border-top: 2px solid #ccc; padding-top: 16px; margin-top: 40px; }
 h3 { font-weight: normal; margin-bottom: 4px; } code { color: #888; font-size: 0.8em; }
 .st { font-size: 0.6em; color: #a04000; font-weight: normal; } pre { background: #f6f6f6; padding: 8px; overflow-x: auto; }
</style></head><body>
<h1>EEG Analysis Review</h1>
<p>Output of <code>manage.py analysis --dry-run --output-dir</code> — not saved to the database.
Generated %s.</p>
<table><tr><th>Trial</th><th>Subject</th><th>Electrodes</th><th>Best (RP 200–300 ms)</th><th>Status</th></tr>
%s</table>
%s
</body></html>""" % (datetime.now().strftime("%Y-%m-%d %H:%M"), "\n".join(toc), "\n".join(body))
        with open(os.path.join(out, "index.html"), "w") as f:
            f.write(page)
        print("  wrote %s" % os.path.join(out, "index.html"))

    def save_report(self, trial, result):
        trial.analysisReport.save("trial%d-report.pdf" % trial.pk, ContentFile(result["pdf"]),
                                  save=False)
        # refreshed summary (adds the report fields); the old JSON file stays on disk
        trial.analysisData.save("trial%d-summary.json" % trial.pk,
                                ContentFile(json.dumps(result["summary"], indent=1)), save=False)
        trial.save(update_fields=["analysisReport", "analysisData"])
        print("  saved report for trial #%d: %s" % (trial.pk, trial.analysisReport.name))

    def save_to_model(self, trial, result, log_text):
        trial.analysisOutput = log_text
        trial.analysisStarted = True
        trial.analysisError = result is None
        fields = ["analysisOutput", "analysisStarted", "analysisError"]
        if result is not None:
            for name, png in result["images"].items():
                field = erp.FIELDS[name]
                getattr(trial, field).save("trial%d-%s.png" % (trial.pk, field),
                                           ContentFile(png), save=False)
                fields.append(field)
            trial.analysisData.save("trial%d-summary.json" % trial.pk,
                                    ContentFile(json.dumps(result["summary"], indent=1)),
                                    save=False)
            trial.analysisReport.save("trial%d-report.pdf" % trial.pk,
                                      ContentFile(result["pdf"]), save=False)
            fields += ["analysisData", "analysisReport"]
        # only the analysis columns — never touches trialData, presses, uploads, etc.
        trial.save(update_fields=fields)
        print("  saved trial #%d (%s)" % (trial.pk, "error" if result is None
                                          else result["summary"]["status"]))
