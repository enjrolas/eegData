from django.core.management.base import BaseCommand
import pyedflib
from pyedflib import highlevel
from scipy import *
import numpy as np
import math
import pandas as pd
import matplotlib.pyplot as plt
import sys
import os
import traceback
import scipy
from datetime import datetime
import pprint
from scipy.io import savemat, loadmat
from scipy import fftpack, linalg
import json
import csv
import urllib.request
from Trial.models import *
from django.conf import settings
from io import StringIO

class Command(BaseCommand):
  help = 'Help for command'
 
  verbose=False
  fs=4
  signals=[]
  notchB=None
  notchA=None
  count=0

  def create_fir_filter(self, num_taps, cutoff_frequency, sampling_rate):
      # Normalized cutoff frequency
      normalized_cutoff = 2 * cutoff_frequency / sampling_rate

      # Create an array of tap indices
      indices = np.arange(-num_taps // 2, num_taps // 2 + 1)

      # Calculate the sinc filter
      sinc_filter = np.sinc(normalized_cutoff * indices)

      # Apply a window function (Hann window)
      window = np.hanning(len(sinc_filter))
      fir_filter = sinc_filter * window
      return fir_filter

  #this function adds and averages multiple occurrences, from 'beforeOffset' to 'afterOffset' where a signal occurs, and returns a time series with that average
  #<beforeOffset> is in ms before the stimulus, <afterOffset> is in ms after the stimulus
  #<stimuliList> is a list of times, in ms, that occur in <signal>.  
  #the elements in signal and average have a sample frequency fs, in samples per second
  def periStimulusAverage(self, signalName, stimuliList, beforeOffset, afterOffset):
      beforeOffset=int(-beforeOffset/(1000/fs))    
      afterOffset=int(afterOffset/(1000/fs))
      average=np.zeros(afterOffset-beforeOffset)
      com="Cz"
      O1SignalNotch=signals[signal_labels.index(signalName)]
      common=signals[signal_labels.index(com)]
      signalNotch=scipy.signal.filtfilt(notchB, notchA, O1SignalNotch)
      commonNotch=scipy.signal.filtfilt(notchB, notchA, common)
      notchDiff=signalNotch-commonNotch

      stimuli=0
      for value in stimuliList:
          stimuli+=1
          for i in range(beforeOffset, afterOffset):
                  average[i]= average[i]*(stimuli-1)/stimuli + notchDiff[int(value/(1000/fs))+i]/stimuli
      return average, signalNotch



  #if testRun is set to True, it won't set the analysisStarted or analysisError flags
  def analyzeTrial(self, trial, channel=None, hardCodedStartTime="", trimStart=0, trimEnd=5000, verbose=True, outputString=None, testRun=False):
      global fs, signals, signal_labels, notchB, notchA
      global count
      print("count: %s" % self.count)

      trialTimestamp=trial.timestamp
      timeOffset=0
      if trial.javascriptVersion=="1.1":
          print("javascript v1.1")
          timeOffset=-300
          print("time offset is %d" % timeOffset)
      try:
        dataFileDirectory="media/"
        filename=os.path.join(dataFileDirectory,trial.edfFile.name)
        print("filename:  %s" % filename)
        print(os.getcwd())
        signals, signal_headers, header = highlevel.read_edf(filename)
        f = pyedflib.EdfReader(filename)
        signal_labels = f.getSignalLabels()
        numChannels = f.signals_in_file

        f.close()
        print("recognition presses")
        if verbose:
          pprint.pprint(trial.recognitionPresses)
        recognitionPresses= json.loads(trial.recognitionPresses)
        trialData=json.loads(trial.trialData)
        if verbose:
          #print("trial data:")
          #pprint.pprint(trialData)
          #print("recognition presses:")
          #pprint.pprint(recognitionPresses)          
          print("header")
          pprint.pprint(header)
          print("signal header")
          pprint.pprint(signal_headers)
        n = len(signals)
        print(n)
        print("eeg start time from the model:  %s" % trial.EDFStartTime) 
        eegStartTime=header["startdate"]
        print("EEG start time from the EDF file: %s" % eegStartTime)
        try:
          trialTimestamp=datetime.strptime(trial.trialTimestamp, "%m/%d/%Y %H:%M:%S.%f")  
        except:  #early trials only did hours and minutes, later goes down to seconds with decimal
            trialTimestamp=datetime.strptime(trial.trialTimestamp, "%m/%d/%Y %H:%M")  
        print("trial timestamp: %s" % trialTimestamp)
        difference=(trialTimestamp-eegStartTime).total_seconds()
        print("time difference by trial start time: %s" % (trialTimestamp-eegStartTime))
        print("time difference total seconds by trial start time: %s" % difference)
        if channel is None:            
          opticalTrigger1='CH21'  #'EKG1'
          opticalTrigger2='Cz'  #'EKG2'
          #opticalTrigger1='EKG1'  #'EKG1'
          #opticalTrigger2='EKG2'  #'EKG2'

          ekg1=signals[signal_labels.index(opticalTrigger1)]
          ekg2=signals[signal_labels.index(opticalTrigger2)]
          diff=ekg1-ekg2

          #notch filter
          fs = signal_headers[0]["sample_frequency"]  # Sample frequency (Hz) (250hz by default)
          print("fs: %f" % fs)
          f0 = 60.0  # Frequency to be removed from signal (Hz)
          Q = 30.0  # Quality factor
          # Design notch filter
          notchB, notchA = signal.iirnotch(f0, Q, fs)
          notch=scipy.signal.filtfilt(notchB, notchA, diff)

          #trying a low-pass filter
          fs = signal_headers[0]["sample_frequency"]  # Sample frequency (Hz) (250hz by default)
          bpB, bpA = signal.butter(4, [.04, .12], 'bandpass') #, analog=True)


          #bandpass=scipy.signal.filtfilt(bpB, bpA, notch)
          bandpass=diff

          # Detect rising zero-crossings
          risingZeroCrossings = np.where((bandpass[:-1] < 0) & (bandpass[1:] >= 0))[0]
          differences=[]
          for index in range(1,len(risingZeroCrossings)):
            differences.append(risingZeroCrossings[index]-risingZeroCrossings[index-1])

          # Detect falling zero-crossings -- remember that these are sample indices and not time values
          allZeroCrossings = np.where(( (bandpass[:-1] > 0) & (bandpass[1:] <= 0)) | (bandpass[:-1] < 0) & (bandpass[1:] >= 0)) [0]                
          
          # Sliding window parameters
          window_size = 10  # samples
          step_size = 10     # step between windows
          num_windows = (len(bandpass) - window_size) // step_size + 1
          
          # Compute peak-to-peak in each window
          ptp_values = np.array([
            np.ptp(bandpass[i:i+window_size])
            for i in range(0, len(bandpass) - window_size + 1, step_size)
          ])
          
          # Corresponding time points (center of each window)
          times = (1000/fs)*np.array([
            i + window_size // 2
            for i in range(0, len(bandpass) - window_size + 1, step_size)
          ])
          
          riseTime=0
          threshhold=2000
          rti=0
          print("EDF raw start time: %s" % trial.EDFStartTime)
          if hardCodedStartTime:
            try:
              edfStartTime=datetime.strptime(trial.EDFStartTime, "%H:%M:%S.%f")
            except:  #early trials only had hours and minutes
              edfStartTime=datetime.strptime(trial.EDFStartTime, "%H:%M")
            edfStartTime=datetime.combine(eegStartTime.date(), edfStartTime.time())
            print("edf start time: %s" % edfStartTime)
            print("eeg Start time: %s" % eegStartTime)
            print("start time of trial, before the calibration flashes:  %s" % (edfStartTime-eegStartTime))
            
            #startTime=(edfStartTime-eegStartTime).total_seconds()
            startTime=difference
            print("start time: %s" % startTime)
            startSample=int(startTime*fs/1000)
            print("%f - %f" % (trimStart, trimEnd))
            trimStart+=startTime*1000
            trimEnd+=startTime*1000
            print("%f - %f" % (trimStart, trimEnd))
          time = np.arange(len(diff)) * 1000/fs
          
          print("startTime is %f" % startTime)
          cleanDict={}
          scrambledImages={}
          clearImages={}
          filteredImages={}
          promptDelay=1000
          if verbose:
            pprint.pprint(trialData['data'])
          for key, value in trialData['data'].items():
            if key.isdigit():
              cleanDict[int(key)]=value
              values=value.split(",")
              if values[2]=='false':
                t=int(round(float(values[0])))+float(startTime*1000)+timeOffset
                clearImages[t]=values[1]
                filtered=False
                for val in recognitionPresses:
                  try:
                    if val>t and val<t + promptDelay:
                      if not filtered:
                        filteredImages[val]=values[1]
                      filtered=True
                  except Exception as e:
                    print(e)
              else:
                scrambledImages[int(round(float(values[0])))+float(startTime*1000)+timeOffset]=values[1]
            else:
              cleanDict[key]=value

          print("start time: %s" % startTime)
          print("time offset: %s" % timeOffset)
          if verbose:
            pprint.pprint(recognitionPresses)
            '''
            for index, value in enumerate(recognitionPresses):
              if value!="null":
                recognitionPresses[index]=value + startTime*1000 + timeOffset   #offset the recognition press time (time =0 is the first image) to line up with the start time on the EEG
                print("%s - %s %s %s" % (index, value, startTime*1000, timeOffset))
            '''
          if verbose:
            print(list(clearImages.keys())[0])
            print("adjusted recongition presses")

          beforeOffset=300 #ms

          afterOffset=300 #ms
          O1psa, O1SignalNotch=self.periStimulusAverage("O1", clearImages.keys(), beforeOffset, afterOffset)
          O2psa, O2SignalNotch=self.periStimulusAverage("O2", clearImages.keys(), beforeOffset, afterOffset)
          T5psa, T5SignalNotch=self.periStimulusAverage("T5", clearImages.keys(), beforeOffset, afterOffset)
          T6psa, T6SignalNotch=self.periStimulusAverage("T6", clearImages.keys(), beforeOffset, afterOffset)
          
          O1fpsa, tmp=self.periStimulusAverage("O1", filteredImages.keys(), beforeOffset, afterOffset)
          O2fpsa, tmp=self.periStimulusAverage("O2", filteredImages.keys(), beforeOffset, afterOffset)
          T5fpsa, tmp=self.periStimulusAverage("T5", filteredImages.keys(), beforeOffset, afterOffset)
          T6fpsa, tmp=self.periStimulusAverage("T6", filteredImages.keys(), beforeOffset, afterOffset)
          
          O1kppsa, tmp=self.periStimulusAverage("O1", recognitionPresses, promptDelay, 0)
          O2kppsa, tmp=self.periStimulusAverage("O2", recognitionPresses, promptDelay, 0)
          T5kppsa, tmp=self.periStimulusAverage("T5", recognitionPresses, promptDelay, 0)
          T6kppsa, tmp=self.periStimulusAverage("T6", recognitionPresses, promptDelay, 0)
          
          
          O1pnsa, tmp=self.periStimulusAverage("O1", scrambledImages.keys(), beforeOffset, afterOffset)
          O2pnsa, tmp=self.periStimulusAverage("O2", scrambledImages.keys(), beforeOffset, afterOffset)
          T5pnsa, tmp=self.periStimulusAverage("T5", scrambledImages.keys(), beforeOffset, afterOffset)
          T6pnsa, tmp=self.periStimulusAverage("T6", scrambledImages.keys(), beforeOffset, afterOffset)
          
          sig="O1"
          com="Oz"
          
          bigplot, axes=plt.subplots(2,1,figsize=(20,10))
          bigplot.suptitle("Aligning %s and %s with images and key presses in trial #%s" % (sig, com, trial.pk), fontsize=16)

          axes[0].plot(time, O1SignalNotch, color="lightGreen", label="%s - %s" % (sig, com))
          axes[0].plot(time, diff, color="purple", label="CH21 - Cz")
          #            axes[0].plot(time, O2Notch, color="orange", label="O2")
          axes[1].plot(time, O1SignalNotch, color="lightGreen", label=sig)
          axes[1].vlines(x=recognitionPresses, ymin=min(O1SignalNotch), ymax=max(O1SignalNotch), color='purple', linestyle="--", linewidth=1, label="recognition presses")
          axes[1].vlines(x=list(clearImages.keys()), ymin=min(O1SignalNotch)/2, ymax=max(O1SignalNotch)/2, color='green', linestyle="--", linewidth=2, label="clear images")
          axes[1].vlines(x=list(scrambledImages.keys()), ymin=min(O1SignalNotch)/2, ymax=max(O1SignalNotch)/2, color='red', linestyle="--", linewidth=2, label="scrambled images")            
          axes[0].legend()
          axes[1].legend()
          axes[0].set_xlim(trimStart, trimEnd)
          axes[1].set_xlim(trimStart, trimEnd)            
          #bigplot.show()
        
          analysisImageDirectory='analysis/'
          #analysisImageDirectory=os.path.join(settings.MEDIA_ROOT, 'analysis')
          print("analysis image directory: %s" % analysisImageDirectory)
          periStimulusTime = np.arange(-beforeOffset, afterOffset, 1000/fs)
          print("before: %f, after: %f, length: %f" % (-beforeOffset/(1000/fs), afterOffset/(1000/fs), len(periStimulusTime)))
          plt.figure(figsize=(20, 3))
          plt.title("Trial #%d, peri-stimulus averages over time, averaged across %d stimuli and %d non-stimuli" % (trial.pk, len(clearImages.keys()), len(scrambledImages.keys())))
          plt.plot(periStimulusTime,O1psa, label="O1 peri-stimulus average")
          plt.plot(periStimulusTime,O2psa, label="O2 peri-stimulus average")
          plt.plot(periStimulusTime,T5psa, label="T5 peri-stimulus average")
          plt.plot(periStimulusTime,T6psa, label="T6 peri-stimulus average")
          plt.xlabel("ms before/after stimulus presentation")            
          #            plt.plot(periStimulusTime,filteredPeriStimulusAverage, label="filtered peri-stimulus average")
          plt.legend(loc="upper right")
          periStimulusAverageFile=os.path.join(analysisImageDirectory, "trial%d-periStimulusAverage.png" % trial.pk)
          print("saving figure to %s" % periStimulusAverageFile)
          plt.savefig(periStimulusAverageFile)
          print("updating model")
          trial.periStimulusAverage.name=periStimulusAverageFile
          print("model updated")
          #plt.show()
          
          plt.figure(figsize=(20, 3))
          plt.title("Trial #%d, filtered peri-stimulus (only with key press) averages over time, averaged across %d prompt (within %d ms of presentation) presses" % (trial.pk, len(filteredImages.keys()), promptDelay))
          plt.plot(periStimulusTime,O1fpsa, label="O1 filtered peri-stimulus average")
          plt.plot(periStimulusTime,O2fpsa, label="O2 filtered peri-stimulus average")
          plt.plot(periStimulusTime,T5fpsa, label="T5 filtered peri-stimulus average")
          plt.plot(periStimulusTime,T6fpsa, label="T6 filtered peri-stimulus average")
          plt.xlabel("ms before/after stimulus presentation")            
          #            plt.plot(periStimulusTime,filteredPeriStimulusAverage, label="filtered peri-stimulus average")
          plt.legend(loc="upper right")
          filteredPeriPromptStimulusAverageFile=os.path.join(analysisImageDirectory, "trial%d-filteredPeriPromptStimulusAverage.png" % trial.pk)
          print("saving peri prompt stimulus average figure to %s" % filteredPeriPromptStimulusAverageFile)
          plt.savefig(filteredPeriPromptStimulusAverageFile)
          trial.filteredPeriPromptStimulusAverage.name=filteredPeriPromptStimulusAverageFile
          #plt.show()
          
          kpStimulusTime = np.arange(-promptDelay, 0, 1000/fs)
          plt.figure(figsize=(20, 3))
          plt.title("Trial #%d, averaged around %d key press events" % (trial.pk, len(recognitionPresses)))
          plt.plot(kpStimulusTime,O1kppsa, label="O1, averaged around key press event")
          plt.plot(kpStimulusTime,O2kppsa, label="O2, averaged around key press event")
          plt.plot(kpStimulusTime,T5kppsa, label="T5, averaged around key press event")
          plt.plot(kpStimulusTime,T6kppsa, label="T6, averaged around key press event")
          plt.xlabel("ms before/after key press")            
          #            plt.plot(periStimulusTime,filteredPeriStimulusAverage, label="filtered peri-stimulus average")
          plt.legend(loc="upper right")
          filteredPeriStimulusAverageFile=os.path.join(analysisImageDirectory, "trial%d-filteredPeriStimulusAverage.png" % trial.pk)
          plt.savefig(filteredPeriStimulusAverageFile)
          trial.filteredPeriStimulusAverage.name=filteredPeriStimulusAverageFile
          
          
          
          
          plt.figure(figsize=(20, 3))
          plt.title("trial #%d, peri-non-stimulis average over time" % trial.pk)
          plt.plot(periStimulusTime,O1pnsa,  label="O1 peri-non-stimulus average")
          plt.plot(periStimulusTime,O2pnsa,  label="O2 peri-non-stimulus average")
          plt.plot(periStimulusTime,T5pnsa,  label="T5 peri-non-stimulus average")
          plt.plot(periStimulusTime,T6pnsa,  label="T6 peri-non-stimulus average")
          plt.xlabel("ms before/after stimulus presentation")
          plt.legend()
          periNonStimulusAverageFile=os.path.join(analysisImageDirectory, "trial%d-periNonStimulusAverage.png" % trial.pk)
          plt.savefig(periNonStimulusAverageFile)
          trial.periNonStimulusAverage.name=periNonStimulusAverageFile
          if not testRun:
            trial.analysisStarted=True
            trial.analysisError=False
            trial.save()
        else:
          priOAnt(channel)

          ax.plot(signals[signal_labels.index(channel)] , color='purple' )
          plt.show()

          rfig, raxs=plt.subplots(1, 1, figsize=(6,8))
          rfig.suptitle("user recognition presses", fontsize=16)
          raxs[0].plot()
          trial.analysisOutput=outputString.getvalue()
          if not testRun:
            trial.analysisStarted=True
            trial.analysisError=False
            trial.save()
        self.count+=1

      except:
        traceback.print_exc(file=sys.stdout)
        trial.analysisOutput=outputString.getvalue()
        print("marking the error in trial %s and saving it" % trial)
        if not testRun:
          trial.analysisError=True
          trial.analysisStarted=True
          trial.save()

  def add_arguments(self, parser):
    parser.add_argument("--trialId", type=int)
 
  def handle(self, *args, **options):
    if options["trialId"]:
      trialId=int(options["trialId"])
      print("trial Id is %s" % trialId)
      newTrials=Trial.objects.filter(pk=trialId)
      print(newTrials)
    else:
      newTrials=Trial.objects.exclude(edfFile='').exclude(EDFStartTime=0).filter(analysisError=False).filter(analysisStarted=False).order_by('timestamp')
    if newTrials.count()>0:
      for trial in newTrials:
        print("trial id: %s" % trial.pk)
        # Create a StringIO object
        outputString = StringIO()
        print("trial: %s" % trial)
        self.analyzeTrial(trial, hardCodedStartTime=True, trimStart=-500, trimEnd=5000, outputString=outputString, testRun=False, verbose=False)
        sys.stdout = sys.__stdout__
    else:
      print("no new trials")
      print("done")





