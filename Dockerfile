FROM python:3.11-alpine

WORKDIR /app

# Install requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY app.py .
COPY templates/ templates/

# Expose the web UI port
EXPOSE 8080

CMD ["python", "-u", "app.py"]