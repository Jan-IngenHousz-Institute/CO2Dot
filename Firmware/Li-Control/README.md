This repository is a lightweight LI‑6800 control/measurement harness. The instrument runs a background program (RemoteEnvMeasure.py) that watches for a JSON command, applies setpoints, optionally waits for stabilization, logs one record, and writes an acknowledgment JSON. 
The local notebook (Example_LiControl.ipynb) sends commands over scp/ssh and waits for the matching ack.

RemoteEnvMeasure.py should be placed on the instrument under /home/licor/apps/dynamic/ (this matches where LI‑6800 background programs are stored).
The JSON files are runtime artifacts in that same directory: remote_cmd.json is uploaded by your client, and remote_ack.json is created by the background program.

How to start the Background Program:
Once you copied the file RemoteEnvMeasure.py into /home/licor/apps/dynamic/.
Go on the LI‑6800 console, open the Programs tab, go into BP Builder, open the background program file, and use Start BP to run it.
If you expect LOG() to produce output, make sure a log file is open; background programs only write when a log file is open.
