FROM python:3.11-alpine

WORKDIR /app

# Install Tailscale and Python requirements
RUN apk update && apk add tailscale
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY app.py entrypoint.sh ./
COPY templates/ templates/
COPY static/ static/

# Make the entrypoint script executable
RUN chmod +x entrypoint.sh

# Expose the web UI port
EXPOSE 8080

CMD ["./entrypoint.sh"]