from django.shortcuts import render
import logging
logger = logging.getLogger(__name__)

def home(request):
  logger.debug("home view")
#  logger.debug("trials: %s" % trials)
  context={
  }
  return render(request, 'eegdata/index.html', context)
