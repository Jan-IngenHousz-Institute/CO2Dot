# RemoteEnvMeasure.py
# Place on LI-6800 (e.g.): /home/licor/apps/dynamic/RemoteEnvMeasure.py
#
# Command interface:
#   - Drop a JSON file to: /home/licor/apps/dynamic/remote_cmd.json
#   - This BP reads it, applies environment setpoints, optionally waits, logs, then writes:
#       /home/licor/apps/dynamic/remote_ack.json
#
# IMPORTANT:
#   - If Auto Controls are running, they can override manual/setpoint controls.
#   - LOG() only writes if a log file is open on the console.

from bpdefs import (
    EXEC, ASSIGN, IF, RETURN, WHILE, WAIT, SETCONTROL, SHOW, LOG,
    DataDict
)

steps = [
    EXEC(0, source="import os, json, time"),

    # Paths
    ASSIGN("cmd_path", exp="'/home/licor/apps/dynamic/remote_cmd.json'"),
    ASSIGN("ack_path", exp="'/home/licor/apps/dynamic/remote_ack.json'"),

    # Defaults
    ASSIGN("default_wait_s", exp="10"),
    ASSIGN("default_co2_tol", exp="20"),   # ppm
    ASSIGN("default_rh_tol", exp="2.0"),   # %RH
    ASSIGN("default_t_tol", exp="0.5"),    # °C

    # Tracked measurements for waits + ack
    ASSIGN("co2r_meas", dd=DataDict("CO2_r", "Meas"), track=True),
    ASSIGN("co2s_meas", dd=DataDict("CO2_s", "Meas"), track=True),
    ASSIGN("h2or_meas", dd=DataDict("H2O_r", "Meas"), track=True),
    ASSIGN("h2os_meas", dd=DataDict("H2O_s", "Meas"), track=True),
    ASSIGN("tch_meas",  dd=DataDict("Tchamber", "Meas"), track=True),  # measured Tair
    ASSIGN("tleaf_meas", dd=DataDict("Tleaf", "Meas"), track=True),
    ASSIGN("ppfd_in",   dd=DataDict("PPFD_in", "Meas"), track=True),
    ASSIGN("rhcham_meas", dd=DataDict("RHcham", "GasEx"), track=True),

    SHOW(string="'RemoteEnvMeasure running. Waiting for command file: {}'.format(cmd_path)"),

    WHILE("True", steps=(

        # Wait for command file
        WAIT(event="os.path.exists(cmd_path)"),

        # Read JSON + remove file
        EXEC(0, source=
            "cmd=None\n"
            "err=None\n"
            "try:\n"
            "  with open(cmd_path,'r') as f:\n"
            "    cmd=json.load(f)\n"
            "except Exception as e:\n"
            "  err=str(e)\n"
            "  cmd={'action':'error','error':err}\n"
            "try:\n"
            "  os.remove(cmd_path)\n"
            "except Exception:\n"
            "  pass\n"
        ),

        SHOW(string="'CMD: {}'.format(cmd)"),

        # Stop command
        IF("str(cmd.get('action','')).lower() in ('stop','quit','exit')", steps=(
            SHOW(string="'Stopping RemoteEnvMeasure.'"),
            RETURN(),
        )),

        # -----------------------------
        # Controller On/Off handling
        # -----------------------------
        # The LI-6800 uses On/Off controllers for several environment systems.
        # If you set a setpoint while the controller is Off, it won't regulate.
        #
        # You can explicitly force controller states with keys like:
        #   flow_on, h2o_on, temp_on, co2_on, fan_on, pressure_on (bool/int)
        # Otherwise, we auto-enable controllers when their related setpoints are present.

        # Pull explicit controller flags (may be None)
        ASSIGN("flow_on", exp="cmd.get('flow_on', None)"),
        ASSIGN("h2o_on", exp="cmd.get('h2o_on', None)"),
        ASSIGN("temp_on", exp="cmd.get('temp_on', None)"),
        ASSIGN("co2_on", exp="cmd.get('co2_on', None)"),
        ASSIGN("fan_on", exp="cmd.get('fan_on', None)"),
        ASSIGN("pressure_on", exp="cmd.get('pressure_on', None)"),

        # Determine if setpoints imply controller should be on
        ASSIGN("needs_flow",
               exp="any(k in cmd for k in ('flow','flow_percent','co2_r','co2_s','pressure','tair','tleaf','txchg','rh_air','sd_air','vpd_leaf','h2o_r','h2o_s','h2o_pct','humidifier_pct','desiccant_pct'))"),
        ASSIGN("needs_h2o",
               exp="any(k in cmd for k in ('rh_air','sd_air','vpd_leaf','h2o_r','h2o_s','h2o_pct','humidifier_pct','desiccant_pct'))"),
        ASSIGN("needs_temp",
               exp="any(k in cmd for k in ('tair','tleaf','txchg'))"),
        ASSIGN("needs_co2",
               exp="any(k in cmd for k in ('co2_r','co2_s'))"),
        ASSIGN("needs_fan",
               exp="any(k in cmd for k in ('fan_rpm',))"),
        ASSIGN("needs_pressure",
               exp="any(k in cmd for k in ('pressure',))"),

        # Apply controller flags: explicit value wins; else auto-enable if needed
        IF("flow_on is not None", steps=(
            SETCONTROL("Flow On/Off", "int(flow_on)", "int"),
        )),
        IF("flow_on is None and needs_flow", steps=(
            SETCONTROL("Flow On/Off", "1", "int"),
        )),

        IF("h2o_on is not None", steps=(
            SETCONTROL("H2O_On/Off", "int(h2o_on)", "int"),
        )),
        IF("h2o_on is None and needs_h2o", steps=(
            SETCONTROL("H2O_On/Off", "1", "int"),
        )),

        IF("temp_on is not None", steps=(
            SETCONTROL("Temp_On/Off", "int(temp_on)", "int"),
        )),
        IF("temp_on is None and needs_temp", steps=(
            SETCONTROL("Temp_On/Off", "1", "int"),
        )),

        IF("co2_on is not None", steps=(
            SETCONTROL("CO2 On/Off", "int(co2_on)", "int"),
        )),
        IF("co2_on is None and needs_co2", steps=(
            SETCONTROL("CO2 On/Off", "1", "int"),
        )),

        IF("fan_on is not None", steps=(
            SETCONTROL("Fan On/Off", "int(fan_on)", "int"),
        )),
        IF("fan_on is None and needs_fan", steps=(
            SETCONTROL("Fan On/Off", "1", "int"),
        )),

        IF("pressure_on is not None", steps=(
            SETCONTROL("Pressure_On/Off", "int(pressure_on)", "int"),
        )),
        IF("pressure_on is None and needs_pressure", steps=(
            SETCONTROL("Pressure_On/Off", "1", "int"),
        )),

        # -----------------------------
        # Apply setpoints (if present)
        # -----------------------------
        # CO2
        ASSIGN("sp_co2r", exp="cmd.get('co2_r', None)"),
        IF("sp_co2r is not None", steps=(SETCONTROL("CO2_r", "sp_co2r", "float"),)),

        ASSIGN("sp_co2s", exp="cmd.get('co2_s', None)"),
        IF("sp_co2s is not None", steps=(SETCONTROL("CO2_s", "sp_co2s", "float"),)),

        # Light (pick any you want)
        ASSIGN("sp_qin", exp="cmd.get('qin', None)"),
        IF("sp_qin is not None", steps=(SETCONTROL("Qin", "sp_qin", "float"),)),

        ASSIGN("sp_qhead", exp="cmd.get('q_head', None)"),
        IF("sp_qhead is not None", steps=(SETCONTROL("Q_Head", "sp_qhead", "float"),)),

        ASSIGN("sp_qcon", exp="cmd.get('q_console', None)"),
        IF("sp_qcon is not None", steps=(SETCONTROL("Q_Console", "sp_qcon", "float"),)),

        ASSIGN("sp_qall", exp="cmd.get('q_all', None)"),
        IF("sp_qall is not None", steps=(SETCONTROL("Q_All", "sp_qall", "float"),)),

        # Flow
        ASSIGN("sp_flow", exp="cmd.get('flow', None)"),
        IF("sp_flow is not None", steps=(SETCONTROL("Flow", "sp_flow", "float"),)),

        ASSIGN("sp_flowpct", exp="cmd.get('flow_percent', None)"),
        IF("sp_flowpct is not None", steps=(SETCONTROL("Flow_%", "sp_flowpct", "float"),)),

        # Temperature
        ASSIGN("sp_tair", exp="cmd.get('tair', None)"),
        IF("sp_tair is not None", steps=(SETCONTROL("Tair", "sp_tair", "float"),)),

        ASSIGN("sp_tleaf", exp="cmd.get('tleaf', None)"),
        IF("sp_tleaf is not None", steps=(SETCONTROL("Tleaf", "sp_tleaf", "float"),)),

        ASSIGN("sp_txchg", exp="cmd.get('txchg', None)"),
        IF("sp_txchg is not None", steps=(SETCONTROL("Txchg", "sp_txchg", "float"),)),

        # H2O / humidity (choose ONE target method typically)
        ASSIGN("sp_rh", exp="cmd.get('rh_air', None)"),
        IF("sp_rh is not None", steps=(SETCONTROL("RH_air", "sp_rh", "float"),)),

        ASSIGN("sp_sd", exp="cmd.get('sd_air', None)"),
        IF("sp_sd is not None", steps=(SETCONTROL("SD_air", "sp_sd", "float"),)),

        ASSIGN("sp_vpd", exp="cmd.get('vpd_leaf', None)"),
        IF("sp_vpd is not None", steps=(SETCONTROL("VPD_leaf", "sp_vpd", "float"),)),

        ASSIGN("sp_h2or", exp="cmd.get('h2o_r', None)"),
        IF("sp_h2or is not None", steps=(SETCONTROL("H2O_r", "sp_h2or", "float"),)),

        ASSIGN("sp_h2os", exp="cmd.get('h2o_s', None)"),
        IF("sp_h2os is not None", steps=(SETCONTROL("H2O_s", "sp_h2os", "float"),)),

        ASSIGN("sp_h2opct", exp="cmd.get('h2o_pct', None)"),
        IF("sp_h2opct is not None", steps=(SETCONTROL("H2O_%", "sp_h2opct", "float"),)),

        ASSIGN("sp_humid", exp="cmd.get('humidifier_pct', None)"),
        IF("sp_humid is not None", steps=(SETCONTROL("Humidifier_%", "sp_humid", "float"),)),

        ASSIGN("sp_des", exp="cmd.get('desiccant_pct', None)"),
        IF("sp_des is not None", steps=(SETCONTROL("Desiccant_%", "sp_des", "float"),)),

        # Fan & Pressure
        ASSIGN("sp_fan", exp="cmd.get('fan_rpm', None)"),
        IF("sp_fan is not None", steps=(SETCONTROL("Fan_rpm", "sp_fan", "float"),)),

        ASSIGN("sp_p", exp="cmd.get('pressure', None)"),
        IF("sp_p is not None", steps=(SETCONTROL("Pressure", "sp_p", "float"),)),

        # -----------------------------
        # Optional waits (stabilization)
        # -----------------------------
        # CO2 wait (reference)
        ASSIGN("co2_tol", exp="cmd.get('co2_tol', default_co2_tol)"),
        IF("sp_co2r is not None and cmd.get('wait_for_co2', False)", steps=(
            SHOW(string="'Waiting for CO2_r to reach target (±{} ppm)...'.format(co2_tol)"),
            WAIT(event="abs(co2r_meas - sp_co2r) < co2_tol"),
        )),

        # RH wait (uses RHcham; may not update in some modes)
        ASSIGN("rh_tol", exp="cmd.get('rh_tol', default_rh_tol)"),
        IF("sp_rh is not None and cmd.get('wait_for_rh', False)", steps=(
            SHOW(string="'Waiting for RHcham to reach target (±{} %RH)...'.format(rh_tol)"),
            WAIT(event="abs(rhcham_meas - sp_rh) < rh_tol"),
        )),

        # Temperature wait (air)
        ASSIGN("t_tol", exp="cmd.get('t_tol', default_t_tol)"),
        IF("sp_tair is not None and cmd.get('wait_for_tair', False)", steps=(
            SHOW(string="'Waiting for Tchamber to reach target (±{} C)...'.format(t_tol)"),
            WAIT(event="abs(tch_meas - sp_tair) < t_tol"),
        )),

        # Extra fixed wait
        ASSIGN("wait_s", exp="cmd.get('wait_s', default_wait_s)"),
        IF("wait_s and wait_s > 0", steps=(
            SHOW(string="'Extra wait: {} s'.format(wait_s)"),
            WAIT(dur="wait_s", units="Seconds"),
        )),

        # Log one record (requires an open log file)
        IF("cmd.get('log', True)", steps=(
            SHOW(string="'Logging one record (LOG())...'"),
            LOG(),
        )),

        # Ack file
        EXEC(0, source=
            "ack={\n"
            " 'ts': time.time(),\n"
            " 'cmd_id': cmd.get('cmd_id', None),\n"
            " 'cmd': cmd,\n"
            " 'meas': {\n"
            "   'CO2_r': co2r_meas,\n"
            "   'CO2_s': co2s_meas,\n"
            "   'H2O_r': h2or_meas,\n"
            "   'H2O_s': h2os_meas,\n"
            "   'Tchamber': tch_meas,\n"
            "   'Tleaf': tleaf_meas,\n"
            "   'RHcham': rhcham_meas,\n"
            "   'PPFD_in': ppfd_in,\n"
            " },\n"
            " 'error': cmd.get('error', None)\n"
            "}\n"
            "with open(ack_path,'w') as f:\n"
            "  f.write(json.dumps(ack, indent=2))\n"
        ),

        SHOW(string="'Done. Waiting for next command.'"),
    )),
]