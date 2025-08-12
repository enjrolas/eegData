from django.db import models

class Dataset(models.Model):
  name=models.TextField()
  default=models.BooleanField(default=False)
  def __str__(self):
    return self.name

class Image(models.Model):
  image=models.FileField(upload_to='images')
  resizedImage=models.FileField(upload_to='resizedImages', blank=True, default=None)
  dataset=models.ForeignKey(Dataset, on_delete=models.PROTECT)
  def __str__(self):
    return("%s: %s" % (self.dataset, self.image))
