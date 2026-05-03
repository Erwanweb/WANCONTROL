# WanCheck plugin (simple dual WAN)

"""
<plugin key="WanCheck" name="WAN Check Fiber / Starlink" author="Erwan" version="1.0.0">
    <params>
        <param field="Mode1" label="Ping MAIN (Fiber)" width="200px" required="true" default="8.8.8.8"/>
        <param field="Mode2" label="Ping STARLINK" width="200px" required="true" default="1.1.1.1"/>
        <param field="Mode3" label="Interval (sec)" width="75px" required="true" default="30"/>
    </params>
</plugin>
"""

import Domoticz
import subprocess
import time
import re


class BasePlugin:
    def __init__(self):
        self.interval = 30
        self.last = 0

    def onStart(self):
        Domoticz.Log("WAN Check started")

        self.main_ip = Parameters["Mode1"]
        self.star_ip = Parameters["Mode2"]
        self.interval = int(Parameters["Mode3"])

        create_device(1, "MAIN Internet")
        create_device(2, "MAIN Latency")
        create_device(3, "STARLINK Internet")
        create_device(4, "STARLINK Latency")

        Domoticz.Heartbeat(10)

    def onHeartbeat(self):
        if time.time() - self.last < self.interval:
            return

        self.last = time.time()

        self.check(self.main_ip, 1, 2, "MAIN")
        self.check(self.star_ip, 3, 4, "STARLINK")

    def check(self, ip, unit_status, unit_latency, name):
        ok, latency = ping(ip)

        if ok:
            update_switch(unit_status, True)
            update_value(unit_latency, latency)
            Domoticz.Log(f"{name} OK {latency} ms")
        else:
            update_switch(unit_status, False)
            update_value(unit_latency, 0)
            Domoticz.Log(f"{name} DOWN")


def ping(ip):
    try:
        cmd = ["ping", "-c", "2", "-W", "2", ip]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)

        if result.returncode != 0:
            return False, 0

        match = re.search(r"= [\d.]+/([\d.]+)/", result.stdout)
        if match:
            return True, round(float(match.group(1)), 1)

        return True, 0

    except:
        return False, 0


def create_device(unit, name):
    if unit not in Devices:
        Domoticz.Device(
            Name=name,
            Unit=unit,
            Type=244,
            Subtype=73,
            Used=1
        ).Create()


def update_switch(unit, state):
    n = 1 if state else 0
    s = "On" if state else "Off"
    if Devices[unit].nValue != n:
        Devices[unit].Update(nValue=n, sValue=s)


def update_value(unit, value):
    Devices[unit].Update(nValue=0, sValue=str(value))


global _plugin
_plugin = BasePlugin()


def onStart():
    _plugin.onStart()


def onHeartbeat():
    _plugin.onHeartbeat()
