import os
import time
import socket
import threading
import requests
import logging
import json
import subprocess
import re
import urllib.parse
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template, jsonify, request, redirect, url_for, session
from cryptography.fernet import Fernet

app = Flask(__name__)
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=3650) 
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

CONFIG_FILE = "data/config.json"
HISTORY_FILE = "data/history.json"
KEY_DIR = "/app/auth_key"
KEY_FILE = os.path.join(KEY_DIR, "secret.key")

os.makedirs("static", exist_ok=True)
os.makedirs("data", exist_ok=True)
os.makedirs(KEY_DIR, exist_ok=True)

try:
    subprocess.run(["tailscale", "set", "--accept-routes=true"], check=False)
except: pass

if not os.path.exists(KEY_FILE):
    with open(KEY_FILE, 'wb') as kf: kf.write(Fernet.generate_key())

with open(KEY_FILE, 'rb') as kf:
    key_bytes = kf.read()
    cipher_suite = Fernet(key_bytes)

app.secret_key = key_bytes

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            cfg = json.load(f)
            cfg.setdefault("map_url", "")
            cfg.setdefault("report_url", "")
            cfg.setdefault("mapbox_token", "")
            cfg.setdefault("latitude", "")
            cfg.setdefault("longitude", "")
            cfg.setdefault("ts_authkey", "")
            cfg.setdefault("session_timeout", 24)
            cfg.setdefault("timezone", "America/New_York")
            cfg.setdefault("ui_layout", "2x2")
            cfg.setdefault("ui_text_size", "15px")
            cfg.setdefault("watchdog_ip", "")
            cfg.setdefault("watchdog_port", 80)
            cfg.setdefault("watchdog_threshold", 5)
            cfg.setdefault("watchdog_ip_2", "")
            cfg.setdefault("watchdog_port_2", 80)
            cfg.setdefault("watchdog_threshold_2", 5)
            cfg.setdefault("nut_host_2", "")
            cfg.setdefault("nut_port_2", 3493)
            cfg.setdefault("nut_ups_names_2", "auto")
            cfg.setdefault("ups_min_runtime_2", 10)
            cfg.setdefault("snmp_ip", "")
            cfg.setdefault("snmp_name", "")
            cfg.setdefault("snmp_community", "public")
            cfg.setdefault("snmp_ip_2", "")
            cfg.setdefault("snmp_name_2", "")
            cfg.setdefault("snmp_community_2", "public")
            
            if "admin_username" not in cfg:
                cfg["admin_username"] = "admin"
                cfg["admin_password"] = cipher_suite.encrypt(b"admin").decode('utf-8')
            return cfg
    
    config = {
        "admin_username": "admin", "admin_password": cipher_suite.encrypt(b"admin").decode('utf-8'),
        "session_timeout": 24, "timezone": "America/New_York",
        "ui_layout": "2x2", "ui_text_size": "15px",
        "company_name": "", "zip_code": "", "threshold_mins": 45,
        "kubra_url": "", "map_url": "", "report_url": "",
        "nut_host": "", "nut_port": 3493, "nut_ups_names": "auto", "ups_min_runtime": 10,
        "nut_host_2": "", "nut_port_2": 3493, "nut_ups_names_2": "auto", "ups_min_runtime_2": 10,
        "snmp_ip": "", "snmp_name": "", "snmp_community": "public",
        "snmp_ip_2": "", "snmp_name_2": "", "snmp_community_2": "public",
        "pushover_user": "", "pushover_token": "",
        "mapbox_token": "", "latitude": "", "longitude": "", "ts_authkey": "",
        "watchdog_ip": "", "watchdog_port": 80, "watchdog_threshold": 5,
        "watchdog_ip_2": "", "watchdog_port_2": 80, "watchdog_threshold_2": 5
    }
    save_config(config)
    return config

def save_config(config):
    with open(CONFIG_FILE, 'w') as f: json.dump(config, f, indent=4)

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, 'r') as f:
            data = json.load(f)
            if "watchdog" not in data: data["watchdog"] = []
            if "snmp" not in data: data["snmp"] = []
            return data
    return {"grid": [], "ups": [], "watchdog": [], "snmp": []}

def save_history(history):
    history["grid"] = history["grid"][-50:]
    history["ups"] = history["ups"][-50:]
    history["watchdog"] = history.get("watchdog", [])[-50:]
    history["snmp"] = history.get("snmp", [])[-50:]
    with open(HISTORY_FILE, 'w') as f: json.dump(history, f, indent=4)

app_config = load_config()

os.environ['TZ'] = app_config.get("timezone", "America/New_York")
time.tzset()

state = {
    "is_outage": False, "customers_affected": 0, "outage_start_time": None, "outage_max_affected": 0,
    "alert_sent": False, "last_check": None, "error_msg": None, "etr": "Unavailable",
    "discovery_failed": False,
    "nut_enabled": bool(app_config.get("nut_host") or app_config.get("nut_host_2")), 
    "ups_data": {}, "nut_last_check": None, "nut_error": None,
    "watchdogs": {
        "1": {"online": True, "down_time": None, "alert_sent": False},
        "2": {"online": True, "down_time": None, "alert_sent": False}
    },
    "watchdog_last_check": None,
    "snmp": {
        "1": {"online": False, "uptime_s": None, "last_check": None},
        "2": {"online": False, "uptime_s": None, "last_check": None}
    }
}

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'): return redirect(url_for('login_page'))
        timeout_hours = int(app_config.get("session_timeout", 24))
        if timeout_hours > 0:
            login_time = session.get('login_time', 0)
            if time.time() - login_time > (timeout_hours * 3600):
                session.pop('logged_in', None)
                session.pop('login_time', None)
                return redirect(url_for('login_page', timeout=1))
        return f(*args, **kwargs)
    return decorated_function

def format_uptime(seconds):
    if seconds is None: return "Unknown"
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    mins = int((seconds % 3600) // 60)
    if days > 0: return f"{days}d {hours}h {mins}m"
    if hours > 0: return f"{hours}h {mins}m"
    return f"{mins} mins"

def auto_discover_api(map_url, zip_code):
    if not map_url: return ""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        resp = requests.get(map_url, timeout=10, headers=headers)
        resp.raise_for_status()
        text = resp.text
        
        iframes = re.findall(r'<iframe.*?src=[\'"]([^\'"]+)[\'"]', text, re.IGNORECASE)
        for iframe in iframes:
            if not iframe.startswith('http'): iframe = urllib.parse.urljoin(map_url, iframe)
            try: text += " " + requests.get(iframe, timeout=5, headers=headers).text
            except: pass

        scripts = re.findall(r'<script.*?src=[\'"]([^\'"]+\.js[^\'"]*)[\'"]', text, re.IGNORECASE)
        for script in scripts:
            if not script.startswith('http') or urllib.parse.urlparse(script).netloc == urllib.parse.urlparse(map_url).netloc:
                if not script.startswith('http'): script = urllib.parse.urljoin(map_url, script)
                try: text += " " + requests.get(script, timeout=5, headers=headers).text
                except: pass
            
        uuids = list(set(re.findall(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', text, re.IGNORECASE)))
        for uid in uuids:
            test_url = f"https://kubra.io/data/{uid.lower()}/public/thematic-1/thematic_areas.json"
            try:
                r = requests.get(test_url, timeout=5)
                if r.status_code == 200 and "areas" in r.json(): return test_url
            except: pass
        
        json_links = list(set(re.findall(r'[\'"]([^\'"]+\.json)[\'"]', text)))
        for link in json_links:
            full_url = urllib.parse.urljoin(map_url, link) if not link.startswith('http') else link
            try:
                r = requests.get(full_url, timeout=5)
                if r.status_code == 200:
                    data = r.json()
                    if "zips" in data:
                        for z in data["zips"]:
                            if str(z.get("zipCode", "")) == zip_code: return full_url
                    elif "areas" in data:
                        return full_url
            except: pass
    except Exception as e: logging.error(f"Auto-discovery failed: {e}")
    return ""

@app.route("/login", methods=["GET", "POST"])
def login_page():
    error = None
    if request.args.get('timeout'): error = "Your session has expired. Please log in again."
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        cfg_user = app_config.get("admin_username")
        try: cfg_pass = cipher_suite.decrypt(app_config.get("admin_password").encode('utf-8')).decode('utf-8')
        except Exception: cfg_pass = "admin" 
        if username == cfg_user and password == cfg_pass:
            session.permanent = True
            session['logged_in'] = True
            session['login_time'] = time.time()
            return redirect(url_for('index'))
        else: error = "Invalid credentials. Please try again."
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.pop('logged_in', None)
    session.pop('login_time', None)
    return redirect(url_for('login_page'))

@app.route("/")
@login_required
def index():
    duration = 0
    event_active = False
    
    if state["is_outage"] and state["outage_start_time"]:
        duration = int((datetime.now() - state["outage_start_time"]).total_seconds() / 60)
        event_active = True
        
    wd_durations = {"1": 0, "2": 0}
    for w_id in ["1", "2"]:
        if not state["watchdogs"][w_id]["online"] and state["watchdogs"][w_id]["down_time"]:
            wd_durations[w_id] = int((datetime.now() - state["watchdogs"][w_id]["down_time"]).total_seconds() / 60)
            event_active = True
            
    if state["nut_enabled"]:
        for ups in state["ups_data"].values():
            if "OB" in ups.get("status", ""): event_active = True
                
    return render_template("index.html", state=state, config=app_config, duration=duration, wd_durations=wd_durations, ts_status=get_ts_status(), event_active=event_active, format_uptime=format_uptime)

@app.route("/history")
@login_required
def history_page():
    history_data = load_history()
    history_data["grid"] = history_data["grid"][::-1]
    history_data["ups"] = history_data["ups"][::-1]
    history_data["watchdog"] = history_data.get("watchdog", [])[::-1]
    history_data["snmp"] = history_data.get("snmp", [])[::-1]
    return render_template("history.html", state=state, config=app_config, history=history_data)

@app.route("/config", methods=["GET", "POST"])
@login_required
def config_page():
    global app_config
    if request.method == "POST":
        def get_secure(field_name):
            val = request.form.get(field_name, "").strip()
            if not val: return app_config.get(field_name, "")
            if val.lower() == "clear": return ""
            return val

        new_username = request.form.get("admin_username", "").strip()
        new_password = request.form.get("admin_password", "")
        if new_username: app_config["admin_username"] = new_username
        if new_password: app_config["admin_password"] = cipher_suite.encrypt(new_password.encode('utf-8')).decode('utf-8')

        new_tz = request.form.get("timezone", "America/New_York").strip()
        os.environ['TZ'] = new_tz
        time.tzset()

        new_ts_key = get_secure("ts_authkey")
        if new_ts_key and new_ts_key != app_config.get("ts_authkey"):
            try: subprocess.run(["tailscale", "up", "--authkey", new_ts_key, "--hostname", "outage-tracker", "--accept-routes=true"], check=True)
            except Exception as e: logging.error(f"Tailscale auth failed: {e}")
        elif request.form.get("ts_authkey", "").strip().lower() == "clear":
            subprocess.run(["tailscale", "logout"])

        api_url = request.form.get("kubra_url", "").strip()
        map_url = request.form.get("map_url", "").strip()
        zip_c = request.form.get("zip_code", "").strip()
        
        if not api_url and map_url and zip_c:
            logging.info(f"Attempting to auto-discover API URL from {map_url}...")
            discovered = auto_discover_api(map_url, zip_c)
            if discovered:
                api_url = discovered
                logging.info(f"✅ Auto-discovered API URL: {api_url}")
            else:
                logging.warning("❌ Auto-discovery failed.")

        app_config.update({
            "session_timeout": int(request.form.get("session_timeout", 24)), "timezone": new_tz,
            "ui_layout": request.form.get("ui_layout", "2x2"), "ui_text_size": request.form.get("ui_text_size", "15px"),
            "company_name": request.form.get("company_name", "").strip(), "zip_code": zip_c,
            "threshold_mins": int(request.form.get("threshold_mins", 45)), "kubra_url": api_url,
            "map_url": map_url, "report_url": request.form.get("report_url", "").strip(),
            "nut_host": request.form.get("nut_host", "").strip(), "nut_port": int(request.form.get("nut_port", 3493)),
            "nut_ups_names": request.form.get("nut_ups_names", "auto").strip(), "ups_min_runtime": int(request.form.get("ups_min_runtime", 10)),
            "nut_host_2": request.form.get("nut_host_2", "").strip(), "nut_port_2": int(request.form.get("nut_port_2", 3493)),
            "nut_ups_names_2": request.form.get("nut_ups_names_2", "auto").strip(), "ups_min_runtime_2": int(request.form.get("ups_min_runtime_2", 10)),
            "watchdog_ip": request.form.get("watchdog_ip", "").strip(), "watchdog_port": int(request.form.get("watchdog_port", 80)),
            "watchdog_threshold": int(request.form.get("watchdog_threshold", 5)),
            "watchdog_ip_2": request.form.get("watchdog_ip_2", "").strip(), "watchdog_port_2": int(request.form.get("watchdog_port_2", 80)),
            "watchdog_threshold_2": int(request.form.get("watchdog_threshold_2", 5)),
            "snmp_ip": request.form.get("snmp_ip", "").strip(), "snmp_name": request.form.get("snmp_name", "").strip(),
            "snmp_community": request.form.get("snmp_community", "public").strip(),
            "snmp_ip_2": request.form.get("snmp_ip_2", "").strip(), "snmp_name_2": request.form.get("snmp_name_2", "").strip(),
            "snmp_community_2": request.form.get("snmp_community_2", "public").strip(),
            "latitude": get_secure("latitude"), "longitude": get_secure("longitude"),
            "mapbox_token": get_secure("mapbox_token"), "pushover_user": get_secure("pushover_user"),
            "pushover_token": get_secure("pushover_token"), "ts_authkey": new_ts_key,
        })
        save_config(app_config)
        state["nut_enabled"] = bool(app_config.get("nut_host") or app_config.get("nut_host_2"))
        
        return redirect(url_for('config_page'))
        
    nut_status_1 = get_nut_status(app_config.get("nut_host"), app_config.get("nut_port", 3493))
    nut_status_2 = get_nut_status(app_config.get("nut_host_2"), app_config.get("nut_port_2", 3493))
    return render_template("config.html", config=app_config, ts_status=get_ts_status(), nut_status=nut_status_1, nut_status_2=nut_status_2)

@app.route("/test-pushover", methods=["POST"])
@login_required
def test_pushover():
    if send_pushover("🔔 Pushover Test", "Configuration working perfectly.", priority=0, include_map=True):
        return jsonify({"status": "success", "message": "Test sent! Check your device."})
    return jsonify({"status": "error", "message": "Failed to send alert. Check keys."}), 500

def get_ts_status():
    ts_status = "Offline"
    try:
        res = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True)
        if res.returncode == 0:
            ts_data = json.loads(res.stdout)
            ts_status = ts_data.get("BackendState", "Offline")
            if ts_status == "Running":
                ip = ts_data.get("Self", {}).get("TailscaleIPs", [""])[0]
                ts_status = f"Connected ({ip})"
    except Exception: pass
    return ts_status

def get_nut_status(host, port):
    if not host: return "Not Configured"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect((host, port))
        return "Connected"
    except Exception: return "Offline / Unreachable"

def update_outage_map():
    token = app_config.get("mapbox_token")
    lat = app_config.get("latitude")
    lon = app_config.get("longitude")
    if not token or not lat or not lon: return None
    url = f"https://api.mapbox.com/styles/v1/mapbox/dark-v11/static/pin-l+f44336({lon},{lat})/{lon},{lat},13,0/800x400@2x?access_token={token}"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        filepath = "static/outage_map.jpg"
        with open(filepath, 'wb') as f: f.write(resp.content)
        return filepath
    except Exception: return None

def send_pushover(title, message, priority=1, include_map=False):
    user = app_config.get("pushover_user")
    token = app_config.get("pushover_token")
    if not user or not token: return False
    data = {"token": token, "user": user, "title": title, "message": message, "priority": priority}
    image_path = update_outage_map() if include_map else None
    try:
        if image_path and os.path.exists(image_path):
            with open(image_path, "rb") as img:
                files = {"attachment": ("map.jpg", img, "image/jpeg")}
                resp = requests.post("https://api.pushover.net/1/messages.json", data=data, files=files)
        else:
            resp = requests.post("https://api.pushover.net/1/messages.json", data=data)
        resp.raise_for_status()
        return True
    except Exception: return False

def fetch_nut_data(host, port, names):
    if not host: return None
    results = {}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(10)
            s.connect((host, port))
            ups_list = []
            if names.lower() == "auto":
                s.sendall(b"LIST UPS\n")
                data = b""
                while b"END LIST UPS" not in data:
                    chunk = s.recv(4096)
                    if not chunk: break
                    data += chunk
                for line in data.decode('ascii').split('\n'):
                    if line.startswith('UPS '): ups_list.append(line.split(' ')[1])
            else:
                ups_list = [x.strip() for x in names.split(',')]

            for ups_name in ups_list:
                s.sendall(f"LIST VAR {ups_name}\n".encode('ascii'))
                data = b""
                while b"END LIST VAR" not in data:
                    chunk = s.recv(4096)
                    if not chunk: break
                    data += chunk
                vars_dict = {}
                for line in data.decode('ascii').split('\n'):
                    if line.startswith('VAR'):
                        parts = line.strip().split(' ', 3)
                        if len(parts) == 4: vars_dict[parts[2]] = parts[3].strip('"')
                results[ups_name] = vars_dict
        return results
    except: return None

def poll_snmp():
    while True:
        c_ip1 = app_config.get("snmp_ip")
        c_ip2 = app_config.get("snmp_ip_2")
        c_n1 = app_config.get("snmp_name")
        c_n2 = app_config.get("snmp_name_2")
        c_c1 = app_config.get("snmp_community")
        c_c2 = app_config.get("snmp_community_2")
        
        for s_id in ["1", "2"]:
            suffix = "" if s_id == "1" else "_2"
            ip = app_config.get(f"snmp_ip{suffix}")
            comm = app_config.get(f"snmp_community{suffix}", "public")
            name = app_config.get(f"snmp_name{suffix}") or f"Hardware {s_id}"

            s_state = state["snmp"][s_id]

            if ip:
                try:
                    res = subprocess.run(["snmpget", "-v2c", "-c", comm, "-O", "tv", "-t", "3", "-r", "1", ip, "1.3.6.1.2.1.1.3.0"], capture_output=True, text=True)
                    s_state["last_check"] = datetime.now().strftime("%I:%M:%S %p")
                    
                    if res.returncode == 0:
                        ticks_str = res.stdout.strip()
                        try:
                            ticks = int(re.search(r'\d+', ticks_str).group())
                            new_uptime_s = ticks / 100.0
                            s_state["online"] = True
                            
                            if s_state["uptime_s"] is not None:
                                if new_uptime_s < (s_state["uptime_s"] - 60):
                                    send_pushover("🔄 Hardware Reboot", f"{name} ({ip}) has rebooted.\nNew Uptime: {format_uptime(new_uptime_s)}", priority=0)
                                    hist = load_history()
                                    hist["snmp"].append({
                                        "name": name, "ip": ip, "time": datetime.now().strftime("%Y-%m-%d %I:%M %p"),
                                        "old_uptime": format_uptime(s_state["uptime_s"])
                                    })
                                    save_history(hist)

                            s_state["uptime_s"] = new_uptime_s
                        except: s_state["online"] = False
                    else:
                        s_state["online"] = False
                except Exception as e:
                    s_state["online"] = False
            else:
                s_state["online"] = False
                s_state["uptime_s"] = None
                
        for _ in range(300):
            if (app_config.get("snmp_ip") != c_ip1 or app_config.get("snmp_ip_2") != c_ip2 or
                app_config.get("snmp_name") != c_n1 or app_config.get("snmp_name_2") != c_n2 or
                app_config.get("snmp_community") != c_c1 or app_config.get("snmp_community_2") != c_c2):
                break
            time.sleep(1)

def poll_watchdog():
    while True:
        c_ip1 = app_config.get("watchdog_ip")
        c_port1 = app_config.get("watchdog_port")
        c_ip2 = app_config.get("watchdog_ip_2")
        c_port2 = app_config.get("watchdog_port_2")
        
        for w_id in ["1", "2"]:
            suffix = "" if w_id == "1" else "_2"
            ip = app_config.get(f"watchdog_ip{suffix}")
            port = app_config.get(f"watchdog_port{suffix}", 80)
            thresh = app_config.get(f"watchdog_threshold{suffix}", 5)

            if ip:
                is_online = False
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.settimeout(4)
                        s.connect((ip, int(port)))
                        is_online = True
                except Exception: pass

                state["watchdog_last_check"] = datetime.now().strftime("%I:%M:%S %p")
                wd_state = state["watchdogs"][w_id]
                name = "Primary WAN" if w_id == "1" else "Secondary WAN"

                if is_online:
                    if not wd_state.get("online", True):
                        elapsed = (datetime.now() - wd_state["down_time"]).total_seconds() / 60
                        hist = load_history()
                        hist["watchdog"].append({
                            "target": f"{name} ({ip}:{port})", 
                            "start": wd_state["down_time"].strftime("%Y-%m-%d %I:%M %p"),
                            "end": datetime.now().strftime("%Y-%m-%d %I:%M %p"), 
                            "duration_mins": int(elapsed)
                        })
                        save_history(hist)

                        if wd_state.get("alert_sent"):
                            send_pushover("✅ Network Restored", f"{name} connection to {ip}:{port} restored.\nDowntime: {int(elapsed)} mins.", priority=0)
                    
                    wd_state["online"] = True
                    wd_state["down_time"] = None
                    wd_state["alert_sent"] = False
                else:
                    if wd_state.get("online", True):
                        wd_state["online"] = False
                        wd_state["down_time"] = datetime.now()
                        wd_state["alert_sent"] = False

                    if wd_state["down_time"]:
                        elapsed = (datetime.now() - wd_state["down_time"]).total_seconds() / 60
                        if elapsed >= thresh and not wd_state["alert_sent"]:
                            send_pushover("🌐 ⚠️ Network Offline", f"{name} connection to {ip}:{port} failed for >{thresh} mins.", priority=1)
                            wd_state["alert_sent"] = True

        for _ in range(60):
            if (app_config.get("watchdog_ip") != c_ip1 or app_config.get("watchdog_ip_2") != c_ip2 or
                app_config.get("watchdog_port") != c_port1 or app_config.get("watchdog_port_2") != c_port2):
                break
            time.sleep(1)

def poll_nut():
    while True:
        c_host1 = app_config.get("nut_host")
        c_port1 = app_config.get("nut_port", 3493)
        c_names1 = app_config.get("nut_ups_names", "auto")
        c_thresh1 = app_config.get("ups_min_runtime", 10)

        c_host2 = app_config.get("nut_host_2")
        c_port2 = app_config.get("nut_port_2", 3493)
        c_names2 = app_config.get("nut_ups_names_2", "auto")
        c_thresh2 = app_config.get("ups_min_runtime_2", 10)

        errors = []

        def process_data(multi_ups_data, threshold):
            for ups_name, vars_dict in multi_ups_data.items():
                if ups_name not in state["ups_data"]:
                    state["ups_data"][ups_name] = {"alert_sent": False, "is_ob": False, "ob_start_time": None, "min_charge": 100}
                
                ups_state = state["ups_data"][ups_name]
                ups_state["status"] = vars_dict.get("ups.status", "UNKNOWN")
                ups_state["charge"] = int(float(vars_dict.get("battery.charge", 0)))
                ups_state["runtime_mins"] = int(float(vars_dict.get("battery.runtime", 0))) // 60
                
                if "OB" in ups_state["status"]:
                    if not ups_state["is_ob"]:
                        ups_state["is_ob"] = True
                        ups_state["ob_start_time"] = datetime.now()
                        ups_state["min_charge"] = ups_state["charge"]
                    else:
                        if ups_state["charge"] < ups_state["min_charge"]:
                            ups_state["min_charge"] = ups_state["charge"]

                    if ups_state["runtime_mins"] <= threshold and not ups_state["alert_sent"]:
                        send_pushover(title=f"⚠️ CRITICAL: {ups_name} Low!", message=f"UPS '{ups_name}' on battery, {ups_state['runtime_mins']} mins left.", priority=1)
                        ups_state["alert_sent"] = True

                elif "OL" in ups_state["status"]:
                    if ups_state["is_ob"] and ups_state["ob_start_time"]:
                        elapsed = (datetime.now() - ups_state["ob_start_time"]).total_seconds() / 60
                        hist = load_history()
                        hist["ups"].append({
                            "ups_name": ups_name, "start": ups_state["ob_start_time"].strftime("%Y-%m-%d %I:%M %p"),
                            "end": datetime.now().strftime("%Y-%m-%d %I:%M %p"), "duration_mins": int(elapsed), "min_charge": ups_state["min_charge"]
                        })
                        save_history(hist)
                        ups_state["is_ob"] = False
                        ups_state["ob_start_time"] = None
                        
                    if ups_state["alert_sent"]:
                        send_pushover(title=f"🔌 UPS {ups_name} Restored", message="Back on grid power.", priority=0)
                    ups_state["alert_sent"] = False

        if c_host1:
            data1 = fetch_nut_data(c_host1, c_port1, c_names1)
            if data1 is not None: process_data(data1, c_thresh1)
            else: errors.append(f"Primary NUT ({c_host1}) offline")

        if c_host2:
            data2 = fetch_nut_data(c_host2, c_port2, c_names2)
            if data2 is not None: process_data(data2, c_thresh2)
            else: errors.append(f"Secondary NUT ({c_host2}) offline")

        state["nut_last_check"] = datetime.now().strftime("%I:%M:%S %p")
        state["nut_error"] = " | ".join(errors) if errors else None
        
        for _ in range(30):
            if (app_config.get("nut_host") != c_host1 or app_config.get("nut_host_2") != c_host2 or
                app_config.get("nut_port") != c_port1 or app_config.get("nut_port_2") != c_port2 or
                app_config.get("nut_ups_names") != c_names1 or app_config.get("nut_ups_names_2") != c_names2):
                break
            time.sleep(1)

def poll_gp_outages():
    while True:
        url = app_config.get("kubra_url")
        map_url = app_config.get("map_url")
        zip_c = app_config.get("zip_code")
        thresh = app_config.get("threshold_mins", 45)
        company = app_config.get("company_name", "Utility")

        if not url and map_url and zip_c:
            if not state.get("discovery_failed"):
                state["last_check"] = "🔍 Discovering API..."
                discovered = auto_discover_api(map_url, zip_c)
                if discovered:
                    app_config["kubra_url"] = discovered
                    save_config(app_config)
                    url = discovered
                    state["last_check"] = "API Discovered! Starting poll..."
                    state["discovery_failed"] = False
                else:
                    state["error_msg"] = "Auto-discovery failed. Please enter the Outage API JSON URL manually."
                    state["last_check"] = "Failed"
                    state["discovery_failed"] = True
        
        if url and zip_c:
            try:
                req = requests.get(url, timeout=10)
                
                if req.status_code == 404 and map_url:
                    state["last_check"] = "🔧 Auto-Healing Link..."
                    logging.warning("API returned 404. Attempting auto-heal...")
                    discovered = auto_discover_api(map_url, zip_c)
                    if discovered:
                        app_config["kubra_url"] = discovered
                        save_config(app_config)
                        url = discovered
                        req = requests.get(url, timeout=10)
                        logging.info(f"✅ Successfully auto-healed API to: {url}")
                        state["discovery_failed"] = False
                    else:
                        state["discovery_failed"] = True
                        raise ValueError("Auto-heal failed. Map website structure might have changed.")
                        
                req.raise_for_status()
                report_data = req.json()
                state["last_check"] = datetime.now().strftime("%I:%M %p")
                state["error_msg"] = None
                affected = 0
                etr_found = "Unavailable"
                
                if "areas" in report_data:
                    for area in report_data.get("areas", []):
                        area_name = str(area.get("name", area.get("id", "")))
                        if zip_c in area_name:
                            cust_a = area.get("cust_a", 0)
                            affected = int(cust_a.get("val", 0)) if isinstance(cust_a, dict) else int(cust_a)
                            raw_etr = area.get("etr", "Unavailable")
                            etr_found = raw_etr.get("val", "Unavailable") if isinstance(raw_etr, dict) else str(raw_etr)
                            if not etr_found or etr_found.lower() == "none": etr_found = "Unavailable"
                            break
                            
                elif "zips" in report_data:
                    for z in report_data.get("zips", []):
                        if str(z.get("zipCode", "")) == zip_c:
                            affected = int(z.get("custOutPlan", 0)) + int(z.get("custOutUnplan", 0))
                            raw_etr = z.get("etr", z.get("estimatedTimeOfRestoration", "Unavailable"))
                            etr_found = raw_etr.get("val", "Unavailable") if isinstance(raw_etr, dict) else str(raw_etr)
                            if not etr_found or etr_found.lower() == "none": etr_found = "Unavailable"
                            break

                state["customers_affected"] = affected
                state["etr"] = etr_found

                if affected > 0:
                    if not state["is_outage"]:
                        state["is_outage"] = True
                        state["outage_start_time"] = datetime.now()
                        state["outage_max_affected"] = affected
                        state["alert_sent"] = False
                    else:
                        if affected > state["outage_max_affected"]:
                            state["outage_max_affected"] = affected
                    
                    elapsed = (datetime.now() - state["outage_start_time"]).total_seconds() / 60
                    if elapsed >= thresh and not state["alert_sent"]:
                        msg = f"Power out in {zip_c} for >{thresh} mins.\nAffected: {affected}\nEst. Restoration: {etr_found}"
                        send_pushover(title=f"🚨 {company} Outage Alert", message=msg, include_map=True)
                        state["alert_sent"] = True
                else:
                    if state["is_outage"]:
                        elapsed = (datetime.now() - state["outage_start_time"]).total_seconds() / 60
                        hist = load_history()
                        hist["grid"].append({
                            "company": company, "zip": zip_c, "start": state["outage_start_time"].strftime("%Y-%m-%d %I:%M %p"),
                            "end": datetime.now().strftime("%Y-%m-%d %I:%M %p"), "duration_mins": int(elapsed), "max_affected": state["outage_max_affected"]
                        })
                        save_history(hist)

                        msg = f"Restored in {zip_c}!\nOutage lasted {int(elapsed)} mins."
                        send_pushover(title=f"✅ {company} Power Restored", message=msg, priority=0)
                        
                    state["is_outage"] = False
                    state["outage_start_time"] = None
                    state["alert_sent"] = False
                    state["etr"] = "Unavailable"
            except Exception as e:
                state["error_msg"] = str(e)
                logging.error(f"API Error: {e}")

        for _ in range(300): 
            if app_config.get("kubra_url") != url or app_config.get("zip_code") != zip_c: break
            time.sleep(1)

if __name__ == "__main__":
    threading.Thread(target=poll_gp_outages, daemon=True).start()
    threading.Thread(target=poll_nut, daemon=True).start()
    threading.Thread(target=poll_watchdog, daemon=True).start()
    threading.Thread(target=poll_snmp, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)