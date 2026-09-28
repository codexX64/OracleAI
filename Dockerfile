FROM python:3.12-slim

# fuseau + certificats (Claude Code et la recherche web font du TLS)
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates tzdata curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN mkdir -p /app/data
ENV DATA_DIR=/app/data \
    HOME=/root \
    PATH="/root/.local/bin:${PATH}"

# Claude Code, pour l'adversaire « claude-code » : l'abonnement, sans clé d'API.
# En conteneur, il s'authentifie avec CLAUDE_CODE_OAUTH_TOKEN (claude setup-token).
# Si le téléchargement échoue, l'image se construit quand même : les autres
# adversaires marchent, et Oracle dit clairement que le binaire manque.
RUN curl -fsSL https://claude.ai/install.sh | bash \
    || echo "Claude Code non installé : adversaire claude-code indisponible"

COPY backend ./backend
COPY frontend ./frontend

EXPOSE 8112
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8112"]
