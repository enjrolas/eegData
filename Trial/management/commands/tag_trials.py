"""
Refresh the automatic tags on trials from their saved analysis summaries
(no re-analysis).  Manual tags are never touched.

    python manage.py tag_trials                # every trial
    python manage.py tag_trials --trialId 144  # specific trial(s)
    python manage.py tag_trials --dry-run      # print what would be set
"""
from django.core.management.base import BaseCommand

from Analysis.tags import apply_tags, compute_tags, ensure_tags
from Trial.models import Trial


class Command(BaseCommand):
    help = "Set the automatic analysis tags on trials from their saved summaries"

    def add_arguments(self, parser):
        parser.add_argument("--trialId", type=int, nargs="+")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        trials = Trial.objects.order_by("pk")
        if opts["trialId"]:
            trials = trials.filter(pk__in=opts["trialId"])
        if not opts["dry_run"]:
            ensure_tags()
        for t in trials:
            tags = compute_tags(t) if opts["dry_run"] else apply_tags(t)
            if opts["verbosity"] > 1 or t.edfFile:
                print("#%d: %s" % (t.pk, ", ".join(tags) or "—"))
