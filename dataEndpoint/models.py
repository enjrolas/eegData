from django.db import models

class Subject(models.Model):
  MALE= "ML"
  FEMALE = "FL"
  NON_BINARY = "NB"
  RATHER_NOT_SAY= "NS"

  SEX_CHOICES = [
    (MALE, "Male"),
    (FEMALE, "Female"),
    (NON_BINARY, "Non-binary"),
    (RATHER_NOT_SAY, "Rather Not Say"),
  ]
  createdAt=models.DateTimeField(auto_now_add=True)
  updatedAt = models.DateTimeField(auto_now=True)
  firstName=models.TextField(blank=True)
  lastName=models.TextField(blank=True)
  age=models.IntegerField(blank=True)
  concussionDate=models.DateField(blank=True)
  sex=models.CharField(
    max_length=2,
    choices=SEX_CHOICES,
    default=RATHER_NOT_SAY)

  def __str__(self):
    return "%d: %d year-old %s, concussion date %s" % (self.pk, self.age, self.sex, self.concussionDate)

class Trial(models.Model):
  subject=models.ForeignKey(Subject, default=1, on_delete=models.PROTECT)
  createdAt=models.DateTimeField(auto_now_add=True)
  updatedAt=models.DateTimeField(auto_now=True)
  data=models.TextField(blank=True)

  def __str__(self):
    return "%d: subject %d on %s" % (self.pk, self.subject.pk, self.createdAt)
