#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# .env
[ -f .env ] || { cp .env.example .env; echo "→ .env créé depuis .env.example — édite tes clés puis relance."; }

# venv
# venv — Python 3.10–3.13 (3.14 est trop récent pour pydantic-core en wheel)
PY=""
for c in python3.12 python3.13 python3.11 python3.10 python3.14 python3; do
  if command -v "$c" >/dev/null 2>&1; then
    ver=$("$c" -c 'import sys;print(sys.version_info[0]*100+sys.version_info[1])' 2>/dev/null || echo 0)
    if [ "$ver" -ge 310 ] && [ "$ver" -le 313 ]; then PY="$c"; break; fi
  fi
done
if [ -z "$PY" ]; then
  echo "✗ Aucun Python 3.10–3.13 trouvé (le 3.14 est trop récent pour les dépendances)."
  echo "  Installe-le :  brew install python@3.12"
  exit 1
fi

if [ ! -d .venv ]; then
  echo "→ Création de l'environnement Python ($PY)…"
  "$PY" -m venv .venv
fi
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

# charge .env
set -a; source .env; set +a

echo "→ Oracle AI sur http://0.0.0.0:8112"
exec uvicorn backend.app:app --host 0.0.0.0 --port 8112
