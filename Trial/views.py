from django.urls import reverse
from django.shortcuts import render
import logging
logger = logging.getLogger(__name__)
from Dataset.models import *
from .models import *
from django.http import JsonResponse
import json
from django.views.decorators.csrf import csrf_exempt
from django.core import serializers

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
  calibrationTimestamps=request.POST.get("calibrationTimestamps")
  calibrationPeriod=request.POST.get("calibrationPeriod")
  javascriptVersion=request.POST.get("javascriptVersion")
  trialStartMillis=request.POST.get("trialStartMillis")
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
  trial.calibrationFlashes=calibrationFlashes
  trial.calibrationPeriod=calibrationPeriod
  trial.calibrationTimestamps=calibrationTimestamps
  trial.trialStartMillis=trialStartMillis
  #trial.javascriptVersion=javascriptVersion
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

def trialData(request, id):
  trial=Trial.objects.get(pk=id)
  jsonResponse={
    "trial": trial.pk,
    "timestamp": trial.timestamp,    
    }
  data = serializers.serialize('json', [trial])
  jsonData=json.loads(data)
  return JsonResponse(jsonData[0], safe=False)

def recorded_key(trial):
  """Sortable 'YYYYmmddHHMMSS.fff' from trialTimestamp ("M/D/YYYY H:M[:S[.f]]");
  falls back to the record's creation time."""
  import re
  m=re.match(r"\s*(\d+)/(\d+)/(\d{4})\s+(\d+):(\d+)(?::(\d+)(?:\.(\d+))?)?", trial.trialTimestamp or "")
  if m:
    mo, d, y, h, mi, sec, frac = m.groups()
    return "%s%02d%02d%02d%02d%02d.%s" % (y, int(mo), int(d), int(h), int(mi), int(sec or 0), (frac or "0").ljust(3, "0"))
  return trial.timestamp.strftime("%Y%m%d%H%M%S.000") if trial.timestamp else ""

def analyses(request):
  from Analysis.summary import load_summary, status_of
  from Analysis.labels import WINDOW_TEXT
  rows=[]
  trials=Trial.objects.select_related('subject').prefetch_related('tags').order_by('-timestamp')
  tag=Tag.objects.filter(slug=request.GET.get('tag')).first() if request.GET.get('tag') else None
  if tag:
    trials=trials.filter(tags=tag)
  for trial in trials:
    summary=load_summary(trial) if trial.edfFile else None
    cls, label=status_of(trial, summary)
    best=None
    if summary and summary.get("best_channel"):
      best={"channel": summary["best_channel"], "uv": "%+.1f" % summary["best_mean_uv"],
            "p": "%.3f" % summary["best_p"], "sig": summary["best_p"] < 0.05,
            "peak": "%.0f" % summary["best_peak_ms"] if summary.get("best_peak_ms") is not None else None}
    lat=None
    if trial.rpLatencyMs is not None:
      lat={"ms": "%.0f" % trial.rpLatencyMs, "lo": "%.0f" % trial.rpLatencyCiLowMs,
           "hi": "%.0f" % trial.rpLatencyCiHighMs, "ch": trial.rpLatencyChannel,
           "pm": "%.0f" % ((trial.rpLatencyCiHighMs - trial.rpLatencyCiLowMs) / 2)}
    tags=[t for t in trial.tags.all() if t.slug != "no-data-file"]
    rows.append({"trial": trial, "cls": cls, "label": label, "best": best, "lat": lat,
                 "when": recorded_key(trial),
                 "tags": [t for t in tags if t.category != "channel"],
                 "chtags": [t for t in tags if t.category == "channel"],
                 "channels": ", ".join(summary.get("channels", [])) if summary else ""})
  with_data=[r for r in rows if r["trial"].edfFile]
  context={
    "rows": rows,
    "tag": tag,
    "all_tags": Tag.objects.filter(trials__isnull=False).distinct(),
    "window": WINDOW_TEXT,
    "n_total": len(rows),
    "n_data": len(with_data),
    "n_ok": sum(1 for r in with_data if r["label"]=="ok"),
    "n_sig": sum(1 for r in with_data if r["best"] and r["best"]["sig"]),
    }
  return render(request, "Trial/analyses.html", context)

def analysis(request, id):
  from Analysis.labels import IMAGES
  trial=Trial.objects.get(pk=id)
  figures=[(title, getattr(trial, field).url) for _n, field, title, _note in IMAGES
           if getattr(trial, field)]
  context={
    "trial": trial,
    "figures": figures,
    }
  return render(request, "Trial/analysis.html", context)
  
