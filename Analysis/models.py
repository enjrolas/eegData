from django.db import models
from Trial.models import *

class Analysis(models.Model):
  lowPassFrequency=models.FloatField(default=60)
  highPassFrequency=models.FloatField(default=0)
  promptStimulusTime=models.IntegerField(default=1000)
  trial=models.ForeignKey(Trial, blank=True, on_delete=models.PROTECT)
