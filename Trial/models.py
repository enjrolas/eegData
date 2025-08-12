from django.db import models
from Dataset.models import *

class Subject(models.Model):
  dateCreated=models.DateTimeField(auto_now_add=True)
  subjectId=models.TextField(blank=True)
  notes=models.TextField(blank=True)

  def __str__(self):
    return "%s" % (self.subjectId)
  
class Trial(models.Model):
  # iterable
  KEY="k"
  MICROPHONE="m"
  STYLE_CHOICES =[
      (KEY, "Key-Type Interaction"), 
      (MICROPHONE, "Microphone-and-Clicker Interaction"), 
    ]

  timestamp = models.DateTimeField(auto_now_add=True)
  javascriptVersion=models.TextField(default="1.1")
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
  calibrationFlashes=models.IntegerField(default=5)
  calibrationPeriod=models.IntegerField(default=60)
  def __str__(self):
    return "Trial #%2d:  subject %s at %s" % (self.pk, self.subject, self.trialTimestamp)
