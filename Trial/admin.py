from django.contrib import admin, messages
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

from Analysis.labels import IMAGES, WINDOW_TEXT, PEAK_SEARCH_MS
from Analysis.runner import run_analysis
from Analysis.summary import load_summary, status_of
from .models import Subject, Trial

admin.site.site_header = "EEG Data"
admin.site.site_title = "EEG Data admin"
admin.site.index_title = "Trials, subjects & analyses"


def status_badge(trial, summary=None):
    cls, label = status_of(trial, summary)
    return format_html('<span class="eeg-badge eeg-{}">{}</span>', cls, label)


@admin.register(Subject)
class SubjectAdmin(admin.ModelAdmin):
    list_display = ("subjectId", "dateCreated", "trial_count")
    search_fields = ("subjectId", "notes")

    @admin.display(description="Trials")
    def trial_count(self, obj):
        return obj.trial_set.count()


@admin.register(Trial)
class TrialAdmin(admin.ModelAdmin):
    list_display = ("id", "subject", "trialTimestamp", "has_edf", "analysis_status",
                    "best_result", "report_link")
    list_display_links = ("id", "trialTimestamp")
    list_filter = ("analysisError", "analysisStarted", "subject", "dataset")
    search_fields = ("id", "subject__subjectId", "trialTimestamp")
    list_per_page = 50
    actions = ["rerun_analysis"]
    readonly_fields = ("analysis_results", "timestamp")
    fieldsets = (
        ("Analysis results", {"fields": ("analysis_results",)}),
        ("Data files", {
            "fields": ("edfFile", "layFile", "statFile", "EDFStartTime"),
            "description": "Uploading a new EDF file runs the analysis automatically when you "
                           "save. Results (or errors) appear at the top of the page.",
        }),
        ("Trial", {"fields": ("subject", "dataset", "trialTimestamp", "timestamp",
                              "javascriptVersion", "trialStyle", "delayBetweenImages",
                              "scrambleRatio", "totalImages")}),
        ("Calibration", {"classes": ("collapse",),
                         "fields": ("calibrationFlashes", "calibrationPeriod",
                                    "calibrationTimestamps", "trialStartMillis")}),
        ("Recorded data (JSON)", {"classes": ("collapse",),
                                  "fields": ("trialData", "recognitionPresses")}),
        ("Analysis files & flags", {"classes": ("collapse",),
                                    "fields": ("analysisStarted", "analysisError",
                                               "periStimulusAverage",
                                               "filteredPeriStimulusAverage",
                                               "filteredPeriPromptStimulusAverage",
                                               "periNonStimulusAverage", "analysisData",
                                               "analysisReport", "analysisOutput")}),
    )

    # --- changelist columns -------------------------------------------------
    @admin.display(description="EDF", boolean=True)
    def has_edf(self, obj):
        return bool(obj.edfFile)

    @admin.display(description="Analysis")
    def analysis_status(self, obj):
        return status_badge(obj)

    @admin.display(description="Best channel (RP %s)" % WINDOW_TEXT)
    def best_result(self, obj):
        s = load_summary(obj)
        if not s or not s.get("best_channel"):
            return "—"
        txt = "%s %+.1f µV, p=%.3f" % (s["best_channel"], s["best_mean_uv"], s["best_p"])
        return format_html("<b>{}</b>", txt) if s["best_p"] < 0.05 else txt

    @admin.display(description="Report")
    def report_link(self, obj):
        if not obj.analysisReport:
            return "—"
        return format_html('<a class="eeg-pdf" href="{}" target="_blank">PDF</a>',
                           obj.analysisReport.url)

    # --- results panel on the change page -----------------------------------
    @admin.display(description="")
    def analysis_results(self, obj):
        if not obj.pk:
            return "Save the trial with an EDF file to run the analysis."
        if not obj.edfFile:
            return "No EDF file attached yet. Upload one below and save to run the analysis."
        s = load_summary(obj)
        parts = [format_html('<div class="eeg-results"><div class="eeg-head">{}', status_badge(obj, s))]
        if obj.analysisReport:
            parts.append(format_html(' <a class="eeg-pdf" href="{}" target="_blank">Download PDF report</a>',
                                     obj.analysisReport.url))
        parts.append(format_html(' <a href="{}" target="_blank">EDF viewer</a>',
                                 "/edfviewer/%d" % obj.pk))
        parts.append(mark_safe("</div>"))
        if obj.analysisError:
            parts.append(format_html('<pre class="eeg-log eeg-log-error">{}</pre>', obj.analysisOutput))
        elif s and "n_images" in s:
            parts.append(format_html(
                "<p class='eeg-meta'>{} images ({} clear, {} scrambled) • {} keypresses • "
                "alignment: {}</p>",
                s.get("n_images", "?"), s.get("n_clear", "?"), s.get("n_scrambled", "?"),
                s.get("n_presses", "?"), s.get("alignment_detail", s.get("alignment", "?"))))
        if s and not obj.analysisError:
            for w in s.get("warnings", []):
                parts.append(format_html('<p class="eeg-warning">⚠ {}</p>', w))
            if s.get("stats"):
                rows = format_html_join("", "<tr class='{}'><td>{}</td><td>{} / {}</td><td>{} µV</td>"
                                            "<td>{}</td><td>{} µV @ {} ms</td></tr>",
                                        (("eeg-sig" if v["p"] < 0.05 else "", c,
                                          v["n_clear"], v["n_scr"], "%+.2f" % v["mean"],
                                          "%.3f" % v["p"], "%+.1f" % v["peak_uv"],
                                          "%.0f" % v["peak_ms"])
                                         for c, v in s["stats"].items()))
                parts.append(format_html(
                    "<table class='eeg-table'><thead><tr><th>Channel</th><th>Epochs clear / "
                    "scrambled</th><th>Mean diff. {}</th><th>p</th><th>Largest difference "
                    "{}–{} ms</th></tr></thead><tbody>{}</tbody></table>",
                    WINDOW_TEXT, PEAK_SEARCH_MS[0], PEAK_SEARCH_MS[1], rows))
        if not obj.analysisError:
            imgs = []
            for _name, field, title, note in IMAGES:
                f = getattr(obj, field)
                if f:
                    imgs.append(format_html(
                        '<figure><figcaption><b>{}</b> <span>{}</span></figcaption>'
                        '<a href="{}" target="_blank"><img src="{}" alt="{}"></a></figure>',
                        title, note, f.url, f.url, title))
            if imgs:
                parts.append(format_html('<div class="eeg-figs">{}</div>', mark_safe("".join(imgs))))
            if obj.analysisOutput:
                parts.append(format_html('<details><summary>Analysis log</summary>'
                                         '<pre class="eeg-log">{}</pre></details>', obj.analysisOutput))
        parts.append(mark_safe("</div>"))
        return mark_safe("".join(str(p) for p in parts))

    # --- automatic processing on upload --------------------------------------
    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if "edfFile" in form.changed_data and obj.edfFile:
            self._analyse(request, obj)

    def _analyse(self, request, obj):
        request._eeg_analysed = True
        ok, tail = run_analysis(obj.pk)
        obj.refresh_from_db()
        if not ok or obj.analysisError:
            detail = (obj.analysisOutput or tail).strip().splitlines()
            messages.error(request, format_html(
                "Analysis of trial #{} failed: {}", obj.pk, detail[-1] if detail else "unknown error"))
            return False
        s = load_summary(obj) or {}
        st = s.get("status", "done")
        best = ("best %s %+.1f µV, p=%.3f" % (s["best_channel"], s["best_mean_uv"], s["best_p"])
                if s.get("best_channel") else "no channel results")
        msg = format_html("Analysis of trial #{} complete — {} ({}).", obj.pk, st, best)
        (messages.success if st == "ok" else messages.warning)(request, msg)
        return True

    def response_change(self, request, obj):
        # after an upload, stay on the trial so the results/errors are visible
        if getattr(request, "_eeg_analysed", False) and "_addanother" not in request.POST:
            return HttpResponseRedirect(reverse("admin:Trial_trial_change", args=(obj.pk,)))
        return super().response_change(request, obj)

    def response_add(self, request, obj, post_url_continue=None):
        if getattr(request, "_eeg_analysed", False) and "_addanother" not in request.POST:
            return HttpResponseRedirect(reverse("admin:Trial_trial_change", args=(obj.pk,)))
        return super().response_add(request, obj, post_url_continue)

    @admin.action(description="Re-run analysis (new images + PDF; old files are kept)")
    def rerun_analysis(self, request, queryset):
        for trial in queryset.order_by("pk")[:10]:
            if trial.edfFile:
                self._analyse(request, trial)
            else:
                messages.warning(request, "Trial #%d has no EDF file — skipped." % trial.pk)
        if queryset.count() > 10:
            messages.warning(request, "Only the first 10 selected trials were analysed (each takes "
                                      "~15 s); run `manage.py analysis --trialId …` for more.")
