FROM python:3.11-slim

WORKDIR /app

# Installer dépendances système minimales
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tini && \
    rm -rf /var/lib/apt/lists/*

# Installer les dépendances Python
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copier ton code dans l’image
COPY . /app

# Variables par défaut
ENV PORT=8080 \
    PYTHONUNBUFFERED=1

EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "-s", "--"]
CMD ["python", "bot.py"]
