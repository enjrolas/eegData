from django.urls import reverse
from django.shortcuts import render
import logging
logger = logging.getLogger(__name__)
from Dataset.models import *
from .models import *
from django.http import JsonResponse
import json
from django.views.decorators.csrf import csrf_exempt

def jigsaw(request):
  logger.debug("home view")
  datasetName=request.GET.get('dataset', 'Celebrities')
  trialStyle=request.GET.get('trialStyle')
  logger.debug("datasetName: %s" % datasetName)
  dataset=Dataset.objects.get(name=datasetName)
  images=Image.objects.filter(dataset=dataset)
#  logger.debug("trials: %s" % trials)
  context={
    'images': images,
    'trialStyle': trialStyle,
  }
  return render(request, 'Trial/jigsaw.html', context)

def home(request):
  logger.debug("setup form")
  t=Trial()
  datasets=Dataset.objects.all()
  subjects=Subject.objects.all()
  context={    
    "datasets": datasets,
    'subjects':subjects,
    'trial':t,
  }
  return render(request, 'Trial/setupForm.html', context)

@csrf_exempt
def jsonRecorder(request):
  logger.debug(request.POST)
  subjectId=request.POST.get("subjectId").upper()
 # subjectId=request.POST["subjectId"].upper()
  timestamp=request.POST.get("timestamp")
  dataset=request.POST.get("dataset")
  delayBetweenImages=int(request.POST.get("delayBetweenImages"))
  scrambleRatio=float(request.POST.get("scrambleRatio"))
  totalImages=int(request.POST.get("totalImages"))
  trialDict=request.POST.get("trialDict")
  recognitionPresses=request.POST.get("recognitionPresses")
  trialStyle=request.POST.get("trialStyle")
  calibrationFlashes=request.POST.get("calibrationFlashes")
  calibrationPeriod=request.POST.get("calibrationPeriod")
  trial=Trial()
  subject=Subject.objects.get(subjectId=subjectId)
  trial.subject=subject
  trial.trialData=trialDict
  trial.trialTimestamp=timestamp
  logger.debug(timestamp)
  logger.debug(request.POST.get("timestamp"))
  trial.dataset=Dataset.objects.get(name=dataset)
  trial.delayBetweenImages=delayBetweenImages
  trial.scrambleRatio=scrambleRatio
  trial.totalImages=totalImages
  trial.recognitionPresses=recognitionPresses
  trial.trialStyle=trialStyle
  trial.calibratedFlashes=calibrationFlashes
  trial.calibrationPeriod=calibrationPeriod
  
  trial.save()
  change_url = reverse("admin:Trial_trial_change", args=(trial.id,))
  return JsonResponse({"response":"ok", "trial": trial.pk, 'adminUrl':change_url, 'timestamp': timestamp})

  #subjectId=request.POST.get("subjectId")#.upper()
  #return JsonResponse({"response":request.POST})

#def trialComplete(request):

@csrf_exempt
def newSubject(request):
  subject=Subject()
  subject.subjectId=request.POST.get('subjectId').upper()
  subject.save()
  return JsonResponse({"subjectId":str(subject)})
