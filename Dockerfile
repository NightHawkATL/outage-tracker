FROM python:3.11-alpine

WORKDIR /app

# Install Tailscale, networking tools, timezone data, and SNMP tools
RUN apk update && apk add tailscale iptables iproute2 tzdata net-snmp-tools
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create the secure key directory
RUN mkdir -p /app/auth_key

# Copy application files
COPY app.py entrypoint.sh reset_auth.py ./
COPY templates/ templates/
COPY static/ static/

RUN chmod +x entrypoint.sh

# Set version last so dependency layers are not invalidated on version changes
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

EXPOSE 8080

CMD ["./entrypoint.sh"]