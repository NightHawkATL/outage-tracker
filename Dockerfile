FROM python:3.11-alpine

WORKDIR /app

# Install requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create the secure key directory (separate from data volume)
RUN mkdir -p /app/auth_key

# Copy application files
COPY app.py entrypoint.sh reset_auth.py ./
COPY templates/ templates/
COPY static/ static/

# Make scripts executable
RUN chmod +x entrypoint.sh

EXPOSE 8080

CMD ["./entrypoint.sh"]