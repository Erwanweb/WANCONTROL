# WanCheck plugin (dual WAN, FlowBalancing or Backup)

"""
<plugin key="WanCheck" name="WAN Check WAN1/WAN2" author="Erwan" version="2.0.0">
    <params>
        <param field="Username" label="ID/UserKey for Telegram,Pushover (0 if none)" width="600px" required="false" default="0,0"/>
        <param field="Mode3" label="WAN2 Mode" width="200px">
            <options>
                <option label="Flow Balancing" value="FlowBalancing" default="true"/>
                <option label="Backup (alimentation controlee)" value="Backup"/>
            </options>
        </param>
        <param field="Mode4" label="Backup: idx prise WAN2" width="100px" required="false" default="0"/>
        <param field="Mode5" label="Backup: heure test quotidien (HH:MM)" width="100px" required="false" default="04:00"/>
        <param field="Address" label="ER605 IP (verif WAN2 via API)" width="200px" required="false" default=""/>
        <param field="Mode6" label="ER605 Admin Username" width="150px" required="false" default="admin"/>
        <param field="Password" label="ER605 Admin Password" width="200px" required="false" password="true"/>
    </params>
</plugin>
"""

import Domoticz
import subprocess
import time
import re
import ssl
import http.cookiejar
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
        self.valid_main_state = None
        self.valid_star_state = None
        # notification IDs
        self.TelegramToken = ""
        self.PushoverToken = ""
        self.TelegramID = "0"
        self.PushoverUserKey = "0"
        # Delais de demarrage
        self.start_time = time.time()
        self.startup_silence = 300       # 5 min : le temps que le Pi/reseau soit stable apres un reboot
        self.startup_silence_logged = False
        self.er605_unreachable_logged = False
        # Backup mode (alimentation controlee du WAN2)
        self.wan2_mode = "FlowBalancing"
        self.wan2_label = "WAN2"
        self.wan2_switch_idx = 0
        self.daily_test_hour = 4
        self.daily_test_minute = 0
        self.wan2_active_reason = None   # None, "backup" ou "dailytest"
        self.wan2_status_text = "OFF (standby)"
        self.wan2_power_since = None
        self.wan1_ok_since = None
        self.wan1_stable_cutoff = 600    # 10 minutes de MAIN ok avant de couper le WAN2
        # test WAN2 adaptatif : au lieu d'attendre un delai fixe puis tester une seule
        # fois (parfois trop court, parfois trop long), on interroge l'ER605 a chaque
        # cycle (deja limite a 1x/min via connectivity_check_interval) et on notifie
        # une seule fois des que le resultat (succes ou echec) est connu
        self.wan2_test_timeout = 600     # 10 min max avant de declarer le test en echec
        self.wan2_test_success_notified = False
        self.wan2_test_failure_notified = False
        self.wan2_down_time = None       # heure HH:MM de mise sous tension, pour le message consolide
        self.last_daily_test_date = ""
        self.switch_check_interval = 300  # verification periodique etat reel vs attendu de la prise
        self.last_switch_check = 0
        # verification WAN2 via l'API native de l'ER605 (plus fiable que ping/traceroute
        # cote LAN, car l'ER605 gere lui-meme le failover et route tout via WAN1 tant
        # qu'il ne le juge pas down, meme si WAN2 est alimente)
        self.er605_host = ""
        self.er605_user = ""
        self.er605_pass = ""
        self.er605_stok = None
        self.er605_opener = None
        # file d'attente de retry pour les notifications non delivrees (ex: coupure au moment de l'envoi)
        self.notification_queue = []
        self.notification_retry_interval = 30
        self.notification_max_age = 3600
        self.notification_max_queue = 20
        self.last_notification_retry = 0
        # les checks reseau (ping/traceroute + API ER605) sont couteux (subprocess,
        # requetes HTTP) : on ne les relance que toutes les 60s, pas a chaque
        # heartbeat (20s), pour ne pas surcharger le Pi ni spammer l'ER605
        self.connectivity_check_interval = 60
        self.last_connectivity_check = 0
        self.cached_main_ok = False
        self.cached_main_latency = 0
        self.cached_er605_states = None
        self.cached_flow_star_ok = False

    def onStart(self):
        Domoticz.Log("WAN Checker plugin starting")

        # cibles de ping fixes (plus besoin d'etre configurables : WAN1 est verifie
        # par l'ER605 quand dispo, WAN2 n'est plus utilise que pour un fallback on/off)
        self.main_ip = "8.8.8.8"
        self.star_ip = "1.1.1.1"

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

        self.er605_host = Parameters["Address"].strip()
        try:
            self.er605_user = Parameters["Mode6"].strip() or "admin"
        except (KeyError, AttributeError):
            self.er605_user = "admin"
        self.er605_pass = Parameters["Password"]

        if self.wan2_mode == "Backup" and self.er605_host:
            self.er605_build_opener()
            Domoticz.Log("ER605 API check enabled for WAN2 ({})".format(self.er605_host))

        create_switch(1, "WAN1 (MAIN)")
        create_latency(2, "WAN1 (MAIN) Latency")
        create_switch(3, self.wan2_label)

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
                    self.wan2_power_since = time.time()
                    self.wan2_down_time = time.strftime("%H:%M")
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

        # periode de grace au demarrage/redemarrage du plugin (ex: reboot planifie
        # du Pi) : le temps que le reseau soit stable, on ne lance aucun test et
        # donc aucune notification, pour eviter les faux positifs en cascade
        elapsed_since_start = time.time() - self.start_time
        if elapsed_since_start < self.startup_silence:
            if not self.startup_silence_logged:
                Domoticz.Log("WAN Checker: periode de grace au demarrage ({}s), tests suspendus".format(
                    self.startup_silence
                ))
                self.startup_silence_logged = True
            return

        # le routeur ER605 lui-meme reboote chaque nuit (reboot programme sur
        # l'appareil) : pendant qu'il redemarre, tout est injoignable (WAN1 et WAN2
        # passent par lui) et on aurait sinon une cascade de faux positifs. On le
        # ping d'abord ; s'il ne repond pas, on suspend les tests le temps qu'il
        # revienne, plutot que de se fier a un horaire fixe (qui devrait en plus
        # suivre le changement heure ete/hiver)
        if not self.is_er605_reachable():
            if not self.er605_unreachable_logged:
                Domoticz.Log("WAN Checker: ER605 injoignable (reboot en cours ?), tests suspendus")
                self.er605_unreachable_logged = True
            return
        self.er605_unreachable_logged = False

        self.check_all()

    def check_all(self):
        self.retry_pending_notifications()

        # les checks reseau (ping/traceroute + API ER605) sont couteux : on ne les
        # relance reellement que toutes les 60s (connectivity_check_interval), pas a
        # chaque heartbeat de 20s ; entre-temps on reutilise le dernier resultat connu
        now = time.time()
        if now - self.last_connectivity_check >= self.connectivity_check_interval:
            self.last_connectivity_check = now

            self.cached_main_ok, self.cached_main_latency = check_route(self.main_ip, "192.168.1.1")

            # etat natif ER605 (un seul appel donne WAN1 + WAN2), plus fiable que le
            # ping/traceroute cote LAN puisque c'est l'ER605 qui decide reellement du
            # routage : on le prend comme source de verite quand il est disponible
            self.cached_er605_states = self.er605_online_states()

            if self.wan2_mode != "Backup":
                self.cached_flow_star_ok, _ = check_route(self.star_ip, "192.168.2.1")

        main_ok, main_latency = self.cached_main_ok, self.cached_main_latency
        er605_states = self.cached_er605_states

        if er605_states and "WAN1" in er605_states:
            main_ok = er605_states["WAN1"]
            # latence WAN1 : on garde le ping du Pi (deja fiable puisque WAN1 est la
            # route par defaut) plutot que le diagnostic ER605, plus lourd (plusieurs
            # requetes/poll) et inutile a relancer sans raison

        if self.wan2_mode == "Backup":
            star_ok = self.manage_backup_power(er605_states)
        else:
            star_ok = self.cached_flow_star_ok
            if er605_states and "WAN2" in er605_states:
                star_ok = er605_states["WAN2"]

        # historique latence (WAN1 uniquement : WAN2 est un lien de secours,
        # seule sa connectivite compte, pas son debit)
        if main_ok:
            self.lat_main_hist.append(main_latency)

        # limite taille historique (15 mesures ≈ 5 min avec interval 20s)
        if len(self.lat_main_hist) > 15:
            self.lat_main_hist.pop(0)

        # toujours mettre à jour les ON/OFF
        # update_switch(1, main_ok)
        # update_switch(3, star_ok)

        # latence mise à jour seulement toutes les 5 minutes
        if time.time() - self.last_latency_update >= self.latency_interval:

            avg_main = sum(self.lat_main_hist) / len(self.lat_main_hist) if self.lat_main_hist else 0

            update_value(2, round(avg_main, 1))

            self.last_latency_update = time.time()

        if self.wan2_mode == "Backup":
            Domoticz.Log("WAN1 (MAIN) {} - {} ms / {}: {}".format(
                "OK" if main_ok else "DOWN",
                main_latency,
                self.wan2_label,
                self.wan2_status_text
            ))
        else:
            Domoticz.Log("WAN1 (MAIN) {} - {} ms / {} {}".format(
                "OK" if main_ok else "DOWN",
                main_latency,
                self.wan2_label,
                "OK" if star_ok else "DOWN"
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

                # en mode Backup, les notifications WAN1/WAN2 sont gerees par
                # manage_backup_power() (message unique et consolide pour tout un
                # incident) : on evite ici un doublon qui, en plus, arriverait
                # potentiellement dans le desordre (files d'attente independantes
                # pendant une coupure). En FlowBalancing, c'est le seul mecanisme
                # de notification, donc on le garde.
                if self.wan2_mode != "Backup":
                    title, msg, priority = build_message(main_ok, star_ok, main_latency, self.wan2_label)
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

        else:
            self.pending_main_state = None
            self.pending_star_state = None
            self.pending_count = 0


# Backup mode (alimentation controlee du WAN2) ---------------------------------------------------
    def manage_backup_power(self, er605_states=None):
        if not self.wan2_switch_idx:
            self.wan2_status_text = "non configure (Mode4 manquant)"
            return False

        now = time.time()
        lt = time.localtime(now)
        today_str = time.strftime("%Y-%m-%d", lt)

        self.check_switch_drift(now)

        # coupure du WAN2 des que le WAN1 (MAIN) est stable depuis 10 min
        if (self.wan2_active_reason == "backup"
                and self.wan1_ok_since is not None
                and (now - self.wan1_ok_since) >= self.wan1_stable_cutoff):
            self.power_wan2(False, "WAN1 (MAIN) ok depuis plus de 10 min")

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
            self.wan2_status_text = "OFF (standby, WAN1 ok)"
            return False

        elapsed = now - self.wan2_power_since

        # l'ER605 gere lui-meme le failover et garde WAN1 actif tant qu'il ne le juge
        # pas down : le ping/traceroute cote LAN ne passe donc jamais reellement par
        # WAN2 en usage normal. L'etat natif ER605 (deja recupere dans check_all, donc
        # au plus 1x/min) fait foi quand il est disponible ; sinon on retombe sur le
        # ping/traceroute du Pi. Seule la connectivite compte, pas la latence.
        er605_wan2_up = er605_states.get("WAN2") if er605_states else None
        if er605_wan2_up is not None:
            star_ok = er605_wan2_up
        else:
            star_ok, _ = check_route(self.star_ip, "192.168.2.1")

        self.wan2_status_text = "{} ({})".format(
            "OK" if star_ok else "DOWN", self.wan2_active_reason
        )

        # test adaptatif : plutot qu'un delai fixe avant un test unique (parfois trop
        # court, parfois trop long), on interroge l'ER605 en continu (deja limite a
        # 1x/min) et on notifie une seule fois des que le resultat est connu -
        # succes immediat, ou echec confirme apres wan2_test_timeout
        if star_ok and not self.wan2_test_success_notified:
            self.wan2_test_success_notified = True
            now_str = time.strftime("%H:%M")

            if self.wan2_active_reason == "backup":
                Domoticz.Log("Backup mode: {} confirme OK".format(self.wan2_label))
                Send_Notifications(
                    self,
                    "WAN ALERT - {} operationnel".format(self.wan2_label),
                    "WAN1 (MAIN) DOWN a {} - {} alimente - {} OK a {}".format(
                        self.wan2_down_time, self.wan2_label, self.wan2_label, now_str
                    ),
                    1
                )
            else:  # dailytest
                Domoticz.Log("Backup mode: daily {} test result = OK".format(self.wan2_label))
                Send_Notifications(
                    self,
                    "WAN ALERT - Test quotidien {}".format(self.wan2_label),
                    "{} OK".format(self.wan2_label),
                    1
                )
                self.last_daily_test_date = today_str
                self.save_backup_state()
                self.power_wan2(False, "fin de test quotidien")

        elif not star_ok and elapsed >= self.wan2_test_timeout and not self.wan2_test_failure_notified:
            self.wan2_test_failure_notified = True

            if self.wan2_active_reason == "backup":
                Domoticz.Log("Backup mode: {} toujours DOWN apres {} min".format(
                    self.wan2_label, int(self.wan2_test_timeout / 60)
                ))
                Send_Notifications(
                    self,
                    "WAN ALERT - {} en echec".format(self.wan2_label),
                    "WAN1 (MAIN) DOWN a {} - {} alimente mais toujours DOWN apres {} min".format(
                        self.wan2_down_time, self.wan2_label, int(self.wan2_test_timeout / 60)
                    ),
                    2
                )
                # on laisse le WAN2 alimente et on continue a surveiller : une reussite
                # tardive enverra quand meme la notification de succes ci-dessus
            else:  # dailytest
                Domoticz.Log("Backup mode: daily {} test result = FAILED".format(self.wan2_label))
                Send_Notifications(
                    self,
                    "WAN ALERT - Test quotidien {}".format(self.wan2_label),
                    "{} FAILED".format(self.wan2_label),
                    2
                )
                self.last_daily_test_date = today_str
                self.save_backup_state()
                self.power_wan2(False, "fin de test quotidien")

        return star_ok

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

    def er605_build_opener(self):
        cj = http.cookiejar.CookieJar()
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.er605_opener = request.build_opener(
            request.HTTPCookieProcessor(cj),
            request.HTTPSHandler(context=ctx)
        )

    def er605_login(self):
        if self.er605_opener is None:
            self.er605_build_opener()

        url = "https://{}/cgi-bin/luci/;stok=/login?form=login".format(self.er605_host)
        payload = "data=" + parse.quote(json.dumps({
            "method": "login",
            "params": {"username": self.er605_user, "password": self.er605_pass}
        }))
        req = request.Request(
            url, data=payload.encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        resp = self.er605_opener.open(req, timeout=10)
        result = json.loads(resp.read().decode("utf-8"))

        if str(result.get("error_code")) != "0":
            self.er605_stok = None
            raise Exception("login error_code={}".format(result.get("error_code")))

        self.er605_stok = result["result"]["stok"]

    def er605_call(self, path, method_name):
        if not self.er605_stok:
            self.er605_login()

        def do_call():
            url = "https://{}/cgi-bin/luci/;stok={}/{}".format(self.er605_host, self.er605_stok, path)
            payload = "data=" + parse.quote(json.dumps({"method": method_name}))
            req = request.Request(
                url, data=payload.encode("utf-8"),
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            resp = self.er605_opener.open(req, timeout=10)
            return json.loads(resp.read().decode("utf-8"))

        try:
            result = do_call()
            retry_needed = str(result.get("error_code")) != "0"
        except Exception:
            # stok invalide/expire -> le serveur peut repondre par une erreur HTTP
            # (ex: 404) plutot qu'un JSON avec error_code : on traite ce cas comme
            # une session expiree aussi, sinon le plugin resterait bloque en boucle
            result = None
            retry_needed = True

        if retry_needed:
            # session probablement expiree : on relogin une fois et on reessaie
            self.er605_login()
            result = do_call()

        if str(result.get("error_code")) != "0":
            raise Exception("error_code={}".format(result.get("error_code")))

        return result.get("result", [])

    def is_er605_reachable(self):
        # simple ping LAN (rapide, pas d'auth) : sert juste a detecter que l'ER605
        # est en train de reboot avant de lancer les vrais tests WAN
        if not self.er605_host:
            return True  # pas d'ER605 configure, rien a verifier, on ne bloque pas
        try:
            result = subprocess.run(
                ["ping", "-c", "1", "-W", "1", self.er605_host],
                stdout=subprocess.PIPE, text=True
            )
            return result.returncode == 0
        except Exception as e:
            Domoticz.Error("ER605 reachability ping failed: {}".format(str(e)))
            return True  # en cas de doute, ne pas bloquer les tests

    def er605_online_states(self):
        # un seul appel API donne l'etat natif (up/down) des deux interfaces WAN,
        # tel que detecte par l'ER605 lui-meme -> {"WAN1": bool, "WAN2": bool}
        if not self.er605_host:
            return None
        try:
            states = {}
            for item in self.er605_call("admin/online?form=state", "get"):
                iface = item.get("interface")
                if iface:
                    states[iface] = item.get("state") == "up"
            return states
        except Exception as e:
            Domoticz.Error("ER605 online status check failed: {}".format(str(e)))
        return None

    def power_wan2(self, on, reason):
        try:
            switch_command(self.wan2_switch_idx, "On" if on else "Off")
        except Exception as e:
            Domoticz.Error("Error switching {} power: {}".format(self.wan2_label, str(e)))
            return

        if on:
            self.wan2_active_reason = reason if reason in ("backup", "dailytest") else "backup"
            self.wan2_power_since = time.time()
            self.wan2_down_time = time.strftime("%H:%M")
            self.wan2_test_success_notified = False
            self.wan2_test_failure_notified = False
            Domoticz.Log("{} power ON ({})".format(self.wan2_label, reason))
            # pas de notification immediate ici : un seul message consolide sera
            # envoye par manage_backup_power() des que le resultat du test est connu
        else:
            was_active_for_backup = self.wan2_active_reason == "backup"
            self.wan2_active_reason = None
            self.wan2_power_since = None
            Domoticz.Log("{} power OFF ({})".format(self.wan2_label, reason))
            if was_active_for_backup:
                Send_Notifications(self, "WAN ALERT - {} coupe".format(self.wan2_label),
                                    "WAN1 (MAIN) ok depuis plus de 10 min, {} coupe".format(self.wan2_label), 1)


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

def build_message(main_ok, star_ok, main_latency, wan2_label="WAN2"):
    main_txt = "🌐 WAN1 (MAIN) OK ✅ {} ms".format(main_latency) if main_ok else "❌ WAN1 (MAIN) DOWN"
    star_txt = "📡 {} OK ✅".format(wan2_label) if star_ok else "❌ {} DOWN".format(wan2_label)

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
