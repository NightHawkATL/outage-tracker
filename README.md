# power-outage-tracker

Docker container to sense power outages from a NUT server and start checking for outages from provider and possible repair times.


Verifying the KUBRA Config URL (Important Step)

Because third-party platforms update and change endpoints occasionally, I designed the script to read the "static" configuration file rather than the dynamic data files (which change folders every 10 minutes). The default URL I provided in the compose file should work out of the box, but if it doesn't, here is how you verify it:

    Open your browser and go to the [Georgia Power Outage Map](https://outagemap.georgiapower.com).

    Press F12 to open Developer Tools and go to the Network tab.

    Refresh the page. In the Network filter box, type config.json.

    Right-click the file that appears (it will look something like config.json or data_configs.json), copy the URL, and paste it into the KUBRA_CONFIG_URL variable in your docker-compose.yml.

How to Deploy via Dockhand

    Create a folder on your VPS.

    Place the docker-compose.yml, Dockerfile, requirements.txt, and app.py in the root of the folder.

    Create the templates folder and put index.html inside it.

    Point Dockhand to this directory, or just run docker compose up -d natively in your terminal.

    Visit http://YOUR_VPS_IP:8080 in your browser.

The background task immediately begins polling silently every 5 minutes. If it spots your zip code experiencing an outage, it starts the clock. Once it passes the threshold limit, your Pushover notification will hit your phone!