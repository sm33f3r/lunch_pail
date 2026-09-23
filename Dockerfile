FROM python:3.13-slim

WORKDIR /app

# Install dependencies first so this layer is cached when only source changes.
COPY reporter/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the reporter package.
COPY reporter/ reporter/

# Config is supplied at runtime via environment variables (Coolify deploy).
# No .env file is baked in.
CMD ["python", "-m", "reporter"]
