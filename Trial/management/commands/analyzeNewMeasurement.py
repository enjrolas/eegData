from django.core.management.base import BaseCommand
from Measurement.models import *
from django.conf import settings
import pprint
from django.core.files import File
import os
import sys
import time
import subprocess
from datetime import datetime
import re

class Command(BaseCommand):
  help = 'Help for command'

  def analyzeNewMeasurement(self, measurementId):
    print("Measurement ID is %s" % measurementId)
    measurement=Measurement.objects.get(id=measurementId)
    print(measurement)

    startTime=time.time()
    print("processing work: %s" % measurement)
    leftEyes=Image.objects.filter(eyeSide='LE').filter(measurement=measurement)

    leftEyesPreferred=leftEyes.filter(preferred=True)
    try:
      if leftEyesPreferred.count()>0:
        leftEye=leftEyesPreferred.first()
      else:
        print("no preferred image found, choosing the first left image")
        leftEye=leftEyes.get(order=0)
      print(leftEye)
    except Exception as e: #if there are no left side images
      print(e)
      measurement.analysisStarted=True
      measurement.analysisError=True
      measurement.save()


    rightEyes=Image.objects.filter(eyeSide='RI').filter(measurement=measurement)
    rightEyesPreferred=rightEyes.filter(preferred=True)
    try:
      if rightEyesPreferred.count()>0:
        rightEye=rightEyesPreferred.first()
      else:
        print("no preferred image found, choosing the first right image")
        rightEye=rightEyes.get(order=0)
      print(rightEye)
    except Exception as e:
      print(e)
      measurement.analysisStarted=True
      measurement.analysisError=True
      measurement.save()
      
    outputDir="../../../media/data/Measurement_%d" % measurement.id
    args=["python", "analysis.py",
          "--n_mires","22",
          "--working_distance","75.0",
          "--camera_params","4.8 6.4 4.755",
          "--model_file","./data/ring_distribution.txt",
          "--base_dir", "../../../media/",
          "--left_eye", leftEye.file.name,
          "--right_eye",rightEye.file.name,
          "--center_selection","auto",
          "--mire_seg_method","dl",
          "--mire_loc_method","graph_cluster",
          "--verbose",
          "--output_dir", outputDir ]

    pprint.pprint(args)
    os.chdir(settings.COMMANDS_ROOT)
    measurement.analysisStarted=True
    measurement.analysisStartTime=datetime.now()
    #measurement.save()
    try:
      output=subprocess.run(args,capture_output=True, text=True)
      print("everything ran well")
    except subprocess.CalledProcessError as e:
      print(e)
      print("we had an error, marking it down and saving")
      measurement.analysisStdOutput=output.stdout
      measurement.analysisStdError=output.stderr
      measurement.analysisStarted=True
      measurement.analysisError=True
      measurement.save()
    #measurement=Measurement.objects.get(id=measurementId)
    print("measurement is %s" % measurement)
#    output=subprocess.check_output(args)    

    measurement.analysisStdOutput=output.stdout
    measurement.analysisStdError=output.stderr
    with open("stderr.txt", "w") as text_file:
      text_file.write(output.stderr)
    with open("stdout.txt", "w") as text_file:
      text_file.write(output.stdout)
    print("stdout:")
    pprint.pprint(output.stdout)
    print(output.stdout)
    print("stderr:")
    pprint.pprint(output.stderr)
    print(output.stderr)
    try:
      out=output.stderr
      rski=out.find("SimK")
      lski=out.find("SimK", rski+1)
      rsk=out[rski : out.find("\n", rski)]
      rskParts=re.split(r'[, ]+', rsk)
      lsk=out[lski : out.find("\n", lski)]
      lskParts=re.split(r'[, ]+', lsk)
      measurement.right_sim_k1=float(rskParts[1])
      measurement.right_sim_k2=float(rskParts[2])
      
      measurement.left_sim_k1=float(lskParts[1])
      measurement.left_sim_k2=float(lskParts[2])
      
      measurement.left_average_k=(measurement.left_sim_k1+measurement.left_sim_k2)/2
      measurement.right_average_k=(measurement.right_sim_k1+measurement.right_sim_k2)/2
      rppki=out.find("PPK")
      lppki=out.find("PPK", rppki+1)
      rppk=out[rppki : out.find("CLMI", rppki)]
      lppk=out[lppki : out.find("CLMI", lppki)]
      
      rppkParts=rppk.split(' ')
      lppkParts=lppk.split(' ')
      measurement.right_ppk=float(rppkParts[1])
      measurement.left_ppk=float(lppkParts[1])
      
      rdiffi=out.find("diff")
      ldiffi=out.find("diff", rdiffi+1)
      rdiff=out[rdiffi : out.find(",", rdiffi+1)]
      ldiff=out[ldiffi : out.find(",", ldiffi+1)]
      
      ranglei=out.find("angle:")
      langlei=out.find("angle:", ranglei+1)
      rangle=out[ ranglei :out.find(",", ranglei+1)]
      langle=out[ langlei :out.find(",", langlei+1)]
      measurement.right_angle=float(rangle.split(":")[1])
      measurement.left_angle=float(langle.split(":")[1])
      measurement.leftPlots.name="data/Measurement_%d/left/plots.png" % measurementId
      measurement.leftCornealSurface.name="data/Measurement_%d/left/corneal_surface_3d.png" % measurementId
      
      measurement.rightPlots.name="data/Measurement_%d/right/plots.png" % measurementId
      measurement.rightCornealSurface.name="data/Measurement_%d/right/corneal_surface_3d.png" % measurementId
      
      measurement.left_diff=float(ldiff.split(":")[1])
      measurement.right_diff=float(rdiff.split(":")[1])
      measurement.analysisFinishTime=datetime.now()
    except Exception as e:
      print(e)
      measurement.analysisError=True
      measurement.analysisStarted=True
      measurement.analysisFinishTime=datetime.now()
      print("there was an issue parsing the analysis results, probably means the analysis didn't run correctly")
      

    print("saving measurement")
    try:
      measurement.save()
    except Exception as e:
      print(e)
    print(measurement)
    print("output is")
    pprint.pprint(output)
    endTime=time.time()
    print("total time was %.2f seconds" % (endTime-startTime))
      
  def handle(self, *args, **options):
    newMeasurements=Measurement.objects.filter(analysisError=False).filter(analysisStarted=False).order_by('createdAt')
 if newMeasurements.count()>0:
   measurement=newMeasurements.first()
   print("measurement id: %s" % measurement.pk)
   self.analyzeNewMeasurement(measurement.pk)
 else:
   print("no new measurements")
   print("done")
   
