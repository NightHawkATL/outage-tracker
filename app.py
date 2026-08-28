import os
import time
import socket
import ssl
import signal
import shutil
import threading
import requests
import logging
import json
import subprocess
import re
import urllib.parse
import hashlib
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template, jsonify, request, redirect, url_for, session
from cryptography.fernet import Fernet
from werkzeug.security import generate_password_hash, check_password_hash
import paho.mqtt.publish as mqtt_publish

app = Flask(__name__)
APP_VERSION = os.environ.get("APP_VERSION", "dev")

app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=3650) 
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

CONFIG_FILE = "data/config.json"
HISTORY_FILE = "data/history.json"
HISTORY_BACKUP_FILE = "data/history_backup.json"
HISTORY_DATE_FIELDS = {"grid": "start", "ups": "start", "watchdog": "start", "snmp": "time"}
KEY_DIR = "/app/auth_key"
KEY_FILE = os.path.join(KEY_DIR, "secret.key")
MQTT_PUBLISH_LOCK = threading.Lock()
MQTT_DISCOVERY_STATE = {"signature": None, "published_at": 0}
FAST_REFRESH_SECONDS = 30
IDLE_REFRESH_SECONDS = 300
MQTT_STARTUP_SUPPRESS_SECONDS = 120

# --- Docker Hub Update Check (with caching) ---
_update_cache = {"latest": None, "checked": 0, "error": None}
_update_cache_lock = threading.Lock()

# --- Tailscale Update Check (with caching) ---
_ts_update_cache = {"installed": None, "available": None, "update_available": False, "checked": 0, "error": None}
_ts_update_cache_lock = threading.Lock()
_ts_update_refresh_in_progress = False
TS_UPDATE_CACHE_TTL = 3600  # seconds (1 hour)
TAILSCALED_STATE_ARG = "--state=/app/data/tailscaled.state"
DOCKERHUB_REPO = "nighthawkatl/outage-tracker"
DOCKERHUB_TAGS_URL = f"https://hub.docker.com/v2/repositories/{DOCKERHUB_REPO}/tags?page_size=50&page=1&ordering=last_updated"
_CACHE_TTL = 3600  # seconds (1 hour)


def parse_semver_tag(tag):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", str(tag).strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def newest_versioned_tag(tag_names):
    candidates = []
    for name in tag_names:
        parsed = parse_semver_tag(name)
        if parsed is not None:
            candidates.append((parsed, name))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def update_available(current_tag, latest_tag):
    current_semver = parse_semver_tag(current_tag)
    latest_semver = parse_semver_tag(latest_tag)

    if current_semver is not None and latest_semver is not None:
        return latest_semver > current_semver

    return bool(latest_tag) and str(latest_tag).strip() != str(current_tag).strip()


def needs_fast_refresh():
    if state.get("is_outage") and state.get("outage_start_time"):
        return True

    for watchdog_state in state["watchdogs"].values():
        if not watchdog_state.get("online", True) and watchdog_state.get("down_time"):
            return True

    if state.get("nut_enabled"):
        for ups in state.get("ups_data", {}).values():
            if "OB" in ups.get("status", ""):
                return True

    last_check = state.get("last_check") or ""
    return "Discover" in last_check or "Heal" in last_check


def refresh_interval_seconds():
    return FAST_REFRESH_SECONDS if needs_fast_refresh() else IDLE_REFRESH_SECONDS


def mqtt_startup_suppression_active():
    started_at = state.get("process_started_at", time.time())
    return (time.time() - started_at) < MQTT_STARTUP_SUPPRESS_SECONDS


def mqtt_connectivity_state_verified():
    if not mqtt_startup_suppression_active():
        return True

    for w_id in ["1", "2"]:
        suffix = "" if w_id == "1" else "_2"
        if app_config.get(f"watchdog_ip{suffix}"):
            wd_state = state["watchdogs"][w_id]
            if wd_state.get("last_check") and wd_state.get("online") is False and not wd_state.get("ever_online"):
                return False

    for s_id in ["1", "2"]:
        suffix = "" if s_id == "1" else "_2"
        if app_config.get(f"snmp_ip{suffix}"):
            snmp_state = state["snmp"][s_id]
            if snmp_state.get("last_check") and snmp_state.get("online") is False and not snmp_state.get("ever_online"):
                return False

    return True


def mqtt_initial_state_ready():
    has_grid = bool(app_config.get("zip_code") and (app_config.get("kubra_url") or app_config.get("map_url")))
    if has_grid and not (state.get("last_check") or state.get("error_msg") or state.get("discovery_failed")):
        return False

    if state.get("nut_enabled") and not state.get("nut_last_check"):
        return False

    if (app_config.get("watchdog_ip") or app_config.get("watchdog_ip_2")) and not state.get("watchdog_last_check"):
        return False

    for s_id in ["1", "2"]:
        suffix = "" if s_id == "1" else "_2"
        if app_config.get(f"snmp_ip{suffix}") and not state["snmp"][s_id].get("last_check"):
            return False

    if not mqtt_connectivity_state_verified():
        return False

    return True

def get_latest_dockerhub_tag():
    now = time.time()
    with _update_cache_lock:
        if _update_cache["checked"] and now - _update_cache["checked"] < _CACHE_TTL:
            return _update_cache["latest"], _update_cache["error"]
        try:
            resp = requests.get(DOCKERHUB_TAGS_URL, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            if results:
                tag_names = [r.get("name", "") for r in results if r.get("name")]
                tag = newest_versioned_tag(tag_names)
                if tag:
                    _update_cache["latest"] = tag
                    _update_cache["error"] = None
                else:
                    tag = None
                    _update_cache["latest"] = None
                    _update_cache["error"] = "No semantic version tags found"
            else:
                tag = None
                _update_cache["latest"] = None
                _update_cache["error"] = "No tags found"
        except Exception as e:
            tag = None
            _update_cache["latest"] = None
            _update_cache["error"] = str(e)
        _update_cache["checked"] = now
        return _update_cache["latest"], _update_cache["error"]


def get_tailscale_installed_version():
    try:
        res = subprocess.run(["tailscale", "version"], capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            first_line = res.stdout.strip().splitlines()[0].strip() if res.stdout.strip() else ""
            return first_line.split()[0] if first_line else None
    except Exception:
        pass
    return None


def _refresh_tailscale_update_cache():
    global _ts_update_refresh_in_progress
    installed = get_tailscale_installed_version()
    available = None
    error = None
    try:
        subprocess.run(["apk", "update"], capture_output=True, text=True, timeout=10)
        res = subprocess.run(["apk", "list", "--upgradable"], capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                if line.startswith("tailscale-"):
                    match = re.match(r"tailscale-([\d.]+)-r\d+", line)
                    if match:
                        available = match.group(1)
                    break
        else:
            error = res.stderr.strip() or "apk list failed"
    except Exception as exc:
        error = str(exc)

    with _ts_update_cache_lock:
        _ts_update_cache.update({
            "installed": installed,
            "available": available,
            "update_available": bool(available) and update_available(installed, available),
            "checked": time.time(),
            "error": error,
        })
        _ts_update_refresh_in_progress = False


def get_tailscale_update_info(force=False):
    global _ts_update_refresh_in_progress

    if force:
        _refresh_tailscale_update_cache()
        with _ts_update_cache_lock:
            return dict(_ts_update_cache)

    now = time.time()
    with _ts_update_cache_lock:
        is_stale = not _ts_update_cache["checked"] or (now - _ts_update_cache["checked"] >= TS_UPDATE_CACHE_TTL)
        should_start_refresh = is_stale and not _ts_update_refresh_in_progress
        if should_start_refresh:
            _ts_update_refresh_in_progress = True
        snapshot = dict(_ts_update_cache)

    # Never block the page render on apk/network calls; refresh in the background instead.
    if should_start_refresh:
        threading.Thread(target=_refresh_tailscale_update_cache, daemon=True).start()

    return snapshot


def find_tailscaled_pid():
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/comm", "r") as f:
                    if f.read().strip() == "tailscaled":
                        return int(entry)
            except Exception:
                continue
    except Exception:
        pass
    return None


def restart_tailscaled():
    pid = find_tailscaled_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
        for _ in range(20):
            if find_tailscaled_pid() is None:
                break
            time.sleep(0.5)

    subprocess.Popen(["tailscaled", TAILSCALED_STATE_ARG], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)

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
            cfg.setdefault("snmp_version", "2c")
            cfg.setdefault("snmp_port", 161)
            cfg.setdefault("snmp_oid", "1.3.6.1.2.1.1.3.0")
            cfg.setdefault("snmp_community", "public")
            cfg.setdefault("snmp_v3_username", "")
            cfg.setdefault("snmp_v3_auth_protocol", "SHA")
            cfg.setdefault("snmp_v3_auth_password", "")
            cfg.setdefault("snmp_v3_priv_protocol", "AES")
            cfg.setdefault("snmp_v3_priv_password", "")
            cfg.setdefault("snmp_ip_2", "")
            cfg.setdefault("snmp_name_2", "")
            cfg.setdefault("snmp_version_2", "2c")
            cfg.setdefault("snmp_port_2", 161)
            cfg.setdefault("snmp_oid_2", "1.3.6.1.2.1.1.3.0")
            cfg.setdefault("snmp_community_2", "public")
            cfg.setdefault("snmp_v3_username_2", "")
            cfg.setdefault("snmp_v3_auth_protocol_2", "SHA")
            cfg.setdefault("snmp_v3_auth_password_2", "")
            cfg.setdefault("snmp_v3_priv_protocol_2", "AES")
            cfg.setdefault("snmp_v3_priv_password_2", "")
            cfg.setdefault("mqtt_host", "")
            cfg.setdefault("mqtt_port", 1883)
            cfg.setdefault("mqtt_username", "")
            cfg.setdefault("mqtt_password", "")
            cfg.setdefault("mqtt_topic_prefix", "outage_tracker")
            cfg.setdefault("mqtt_discovery_prefix", "homeassistant")
            
            if "admin_username" not in cfg:
                cfg["admin_username"] = "admin"
            if not cfg.get("admin_password"):
                cfg["admin_password"] = generate_password_hash("admin")
            return cfg
    
    config = {
        "admin_username": "admin", "admin_password": generate_password_hash("admin"),
        "session_timeout": 24, "timezone": "America/New_York",
        "ui_layout": "2x2", "ui_text_size": "15px",
        "company_name": "", "zip_code": "", "threshold_mins": 45,
        "kubra_url": "", "map_url": "", "report_url": "",
        "nut_host": "", "nut_port": 3493, "nut_ups_names": "auto", "ups_min_runtime": 10,
        "nut_host_2": "", "nut_port_2": 3493, "nut_ups_names_2": "auto", "ups_min_runtime_2": 10,
        "snmp_ip": "", "snmp_name": "", "snmp_version": "2c", "snmp_port": 161,
        "snmp_oid": "1.3.6.1.2.1.1.3.0", "snmp_community": "public",
        "snmp_v3_username": "", "snmp_v3_auth_protocol": "SHA", "snmp_v3_auth_password": "",
        "snmp_v3_priv_protocol": "AES", "snmp_v3_priv_password": "",
        "snmp_ip_2": "", "snmp_name_2": "", "snmp_version_2": "2c", "snmp_port_2": 161,
        "snmp_oid_2": "1.3.6.1.2.1.1.3.0", "snmp_community_2": "public",
        "snmp_v3_username_2": "", "snmp_v3_auth_protocol_2": "SHA", "snmp_v3_auth_password_2": "",
        "snmp_v3_priv_protocol_2": "AES", "snmp_v3_priv_password_2": "",
        "pushover_user": "", "pushover_token": "",
        "mapbox_token": "", "latitude": "", "longitude": "", "ts_authkey": "",
        "watchdog_ip": "", "watchdog_port": 80, "watchdog_threshold": 5,
        "watchdog_ip_2": "", "watchdog_port_2": 80, "watchdog_threshold_2": 5,
        "mqtt_host": "", "mqtt_port": 1883, "mqtt_username": "", "mqtt_password": "",
        "mqtt_topic_prefix": "outage_tracker", "mqtt_discovery_prefix": "homeassistant"
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

def backup_history():
    if os.path.exists(HISTORY_FILE):
        shutil.copyfile(HISTORY_FILE, HISTORY_BACKUP_FILE)

def parse_history_timestamp(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d %I:%M %p")
    except Exception:
        return None

def filter_history_entries(entries, mode, cutoff_date, keep_count, date_field):
    if mode == "all":
        return []
    if mode == "count":
        if not keep_count or keep_count <= 0:
            return []
        return entries[-keep_count:]
    if mode == "date":
        if not cutoff_date:
            return entries
        kept = []
        for entry in entries:
            ts = parse_history_timestamp(entry.get(date_field, ""))
            if ts is None or ts >= cutoff_date:
                kept.append(entry)
        return kept
    return entries

app_config = load_config()

os.environ['TZ'] = app_config.get("timezone", "America/New_York")
time.tzset()

state = {
    "process_started_at": time.time(),
    "is_outage": False, "customers_affected": 0, "outage_start_time": None, "outage_max_affected": 0,
    "alert_sent": False, "last_check": None, "error_msg": None, "etr": "Unavailable",
    "discovery_failed": False,
    "nut_enabled": bool(app_config.get("nut_host") or app_config.get("nut_host_2")), 
    "ups_data": {}, "nut_last_check": None, "nut_error": None,
    "watchdogs": {
        "1": {"online": True, "down_time": None, "alert_sent": False, "ever_online": False, "last_check": None},
        "2": {"online": True, "down_time": None, "alert_sent": False, "ever_online": False, "last_check": None}
    },
    "watchdog_last_check": None,
    "snmp": {
        "1": {"online": False, "uptime_s": None, "last_check": None, "ever_online": False},
        "2": {"online": False, "uptime_s": None, "last_check": None, "ever_online": False}
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

def sanitize_topic_part(value):
    slug = re.sub(r'[^a-z0-9_]+', '_', str(value).strip().lower())
    return slug.strip('_') or "unknown"

def decrypt_if_possible(value):
    if not value:
        return ""
    try:
        return cipher_suite.decrypt(value.encode('utf-8')).decode('utf-8')
    except Exception:
        return value


def is_password_hash(value):
    if not value:
        return False
    text = str(value)
    return text.startswith(("pbkdf2:", "scrypt:", "argon2:"))


def verify_admin_password(stored_value, provided_password):
    if not stored_value:
        return False

    if is_password_hash(stored_value):
        try:
            return check_password_hash(stored_value, provided_password)
        except Exception:
            return False

    try:
        decrypted = cipher_suite.decrypt(str(stored_value).encode('utf-8')).decode('utf-8')
        return provided_password == decrypted
    except Exception:
        # Backward compatibility for legacy plaintext reset values.
        return provided_password == str(stored_value)

def mqtt_enabled():
    return bool(app_config.get("mqtt_host", "").strip())

def mqtt_device_info():
    return {
        "identifiers": ["outage_tracker"],
        "name": "Outage Tracker",
        "manufacturer": "NightHawk-ATL",
        "model": "Outage Tracker",
        "sw_version": APP_VERSION,
    }

def mqtt_auth_config():
    username = app_config.get("mqtt_username", "").strip()
    password = decrypt_if_possible(app_config.get("mqtt_password", ""))
    if username:
        return {"username": username, "password": password}
    return None

def build_dashboard_snapshot():
    now = datetime.now()
    watchdogs = {}
    watchdog_initialized = bool(state.get("watchdog_last_check"))
    for w_id in ["1", "2"]:
        wd_state = state["watchdogs"][w_id]
        duration = 0
        if not wd_state.get("online", True) and wd_state.get("down_time"):
            duration = int((now - wd_state["down_time"]).total_seconds() / 60)
        watchdogs[w_id] = {
            "online": wd_state.get("online", True) if watchdog_initialized else None,
            "alert_sent": wd_state.get("alert_sent", False),
            "down_minutes": duration,
            "target": app_config.get("watchdog_ip" if w_id == "1" else "watchdog_ip_2", ""),
            "port": app_config.get("watchdog_port" if w_id == "1" else "watchdog_port_2", 80),
        }

    snmp_devices = {}
    for s_id in ["1", "2"]:
        snmp_state = state["snmp"][s_id]
        suffix = "" if s_id == "1" else "_2"
        snmp_devices[s_id] = {
            "online": snmp_state.get("online", False) if snmp_state.get("last_check") else None,
            "uptime_seconds": snmp_state.get("uptime_s"),
            "uptime_human": format_uptime(snmp_state.get("uptime_s")),
            "last_check": snmp_state.get("last_check"),
            "name": app_config.get(f"snmp_name{suffix}") or f"Hardware {s_id}",
            "ip": app_config.get(f"snmp_ip{suffix}", ""),
        }

    ups = {}
    ups_on_battery = 0
    for ups_name, ups_state in state["ups_data"].items():
        status = ups_state.get("status", "UNKNOWN")
        if "OB" in status:
            ups_on_battery += 1
        ups[ups_name] = {
            "status": status,
            "charge": ups_state.get("charge"),
            "runtime_mins": ups_state.get("runtime_mins"),
            "alert_sent": ups_state.get("alert_sent", False),
            "on_battery": "OB" in status,
        }

    overall_status = "ok"
    if state.get("error_msg"):
        overall_status = "error"
    elif state.get("is_outage") or ups_on_battery > 0:
        overall_status = "alert"
    elif any(item["online"] is False for item in watchdogs.values() if item["target"]) or any(
        item["online"] is False for item in snmp_devices.values() if item["ip"]
    ):
        overall_status = "warning"

    return {
        "system": {
            "app_version": APP_VERSION,
            "timezone": app_config.get("timezone", "America/New_York"),
            "tailscale_status": get_ts_status(),
            "overall_status": overall_status,
            "published_at": now.isoformat(),
        },
        "grid": {
            "company": app_config.get("company_name", "Utility"),
            "zip_code": app_config.get("zip_code", ""),
            "is_outage": state.get("is_outage", False),
            "customers_affected": state.get("customers_affected", 0),
            "etr": state.get("etr", "Unavailable"),
            "last_check": state.get("last_check"),
            "error": state.get("error_msg"),
        },
        "nut": {
            "enabled": state.get("nut_enabled", False),
            "last_check": state.get("nut_last_check"),
            "error": state.get("nut_error"),
            "ups_on_battery": ups_on_battery,
            "ups": ups,
        },
        "watchdog": {
            "last_check": state.get("watchdog_last_check"),
            "targets": watchdogs,
        },
        "snmp": {
            "devices": snmp_devices,
        },
    }

def mqtt_messages_for_snapshot(snapshot, force_discovery=False):
    topic_prefix = app_config.get("mqtt_topic_prefix", "outage_tracker").strip() or "outage_tracker"
    discovery_prefix = app_config.get("mqtt_discovery_prefix", "homeassistant").strip() or "homeassistant"
    summary_topic = f"{topic_prefix}/summary"
    device = mqtt_device_info()
    messages = [{"topic": summary_topic, "payload": json.dumps(snapshot), "retain": True, "qos": 1}]

    discovery_signature = hashlib.sha256(
        json.dumps(
            {
                "summary_topic": summary_topic,
                "discovery_prefix": discovery_prefix,
                "ups_names": sorted(snapshot["nut"]["ups"].keys()),
            },
            sort_keys=True,
        ).encode('utf-8')
    ).hexdigest()
    should_publish_discovery = force_discovery or MQTT_DISCOVERY_STATE["signature"] != discovery_signature

    if should_publish_discovery:
        discovery_entities = [
            {
                "component": "sensor",
                "object_id": "overall_status",
                "name": "Overall Status",
                "value_template": "{{ value_json.system.overall_status }}",
                "icon": "mdi:state-machine",
            },
            {
                "component": "binary_sensor",
                "object_id": "grid_outage",
                "name": "Grid Outage Active",
                "value_template": "{{ value_json.grid.is_outage | string | lower }}",
                "payload_on": "true",
                "payload_off": "false",
                "device_class": "problem",
            },
            {
                "component": "sensor",
                "object_id": "grid_customers_affected",
                "name": "Grid Customers Affected",
                "value_template": "{{ value_json.grid.customers_affected }}",
                "icon": "mdi:transmission-tower-off",
            },
            {
                "component": "sensor",
                "object_id": "grid_etr",
                "name": "Grid Estimated Restoration",
                "value_template": "{{ value_json.grid.etr }}",
                "icon": "mdi:clock-outline",
            },
            {
                "component": "sensor",
                "object_id": "tailscale_status",
                "name": "Tailscale Status",
                "value_template": "{{ value_json.system.tailscale_status }}",
                "icon": "mdi:vpn",
            },
            {
                "component": "sensor",
                "object_id": "ups_on_battery_count",
                "name": "UPS On Battery Count",
                "value_template": "{{ value_json.nut.ups_on_battery }}",
                "icon": "mdi:battery-alert",
            },
            {
                "component": "sensor",
                "object_id": "nut_error",
                "name": "UPS Error",
                "value_template": "{{ value_json.nut.error | default('', true) }}",
                "icon": "mdi:alert-circle-outline",
            },
            {
                "component": "binary_sensor",
                "object_id": "watchdog_primary_online",
                "name": "Primary Watchdog Online",
                "value_template": "{{ value_json.watchdog.targets['1'].online | string | lower }}",
                "payload_on": "true",
                "payload_off": "false",
                "device_class": "connectivity",
            },
            {
                "component": "binary_sensor",
                "object_id": "watchdog_secondary_online",
                "name": "Secondary Watchdog Online",
                "value_template": "{{ value_json.watchdog.targets['2'].online | string | lower }}",
                "payload_on": "true",
                "payload_off": "false",
                "device_class": "connectivity",
            },
            {
                "component": "binary_sensor",
                "object_id": "snmp_primary_online",
                "name": "SNMP Primary Online",
                "value_template": "{{ value_json.snmp.devices['1'].online | string | lower }}",
                "payload_on": "true",
                "payload_off": "false",
                "device_class": "connectivity",
            },
            {
                "component": "binary_sensor",
                "object_id": "snmp_secondary_online",
                "name": "SNMP Secondary Online",
                "value_template": "{{ value_json.snmp.devices['2'].online | string | lower }}",
                "payload_on": "true",
                "payload_off": "false",
                "device_class": "connectivity",
            },
        ]

        for entity in discovery_entities:
            discovery_topic = f"{discovery_prefix}/{entity['component']}/outage_tracker/{entity['object_id']}/config"
            payload = {
                "name": entity["name"],
                "unique_id": f"outage_tracker_{entity['object_id']}",
                "state_topic": summary_topic,
                "value_template": entity["value_template"],
                "device": device,
                "object_id": f"outage_tracker_{entity['object_id']}",
            }
            for key in ["icon", "device_class", "payload_on", "payload_off"]:
                if key in entity:
                    payload[key] = entity[key]
            messages.append({"topic": discovery_topic, "payload": json.dumps(payload), "retain": True, "qos": 1})

        for ups_name in sorted(snapshot["nut"]["ups"].keys()):
            ups_slug = sanitize_topic_part(ups_name)
            ups_topic = f"{topic_prefix}/ups/{ups_slug}"
            messages.append({
                "topic": ups_topic,
                "payload": json.dumps(snapshot["nut"]["ups"][ups_name]),
                "retain": True,
                "qos": 1,
            })
            for field, label, component in [
                ("status", "Status", "sensor"),
                ("charge", "Charge", "sensor"),
                ("runtime_mins", "Runtime Minutes", "sensor"),
                ("on_battery", "On Battery", "binary_sensor"),
            ]:
                object_id = f"ups_{ups_slug}_{field}"
                discovery_topic = f"{discovery_prefix}/{component}/outage_tracker/{object_id}/config"
                payload = {
                    "name": f"UPS {ups_name} {label}",
                    "unique_id": f"outage_tracker_{object_id}",
                    "state_topic": ups_topic,
                    "value_template": f"{{{{ value_json.{field} }}}}",
                    "device": device,
                    "object_id": f"outage_tracker_{object_id}",
                }
                if field == "charge":
                    payload["unit_of_measurement"] = "%"
                    payload["icon"] = "mdi:battery"
                elif field == "runtime_mins":
                    payload["unit_of_measurement"] = "min"
                    payload["icon"] = "mdi:battery-clock"
                elif field == "status":
                    payload["icon"] = "mdi:battery-heart-variant"
                else:
                    payload["device_class"] = "battery"
                    payload["payload_on"] = "True"
                    payload["payload_off"] = "False"
                messages.append({"topic": discovery_topic, "payload": json.dumps(payload), "retain": True, "qos": 1})

        MQTT_DISCOVERY_STATE["signature"] = discovery_signature
        MQTT_DISCOVERY_STATE["published_at"] = time.time()

    return messages

def publish_mqtt_status(force_discovery=False):
    if not mqtt_enabled():
        return

    if not mqtt_initial_state_ready():
        return

    with MQTT_PUBLISH_LOCK:
        try:
            snapshot = build_dashboard_snapshot()
            messages = mqtt_messages_for_snapshot(snapshot, force_discovery=force_discovery)
            mqtt_publish.multiple(
                messages,
                hostname=app_config.get("mqtt_host", "").strip(),
                port=int(app_config.get("mqtt_port", 1883)),
                client_id="outage-tracker",
                auth=mqtt_auth_config(),
                keepalive=10,
            )
        except Exception as exc:
            logging.warning("MQTT publish failed: %s", exc)


def mqtt_heartbeat_loop():
    while True:
        publish_mqtt_status()
        interval = refresh_interval_seconds()

        for _ in range(interval):
            time.sleep(1)
            if refresh_interval_seconds() != interval:
                break

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
        cfg_pass = app_config.get("admin_password", "")
        if username == cfg_user and verify_admin_password(cfg_pass, password):
            if not is_password_hash(cfg_pass):
                app_config["admin_password"] = generate_password_hash(password)
                save_config(app_config)
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

@app.route('/api/update')
@login_required
def api_update():
    latest, error = get_latest_dockerhub_tag()
    current = APP_VERSION
    has_update = update_available(current, latest)
    return jsonify({
        "current": current,
        "latest": latest,
        "update": has_update,
        "error": error
    })

@app.route("/")
@login_required
def index():
    duration = 0
    event_active = needs_fast_refresh()
    
    if state["is_outage"] and state["outage_start_time"]:
        duration = int((datetime.now() - state["outage_start_time"]).total_seconds() / 60)
        
    wd_durations = {"1": 0, "2": 0}
    for w_id in ["1", "2"]:
        if not state["watchdogs"][w_id]["online"] and state["watchdogs"][w_id]["down_time"]:
            wd_durations[w_id] = int((datetime.now() - state["watchdogs"][w_id]["down_time"]).total_seconds() / 60)
                
    return render_template("index.html", state=state, config=app_config, duration=duration, wd_durations=wd_durations, ts_status=get_ts_status(), event_active=event_active, format_uptime=format_uptime, app_version=APP_VERSION)

@app.route("/history")
@login_required
def history_page():
    history_data = load_history()
    history_data["grid"] = history_data["grid"][::-1]
    history_data["ups"] = history_data["ups"][::-1]
    history_data["watchdog"] = history_data.get("watchdog", [])[::-1]
    history_data["snmp"] = history_data.get("snmp", [])[::-1]
    backup_available = os.path.exists(HISTORY_BACKUP_FILE)
    return render_template("history.html", state=state, config=app_config, history=history_data, backup_available=backup_available)

@app.route("/history/clear", methods=["POST"])
@login_required
def clear_history_route():
    category = request.form.get("category", "all")
    mode = request.form.get("mode", "all")

    cutoff_date = None
    if mode == "date":
        try:
            cutoff_date = datetime.strptime(request.form.get("clear_date", "").strip(), "%Y-%m-%d")
        except ValueError:
            cutoff_date = None

    keep_count = None
    if mode == "count":
        try:
            keep_count = int(request.form.get("clear_count", "").strip())
        except ValueError:
            keep_count = None

    history_data = load_history()
    backup_history()

    categories = ["grid", "ups", "watchdog", "snmp"] if category == "all" else [category]
    for cat in categories:
        if cat not in history_data:
            continue
        history_data[cat] = filter_history_entries(
            history_data[cat], mode, cutoff_date, keep_count, HISTORY_DATE_FIELDS.get(cat, "start")
        )

    save_history(history_data)
    return redirect(url_for('history_page'))

@app.route("/history/undo", methods=["POST"])
@login_required
def undo_history_clear_route():
    if os.path.exists(HISTORY_BACKUP_FILE):
        shutil.move(HISTORY_BACKUP_FILE, HISTORY_FILE)
    return redirect(url_for('history_page'))

@app.route("/config", methods=["GET", "POST"])
@login_required
def config_page():
    global app_config
    if request.method == "POST":
        def get_int(field_name, default_value):
            raw_val = request.form.get(field_name, "")
            if raw_val is None:
                return default_value
            text = str(raw_val).strip()
            if text == "":
                return default_value
            try:
                return int(text)
            except ValueError:
                return default_value

        def get_secure(field_name):
            val = request.form.get(field_name, "").strip()
            if not val: return app_config.get(field_name, "")
            if val.lower() == "clear": return ""
            return val

        def get_encrypted_secret(field_name):
            val = request.form.get(field_name, "").strip()
            if not val:
                return app_config.get(field_name, "")
            if val.lower() == "clear":
                return ""
            return cipher_suite.encrypt(val.encode('utf-8')).decode('utf-8')

        new_username = request.form.get("admin_username", "").strip()
        new_password = request.form.get("admin_password", "")
        if new_username: app_config["admin_username"] = new_username
        if new_password: app_config["admin_password"] = generate_password_hash(new_password)

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
            "session_timeout": get_int("session_timeout", 24), "timezone": new_tz,
            "ui_layout": request.form.get("ui_layout", "2x2"), "ui_text_size": request.form.get("ui_text_size", "15px"),
            "company_name": request.form.get("company_name", "").strip(), "zip_code": zip_c,
            "threshold_mins": get_int("threshold_mins", 45), "kubra_url": api_url,
            "map_url": map_url, "report_url": request.form.get("report_url", "").strip(),
            "nut_host": request.form.get("nut_host", "").strip(), "nut_port": get_int("nut_port", 3493),
            "nut_ups_names": request.form.get("nut_ups_names", "auto").strip(), "ups_min_runtime": get_int("ups_min_runtime", 10),
            "nut_host_2": request.form.get("nut_host_2", "").strip(), "nut_port_2": get_int("nut_port_2", 3493),
            "nut_ups_names_2": request.form.get("nut_ups_names_2", "auto").strip(), "ups_min_runtime_2": get_int("ups_min_runtime_2", 10),
            "watchdog_ip": request.form.get("watchdog_ip", "").strip(), "watchdog_port": get_int("watchdog_port", 80),
            "watchdog_threshold": get_int("watchdog_threshold", 5),
            "watchdog_ip_2": request.form.get("watchdog_ip_2", "").strip(), "watchdog_port_2": get_int("watchdog_port_2", 80),
            "watchdog_threshold_2": get_int("watchdog_threshold_2", 5),
            "snmp_ip": request.form.get("snmp_ip", "").strip(), "snmp_name": request.form.get("snmp_name", "").strip(),
            "snmp_version": request.form.get("snmp_version", "2c").strip().lower(),
            "snmp_port": get_int("snmp_port", 161),
            "snmp_oid": request.form.get("snmp_oid", "1.3.6.1.2.1.1.3.0").strip() or "1.3.6.1.2.1.1.3.0",
            "snmp_community": request.form.get("snmp_community", "public").strip(),
            "snmp_v3_username": request.form.get("snmp_v3_username", "").strip(),
            "snmp_v3_auth_protocol": request.form.get("snmp_v3_auth_protocol", "SHA").strip().upper() or "SHA",
            "snmp_v3_auth_password": get_encrypted_secret("snmp_v3_auth_password"),
            "snmp_v3_priv_protocol": request.form.get("snmp_v3_priv_protocol", "AES").strip().upper() or "AES",
            "snmp_v3_priv_password": get_encrypted_secret("snmp_v3_priv_password"),
            "snmp_ip_2": request.form.get("snmp_ip_2", "").strip(), "snmp_name_2": request.form.get("snmp_name_2", "").strip(),
            "snmp_version_2": request.form.get("snmp_version_2", "2c").strip().lower(),
            "snmp_port_2": get_int("snmp_port_2", 161),
            "snmp_oid_2": request.form.get("snmp_oid_2", "1.3.6.1.2.1.1.3.0").strip() or "1.3.6.1.2.1.1.3.0",
            "snmp_community_2": request.form.get("snmp_community_2", "public").strip(),
            "snmp_v3_username_2": request.form.get("snmp_v3_username_2", "").strip(),
            "snmp_v3_auth_protocol_2": request.form.get("snmp_v3_auth_protocol_2", "SHA").strip().upper() or "SHA",
            "snmp_v3_auth_password_2": get_encrypted_secret("snmp_v3_auth_password_2"),
            "snmp_v3_priv_protocol_2": request.form.get("snmp_v3_priv_protocol_2", "AES").strip().upper() or "AES",
            "snmp_v3_priv_password_2": get_encrypted_secret("snmp_v3_priv_password_2"),
            "mqtt_host": request.form.get("mqtt_host", "").strip(),
            "mqtt_port": int(request.form.get("mqtt_port", 1883)),
            "mqtt_username": request.form.get("mqtt_username", "").strip(),
            "mqtt_password": get_encrypted_secret("mqtt_password"),
            "mqtt_topic_prefix": request.form.get("mqtt_topic_prefix", "outage_tracker").strip() or "outage_tracker",
            "mqtt_discovery_prefix": request.form.get("mqtt_discovery_prefix", "homeassistant").strip() or "homeassistant",
            "latitude": get_secure("latitude"), "longitude": get_secure("longitude"),
            "mapbox_token": get_secure("mapbox_token"), "pushover_user": get_secure("pushover_user"),
            "pushover_token": get_secure("pushover_token"), "ts_authkey": new_ts_key,
        })
        save_config(app_config)
        state["nut_enabled"] = bool(app_config.get("nut_host") or app_config.get("nut_host_2"))
        publish_mqtt_status(force_discovery=True)
        
        return redirect(url_for('config_page'))
        
    nut_status_1 = get_nut_status(app_config.get("nut_host"), app_config.get("nut_port", 3493))
    nut_status_2 = get_nut_status(app_config.get("nut_host_2"), app_config.get("nut_port_2", 3493))
    mqtt_status = get_mqtt_status(app_config.get("mqtt_host"), app_config.get("mqtt_port", 1883))
    return render_template("config.html", config=app_config, ts_status=get_ts_status(), ts_update=get_tailscale_update_info(), nut_status=nut_status_1, nut_status_2=nut_status_2, mqtt_status=mqtt_status)

@app.route("/test-pushover", methods=["POST"])
@login_required
def test_pushover():
    if send_pushover("🔔 Pushover Test", "Configuration working perfectly.", priority=0, include_map=True):
        return jsonify({"status": "success", "message": "Test sent! Check your device."})
    return jsonify({"status": "error", "message": "Failed to send alert. Check keys."}), 500

@app.route("/tailscale/update", methods=["POST"])
@login_required
def tailscale_update_route():
    try:
        update_step = subprocess.run(["apk", "update"], capture_output=True, text=True, timeout=30)
        if update_step.returncode != 0:
            raise RuntimeError(update_step.stderr.strip() or "apk update failed")

        upgrade_step = subprocess.run(["apk", "add", "--upgrade", "tailscale"], capture_output=True, text=True, timeout=120)
        if upgrade_step.returncode != 0:
            raise RuntimeError(upgrade_step.stderr.strip() or "apk upgrade failed")

        restart_tailscaled()
        info = get_tailscale_update_info(force=True)
        return jsonify({
            "status": "success",
            "message": f"Tailscale updated to {info.get('installed') or 'latest'}.",
            "installed": info.get("installed"),
        })
    except Exception as exc:
        logging.error(f"Tailscale update failed: {exc}")
        return jsonify({"status": "error", "message": "Tailscale update failed. Check server logs."}), 500

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

def get_mqtt_status(host, port):
    if not host: return "Not Configured"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(3)
            s.connect((host, int(port)))
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

def build_snmpget_command(host, port, oid, version, community, v3_username, v3_auth_protocol, v3_auth_password, v3_priv_protocol, v3_priv_password):
    target_oid = oid or "1.3.6.1.2.1.1.3.0"
    cmd = ["snmpget"]
    target_host = str(host).strip()
    if port:
        target_host = f"{target_host}:{port}"

    if version == "3":
        username = (v3_username or "").strip()
        auth_password = decrypt_if_possible(v3_auth_password).strip()
        privacy_password = decrypt_if_possible(v3_priv_password).strip()
        auth_protocol = (v3_auth_protocol or "SHA").upper()
        priv_protocol = (v3_priv_protocol or "AES").upper()

        if not username or not auth_password:
            return None, "SNMPv3 requires username and auth password"

        cmd.extend(["-v3"])
        if privacy_password:
            cmd.extend(["-l", "authPriv", "-u", username, "-a", auth_protocol, "-A", auth_password, "-x", priv_protocol, "-X", privacy_password])
        else:
            cmd.extend(["-l", "authNoPriv", "-u", username, "-a", auth_protocol, "-A", auth_password])
    else:
        cmd.extend(["-v2c", "-c", community or "public"])

    cmd.extend(["-O", "tv", "-t", "3", "-r", "1", target_host, target_oid])
    return cmd, None


def parse_snmp_uptime_seconds(raw_output):
    if not raw_output:
        return None

    text = str(raw_output).strip()
    if not text:
        return None

    timeticks_match = re.search(r"Timeticks:\s*\((\d+)\)", text, re.IGNORECASE)
    if timeticks_match:
        return int(timeticks_match.group(1)) / 100.0

    hms_match = re.search(r"(?:(\d+)\s+days?,\s*)?(\d{1,2}):(\d{2}):(\d{2})(?:\.(\d+))?", text, re.IGNORECASE)
    if hms_match:
        days = int(hms_match.group(1) or 0)
        hours = int(hms_match.group(2))
        mins = int(hms_match.group(3))
        secs = int(hms_match.group(4))
        fraction = hms_match.group(5) or "0"
        frac_seconds = float(f"0.{fraction}")
        return (days * 86400) + (hours * 3600) + (mins * 60) + secs + frac_seconds

    typed_int_match = re.search(r"(?:INTEGER|Gauge32|Counter32|Counter64|Unsigned32):\s*(-?\d+)", text, re.IGNORECASE)
    if typed_int_match:
        raw_value = int(typed_int_match.group(1))
        if raw_value < 0:
            return None
        if "timeticks" in text.lower():
            return raw_value / 100.0
        return float(raw_value)

    paren_value_match = re.search(r"\((\d+)\)", text)
    if paren_value_match:
        return int(paren_value_match.group(1)) / 100.0

    numeric_match = re.search(r"-?\d+", text)
    if numeric_match:
        raw_value = int(numeric_match.group(0))
        if raw_value < 0:
            return None
        if "timeticks" in text.lower() or raw_value >= 8640000:
            return raw_value / 100.0
        return float(raw_value)

    return None

def poll_snmp():
    while True:
        snmp_snapshot = {
            "snmp_ip": app_config.get("snmp_ip"),
            "snmp_name": app_config.get("snmp_name"),
            "snmp_version": app_config.get("snmp_version"),
            "snmp_port": app_config.get("snmp_port"),
            "snmp_oid": app_config.get("snmp_oid"),
            "snmp_community": app_config.get("snmp_community"),
            "snmp_v3_username": app_config.get("snmp_v3_username"),
            "snmp_v3_auth_protocol": app_config.get("snmp_v3_auth_protocol"),
            "snmp_v3_auth_password": app_config.get("snmp_v3_auth_password"),
            "snmp_v3_priv_protocol": app_config.get("snmp_v3_priv_protocol"),
            "snmp_v3_priv_password": app_config.get("snmp_v3_priv_password"),
            "snmp_ip_2": app_config.get("snmp_ip_2"),
            "snmp_name_2": app_config.get("snmp_name_2"),
            "snmp_version_2": app_config.get("snmp_version_2"),
            "snmp_port_2": app_config.get("snmp_port_2"),
            "snmp_oid_2": app_config.get("snmp_oid_2"),
            "snmp_community_2": app_config.get("snmp_community_2"),
            "snmp_v3_username_2": app_config.get("snmp_v3_username_2"),
            "snmp_v3_auth_protocol_2": app_config.get("snmp_v3_auth_protocol_2"),
            "snmp_v3_auth_password_2": app_config.get("snmp_v3_auth_password_2"),
            "snmp_v3_priv_protocol_2": app_config.get("snmp_v3_priv_protocol_2"),
            "snmp_v3_priv_password_2": app_config.get("snmp_v3_priv_password_2"),
        }
        
        for s_id in ["1", "2"]:
            suffix = "" if s_id == "1" else "_2"
            ip = app_config.get(f"snmp_ip{suffix}")
            port = int(app_config.get(f"snmp_port{suffix}", 161) or 161)
            oid = app_config.get(f"snmp_oid{suffix}", "1.3.6.1.2.1.1.3.0")
            version = str(app_config.get(f"snmp_version{suffix}", "2c") or "2c").strip().lower()
            comm = app_config.get(f"snmp_community{suffix}", "public")
            v3_username = app_config.get(f"snmp_v3_username{suffix}", "")
            v3_auth_protocol = app_config.get(f"snmp_v3_auth_protocol{suffix}", "SHA")
            v3_auth_password = app_config.get(f"snmp_v3_auth_password{suffix}", "")
            v3_priv_protocol = app_config.get(f"snmp_v3_priv_protocol{suffix}", "AES")
            v3_priv_password = app_config.get(f"snmp_v3_priv_password{suffix}", "")
            name = app_config.get(f"snmp_name{suffix}") or f"Hardware {s_id}"

            s_state = state["snmp"][s_id]

            if ip:
                try:
                    cmd, err = build_snmpget_command(ip, port, oid, version, comm, v3_username, v3_auth_protocol, v3_auth_password, v3_priv_protocol, v3_priv_password)
                    s_state["last_check"] = datetime.now().strftime("%I:%M:%S %p")
                    if err:
                        s_state["online"] = False
                        continue

                    res = subprocess.run(cmd, capture_output=True, text=True)
                    
                    if res.returncode == 0:
                        s_state["online"] = True
                        s_state["ever_online"] = True
                        new_uptime_s = parse_snmp_uptime_seconds(res.stdout)
                        if new_uptime_s is not None:
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
                        else:
                            s_state["uptime_s"] = None
                            logging.warning("SNMP uptime parse failed for %s (%s). Raw output: %s", name, ip, res.stdout.strip())
                    else:
                        s_state["online"] = False
                except Exception:
                    s_state["online"] = False
            else:
                s_state["online"] = False
                s_state["uptime_s"] = None

        publish_mqtt_status()
                
        for _ in range(300):
            if any(app_config.get(k) != v for k, v in snmp_snapshot.items()):
                break
            time.sleep(1)

def check_watchdog_target(ip, port, timeout=4):
    # Completing the TLS handshake on 443 (instead of an abrupt connect+close) avoids
    # tripping WAF/bouncer tools like CrowdSec that flag bare, protocol-less connections.
    try:
        with socket.create_connection((ip, int(port)), timeout=timeout) as sock:
            if int(port) == 443:
                context = ssl.create_default_context()
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                with context.wrap_socket(sock, server_hostname=str(ip)):
                    pass
        return True
    except Exception:
        return False

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
                is_online = check_watchdog_target(ip, port)

                state["watchdog_last_check"] = datetime.now().strftime("%I:%M:%S %p")
                wd_state = state["watchdogs"][w_id]
                wd_state["last_check"] = state["watchdog_last_check"]
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
                    wd_state["ever_online"] = True
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

        publish_mqtt_status()

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
        publish_mqtt_status()
        
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

        publish_mqtt_status()

        for _ in range(300): 
            if app_config.get("kubra_url") != url or app_config.get("zip_code") != zip_c: break
            time.sleep(1)

if __name__ == "__main__":
    threading.Thread(target=poll_gp_outages, daemon=True).start()
    threading.Thread(target=poll_nut, daemon=True).start()
    threading.Thread(target=poll_watchdog, daemon=True).start()
    threading.Thread(target=poll_snmp, daemon=True).start()
    threading.Thread(target=mqtt_heartbeat_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)