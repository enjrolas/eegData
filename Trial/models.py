from django.db import models
from Dataset.models import *

class Subject(models.Model):
  dateCreated=models.DateTimeField(auto_now_add=True)
  subjectId=models.TextField(blank=True)
  notes=models.TextField(blank=True)

  def __str__(self):
    return "%s" % (self.subjectId)
  
class Tag(models.Model):
  """A characteristic found while analysing a trial.  Automatic tags are set by
  the analysis (Analysis/tags.py); others can be created and attached by hand."""
  CATEGORIES=[("data","Data"),("sync","Sync"),("trial","Trial"),("result","Result"),
              ("channel","Channel"),("note","Note")]
  slug=models.SlugField(max_length=64, unique=True)
  name=models.CharField(max_length=100)
  category=models.CharField(max_length=16, choices=CATEGORIES, default="note")
  description=models.TextField(blank=True)
  automatic=models.BooleanField(default=False, help_text="set and cleared by the analysis")
  order=models.IntegerField(default=100)

  class Meta:
    ordering=["order", "name"]

  def __str__(self):
    return self.name

class Trial(models.Model):
  # iterable
  KEY="k"
  MICROPHONE="m"
  STYLE_CHOICES =[
    (KEY, "Key-Type Interaction"), 
    (MICROPHONE, "Microphone-and-Clicker Interaction"), 
  ]

  timestamp = models.DateTimeField(auto_now_add=True)
  javascriptVersion=models.TextField(default="1.2")
  subject=models.ForeignKey(Subject, blank=True, on_delete=models.PROTECT)
  trialData=models.TextField(blank=True)
  recognitionPresses=models.TextField(blank=True)
  trialTimestamp=models.TextField(blank=True)
  dataset=models.ForeignKey(Dataset, blank=True, on_delete=models.PROTECT)
  delayBetweenImages=models.IntegerField(blank=True)  
  scrambleRatio=models.FloatField(blank=True)
  totalImages=models.IntegerField(blank=True)
  trialStyle=models.CharField(max_length=1, choices=STYLE_CHOICES, default=KEY, blank=True)
  edfFile=models.FileField(upload_to="trialData", blank=True)
  layFile=models.FileField(upload_to="trialData", blank=True)
  statFile=models.FileField(upload_to="trialData", blank=True)
  EDFStartTime=models.TextField(default="00:00:00.0")
  calibrationTimestamps=models.TextField(blank=True)
  calibrationFlashes=models.FloatField(default=5)
  calibrationPeriod=models.IntegerField(default=60)
  trialStartMillis=models.FloatField(default=0)
  analysisStarted=models.BooleanField(default=False)
  analysisError=models.BooleanField(default=False)
  periStimulusAverage=models.FileField(upload_to="analysis", blank=True)
  filteredPeriStimulusAverage=models.FileField(upload_to="analysis", blank=True)
  filteredPeriPromptStimulusAverage=models.FileField(upload_to="analysis", blank=True)
  periNonStimulusAverage=models.FileField(upload_to="analysis", blank=True)
  analysisData=models.FileField(upload_to="analysis", blank=True)
  analysisReport=models.FileField(upload_to="analysis/reports", blank=True)
  syncProof=models.FileField(upload_to="analysis", blank=True)
  clearVsScrambled=models.FileField(upload_to="analysis", blank=True)
  differenceWave=models.FileField(upload_to="analysis", blank=True)
  keypressLockedAverage=models.FileField(upload_to="analysis", blank=True)
  alignmentCheck=models.FileField(upload_to="analysis", blank=True)
  imageSyncPulses=models.IntegerField(null=True, blank=True)
  # the key number: median time from image presentation to the RP (best channel)
  rpLatencyMs=models.FloatField(null=True, blank=True)
  rpLatencyCiLowMs=models.FloatField(null=True, blank=True)
  rpLatencyCiHighMs=models.FloatField(null=True, blank=True)
  rpLatencyChannel=models.CharField(max_length=16, blank=True)
  analysisOutput=models.TextField(blank=True)
  tags=models.ManyToManyField(Tag, blank=True, related_name="trials")
  def __str__(self):
    if self.edfFile:
      data="data file present"
    else:
      data="no data"
    return "Trial #%2d:  subject %s at %s -- %s" % (self.pk, self.subject, self.trialTimestamp, data)
