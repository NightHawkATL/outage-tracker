import os
import time
import socket
import threading
import requests
import logging
import json
from datetime import datetime
from flask import Flask, render_template, jsonify, request, redirect, url_for

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

CONFIG_FILE = "data/config.json"
HISTORY_FILE = "data/history.json"
os.makedirs("static", exist_ok=True)
os.makedirs("data", exist_ok=True)

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            cfg = json.load(f)
            cfg.setdefault("map_url", "")
            cfg.setdefault("report_url", "")
            cfg.setdefault("mapbox_token", "")
            cfg.setdefault("latitude", "")
            cfg.setdefault("longitude", "")
            return cfg
    
    config = {
        "company_name": "", "zip_code": "", "threshold_mins": 45,
        "kubra_url": "", "map_url": "", "report_url": "",
        "nut_host": "", "nut_port": 3493, "nut_ups_names": "auto", "ups_min_runtime": 10,
        "pushover_user": "", "pushover_token": "",
        "mapbox_token": "", "latitude": "", "longitude": ""
    }
    save_config(config)
    return config

def save_config(config):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=4)

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, 'r') as f:
            return json.load(f)
    return {"grid": [], "ups": []}

def save_history(history):
    history["grid"] = history["grid"][-50:]
    history["ups"] = history["ups"][-50:]
    with open(HISTORY_FILE, 'w') as f:
        json.dump(history, f, indent=4)

app_config = load_config()

state = {
    "is_outage": False, "customers_affected": 0, "outage_start_time": None, "outage_max_affected": 0,
    "alert_sent": False, "last_check": None, "error_msg": None, "etr": "Unavailable",
    "nut_enabled": bool(app_config.get("nut_host")), "ups_data": {}, "nut_last_check": None, "nut_error": None
}

def update_outage_map():
    token = app_config.get("mapbox_token")
    lat = app_config.get("latitude")
    lon = app_config.get("longitude")
    
    if not token or not lat or not lon:
        return None
        
    url = f"https://api.mapbox.com/styles/v1/mapbox/dark-v11/static/pin-l+f44336({lon},{lat})/{lon},{lat},13,0/800x400@2x?access_token={token}"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        filepath = "static/outage_map.jpg"
        with open(filepath, 'wb') as f:
            f.write(resp.content)
        return filepath
    except Exception as e:
        logging.error(f"Mapbox Generation Error: {e}")
        return None

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
    except Exception as e:
        logging.error(f"Pushover Error: {e}")
        return False

@app.route("/")
def index():
    duration = 0
    if state["is_outage"] and state["outage_start_time"]:
        duration = int((datetime.now() - state["outage_start_time"]).total_seconds() / 60)
    return render_template("index.html", state=state, config=app_config, duration=duration)

@app.route("/history")
def history_page():
    history_data = load_history()
    history_data["grid"] = history_data["grid"][::-1]
    history_data["ups"] = history_data["ups"][::-1]
    return render_template("history.html", state=state, config=app_config, history=history_data)

@app.route("/config", methods=["GET", "POST"])
def config_page():
    global app_config
    if request.method == "POST":
        
        # Helper function for "Write-Only" secure fields
        def get_secure(field_name):
            val = request.form.get(field_name, "").strip()
            if not val: 
                return app_config.get(field_name, "") # Keep existing if blank
            if val.lower() == "clear": 
                return "" # Delete the key if user typed "clear"
            return val # Save new key

        app_config.update({
            "company_name": request.form.get("company_name", "").strip(),
            "zip_code": request.form.get("zip_code", "").strip(),
            "threshold_mins": int(request.form.get("threshold_mins", 45)),
            "kubra_url": request.form.get("kubra_url", "").strip(),
            "map_url": request.form.get("map_url", "").strip(),
            "report_url": request.form.get("report_url", "").strip(),
            "nut_host": request.form.get("nut_host", "").strip(),
            "nut_port": int(request.form.get("nut_port", 3493)),
            "nut_ups_names": request.form.get("nut_ups_names", "auto").strip(),
            "ups_min_runtime": int(request.form.get("ups_min_runtime", 10)),
            "latitude": get_secure("latitude"),
            "longitude": get_secure("longitude"),
            "mapbox_token": get_secure("mapbox_token"),
            "pushover_user": get_secure("pushover_user"),
            "pushover_token": get_secure("pushover_token"),
        })
        save_config(app_config)
        state["nut_enabled"] = bool(app_config["nut_host"])
        state["is_outage"] = False
        state["outage_start_time"] = None
        state["alert_sent"] = False
        state["etr"] = "Unavailable"
        return redirect(url_for('index'))
    return render_template("config.html", config=app_config)

@app.route("/test-pushover", methods=["POST"])
def test_pushover():
    if send_pushover("🔔 Pushover Test", "Configuration working perfectly.", priority=0, include_map=True):
        return jsonify({"status": "success", "message": "Test sent! Check your device."})
    return jsonify({"status": "error", "message": "Failed to send alert. Check keys."}), 500

def fetch_nut_data():
    host = app_config.get("nut_host")
    port = app_config.get("nut_port", 3493)
    names = app_config.get("nut_ups_names", "auto")
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

def poll_nut():
    while True:
        current_host = app_config.get("nut_host")
        if current_host:
            multi_ups_data = fetch_nut_data()
            state["nut_last_check"] = datetime.now().strftime("%I:%M:%S %p")
            if multi_ups_data is not None:
                state["nut_error"] = None
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

                        if ups_state["runtime_mins"] <= app_config["ups_min_runtime"] and not ups_state["alert_sent"]:
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
            else:
                state["nut_error"] = f"Failed to connect to NUT Server"
        for _ in range(30):
            if app_config.get("nut_host") != current_host: break
            time.sleep(1)

def poll_gp_outages():
    while True:
        url = app_config.get("kubra_url")
        zip_c = app_config.get("zip_code")
        thresh = app_config.get("threshold_mins")
        company = app_config.get("company_name", "Utility")

        if url and zip_c:
            try:
                req = requests.get(url, timeout=10)
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