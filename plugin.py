# WanCheck plugin (simple dual WAN)

"""
<plugin key="WanCheck" name="WAN Check Fiber / Starlink" author="Erwan" version="1.0.0">
    <params>
        <param field="Username" label="ID/UserKey for Telegram,Pushover (0 if none)" width="400px" required="false" default="0,0"/>
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
import urllib.request as request
import urllib.parse as parse
import json


class BasePlugin:
    def __init__(self):
        self.interval = 30
        self.last = 0
        self.last_main_state = None
        self.last_star_state = None
        # notification IDs
        self.TelegramID = "0"
        self.PushoverUserKey = "0"

    def onStart(self):
        Domoticz.Log("WAN Check started")

        self.main_ip = Parameters["Mode1"]
        self.star_ip = Parameters["Mode2"]
        self.interval = int(Parameters["Mode3"])

        create_switch(1, "MAIN Internet")
        create_latency(2, "MAIN Latency")
        create_switch(3, "STARLINK Internet")
        create_latency(4, "STARLINK Latency")

        params = parseCSV(Parameters["Username"])
        if len(params) == 2:
            self.TelegramID = CheckParam("Telegram ID", params[0], "0")
            self.PushoverUserKey = CheckParam("Pushover UserKey", params[1], "0")
        else:
            Domoticz.Error("Error reading ID/UserKey parameters. Expected format: TelegramID,PushoverUserKey")

        Domoticz.Heartbeat(10)

    def onHeartbeat(self):
        if time.time() - self.last < self.interval:
            return
    
        self.last = time.time()
    
        self.check_all()

    def check_all(self):
        main_ok, main_latency = ping(self.main_ip)
        star_ok, star_latency = ping(self.star_ip)
        
        update_switch(1, main_ok)
        update_value(2, main_latency if main_ok else 0)
        
        update_switch(3, star_ok)
        update_value(4, star_latency if star_ok else 0)
        
        Domoticz.Log("MAIN {} - {} ms / STARLINK {} - {} ms".format(
            "OK" if main_ok else "DOWN",
            main_latency,
            "OK" if star_ok else "DOWN",
            star_latency
        ))
        
        # premier passage : pas de notification
        if self.last_main_state is None:
            self.last_main_state = main_ok
            self.last_star_state = star_ok
            return
        
        # notification uniquement si changement d'état
        if main_ok != self.last_main_state or star_ok != self.last_star_state:
            msg, priority = build_message(main_ok, star_ok, main_latency, star_latency)
            Send_Notifications(self, msg, priority)
        
        self.last_main_state = main_ok
        self.last_star_state = star_ok

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

def create_switch(unit, name):
    if unit not in Devices:
        Domoticz.Device(
            Name=name,
            Unit=unit,
            Type=244,
            Subtype=73,
            Used=1
        ).Create()


def create_latency(unit, name):
    if unit not in Devices:
        Domoticz.Device(
            Name=name,
            Unit=unit,
            Type=243,
            Subtype=31,
            Used=1
        ).Create()


def update_switch(unit, state):
    n = 1 if state else 0
    s = "On" if state else "Off"
    if Devices[unit].nValue != n:
        Devices[unit].Update(nValue=n, sValue=s)


def update_value(unit, value):
    Devices[unit].Update(nValue=0, sValue=str(value))


# Plugin notification functions ---------------------------------------------------

def build_message(main_ok, star_ok, main_latency, star_latency):
    main_txt = "🌐 MAIN fibre Movistar OK ✅ {} ms".format(main_latency) if main_ok else "❌ MAIN fibre Movistar DOWN"
    star_txt = "📡 STARLINK OK ✅ {} ms".format(star_latency) if star_ok else "❌ STARLINK DOWN"

    if main_ok and star_ok:
        title = "✅ Internet rétabli sur les 2 WAN"
        priority = 0
    elif main_ok and not star_ok:
        title = "⚠️ Starlink DOWN, fibre Movistar OK"
        priority = 0
    elif not main_ok and star_ok:
        title = "⚠️ Fibre Movistar DOWN, Starlink OK"
        priority = 0
    else:
        title = "🚨 ALERTE : plus aucun Internet"
        priority = 1

    return "{}\n{}\n{}".format(title, main_txt, star_txt), priority
    
def Send_Notifications(self, message, priority=0):
    if self.TelegramID != "0" and self.TelegramID != "":
        TelegramAPI(self.TelegramID, message)

    if self.PushoverUserKey != "0" and self.PushoverUserKey != "":
        PushoverAPI(self.PushoverUserKey, message, priority)

def TelegramAPI(chat_id, message):
    resultJson = None
    # BotID pour One by Ronelabs
    url = "https://api.telegram.org/bot8284753746:AAFIny-n6t2VtevOU-AEVU9UzrSrR6Y_SvM/sendMessage?chat_id={}&text={}".format(
        chat_id,
        parse.quote(message)
    )

    Domoticz.Debug("Calling Telegram API")

    try:
        req = request.Request(url)
        response = request.urlopen(req, timeout=10)

        if response.status == 200:
            resultJson = json.loads(response.read().decode("utf-8"))
            if resultJson.get("ok") != True:
                Domoticz.Error("Telegram API error: {}".format(resultJson))
                resultJson = None
        else:
            Domoticz.Error("Telegram API HTTP error = {}".format(response.status))

    except Exception as e:
        Domoticz.Error("Telegram API exception: {}".format(str(e)))

    return resultJson

def PushoverAPI(user_key, message, priority=0):
    try:
        data = parse.urlencode({
            "token": "akkhoxtbzgvcj7z5tkt1nsaum6o8gs", # token ELE pour One by Ronelabs
            "user": user_key,
            "title": "ONE By Ronelabs",
            "message": message,
            "priority": priority
        }).encode("utf-8")

        req = request.Request("https://api.pushover.net/1/messages.json", data=data)
        response = request.urlopen(req, timeout=10)

        if response.status != 200:
            Domoticz.Error("Pushover API HTTP error = {}".format(response.status))

    except Exception as e:
        Domoticz.Error("Pushover API exception: {}".format(str(e)))

# Plugin  ---------------------------------------------------

global _plugin
_plugin = BasePlugin()


def onStart():
    _plugin.onStart()


def onHeartbeat():
    _plugin.onHeartbeat()

# Plugin utility functions ---------------------------------------------------

def parseCSV(strCSV):
    listvals = []
    for value in strCSV.split(","):
        listvals.append(value.strip())
    return listvals


def CheckParam(name, value, default):
    try:
        if value is None or value == "":
            Domoticz.Error("Parameter '{}' is empty, using default '{}'".format(name, default))
            return default
        return value
    except Exception as e:
        Domoticz.Error("Error checking parameter '{}': {}".format(name, str(e)))
        return default
