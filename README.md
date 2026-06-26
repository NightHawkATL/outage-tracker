# ⚡ Outage Tracker

Outage Tracker is a lightweight, self-hosted Docker application designed to monitor both your local home rack's battery health and your neighborhood's power grid simultaneously. 

While standard UPS notification scripts run locally and fail if your home internet goes down, Outage Tracker is designed to be hosted externally (like on a Cloud VPS). It queries your utility company's API to track grid failures in your area, while tunneling into your Network UPS Tools (NUT) server via a built-in mesh VPN to monitor your local battery runtime. 

Main Page:</br>
<img width="882" height="860" alt="image" src="https://github.com/user-attachments/assets/350b7acd-7df0-4845-a035-66fbcf162655" />

History Logs:</br>
<img width="1186" height="294" alt="image" src="https://github.com/user-attachments/assets/e347e3f5-a1d9-4f90-a432-f626b852dfb6" />

## 🤔 Why dual-tracking? (Grid vs. UPS)

If you already have a UPS, why do you need to poll the power company?

1. **The "Dead Internet" Problem:** If your neighborhood loses power, the coax/fiber node down the street might lose power too. Even if your servers and router are on a UPS, your home internet drops. A local NUT server can't send you an email/push notification without internet. Because Outage Tracker runs on a remote VPS, it will see the utility company report the outage and alert you, even if your house is completely offline.
2. **The "Neighborhood" View:** The Utility API tells you what is happening in your Zip Code. You can get alerted about a major outage hitting your neighborhood while you are at work, before you even get home.
3. **The "Local Rack" View:** Meanwhile, the NUT integration tells you exactly what is happening to your physical hardware. If the power drops, Outage Tracker monitors the exact battery percentage and runtime of your UPS array, sending critical alerts when your servers are about to die.

## ✨ Features

* **Grid Monitoring (KUBRA API):** Natively supports tracking any major utility company that uses the KUBRA Storm Center platform (Georgia Power, Duke Energy, Alabama Power, FirstEnergy, Entergy) as well as Pacific Power.
* **Multi-UPS Array Support:** Connects to your local NUT server. Use the `auto` setting to automatically discover and independently track every UPS in your server rack.
* **Dual-WAN Watchdog:** Continuously pings up to two IP addresses (like your home Public IP or a Tailscale node) to monitor your home internet connection for downtime, with independent alerting.
* **Built-in Tailscale VPN:** No need to install VPN software on your Docker host. Outage Tracker runs its own internal Tailscale daemon to securely bridge your cloud VPS to your home network.
* **Zero-Knowledge Security:** Sensitive API tokens, VPN auth keys, and home coordinates are configured as "Write-Only" in the UI. Once saved, they are hidden from the frontend to protect your data.
* **Live Connection Diagnostics:** The settings dashboard features real-time socket checks to visually verify if your NUT server and Tailscale network are connected.
* **Rich Map Notifications:** Optionally integrate a free Mapbox API key to instantly generate and attach a street-level map of the outage area directly to your phone's lock screen.
* **Event History Logs:** Persistently tracks the duration, severity, and timestamps of every local grid outage, UPS battery event, and network downtime so you can review your infrastructure stability over time.

---

## 📂 Folder Structure

Before building the container, ensure your project directory looks like this:

```text
outage-tracker/
├── Dockerfile
├── compose.yaml
├── requirements.txt
├── app.py
├── entrypoint.sh      
├── reset_auth.py      <-- Password reset tool
├── auth_key/          <-- Auto-generated folder containing your secret decryption key
├── static/
│   ├── favicon.ico    <-- Your browser tab icon
│   └── logo.svg       <-- Your custom header logo
└── templates/
    ├── config.html
    ├── history.html
    ├── index.html
    └── login.html     <-- Secure login screen
```

---

## 🚀 Installation

Deploy via Docker Compose. The application uses a single persistent volume to save your settings, Tailscale identity, and history logs.

### `compose.yaml`

```yaml
services:
  outage-tracker:
    build: .
    container_name: outage-tracker
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - ./data:/app/data
      - ./auth_key:/app/auth_key    # Persists the encryption key securely outside the data folder
      - /dev/net/tun:/dev/net/tun   # Required for Tailscale networking
    cap_add:
      - NET_ADMIN                   # Required for Tailscale networking
      - NET_RAW
    environment:
      - TZ=America/New_York         # Change this to your local timezone
```

Start the container:
```bash
docker compose up -d --build
```
Once running, access the dashboard at `http://<YOUR-DOCKER-IP>:8080`.

---

## ⚙️ Configuration

On your first boot, you will be met with a secure login screen. 
**Default Username:** `admin`
**Default Password:** `admin`
*(Note: Please log in and immediately change your password in the Settings page!)*

> **🔐 Forgot your password?** If you get locked out of your dashboard, SSH into your docker host and run `docker exec -it outage-tracker python reset_auth.py`. Your password will instantly reset to `admin` without deleting any of your saved utility or VPN settings!

The app will initially load as a "Blank Slate". Click the **⚙️ Settings** button in the top right of the dashboard to configure your tracker.

<img width="806" height="2368" alt="configuration" src="https://github.com/user-attachments/assets/3e7e7598-76c2-41e3-b8d2-e656b10d0d57" />

### 1. Built-in Tailscale VPN (For Remote VPS Users)
If you are running this on a Cloud VPS, **do not** port-forward your home router to expose your NUT server to the internet. 
1. Generate an Auth Key from your [Tailscale Admin Console](https://login.tailscale.com/admin/settings/keys).
2. Paste it into the Web UI. The container will instantly authenticate and join your Tailnet, allowing you to securely ping your home server's `100.x.x.x` IP address.

### 2. Utility Grid Settings
To track your local power grid, you need to provide the direct JSON data URL from your utility company's map. Finding your Zip Code endpoint is easy:
1. Open your power company's outage map in your desktop browser.
2. Press **F12** to open Developer Tools and navigate to the **Network** tab.
3. In the Network filter box, type `json`.
4. On the actual Map UI, find the **Map Legend / Menu** and change the view mode from "Clusters" (circles) to **"View by Zip/City"** or **"Zip Code"**.
5. The exact moment the map shades the zip codes, a new file will appear at the bottom of your Network tab (usually named `thematic_areas.json` or `listCA.json`).
6. Click that file, copy its **Request URL**, and paste it into the Web UI settings.

<img width="2167" height="874" alt="Untitled-1" src="https://github.com/user-attachments/assets/bf009ac6-ee74-4ad2-a5df-2351c46a9941" />

*(Optional: You can also paste the URL to your utility's main map and report pages to generate a clickable banner and button on your dashboard).*

### 3. Local UPS Settings (Optional)
If you run a local NUT server, enter its IP and Port. 
* Set **UPS Names** to `auto` to automatically fetch every UPS attached to the server, or list them manually (e.g., `nutdev1,nutdev2`).

### 4. Home Network Watchdog (Optional)
Continuously ping up to two devices to detect ISP or local network failures. You can enter your home's Public IP, a Dynamic DNS hostname, or a secure Tailscale IP (e.g., `100.x.x.x`). 
* If your target goes offline longer than the configured threshold, a network downtime alert will be sent.
* Configure the secondary failover target for multi-WAN setups.

{New Image Here}

### 5. Mapbox Image Alerts (Optional)
To receive rich map images of your neighborhood attached to your Pushover alerts:
* Copy your **Default Public Token** (`pk.eyJ1...`) from Mapbox.
* Enter the Token, plus your exact home **Latitude** and **Longitude** in the Web UI.

### 6. Pushover Integration
Create a free account at [Pushover.net](https://pushover.net/) and create an "Application" to get your API Token. Use the **"Test Pushover Alert"** button on the main dashboard to verify your keys are correct and preview your Mapbox generation!

<img width="429" height="351" alt="pushover_test" src="https://github.com/user-attachments/assets/b8f364b7-cb07-4a82-80ff-dfa4945a35a5" />

---

## 📱 Pushover Notification Examples

Depending on your configuration and the events that occur, Outage Tracker will push rich notifications directly to your device. Here is exactly what those alerts will look like (using *Georgia Power*, Zip Code *30114*, and a UPS named *nutdev1* as examples):

### ⚡ Grid Outage Events

**Active Outage Alert** (Priority 1 - High)
*Triggered when the utility company API reports an outage in your zip code that outlasts your configured threshold.*
> **Title:** 🚨 Georgia Power Outage Alert  
> **Message:**   
> Power out in 30114 for >10 mins.  
> Affected: 1245  
> Est. Restoration: Today at 5:00 PM  
> **Attachment:** 🗺️ *(A dark-themed Mapbox street map of your exact home coordinates)*

**Grid Power Restored** (Priority 0 - Normal)
*Triggered when the utility company API updates to show 0 customers affected in your zip code.*
> **Title:** ✅ Georgia Power Power Restored  
> **Message:**   
> Restored in 30114!  
> Outage lasted 112 mins.

### 🔋 Local UPS Events

**Critical Battery Alert** (Priority 1 - High)
*Triggered immediately when your local UPS switches to "On Battery" (OB) and its estimated runtime drops below your configured minimum (e.g., 10 minutes).*
> **Title:** ⚠️ CRITICAL: nutdev1 Low!  
> **Message:**   
> UPS 'nutdev1' on battery, 9 mins left.

**UPS Power Restored** (Priority 0 - Normal)
*Triggered when your UPS switches back to "On Line" (OL) grid power after an event.*
> **Title:** 🔌 UPS nutdev1 Restored  
> **Message:**   
> Back on grid power.

### 🌐 Watchdog Events

**Network Offline** (Priority 1 - High)
*Triggered when the watchdog fails to ping the target for your configured threshold limit.*
> **Title:** 🌐 ⚠️ Network Offline  
> **Message:**   
> Primary WAN connection to 100.122.66.74:80 failed for >5 mins.

**Network Restored** (Priority 0 - Normal)
*Triggered when the network target becomes reachable again.*
> **Title:** ✅ Network Restored  
> **Message:**   
> Primary WAN connection to 100.122.66.74:80 restored.  
> Downtime: 45 mins.

### 🔔 System Testing

**Manual UI Test** (Priority 0 - Normal)
*Triggered when you click the "Test Pushover Alert" button on your dashboard.*
> **Title:** 🔔 Pushover Test  
> **Message:**   
> Configuration working perfectly. Here is your map!  
> **Attachment:** 🗺️ *(Your generated Mapbox image)*

> **A Note on Priorities:** Active Grid Outages, Critical UPS alerts, and Network Offline alerts are sent as **Priority 1**. In Pushover, this means the notification will play a sound and vibrate your phone **even if your phone is set to "Quiet Hours"**. The "Restored" and "Test" messages are sent as **Priority 0**, meaning they will deliver normally and won't wake you up in the middle of the night just to tell you the power came back on!

---

## 🛡️ Firewalls & Advanced Networking

If you are operating a multi-VLAN homelab or a "Zero Trust" environment, you may encounter `[Errno 111] Connection refused` when Outage Tracker attempts to reach your NUT server. Here is how to resolve cross-VLAN and VPN routing issues:

### 1. Configure NUT to listen on all interfaces
By default, NUT only listens to `localhost`. You must tell it to listen to your Tailscale and VLAN interfaces.
Edit `/etc/nut/upsd.conf` on your NUT server:
```text
# Comment out other LISTEN directives and add:
LISTEN 0.0.0.0 3493
```
Restart the service: `sudo systemctl restart nut-server`

### 2. Configure Host Firewalls (UFW)
If your NUT Server runs a host-level firewall like UFW, it will block incoming connections from the VPN and other local subnets.
Run the following on your NUT server to allow traffic:
```bash
# Allow Tailscale VPN traffic
sudo ufw allow in on tailscale0 to any port 3493

# Allow internal cross-VLAN traffic (e.g., from Home Assistant)
sudo ufw allow from 10.0.0.0/8 to any port 3493
```

### 3. The "Tailscale Route Hijack" Fix
If local devices (like Home Assistant on VLAN 103) suddenly lose access to your NUT server (on VLAN 1) after installing Tailscale, you are likely experiencing **Asymmetric Routing**. The NUT server receives the local packet, but attempts to send the reply back *through* the Tailscale tunnel instead of your physical router.

To fix this, disable route acceptance on the NUT server so it ignores Tailscale subnets and respects your physical router's routing table:
```bash
sudo tailscale up --accept-routes=false
```
*(Alternatively, create a Layer 3 pinhole rule in your primary router/firewall to pass traffic directly between the VLANs, keeping local traffic entirely off the VPN).*

---

## 🛠️ Tech Stack
* **Backend:** Python 3.11 (Raw TCP sockets for NUT, Requests for API polling)
* **Frontend:** Flask, HTML5, Bootstrap 5 (Dark Mode)
* **Container:** Alpine Linux (Minimal footprint)

## 📝 License
MIT License. Feel free to fork, modify, and expand!
