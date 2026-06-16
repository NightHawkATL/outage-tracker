# ⚡ Outage Tracker

Outage Tracker is a lightweight, self-hosted Docker application designed to monitor both your local home rack's battery health and your neighborhood's power grid simultaneously. 

While standard UPS notification scripts run locally and fail if your home internet goes down, Outage Tracker is designed to be hosted externally (like on a Cloud VPS). It queries your utility company's API to track grid failures in your area, while tunneling into your Network UPS Tools (NUT) server via a mesh VPN to monitor your local battery runtime. 

Main Dashboard:</br>
<img width="990" height="885" alt="image" src="https://github.com/user-attachments/assets/b2d57886-44b8-4760-97f1-985ec3f39133" />

History Logs:</br>
<img width="999" height="246" alt="image" src="https://github.com/user-attachments/assets/bbeba38d-3f12-4a2e-920f-cc980e43faab" />

## 🤔 Why dual-tracking? (Grid vs. UPS)

If you already have a UPS, why do you need to poll the power company?

1. **The "Dead Internet" Problem:** If your neighborhood loses power, the coax/fiber node down the street might lose power too. Even if your servers and router are on a UPS, your home internet drops. A local NUT server can't send you an email/push notification without internet. Because Outage Tracker runs on a remote VPS, it will see the utility company report the outage and alert you, even if your house is completely offline.
2. **The "Neighborhood" View:** The Utility API tells you what is happening in your Zip Code. You can get alerted about a major outage hitting your neighborhood while you are at work, before you even get home.
3. **The "Local Rack" View:** Meanwhile, the NUT integration tells you exactly what is happening to your physical hardware. If the power drops, Outage Tracker monitors the exact battery percentage and runtime of your UPS array, sending critical alerts when your servers are about to die.

## ✨ Features

* **Grid Monitoring (KUBRA API):** Natively supports tracking any major utility company that uses the KUBRA Storm Center platform (Georgia Power, Duke Energy, Alabama Power, FirstEnergy, Entergy) as well as Pacific Power.
* **Multi-UPS Array Support:** Connects to your local NUT server. Use the `auto` setting to automatically discover and independently track every UPS in your server rack.
* **Event History Logs:** Persistently tracks the duration, severity, and timestamps of every local grid outage and UPS battery event so you can review your power stability over time.
* **Smart Alerting:** Configurable delay thresholds so you only get alerted if the neighborhood outage lasts longer than your UPS can handle.
* **Instant Critical Alerts:** If a local UPS goes on battery (`OB`) and drops below your minimum safe runtime, it fires an immediate critical alert.
* **Rich Map Notifications:** Optionally integrate a free Mapbox API key to instantly generate and attach a street-level map of the outage area directly to your phone's lock screen.
* **Web UI Configured:** Update your tracked zip code, API keys, and map URLs directly from the Web UI dashboard without having to rewrite config files.
* **Dynamic Dashboard:** A responsive, dark-mode Bootstrap dashboard. If you don't use a UPS or don't want a map banner, those elements gracefully auto-hide and center the remaining data.

---

## 📂 Folder Structure

Before building the container, ensure your project directory looks like this:

```text
outage-tracker/
├── Dockerfile
├── compose.yaml
├── requirements.txt
├── app.py
├── static/
│   ├── favicon.ico
│   └── logo.svg
└── templates/
    ├── config.html
    ├── history.html
    └── index.html
```

---

## 🚀 Installation

Deploy via Docker Compose. The application uses a single persistent volume to save your settings and history logs from the Web UI.

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
    environment:
      - TZ=America/New_York # Change this to your local timezone
```

Start the container:
```bash
docker compose up -d --build
```
Once running, access the dashboard at `http://<YOUR-DOCKER-IP>:8080`.

---

## ⚙️ Configuration

On your first boot, the app will load as a "Blank Slate". Click the **⚙️ Settings** button in the top right of the dashboard to configure your tracker.

<img width="2174" height="1480" alt="config" src="https://github.com/user-attachments/assets/896439d8-345c-4a41-ba71-bb22a0a7ec93" />

### 1. Utility Grid Settings
To track your local power grid, you need to provide the direct JSON data URL from your utility company's map. Finding your Zip Code endpoint is easy:

1. Open your power company's outage map in your desktop browser.
2. Press **F12** to open Developer Tools and navigate to the **Network** tab.
3. In the Network filter box, type `json`.
4. On the actual Map UI, find the **Map Legend / Menu** and change the view mode from "Clusters" (circles) to **"View by Zip/City"** or **"Zip Code"**.
5. The exact moment the map shades the zip codes, a new file will appear at the bottom of your Network tab (usually named `thematic_areas.json` or `listCA.json`).
6. Click that file, copy its **Request URL**, and paste it into the Web UI settings.

<img width="2167" height="874" alt="Untitled-1" src="https://github.com/user-attachments/assets/bf009ac6-ee74-4ad2-a5df-2351c46a9941" />

*(Optional: You can also paste the URL to your utility's main map and report pages to generate a clickable banner and button on your dashboard).*

### 2. Local UPS Settings (Optional)
If you run a local NUT server, enter its IP and Port. 
* Set **UPS Names** to `auto` to automatically fetch every UPS attached to the server, or list them manually (e.g., `nutdev1,nutdev2`).
* If you leave the NUT Host field blank, the UPS tracking panel will hide itself and the Grid tracking panel will expand to fill the screen.

> **🔒 Remote Hosting & VPNs:** If you are running Outage Tracker on a remote Cloud VPS, **do not** port-forward your home router to expose your NUT server to the public internet! Instead, install a free mesh VPN like [Tailscale](https://tailscale.com/) or WireGuard on both your home server and your Cloud VPS. You can then safely enter your home server's secure `100.x.x.x` Tailscale IP into the Web UI.

### 3. Mapbox Image Alerts (Optional)
To receive rich map images of your neighborhood attached to your Pushover alerts:
* Create a free account at [Mapbox](https://www.mapbox.com/).
* Copy your **Default Public Token** (`pk.eyJ1...`).
* Enter the Token, plus your exact home **Latitude** and **Longitude** in the Web UI.

### 4. Pushover Integration
Create a free account at [Pushover.net](https://pushover.net/) and create an "Application" to get your API Token.
* **User Key:** Found on your main Pushover dashboard.
* **API Token:** Found under your specific Application's settings.

<img width="429" height="351" alt="pushover_test" src="https://github.com/user-attachments/assets/b8f364b7-cb07-4a82-80ff-dfa4945a35a5" />

Use the **"Test Pushover Alert"** button on the main dashboard to verify your keys are correct and preview your Mapbox generation!

---

## 🛠️ Tech Stack
* **Backend:** Python 3.11 (Raw TCP sockets for NUT, Requests for API polling)
* **Frontend:** Flask, HTML5, Bootstrap 5 (Dark Mode)
* **Container:** Alpine Linux (Minimal footprint)

## 📝 License
MIT License. Feel free to fork, modify, and expand!