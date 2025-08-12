from django.shortcuts import render
from .models import *
import logging
logger = logging.getLogger(__name__)

def home(request):
  logger.debug("home view")
  trials=Trial.objects.all()
  logger.debug("trials: %s" % trials)
  context={
    'trials':trials
  }
  return render(request, 'dataEndpoint/index.html', context)
