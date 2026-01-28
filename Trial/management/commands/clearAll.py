from django.core.management.base import BaseCommand
from Trial.models import *

class Command(BaseCommand):
  def handle(self, *args, **options):
    allTrials = Trial.objects.all()
    for trial in allTrials:
      print("clearing %s" % trial)
      trial.analysisStarted=False
      trial.analysisError=False
      trial.periStimulusAverage=None
      trial.filteredPeriStimulusAverage=None
      trial.filteredPeriPromptStimulusAverage=None
      trial.periNonStimulusAverage=None
      trial.save()
