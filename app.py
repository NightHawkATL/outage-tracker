import os
import time
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
KUBRA_CONFIG_URL = os.getenv("KUBRA_CONFIG_URL")

# In-memory State
state = {
    "is_outage": False,
    "customers_affected": 0,
    "outage_start_time": None,
    "alert_sent": False,
    "last_check": None,
    "error_msg": None
}

def send_pushover(title, message):
    if not PUSHOVER_USER or not PUSHOVER_TOKEN:
        logging.warning("Pushover keys not set. Skipping notification.")
        return
    try:
        requests.post("https://api.pushover.net/1/messages.json", data={
            "token": PUSHOVER_TOKEN,
            "user": PUSHOVER_USER,
            "title": title,
            "message": message,
            "priority": 1 # High priority
        })
        logging.info(f"Pushover alert sent: {title}")
    except Exception as e:
        logging.error(f"Failed to send Pushover alert: {e}")

def get_kubra_data():
    """Fetches the dynamic Kubra report data based on the config interval."""
    try:
        # 1. Fetch the static config to find the current dynamic directory
        config_req = requests.get(KUBRA_CONFIG_URL, timeout=10)
        config_req.raise_for_status()
        config_data = config_req.json()
        
        # KUBRA configs usually store the current data path under 'directory'
        current_dir = config_data.get('directory') 
        if not current_dir:
            raise ValueError("Could not find 'directory' in KUBRA config.")

        # 2. Construct the URL for the zip code report
        # The base URL is derived from the config URL
        base_url = KUBRA_CONFIG_URL.split('/resources/')[0]
        zip_report_url = f"{base_url}/resources/data/external/datapoint_generation/{current_dir}/outages/report_zip.json"

        # 3. Fetch the Zip Code Report
        report_req = requests.get(zip_report_url, timeout=10)
        report_req.raise_for_status()
        return report_req.json()

    except Exception as e:
        state["error_msg"] = str(e)
        logging.error(f"Error fetching KUBRA data: {e}")
        return None

def check_outages():
    while True:
        report_data = get_kubra_data()
        state["last_check"] = datetime.now().strftime("%I:%M %p")
        
        if report_data:
            state["error_msg"] = None
            affected_in_zip = 0
            
            # Search for our Zip Code in the report payload
            # Usually stored in report_data['file_data']
            file_data = report_data.get('file_data', [])
            for region in file_data:
                if ZIP_CODE in str(region.get('name', '')):
                    affected_in_zip = int(region.get('cust_a', 0))
                    break

            state["customers_affected"] = affected_in_zip

            if affected_in_zip > 0:
                if not state["is_outage"]:
                    # New Outage Detected
                    state["is_outage"] = True
                    state["outage_start_time"] = datetime.now()
                    state["alert_sent"] = False
                    logging.info(f"Outage detected in {ZIP_CODE}. Affected: {affected_in_zip}")
                
                # Check if threshold is met
                elapsed = (datetime.now() - state["outage_start_time"]).total_seconds() / 60
                if elapsed >= THRESHOLD_MINUTES and not state["alert_sent"]:
                    send_pushover(
                        title="🚨 Georgia Power Outage Alert",
                        message=f"Power has been out in {ZIP_CODE} for >{THRESHOLD_MINUTES} mins.\nCustomers Affected: {affected_in_zip}"
                    )
                    state["alert_sent"] = True
            else:
                # Power is on / restored
                if state["is_outage"]:
                    elapsed = (datetime.now() - state["outage_start_time"]).total_seconds() / 60
                    send_pushover(
                        title="✅ Power Restored",
                        message=f"Power in {ZIP_CODE} has been restored! Outage lasted {int(elapsed)} mins."
                    )
                state["is_outage"] = False
                state["outage_start_time"] = None
                state["alert_sent"] = False

        # Wait 5 minutes before polling again
        time.sleep(300)

@app.route("/")
def index():
    # Calculate duration if active
    duration = 0
    if state["is_outage"] and state["outage_start_time"]:
        duration = int((datetime.now() - state["outage_start_time"]).total_seconds() / 60)
    
    return render_template("index.html", state=state, zip=ZIP_CODE, threshold=THRESHOLD_MINUTES, duration=duration)

if __name__ == "__main__":
    # Start background polling daemon
    threading.Thread(target=check_outages, daemon=True).start()
    # Start web server
    app.run(host="0.0.0.0", port=8080)