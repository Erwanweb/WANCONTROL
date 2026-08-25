# WanCheck plugin (dual WAN, FlowBalancing or Backup)

"""
<plugin key="WanCheck" name="WAN Check WAN1/WAN2" author="Erwan" version="2.0.0">
    <params>
        <param field="Username" label="ID/UserKey for Telegram,Pushover (0 if none)" width="600px" required="false" default="0,0"/>
        <param field="Mode1" label="Ping WAN1 (MAIN)" width="200px" required="true" default="8.8.8.8"/>
        <param field="Mode2" label="Ping WAN2" width="200px" required="true" default="1.1.1.1"/>
        <param field="Mode3" label="WAN2 Mode" width="200px">
            <options>
                <option label="Flow Balancing" value="FlowBalancing" default="true"/>
                <option label="Backup (alimentation controlee)" value="Backup"/>
            </options>
        </param>
        <param field="Mode4" label="Backup: idx prise WAN2" width="100px" required="false" default="0"/>
        <param field="Mode5" label="Backup: heure test quotidien (HH:MM)" width="100px" required="false" default="04:00"/>
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
        self.valid_main_state = None
        self.valid_star_state = None
        # notification IDs
        self.TelegramToken = ""
        self.PushoverToken = ""
        self.TelegramID = "0"
        self.PushoverUserKey = "0"
        # Delais de demarrage
        self.start_time = time.time()
        self.startup_silence = 120
        # Backup mode (alimentation controlee du WAN2)
        self.wan2_mode = "FlowBalancing"
        self.wan2_label = "WAN2"
        self.wan2_switch_idx = 0
        self.daily_test_hour = 4
        self.daily_test_minute = 0
        self.wan2_active_reason = None   # None, "backup" ou "dailytest"
        self.wan2_power_since = None
        self.wan1_ok_since = None
        self.wan2_warmup = 120           # 2 minutes avant de tester apres mise sous tension
        self.wan1_stable_cutoff = 300    # 5 minutes de MAIN ok avant de couper le WAN2
        self.last_daily_test_date = ""
        self.switch_check_interval = 300  # verification periodique etat reel vs attendu de la prise
        self.last_switch_check = 0
        # file d'attente de retry pour les notifications non delivrees (ex: coupure au moment de l'envoi)
        self.notification_queue = []
        self.notification_retry_interval = 30
        self.notification_max_age = 3600
        self.notification_max_queue = 20
        self.last_notification_retry = 0

    def onStart(self):
        Domoticz.Log("WAN Checker plugin starting")

        self.main_ip = Parameters["Mode1"]
        self.star_ip = Parameters["Mode2"]

        self.wan2_mode = Parameters.get("Mode3", "FlowBalancing") if hasattr(Parameters, "get") else Parameters["Mode3"]

        try:
            self.wan2_switch_idx = int(Parameters["Mode4"])
        except (ValueError, KeyError):
            self.wan2_switch_idx = 0

        try:
            hh, mm = Parameters["Mode5"].split(":")
            self.daily_test_hour = int(hh)
            self.daily_test_minute = int(mm)
        except (ValueError, KeyError, AttributeError):
            Domoticz.Error("Invalid daily test hour format (expected HH:MM), using 04:00")
            self.daily_test_hour = 4
            self.daily_test_minute = 0

        self.wan2_label = "WAN2 (Backup)" if self.wan2_mode == "Backup" else "WAN2"

        create_switch(1, "WAN1 (MAIN)")
        create_latency(2, "WAN1 (MAIN) Latency")
        create_switch(3, self.wan2_label)
        create_latency(4, "{} Latency".format(self.wan2_label))

        params = parseCSV(Parameters["Username"])
        if len(params) == 2:
            self.TelegramID = CheckParam("Telegram ID", params[0], "0")
            self.PushoverUserKey = CheckParam("Pushover UserKey", params[1], "0")
        else:
            Domoticz.Error("Error reading ID/UserKey parameters. Expected format: TelegramID,PushoverUserKey")

        self.load_secrets()

        Domoticz.Heartbeat(20)

        self.ensure_user_variables()
        self.load_last_state()
        self.load_backup_state()

        # reprendre l'etat confirme et le chrono "MAIN stable" depuis la derniere valeur connue,
        # sinon ils restent a None apres un redemarrage tant qu'aucun changement n'est detecte
        self.valid_main_state = self.last_main_state
        self.valid_star_state = self.last_star_state
        self.wan1_ok_since = time.time() if self.last_main_state else None

        if self.wan2_mode == "Backup":
            if not self.wan2_switch_idx:
                Domoticz.Error("Backup mode selected but no WAN2 switch idx configured (Mode4)")
            else:
                if get_switch_state(self.wan2_switch_idx):
                    self.wan2_active_reason = "backup"
                    self.wan2_power_since = time.time() - self.wan2_warmup
                    Domoticz.Log("WAN2 switch found ON at startup, resuming backup monitoring")
                Domoticz.Log("WAN Checker running in Backup mode (switch idx {}, daily test {:02d}:{:02d})".format(
                    self.wan2_switch_idx, self.daily_test_hour, self.daily_test_minute
                ))
        else:
            Domoticz.Log("WAN Checker running in Flow Balancing mode")

        #Domoticz.Status("WAN Checker plugin started")

    def onHeartbeat(self):
        if time.time() - self.last < self.interval:
            return

        self.last = time.time()

        self.check_all()

    def check_all(self):
        self.retry_pending_notifications()

        main_ok, main_latency = check_route(self.main_ip, "192.168.1.1")

        if self.wan2_mode == "Backup":
            star_ok, star_latency = self.manage_backup_power()
        else:
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
        # update_switch(1, main_ok)
        # update_switch(3, star_ok)

        # latence mise à jour seulement toutes les 5 minutes
        if time.time() - self.last_latency_update >= self.latency_interval:

            avg_main = sum(self.lat_main_hist) / len(self.lat_main_hist) if self.lat_main_hist else 0
            avg_star = sum(self.lat_star_hist) / len(self.lat_star_hist) if self.lat_star_hist else 0

            update_value(2, round(avg_main, 1))
            update_value(4, round(avg_star, 1))

            self.last_latency_update = time.time()

        Domoticz.Log("WAN1 (MAIN) {} - {} ms / {} {} - {} ms".format(
            "OK" if main_ok else "DOWN",
            main_latency,
            self.wan2_label,
            "OK" if star_ok else "DOWN",
            star_latency
        ))

        # notification
        # premier passage : pas de notification
        if self.last_main_state is None:
            self.last_main_state = main_ok
            self.last_star_state = star_ok

            self.valid_main_state = main_ok
            self.valid_star_state = star_ok
            self.wan1_ok_since = time.time() if main_ok else None

            update_switch(1, main_ok)
            update_switch(3, star_ok)

            self.save_last_state()
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
            if self.pending_count < 4:
                return

            # alerte au 4e heartbeat identique
            if self.pending_count >= 4:

                title, msg, priority = build_message(main_ok, star_ok, main_latency, star_latency, self.wan2_label)

                # anti spam reboot
                if time.time() - self.start_time >= self.startup_silence:
                    Send_Notifications(self, title, msg, priority)

                Domoticz.Log("WAN STATUS CHANGE confirmed")

                # état validé
                self.valid_main_state = main_ok
                self.valid_star_state = star_ok
                self.wan1_ok_since = time.time() if main_ok else None

                update_switch(1, self.valid_main_state)
                update_switch(3, self.valid_star_state)

                self.last_main_state = main_ok
                self.last_star_state = star_ok
                self.save_last_state()

                self.pending_main_state = None
                self.pending_star_state = None
                self.pending_count = 0

                # reset latence si down confirmé
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


# Backup mode (alimentation controlee du WAN2) ---------------------------------------------------
    def manage_backup_power(self):
        if not self.wan2_switch_idx:
            return False, 0

        now = time.time()
        lt = time.localtime(now)
        today_str = time.strftime("%Y-%m-%d", lt)

        self.check_switch_drift(now)

        # coupure du WAN2 des que le WAN1 (MAIN) est stable depuis 5 min
        if (self.wan2_active_reason == "backup"
                and self.wan1_ok_since is not None
                and (now - self.wan1_ok_since) >= self.wan1_stable_cutoff):
            self.power_wan2(False, "WAN1 (MAIN) stable depuis 5 min")

        # test quotidien planifie (seulement si WAN2 inactif et WAN1 (MAIN) ok)
        if (self.wan2_active_reason is None
                and self.valid_main_state
                and self.last_daily_test_date != today_str
                and lt.tm_hour == self.daily_test_hour
                and lt.tm_min == self.daily_test_minute):
            Domoticz.Log("Backup mode: starting daily {} test".format(self.wan2_label))
            self.power_wan2(True, "dailytest")

        # activation du backup : WAN1 (MAIN) confirme DOWN
        if self.wan2_active_reason is None and self.valid_main_state is False:
            Domoticz.Log("Backup mode: WAN1 (MAIN) down, powering {} on".format(self.wan2_label))
            self.power_wan2(True, "backup")

        if self.wan2_active_reason is None:
            return False, 0

        # attente 2 minutes apres mise sous tension avant de tester
        if now - self.wan2_power_since < self.wan2_warmup:
            return False, 0

        star_ok, star_latency = check_route(self.star_ip, "192.168.2.1")

        if self.wan2_active_reason == "dailytest":
            Domoticz.Log("Backup mode: daily {} test result = {}".format(self.wan2_label, "OK" if star_ok else "FAILED"))
            Send_Notifications(
                self,
                "WAN ALERT - Test quotidien {}".format(self.wan2_label),
                "{} {} ({} ms)".format(self.wan2_label, "OK" if star_ok else "FAILED", star_latency),
                1 if star_ok else 2
            )
            self.last_daily_test_date = today_str
            self.save_backup_state()
            self.power_wan2(False, "fin de test quotidien")

        return star_ok, star_latency

    def check_switch_drift(self, now):
        if now - self.last_switch_check < self.switch_check_interval:
            return

        self.last_switch_check = now

        expected_on = self.wan2_active_reason is not None
        actual_on = get_switch_state(self.wan2_switch_idx)

        if actual_on != expected_on:
            Domoticz.Error("{} power mismatch detected (actual={}, expected={}), correcting".format(
                self.wan2_label,
                "On" if actual_on else "Off",
                "On" if expected_on else "Off"
            ))
            try:
                switch_command(self.wan2_switch_idx, "On" if expected_on else "Off")
            except Exception as e:
                Domoticz.Error("Error correcting {} power: {}".format(self.wan2_label, str(e)))

    def power_wan2(self, on, reason):
        try:
            switch_command(self.wan2_switch_idx, "On" if on else "Off")
        except Exception as e:
            Domoticz.Error("Error switching {} power: {}".format(self.wan2_label, str(e)))
            return

        if on:
            self.wan2_active_reason = reason if reason in ("backup", "dailytest") else "backup"
            self.wan2_power_since = time.time()
            Domoticz.Log("{} power ON ({})".format(self.wan2_label, reason))
            if reason == "backup":
                Send_Notifications(self, "WAN ALERT - {} active".format(self.wan2_label),
                                    "WAN1 (MAIN) DOWN, {} mis sous tension".format(self.wan2_label), 1)
        else:
            was_active_for_backup = self.wan2_active_reason == "backup"
            self.wan2_active_reason = None
            self.wan2_power_since = None
            Domoticz.Log("{} power OFF ({})".format(self.wan2_label, reason))
            if was_active_for_backup:
                Send_Notifications(self, "WAN ALERT - {} coupe".format(self.wan2_label),
                                    "WAN1 (MAIN) OK depuis 5 min, {} coupe".format(self.wan2_label), 1)


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

# Users variable ---------------------------------------------------
    def ensure_user_variables(self):
        try:
            data = domoticz_api("type=command&param=getuservariables")
            existing = [v["Name"] for v in data.get("result", [])]

            if "WAN_STATE" not in existing:
                Domoticz.Log("Creating WAN_STATE variable")
                domoticz_api("type=command&param=adduservariable&vname=WAN_STATE&vtype=2&vvalue=0,0")

            if "WAN2_LASTTEST" not in existing:
                Domoticz.Log("Creating WAN2_LASTTEST variable")
                domoticz_api("type=command&param=adduservariable&vname=WAN2_LASTTEST&vtype=2&vvalue=")

        except Exception as e:
            Domoticz.Error("Error ensuring user variables: {}".format(str(e)))

    def load_last_state(self):
        try:
            data = domoticz_api("type=command&param=getuservariables")

            for var in data.get("result", []):
                if var.get("Name") == "WAN_STATE":
                    val = var.get("Value", "0,0")
                    main, star = val.split(",")

                    self.last_main_state = (main == "1")
                    self.last_star_state = (star == "1")

            Domoticz.Log("Loaded WAN state: WAN1={}, WAN2={}".format(
                self.last_main_state,
                self.last_star_state
            ))

        except Exception as e:
            Domoticz.Error("Error loading WAN state: {}".format(str(e)))
            self.last_main_state = None
            self.last_star_state = None

    def save_last_state(self):
        try:
            main_value = "1" if self.last_main_state else "0"
            star_value = "1" if self.last_star_state else "0"

            value = "{},{}".format(main_value, star_value)

            domoticz_api(
                "type=command&param=updateuservariable&vname=WAN_STATE&vtype=2&vvalue={}".format(value)
            )

        except Exception as e:
            Domoticz.Error("Error saving WAN state: {}".format(str(e)))

    def load_backup_state(self):
        try:
            data = domoticz_api("type=command&param=getuservariables")

            for var in data.get("result", []):
                if var.get("Name") == "WAN2_LASTTEST":
                    self.last_daily_test_date = var.get("Value", "")

            Domoticz.Log("Loaded last WAN2 daily test date: {}".format(self.last_daily_test_date))

        except Exception as e:
            Domoticz.Error("Error loading WAN2_LASTTEST: {}".format(str(e)))
            self.last_daily_test_date = ""

    def save_backup_state(self):
        try:
            domoticz_api(
                "type=command&param=updateuservariable&vname=WAN2_LASTTEST&vtype=2&vvalue={}".format(
                    self.last_daily_test_date
                )
            )
        except Exception as e:
            Domoticz.Error("Error saving WAN2_LASTTEST: {}".format(str(e)))

# Notification queue (retry si envoi impossible, ex: coupure pendant le basculement) --------------
    def queue_notification(self, title, message, priority, need_telegram, need_pushover):
        self.notification_queue.append({
            "title": title,
            "message": message,
            "priority": priority,
            "need_telegram": need_telegram,
            "need_pushover": need_pushover,
            "created": time.time(),
        })
        Domoticz.Error("Notification delivery failed, queued for retry: {}".format(title))

        if len(self.notification_queue) > self.notification_max_queue:
            dropped = self.notification_queue.pop(0)
            Domoticz.Error("Notification queue full, dropping oldest: {}".format(dropped["title"]))

    def retry_pending_notifications(self):
        if not self.notification_queue:
            return

        now = time.time()
        if now - self.last_notification_retry < self.notification_retry_interval:
            return

        self.last_notification_retry = now

        still_pending = []
        for item in self.notification_queue:
            if now - item["created"] > self.notification_max_age:
                Domoticz.Error("Dropping stale queued notification (>1h): {}".format(item["title"]))
                continue

            telegram_ok = not item["need_telegram"]
            pushover_ok = not item["need_pushover"]

            if item["need_telegram"]:
                telegram_ok = TelegramAPI(
                    self.TelegramID, self.TelegramToken, item["title"] + "\n" + item["message"]
                ) is not None

            if item["need_pushover"]:
                pushover_ok = PushoverAPI(
                    self.PushoverUserKey, self.PushoverToken, item["title"], item["message"], item["priority"]
                )

            if telegram_ok and pushover_ok:
                Domoticz.Log("Queued notification delivered: {}".format(item["title"]))
                continue

            item["need_telegram"] = not telegram_ok
            item["need_pushover"] = not pushover_ok
            still_pending.append(item)

        self.notification_queue = still_pending

# Other defs ---------------------------------------------------
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


def switch_command(idx, cmd):
    domoticz_api("type=command&param=switchlight&idx={}&switchcmd={}".format(idx, cmd))


def get_switch_state(idx):
    try:
        data = domoticz_api("type=command&param=getdevices&rid={}".format(idx))
        result = data.get("result")
        if result:
            return result[0].get("Status") == "On"
    except Exception as e:
        Domoticz.Error("Error reading switch {} state: {}".format(idx, str(e)))
    return False


# Plugin notification functions ---------------------------------------------------

def build_message(main_ok, star_ok, main_latency, star_latency, wan2_label="WAN2"):
    main_txt = "🌐 WAN1 (MAIN) OK ✅ {} ms".format(main_latency) if main_ok else "❌ WAN1 (MAIN) DOWN"
    star_txt = "📡 {} OK ✅ {} ms".format(wan2_label, star_latency) if star_ok else "❌ {} DOWN".format(wan2_label)

    if main_ok and star_ok:
        title = "WAN ALERT - ✅ Internet rétabli sur les 2 WAN"
        priority = 1
    elif main_ok and not star_ok:
        title = "WAN ALERT - ⚠️ {} DOWN, WAN1 (MAIN) OK".format(wan2_label)
        priority = 1
    elif not main_ok and star_ok:
        title = "WAN ALERT - ⚠️ WAN1 (MAIN) DOWN, {} OK".format(wan2_label)
        priority = 1
    else:
        title = "WAN ALERT - 🚨 PLUS D'INTERNET"
        priority = 2

    return title, "{}\n{}".format(main_txt, star_txt), priority

def Send_Notifications(self, title, message, priority=0):
    need_telegram = self.TelegramID != "0" and self.TelegramID != "" and self.TelegramToken != ""
    need_pushover = self.PushoverUserKey != "0" and self.PushoverUserKey != "" and self.PushoverToken != ""

    telegram_ok = True
    pushover_ok = True

    if need_telegram:
        telegram_ok = TelegramAPI(self.TelegramID, self.TelegramToken, title + "\n" + message) is not None

    if need_pushover:
        pushover_ok = PushoverAPI(self.PushoverUserKey, self.PushoverToken, title, message, priority)

    if (need_telegram and not telegram_ok) or (need_pushover and not pushover_ok):
        self.queue_notification(
            title, message, priority,
            need_telegram and not telegram_ok,
            need_pushover and not pushover_ok
        )

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
            return False

        return True

    except Exception as e:
        Domoticz.Error("Pushover API exception: {}".format(str(e)))
        return False

# Plugin  ---------------------------------------------------

global _plugin
_plugin = BasePlugin()


def onStart():
    _plugin.onStart()


def onHeartbeat():
    _plugin.onHeartbeat()

# Plugin utility functions ---------------------------------------------------

def domoticz_api(query):
    url = "http://127.0.0.1:8080/json.htm?{}".format(query)

    req = request.Request(url)
    response = request.urlopen(req, timeout=10)

    return json.loads(response.read().decode("utf-8"))

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
