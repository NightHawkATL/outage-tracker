import os
import time
import socket
import threading
import requests
import logging
from datetime import datetime
from flask import Flask, render_template

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

# Environment Variables
ZIP_CODE = os.getenv("ZIP_CODE", "30008")
THRESHOLD_MINUTES = int(os.getenv("THRESHOLD_MINUTES", "45"))
PUSHOVER_USER = os.getenv("PUSHOVER_USER_KEY")
PUSHOVER_TOKEN = os.getenv("PUSHOVER_API_TOKEN")
KUBRA_THEMATIC_URL = os.getenv("KUBRA_THEMATIC_URL")

NUT_HOST = os.getenv("NUT_HOST")
NUT_PORT = int(os.getenv("NUT_PORT", "3493"))
NUT_UPS_NAME = os.getenv("NUT_UPS_NAME", "ups")
UPS_MIN_RUNTIME_MINUTES = int(os.getenv("UPS_MIN_RUNTIME_MINUTES", "10"))

# In-memory State
state = {
    "is_outage": False,
    "customers_affected": 0,
    "outage_start_time": None,
    "alert_sent": False,
    "last_check": None,
    "error_msg": None,
    
    # NUT Data
    "nut_enabled": bool(NUT_HOST),
    "ups_status": "UNKNOWN",
    "ups_charge": 0,
    "ups_runtime_mins": 0,
    "nut_last_check": None,
    "nut_error": None,
    "nut_alert_sent": False
}

def send_pushover(title, message, priority=1):
    if not PUSHOVER_USER or not PUSHOVER_TOKEN:
        logging.warning("Pushover keys not set. Skipping notification.")
        return
    try:
        requests.post("https://api.pushover.net/1/messages.json", data={
            "token": PUSHOVER_TOKEN,
            "user": PUSHOVER_USER,
            "title": title,
            "message": message,
            "priority": priority
        })
        logging.info(f"Pushover alert sent: {title}")
    except Exception as e:
        logging.error(f"Failed to send Pushover alert: {e}")

# --- NUT Integration ---
def fetch_nut_vars():
    vars_dict = {}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect((NUT_HOST, NUT_PORT))
            s.sendall(f"LIST VAR {NUT_UPS_NAME}\n".encode('ascii'))
            
            data = b""
            while True:
                chunk = s.recv(4096)
                if not chunk: break
                data += chunk
                if b"END LIST VAR" in data:
                    break
        
        for line in data.decode('ascii').split('\n'):
            if line.startswith('VAR'):
                parts = line.strip().split(' ', 3)
                if len(parts) == 4:
                    v_name = parts[2]
                    v_val = parts[3].strip('"')
                    vars_dict[v_name] = v_val
        return vars_dict
    except Exception as e:
        return None

def poll_nut():
    if not NUT_HOST:
        return
    while True:
        data = fetch_nut_vars()
        state["nut_last_check"] = datetime.now().strftime("%I:%M:%S %p")
        
        if data:
            state["nut_error"] = None
            state["ups_status"] = data.get("ups.status", "UNKNOWN")
            state["ups_charge"] = int(float(data.get("battery.charge", 0)))
            runtime_sec = int(float(data.get("battery.runtime", 0)))
            state["ups_runtime_mins"] = runtime_sec // 60
            
            if "OB" in state["ups_status"]:
                if state["ups_runtime_mins"] <= UPS_MIN_RUNTIME_MINUTES and not state["nut_alert_sent"]:
                    send_pushover(
                        title="CRITICAL: UPS Battery Low!",
                        message=f"Your local UPS is on battery with {state['ups_runtime_mins']} mins remaining.",
                        priority=1
                    )
                    state["nut_alert_sent"] = True
            elif "OL" in state["ups_status"]:
                if state["nut_alert_sent"]:
                    send_pushover(
                        title="UPS Power Restored",
                        message="Your home UPS is back on grid power.",
                        priority=0
                    )
                state["nut_alert_sent"] = False
        else:
            state["nut_error"] = f"Failed to connect to {NUT_HOST}:{NUT_PORT}"
            
        time.sleep(30)

# --- Georgia Power Integration ---
def poll_gp_outages():
    while True:
        try:
            if not KUBRA_THEMATIC_URL:
                raise ValueError("KUBRA_THEMATIC_URL is not configured.")

            req = requests.get(KUBRA_THEMATIC_URL, timeout=10)
            req.raise_for_status()
            report_data = req.json()
            
            state["last_check"] = datetime.now().strftime("%I:%M %p")
            state["error_msg"] = None
            affected_in_zip = 0
            
            # KUBRA V5 stores regions in 'areas'
            areas = report_data.get("areas", [])
            
            for area in areas:
                area_name = str(area.get("name", area.get("id", "")))
                if ZIP_CODE in area_name:
                    cust_a = area.get("cust_a", 0)
                    # Handle data structure variations safely
                    if isinstance(cust_a, dict):
                        affected_in_zip = int(cust_a.get("val", 0))
                    else:
                        affected_in_zip = int(cust_a)
                    break

            state["customers_affected"] = affected_in_zip

            if affected_in_zip > 0:
                if not state["is_outage"]:
                    state["is_outage"] = True
                    state["outage_start_time"] = datetime.now()
                    state["alert_sent"] = False
                
                elapsed = (datetime.now() - state["outage_start_time"]).total_seconds() / 60
                if elapsed >= THRESHOLD_MINUTES and not state["alert_sent"]:
                    send_pushover(
                        title="GP Outage Threshold Reached",
                        message=f"Power out in {ZIP_CODE} for >{THRESHOLD_MINUTES} mins.\nCustomers Affected: {affected_in_zip}"
                    )
                    state["alert_sent"] = True
            else:
                if state["is_outage"]:
                    elapsed = (datetime.now() - state["outage_start_time"]).total_seconds() / 60
                    send_pushover(
                        title="GP Area Power Restored",
                        message=f"Power in {ZIP_CODE} restored. Outage lasted {int(elapsed)} mins.",
                        priority=0
                    )
                state["is_outage"] = False
                state["outage_start_time"] = None
                state["alert_sent"] = False

        except Exception as e:
            state["error_msg"] = str(e)
            logging.error(f"Error fetching GP data: {e}")

        time.sleep(300) # Poll every 5 minutes

@app.route("/")
def index():
    duration = 0
    if state["is_outage"] and state["outage_start_time"]:
        duration = int((datetime.now() - state["outage_start_time"]).total_seconds() / 60)
    
    return render_template("index.html", state=state, zip=ZIP_CODE, threshold=THRESHOLD_MINUTES, duration=duration)

if __name__ == "__main__":
    threading.Thread(target=poll_gp_outages, daemon=True).start()
    threading.Thread(target=poll_nut, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)