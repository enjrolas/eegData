logfile="/home/japhy/logs/analyzeEEGMeasurements.log"
now=$(date)
printf "%s\n" "$now"
echo "analyizing new EEG measurements"
ps -eaf | pgrep -fl "manage.py analysis"   #check if script is already running
# if not found - equals to 1, start it
if [ $? -eq 1 ]
then
    #    echo "no script is running, starting the script" >> "$logfile"
    echo "no script is running, starting the script"
    cd /home/japhy/eegdata
    source /home/japhy/eegdata/.env/bin/activate
    #/home/japhy/eegdata/manage.py analysis 
    /home/japhy/eegdata/manage.py analysis >> "$logfile" 2>&1
    

else
    echo "eq 0 - preventative eye care processing script is already running - do nothing"
fi
