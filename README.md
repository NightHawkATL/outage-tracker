# ⚡ Power Tracker

Power Tracker is a lightweight, self-hosted Docker application designed to monitor both your local home rack's battery health and your neighborhood's power grid simultaneously. 

Instead of waiting for your servers to lose power to know there's an outage, this app queries your utility company's API to track grid failures in your specific Zip Code, while independently polling your Network UPS Tools (NUT) server to monitor your local battery runtime. If either drops below your configured thresholds, it sends a high-priority push notification to your phone via Pushover.

## ✨ Features

* **Grid Monitoring (KUBRA API):** Natively supports tracking any major utility company that uses the KUBRA Storm Center V5 platform (Georgia Power, Duke Energy, Alabama Power, FirstEnergy, Entergy, and many more).
* **Multi-UPS Array Support:** Connects to your local NUT server. Use the `auto` setting to automatically discover and independently track every UPS in your server rack.
* **Smart Alerting:** Configurable delay thresholds so you only get alerted if the neighborhood outage lasts longer than your UPS can handle.
* **Instant Critical Alerts:** If a local UPS goes on battery (`OB`) and drops below your minimum safe runtime, it fires an immediate critical alert.
* **Web UI Dashboard:** A responsive, dark-mode Bootstrap dashboard to view live grid and battery data at a glance.
* **In-App Configuration:** No need to edit `.env` files. Update your tracked zip code, API keys, and map URLs directly from the Web UI.

---

## 🚀 Installation

Deploy via Docker Compose. The application uses a persistent volume to save your settings from the Web UI.

### `docker-compose.yml`

```yaml
version: '3.8'

services:
  power-tracker:
    image: your-repo/power-tracker:latest # Replace with your build details if publishing
    build: .
    container_name: power-tracker
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - ./data:/app/data
    environment:
      - TZ=America/New_York
```

Start the container:
```bash
docker compose up -d --build
```
Once running, access the dashboard at `http://<YOUR-DOCKER-IP>:8080`.

---

## ⚙️ Configuration

On your first boot, click the **⚙️ Settings** button on the dashboard to configure your tracker. 

### Local UPS Settings (Optional)
If you run a local NUT server, enter its IP and Port. 
* Set **UPS Names** to `auto` to automatically fetch every UPS attached to the server, or list them manually (e.g., `nutdev1,nutdev2`).
* If you leave the NUT Host field blank, the UPS tracking panel will gracefully hide itself from the dashboard.

### Finding your KUBRA Thematic URL
To track your local power grid, you need to provide the direct data URL from your utility company's map. KUBRA maps are dynamic, but finding the Zip Code endpoint is easy:

1. Open your power company's outage map in your desktop browser (e.g., Georgia Power Outage Map).
2. Press **F12** to open Developer Tools and navigate to the **Network** tab.
3. In the Network filter box, type `json`.
4. On the actual Map UI, find the **Map Legend / Menu** and change the view mode from "Clusters" (circles) to **"View by Zip/City"** or **"Zip Code"**.
5. The exact moment the map shades the zip codes, a new file will appear at the bottom of your Network tab (usually named `thematic_areas.json`).
6. Click that file, copy its **Request URL**, and paste it into the Web UI settings.

### Pushover Integration
Create a free account at [Pushover.net](https://pushover.net/) and create an "Application" to get your API Token.
* **User Key:** Found on your main Pushover dashboard.
* **API Token:** Found under your specific Application's settings.
Use the **"Test Pushover Alert"** button on the main dashboard to verify your keys are correct!

---

## 🛠️ Tech Stack
* **Backend:** Python 3.11 (Raw TCP sockets for NUT, Requests for API polling)
* **Frontend:** Flask, HTML5, Bootstrap 5 (Dark Mode)
* **Container:** Alpine Linux (Minimal footprint)

## 📝 License
MIT License. Feel free to fork, modify, and expand!