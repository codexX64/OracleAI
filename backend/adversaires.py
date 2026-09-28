"""
L'adversaire du duel : qui affronte le modèle local.

Claude par Claude Code (l'abonnement, sans clé) ou par l'API Anthropic, et
tous ceux qui parlent le dialecte OpenAI : ChatGPT, Kimi, Mistral, DeepSeek,
Gemini, Groq, OpenRouter, ou n'importe quelle API compatible à son adresse.

Réglé par l'environnement (le Hub le pose à l'installation) :
  ADVERSAIRE          identifiant ci-dessous (défaut : déduit de CLAUDE_MODE)
  ADVERSAIRE_CLE      clé d'API du fournisseur (ANTHROPIC_API_KEY reste lue)
  ADVERSAIRE_MODELE   modèle ; vide = choisi dans les réglages d'Oracle
  ADVERSAIRE_URL      adresse, seulement pour « compatible » (finit par /v1)
  ADVERSAIRE_NOM      nom affiché, seulement pour « compatible »

Aucun nom de modèle n'est inventé : la liste proposée est celle que le
fournisseur renvoie lui-même (/models), relue toutes les dix minutes.
"""
import json
import os
import re
import time

import httpx

FOURNISSEURS = {
    "claude-code": {"nom": "Claude", "genre": "cli"},
    "claude-api": {"nom": "Claude", "genre": "anthropic", "url": "https://api.anthropic.com/v1"},
    "openai": {"nom": "ChatGPT", "genre": "openai", "url": "https://api.openai.com/v1"},
    "kimi": {"nom": "Kimi", "genre": "openai", "url": "https://api.moonshot.ai/v1",
             "defaut": "kimi-k2-0905-preview"},
    "mistral": {"nom": "Mistral", "genre": "openai", "url": "https://api.mistral.ai/v1"},
    "deepseek": {"nom": "DeepSeek", "genre": "openai", "url": "https://api.deepseek.com/v1"},
    "gemini": {"nom": "Gemini", "genre": "openai",
               "url": "https://generativelanguage.googleapis.com/v1beta/openai"},
    "groq": {"nom": "Groq", "genre": "openai", "url": "https://api.groq.com/openai/v1"},
    "openrouter": {"nom": "OpenRouter", "genre": "openai", "url": "https://openrouter.ai/api/v1"},
    "compatible": {"nom": "API compatible", "genre": "openai", "url": ""},
}

# Les modèles Claude admis par le garde-fou (liste blanche historique d'Oracle).
CLAUDE_MODELES = ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"]
CLAUDE_DEFAUT = "claude-opus-4-8"

_NOM_MODELE = re.compile(r"^[A-Za-z0-9._:/@\-]{1,120}$")
# ce que /models renvoie mais qui ne sait pas converser
_PAS_CHAT = re.compile(r"embed|whisper|tts|dall-e|image|moderation|audio|transcri|rerank|guard|ocr",
                       re.I)


def actuel():
    """L'adversaire tel que l'environnement le décrit."""
    ident = os.getenv("ADVERSAIRE", "").strip().lower()
    if not ident:
        ident = "claude-api" if os.getenv("CLAUDE_MODE", "cli").lower() == "api" else "claude-code"
    f = dict(FOURNISSEURS.get(ident) or {"nom": ident, "genre": "inconnu"})
    f["id"] = ident
    if ident == "compatible":
        f["url"] = os.getenv("ADVERSAIRE_URL", "").strip().rstrip("/")
        f["nom"] = os.getenv("ADVERSAIRE_NOM", "").strip() or f["nom"]
    f["cle"] = os.getenv("ADVERSAIRE_CLE", "") or (
        os.getenv("ANTHROPIC_API_KEY", "") if ident == "claude-api" else "")
    f["modele_env"] = os.getenv("ADVERSAIRE_MODELE", "").strip() or (
        os.getenv("CLAUDE_MODEL", "").strip() if f["genre"] in ("cli", "anthropic") else "")
    return f


def est_claude(f):
    return f["genre"] in ("cli", "anthropic")


def garde(f, modele):
    """Jamais fable ni mythos, quel que soit le fournisseur ; liste blanche pour Claude.
    Renvoie le modèle, ou lève ValueError avec la raison."""
    m = (modele or "").strip()
    low = m.lower()
    if "fable" in low or "mythos" in low:
        raise ValueError(f"Modèle interdit par le garde-fou : {m}")
    if est_claude(f):
        ok = (low.startswith("claude-opus") or low.startswith("claude-sonnet")
              or low.startswith("claude-haiku") or low in ("opus", "sonnet", "haiku"))
        if not ok:
            raise ValueError(f"Modèle Claude hors liste blanche : {m}")
    elif not _NOM_MODELE.match(m):
        raise ValueError(f"Nom de modèle invalide : {m}")
    return m


def pret(f):
    """Ce qui manque pour jouer, ou None."""
    if f["genre"] == "inconnu":
        return f"Adversaire inconnu : {f['id']}"
    if f["genre"] == "cli":
        # En conteneur, pas de trousseau ni de session montée : sans jeton,
        # Claude Code répondrait « Please run /login » à chaque duel. Hors
        # conteneur (installation native), la session de l'hôte suffit.
        if (os.path.exists("/.dockerenv") and not os.getenv("CLAUDE_CODE_OAUTH_TOKEN")
                and not os.path.exists(os.path.expanduser("~/.claude/.credentials.json"))):
            return "Jeton Claude Code manquant : lance « claude setup-token » sur une machine connectée à ton abonnement, puis colle-le dans le Hub"
        return None          # le binaire est vérifié au moment de l'appel
    if f["id"] == "compatible" and not f.get("url"):
        return "Adresse de l'API compatible manquante (ADVERSAIRE_URL)"
    if not f["cle"]:
        return f"Clé d'API de {f['nom']} manquante"
    return None


_cache = {}


async def modeles(f):
    """Les modèles proposés par le fournisseur lui-même."""
    if est_claude(f):
        return list(CLAUDE_MODELES)
    if pret(f):
        return []
    cle = (f["id"], f.get("url"))
    t, liste = _cache.get(cle, (0, None))
    if liste is not None and time.time() - t < 600:
        return liste
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f["url"] + "/models", headers={"authorization": "Bearer " + f["cle"]})
            r.raise_for_status()
            data = r.json()
        ids = []
        for m in (data.get("data") if isinstance(data, dict) else data) or []:
            i = str(m.get("id", "") if isinstance(m, dict) else m)
            i = i[7:] if i.startswith("models/") else i
            if i and not _PAS_CHAT.search(i) and "fable" not in i.lower() and "mythos" not in i.lower():
                ids.append(i)
        liste = sorted(set(ids))
    except Exception:  # noqa: BLE001 — une liste absente laisse la saisie libre
        liste = []
    _cache[cle] = (time.time(), liste)
    return liste


async def choisir_modele(f, *candidats):
    """Premier candidat valide ; sinon le modèle de l'environnement ; sinon le
    défaut du fournisseur s'il le propose vraiment. None si rien ne tient."""
    for m in (*candidats, f.get("modele_env")):
        if not m:
            continue
        try:
            m = garde(f, m)
        except ValueError:
            continue
        # un modèle Claude resté dans les réglages n'a rien à faire chez Kimi
        if not est_claude(f) and m.lower().startswith("claude-") and f["id"] != "openrouter":
            continue
        return m
    if est_claude(f):
        return CLAUDE_DEFAUT
    d = f.get("defaut")
    if d and d in await modeles(f):
        return d
    return None


# ── les appels (producteurs : poussent dans la file du duel, côté « claude ») ──

async def _fin(out, modele, t0, texte=None, erreur=None):
    meta = {"side": "claude", "model": modele, "ms": int((time.time() - t0) * 1000)}
    if erreur:
        meta["error"] = str(erreur)[:240]
    else:
        meta["text"] = texte or ""
    await out.put(("meta", meta))


async def stream_openai(f, modele, systeme, question, out, max_tokens=2048):
    t0 = time.time()
    full = []
    body = {"model": modele, "stream": True, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": systeme},
                         {"role": "user", "content": question}]}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(240, connect=15)) as c:
            async with c.stream("POST", f["url"] + "/chat/completions", json=body,
                                headers={"authorization": "Bearer " + f["cle"]}) as r:
                if r.status_code >= 400:
                    txt = (await r.aread()).decode(errors="ignore")
                    await _fin(out, modele, t0, erreur=f"{f['nom']} {r.status_code} : {txt[:180]}")
                    return
                async for ligne in r.aiter_lines():
                    if not ligne.startswith("data:"):
                        continue
                    data = ligne[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        j = json.loads(data)
                    except Exception:  # noqa: BLE001
                        continue
                    if j.get("error"):
                        await _fin(out, modele, t0, erreur=f"{f['nom']} : {j['error']}")
                        return
                    for ch in j.get("choices") or []:
                        piece = (ch.get("delta") or {}).get("content") or ""
                        if piece:
                            full.append(piece)
                            await out.put(("delta", {"side": "claude", "text": piece}))
        await _fin(out, modele, t0, texte="".join(full))
    except Exception as e:  # noqa: BLE001
        await _fin(out, modele, t0, erreur=f"{f['nom']} injoignable ({type(e).__name__})")


async def stream_anthropic(f, modele, systeme, question, out, max_tokens=2048):
    t0 = time.time()
    full = []
    body = {"model": modele, "stream": True, "max_tokens": max_tokens, "system": systeme,
            "messages": [{"role": "user", "content": question}]}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(240, connect=15)) as c:
            async with c.stream("POST", f["url"] + "/messages", json=body,
                                headers={"x-api-key": f["cle"],
                                         "anthropic-version": "2023-06-01"}) as r:
                if r.status_code >= 400:
                    txt = (await r.aread()).decode(errors="ignore")
                    await _fin(out, modele, t0, erreur=f"API Anthropic {r.status_code} : {txt[:180]}")
                    return
                async for ligne in r.aiter_lines():
                    if not ligne.startswith("data:"):
                        continue
                    try:
                        j = json.loads(ligne[5:].strip())
                    except Exception:  # noqa: BLE001
                        continue
                    if j.get("type") == "error":
                        await _fin(out, modele, t0, erreur=f"API Anthropic : {j.get('error')}")
                        return
                    d = j.get("delta") or {}
                    if j.get("type") == "content_block_delta" and d.get("type") == "text_delta":
                        piece = d.get("text") or ""
                        if piece:
                            full.append(piece)
                            await out.put(("delta", {"side": "claude", "text": piece}))
        await _fin(out, modele, t0, texte="".join(full))
    except Exception as e:  # noqa: BLE001
        await _fin(out, modele, t0, erreur=f"API Anthropic injoignable ({type(e).__name__})")


def public(f, modele=None, liste=None):
    """Ce que l'interface a le droit de savoir — jamais la clé."""
    return {"id": f["id"], "nom": f["nom"], "genre": f["genre"],
            "claude": est_claude(f), "modele": modele, "modeles": liste or [],
            "manque": pret(f)}
