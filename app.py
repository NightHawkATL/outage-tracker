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

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            cfg = json.load(f)
            # Add defaults for the new map/report URL fields if upgrading old config
            cfg.setdefault("map_url", "https://outagemap.georgiapower.com/")
            cfg.setdefault("report_url", "https://www.georgiapower.com/outage")
            return cfg
    
    config = {
        "company_name": "Georgia Power",
        "zip_code": os.getenv("ZIP_CODE", "30008"),
        "threshold_mins": int(os.getenv("THRESHOLD_MINUTES", "45")),
        "kubra_url": os.getenv("KUBRA_THEMATIC_URL", ""),
        "map_url": "https://outagemap.georgiapower.com/",
        "report_url": "https://www.georgiapower.com/outage",
        "nut_host": os.getenv("NUT_HOST", ""),
        "nut_port": int(os.getenv("NUT_PORT", "3493")),
        "nut_ups_names": os.getenv("NUT_UPS_NAMES", "auto"),
        "ups_min_runtime": int(os.getenv("UPS_MIN_RUNTIME_MINUTES", "10")),
        "pushover_user": os.getenv("PUSHOVER_USER_KEY", ""),
        "pushover_token": os.getenv("PUSHOVER_API_TOKEN", "")
    }
    save_config(config)
    return config

def save_config(config):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=4)

app_config = load_config()

# In-memory State
state = {
    "is_outage": False,
    "customers_affected": 0,
    "outage_start_time": None,
    "alert_sent": False,
    "last_check": None,
    "error_msg": None,
    
    "nut_enabled": bool(app_config.get("nut_host")),
    "ups_data": {},
    "nut_last_check": None,
    "nut_error": None
}

def send_pushover(title, message, priority=1):
    user = app_config.get("pushover_user")
    token = app_config.get("pushover_token")
    if not user or not token:
        return False
    try:
        resp = requests.post("https://api.pushover.net/1/messages.json", data={
            "token": token, "user": user, "title": title, "message": message, "priority": priority
        })
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

@app.route("/config", methods=["GET", "POST"])
def config_page():
    global app_config
    if request.method == "POST":
        app_config.update({
            "company_name": request.form.get("company_name"),
            "zip_code": request.form.get("zip_code"),
            "threshold_mins": int(request.form.get("threshold_mins", 45)),
            "kubra_url": request.form.get("kubra_url"),
            "map_url": request.form.get("map_url", ""),
            "report_url": request.form.get("report_url", ""),
            "nut_host": request.form.get("nut_host", "").strip(),
            "nut_port": int(request.form.get("nut_port", 3493)),
            "nut_ups_names": request.form.get("nut_ups_names", "auto").strip(),
            "ups_min_runtime": int(request.form.get("ups_min_runtime", 10)),
            "pushover_user": request.form.get("pushover_user", "").strip(),
            "pushover_token": request.form.get("pushover_token", "").strip(),
        })
        save_config(app_config)
        state["nut_enabled"] = bool(app_config["nut_host"])
        state["is_outage"] = False
        state["outage_start_time"] = None
        state["alert_sent"] = False
        return redirect(url_for('index'))
    return render_template("config.html", config=app_config)

@app.route("/test-pushover", methods=["POST"])
def test_pushover():
    if send_pushover("🔔 Pushover Test", "Your configuration is working perfectly.", priority=0):
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
                    if line.startswith('UPS '):
                        ups_list.append(line.split(' ')[1])
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
                        if len(parts) == 4:
                            vars_dict[parts[2]] = parts[3].strip('"')
                results[ups_name] = vars_dict
        return results
    except:
        return None

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
                        state["ups_data"][ups_name] = {"alert_sent": False}
                    
                    ups_state = state["ups_data"][ups_name]
                    ups_state["status"] = vars_dict.get("ups.status", "UNKNOWN")
                    ups_state["charge"] = int(float(vars_dict.get("battery.charge", 0)))
                    ups_state["runtime_mins"] = int(float(vars_dict.get("battery.runtime", 0))) // 60
                    
                    if "OB" in ups_state["status"]:
                        if ups_state["runtime_mins"] <= app_config["ups_min_runtime"] and not ups_state["alert_sent"]:
                            send_pushover(title=f"⚠️ CRITICAL: {ups_name} Low!", message=f"UPS '{ups_name}' on battery, {ups_state['runtime_mins']} mins left.", priority=1)
                            ups_state["alert_sent"] = True
                    elif "OL" in ups_state["status"]:
                        if ups_state["alert_sent"]:
                            send_pushover(title=f"🔌 UPS {ups_name} Restored", message="Back on grid power.", priority=0)
                        ups_state["alert_sent"] = False
            else:
                state["nut_error"] = f"Failed to connect to NUT Server"
        
        # Smart Wait: Sleep 30s, but break instantly if config is changed
        for _ in range(30):
            if app_config.get("nut_host") != current_host: break
            time.sleep(1)

def poll_gp_outages():
    while True:
        url = app_config.get("kubra_url")
        zip_c = app_config.get("zip_code")
        thresh = app_config.get("threshold_mins")
        company = app_config.get("company_name")

        if url:
            try:
                req = requests.get(url, timeout=10)
                req.raise_for_status()
                report_data = req.json()
                
                state["last_check"] = datetime.now().strftime("%I:%M %p")
                state["error_msg"] = None
                affected = 0
                
                for area in report_data.get("areas", []):
                    area_name = str(area.get("name", area.get("id", "")))
                    if zip_c in area_name:
                        cust_a = area.get("cust_a", 0)
                        affected = int(cust_a.get("val", 0)) if isinstance(cust_a, dict) else int(cust_a)
                        break

                state["customers_affected"] = affected

                if affected > 0:
                    if not state["is_outage"]:
                        state["is_outage"] = True
                        state["outage_start_time"] = datetime.now()
                        state["alert_sent"] = False
                    
                    elapsed = (datetime.now() - state["outage_start_time"]).total_seconds() / 60
                    if elapsed >= thresh and not state["alert_sent"]:
                        send_pushover(title=f"🚨 {company} Outage Alert", message=f"Power out in {zip_c} for >{thresh} mins.\nAffected: {affected}")
                        state["alert_sent"] = True
                else:
                    if state["is_outage"]:
                        send_pushover(title=f"✅ {company} Power Restored", message=f"Restored in {zip_c}!", priority=0)
                    state["is_outage"] = False
                    state["outage_start_time"] = None
                    state["alert_sent"] = False

            except Exception as e:
                state["error_msg"] = str(e)
                logging.error(f"API Error: {e}")

        # Smart Wait: Sleep 5 mins, but break instantly if config URL is updated
        for _ in range(300): 
            if app_config.get("kubra_url") != url: break
            time.sleep(1)

if __name__ == "__main__":
    threading.Thread(target=poll_gp_outages, daemon=True).start()
    threading.Thread(target=poll_nut, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)