"""
Ce qu'Oracle raconte à SYNAPSE, tout seul.

- chaque duel départagé : le gagnant, les scores, l'écart, les latences —
  jamais le texte des réponses, jamais le fil des conversations ;
- un modèle téléchargé depuis l'interface ;
- une panne d'Ollama (une fois par panne, pas par requête) ;
- un changement de réglage (modèle, mode) — jamais une clé.

Le Hub pose SYNAPSE_URL et un jeton de cerveau (cer_oracle_…) quand SYNAPSE
est installé. Sans eux, rien ne part et rien ne casse. Les envois passent par
une file bornée vidée par un fil d'arrière-plan : SYNAPSE lent ou absent ne
ralentit jamais une réponse.
"""
import os
import threading
import time
from collections import deque

import httpx

URL = os.environ.get("SYNAPSE_URL", "").rstrip("/")
JETON = os.environ.get("SYNAPSE_JETON", "")

FILE_MAX = 300
LOT = 50

_file = deque()
_reveil = threading.Condition()
_pannes = {}          # une panne n'est racontée qu'une fois toutes les 10 min
etat = {"envoyes": 0, "echecs": 0, "perdus": 0, "dernierEnvoi": None, "erreur": None}


def pret():
    return bool(URL and JETON)


def raconter(kind, title, body="", tags=(), meta=None):
    """Empile un événement ; ne bloque jamais l'appelant."""
    if not pret():
        return
    e = {
        "kind": kind,
        "title": str(title)[:300],
        "tags": sorted({"oracle", *tags}),
        "occurred_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
    }
    if body:
        e["body"] = str(body)[:2000]
    if meta:
        e["meta"] = meta
    with _reveil:
        _file.append(e)
        while len(_file) > FILE_MAX:
            _file.popleft()
            etat["perdus"] += 1
        _reveil.notify()


def _vider():
    attente = 5.0
    client = httpx.Client(timeout=15.0)
    while True:
        with _reveil:
            while not _file:
                _reveil.wait()
        time.sleep(2.0)  # laisse un lot se former
        with _reveil:
            lot = [_file[i] for i in range(min(LOT, len(_file)))]
        try:
            r = client.post(
                URL + "/v1/ingest/batch",
                headers={"authorization": "Bearer " + JETON},
                json={"events": lot},
            )
            r.raise_for_status()
            with _reveil:
                for _ in lot:
                    _file.popleft()
            etat["envoyes"] += len(lot)
            etat["dernierEnvoi"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            etat["erreur"] = None
            attente = 5.0
        except Exception as e:  # noqa: BLE001 — la mémoire est optionnelle, jamais bloquante
            etat["echecs"] += 1
            etat["erreur"] = str(e)[:160]
            time.sleep(attente)
            attente = min(300.0, attente * 2)


if pret():
    threading.Thread(target=_vider, daemon=True, name="synapse").start()


def _une_fois(cle, fenetre=600):
    t = time.time()
    if t - _pannes.get(cle, 0) < fenetre:
        return False
    _pannes[cle] = t
    return True


# ── ce qu'on raconte ──

def duel(question, verdict, modele_local, modele_adverse, nom_adverse="Claude"):
    """Un duel départagé. Les scores, jamais les réponses."""
    gagnant = verdict.get("winner")
    if gagnant not in ("qwen", "claude"):
        return
    q = (verdict.get("qwen") or {}).get("total")
    c = (verdict.get("claude") or {}).get("total")
    nom = modele_local if gagnant == "qwen" else modele_adverse
    raconter("duel.verdict",
             f"Duel : {nom} l'emporte ({q} contre {c})",
             f"Question : « {str(question)[:160]} »\n"
             f"Local {modele_local} : {q} · {nom_adverse} {modele_adverse} : {c} · écart {verdict.get('gap')}.",
             tags=("duel", str(nom_adverse).lower()),
             meta={"gagnant": "local" if gagnant == "qwen" else "adversaire",
                   "local": modele_local, "adversaire": nom_adverse, "modele_adverse": modele_adverse,
                   "score_local": q, "score_adverse": c, "gap": verdict.get("gap")})


def modele(nom):
    raconter("model.pull",
             f"Modèle téléchargé : {nom}",
             tags=("modele",),
             meta={"modele": nom})


def panne(message):
    if not _une_fois("ollama:" + str(message)[:80]):
        return
    raconter("incident.ollama",
             f"Oracle : Ollama ne répond pas — {str(message)[:160]}",
             tags=("panne", "ollama"),
             meta={"message": str(message)[:300]})


def reglage(titre, meta=None):
    raconter("oracle.config", str(titre)[:200], tags=("reglage",), meta=meta or {})
