# WanCheck plugin (simple dual WAN)

"""
<plugin key="WanCheck" name="WAN Check Fiber / Starlink" author="Erwan" version="1.0.0">
    <params>
        <param field="Username" label="ID/UserKey for Telegram,Pushover (0 if none)" width="600px" required="false" default="0,0"/>
        <param field="Mode1" label="Ping MAIN (Fiber)" width="200px" required="true" default="8.8.8.8"/>
        <param field="Mode2" label="Ping STARLINK" width="200px" required="true" default="1.1.1.1"/>
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
        self.interval = 20
        self.latency_interval = 300
        self.pending_count = 0
        self.last = 0
        self.last_main_state = None
        self.last_star_state = None
        self.pending_main_state = None
        self.pending_star_state = None
        self.last_latency_update = 0
        self.lat_main_hist = []
        self.lat_star_hist = []
        # notification IDs
        self.TelegramToken = ""
        self.PushoverToken = ""
        self.TelegramID = "0"
        self.PushoverUserKey = "0"

    def onStart(self):
        Domoticz.Log("WAN Checker plugin starting")

        self.main_ip = Parameters["Mode1"]
        self.star_ip = Parameters["Mode2"]

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

        self.load_secrets()

        Domoticz.Heartbeat(20)

        #Domoticz.Status("WAN Checker plugin started")

    def onHeartbeat(self):
        if time.time() - self.last < self.interval:
            return
    
        self.last = time.time()
    
        self.check_all()

    def check_all(self):
        main_ok, main_latency = check_route(self.main_ip, "192.168.1.1")
        star_ok, star_latency = check_route(self.star_ip, "192.168.2.1")

        # historique latence
        if main_ok:
            self.lat_main_hist.append(main_latency)
        if star_ok:
            self.lat_star_hist.append(star_latency)
        
        # limite taille historique (15 mesures ≈ 5 min avec interval 20s)
        if len(self.lat_main_hist) > 15:
            self.lat_main_hist.pop(0)
        if len(self.lat_star_hist) > 15:
            self.lat_star_hist.pop(0)
        
        # toujours mettre à jour les ON/OFF
        update_switch(1, main_ok)
        update_switch(3, star_ok)
        
        # latence mise à jour seulement toutes les 5 minutes
        if time.time() - self.last_latency_update >= self.latency_interval:

            avg_main = sum(self.lat_main_hist) / len(self.lat_main_hist) if self.lat_main_hist else 0
            avg_star = sum(self.lat_star_hist) / len(self.lat_star_hist) if self.lat_star_hist else 0
        
            update_value(2, round(avg_main, 1))
            update_value(4, round(avg_star, 1))
        
            self.last_latency_update = time.time()
        
        Domoticz.Log("MAIN {} - {} ms / STARLINK {} - {} ms".format(
            "OK" if main_ok else "DOWN",
            main_latency,
            "OK" if star_ok else "DOWN",
            star_latency
        ))
        
        # notification
        # premier passage : pas de notification
        if self.last_main_state is None:
            self.last_main_state = main_ok
            self.last_star_state = star_ok
            return

        # changement détecté
        if main_ok != self.last_main_state or star_ok != self.last_star_state:

            # nouveau changement candidat
            if self.pending_main_state != main_ok or self.pending_star_state != star_ok:
                self.pending_main_state = main_ok
                self.pending_star_state = star_ok
                self.pending_count = 1
                Domoticz.Log("WAN change detected, waiting confirmation")
                return

            # même changement confirmé sur heartbeat suivant
            self.pending_count += 1
            Domoticz.Log("WAN change confirmation count: {}".format(self.pending_count))

            # alerte au 3e heartbeat identique
            if self.pending_count >= 4:
                title, msg, priority = build_message(main_ok, star_ok, main_latency, star_latency)
                Send_Notifications(self, title, msg, priority)
                Domoticz.Log("WAN STATUS CHANGE confirmed - sending notification")

                self.last_main_state = main_ok
                self.last_star_state = star_ok
                self.pending_main_state = None
                self.pending_star_state = None
                self.pending_count = 0

                # si coupure confirmée, mettre la latence à 0 immédiatement
                if not main_ok:
                    self.lat_main_hist = []
                    update_value(2, 0)

                if not star_ok:
                    self.lat_star_hist = []
                    update_value(4, 0)

        else:
            self.pending_main_state = None
            self.pending_star_state = None
            self.pending_count = 0


# Plugin notification functions (load Secrets) ---------------------------------------------------
    def load_secrets(self):
        try:
            with open("/home/pi/domoticz/plugins/WANCONTROL/secrets.txt", "r") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("telegram_token="):
                        self.TelegramToken = line.split("=", 1)[1].strip()
                    elif line.startswith("pushover_token="):
                        self.PushoverToken = line.split("=", 1)[1].strip()
    
            Domoticz.Log("WAN Check secrets loaded")
    
        except Exception as e:
            Domoticz.Error("Error loading secrets.txt: {}".format(str(e)))

def check_route(ip, expected_gateway):
    try:
        # ping pour récupérer la latence
        ping_cmd = ["ping", "-c", "2", "-W", "2", ip]
        ping_result = subprocess.run(ping_cmd, stdout=subprocess.PIPE, text=True)

        if ping_result.returncode != 0:
            return False, 0

        latency = 0
        match = re.search(r"= [\d.]+/([\d.]+)/", ping_result.stdout)
        if match:
            latency = round(float(match.group(1)), 1)

        # traceroute court pour vérifier le WAN utilisé
        trace_cmd = ["traceroute", "-m", "3", ip]
        trace_result = subprocess.run(trace_cmd, stdout=subprocess.PIPE, text=True, timeout=10)

        if expected_gateway in trace_result.stdout:
            return True, latency

        return False, 0

    except Exception as e:
        Domoticz.Error("Route check error: {}".format(str(e)))
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
        title = "WAN ALERT - ✅ Internet rétabli sur les 2 WAN"
        priority = 1
    elif main_ok and not star_ok:
        title = "WAN ALERT - ⚠️ Starlink DOWN, fibre Movistar OK"
        priority = 1
    elif not main_ok and star_ok:
        title = "WAN ALERT - ⚠️ Fibre Movistar DOWN, Starlink OK"
        priority = 1
    else:
        title = "WAN ALERT - 🚨 PLUS D'INTERNET"
        priority = 2

    return title, "{}\n{}".format(main_txt, star_txt), priority
    
def Send_Notifications(self, title, message, priority=0):
    if self.TelegramID != "0" and self.TelegramID != "" and self.TelegramToken != "":
        TelegramAPI(self.TelegramID, self.TelegramToken, title + "\n" + message)

    if self.PushoverUserKey != "0" and self.PushoverUserKey != "" and self.PushoverToken != "":
        PushoverAPI(self.PushoverUserKey, self.PushoverToken, title, message, priority)

def TelegramAPI(chat_id, token, message):
    resultJson = None

    url = "https://api.telegram.org/bot{}/sendMessage?chat_id={}&text={}".format(
        token,
        chat_id,
        parse.quote(message)
    )

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

def PushoverAPI(user_key, token, title, message, priority=0):
    try:
        payload = {
            "token": token,
            "user": user_key,
            "title": title,
            "message": message,
            "priority": priority
        }

        if priority == 2:
            payload["retry"] = 60
            payload["expire"] = 3600

        data = parse.urlencode(payload).encode("utf-8")

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
