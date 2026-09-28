# Oracle AI

Interface web pour dialoguer avec une IA locale (Ollama), avec un **mode duel**
optionnel qui compare la réponse locale à celle de Claude et les départage avec
un juge déterministe.

Projet [CodexX64](https://github.com/CodexX64).

## Fonctionnalités

- **Chat continu** avec un modèle local, mémoire du fil de conversation
- **Mode duel** : modèle local vs Claude, jugés sur des critères mesurables
- **Juge déterministe** (jamais un LLM) : substance, richesse, structure, concision, latence
- **Authentification** : comptes multiples, mots de passe Argon2id, 2FA TOTP, codes de secours
- **Conversations persistantes** (SQLite) avec projets, épinglage, recherche, export Markdown
- **Rendu complet** : Markdown, tableaux, code coloré, formules mathématiques (KaTeX)
- **Recherche web** activable par message, avec sources citées
- **Pièces jointes** : fichiers texte et images (collage direct supporté)
- **Sélection de modèle** à la volée, téléchargement de nouveaux modèles depuis l'interface
- **Mode réflexion** activable (raisonnement long) ou désactivé (réponses rapides)

## Prérequis

- Ollama accessible sur le réseau, avec au moins un modèle installé
- Docker et Docker Compose, ou Python 3.10–3.13 pour le lancement natif
- Claude Code installé et connecté sur l'hôte (uniquement pour le mode duel)

## Installation

```bash
cp .env.example .env
nano .env          # renseigner OLLAMA_URL et HOST_HOME
docker compose up -d --build
docker logs -f oracle-ai
```

L'interface écoute sur le port **8112**.

Au premier lancement, un écran de configuration demande de créer le compte
administrateur, puis affiche un QR code à scanner dans une application
d'authentification. Huit codes de secours sont fournis — conservez-les.

### Exposer Ollama sur le réseau

Par défaut Ollama n'écoute que sur `127.0.0.1`. Pour qu'Oracle l'atteigne depuis
une autre machine :

```bash
OLLAMA_HOST=0.0.0.0:11434 ollama serve
```

### Lancement natif

```bash
cp .env.example .env && nano .env
chmod +x run.sh && ./run.sh
```

## Mode duel (Claude)

Le conteneur monte la session Claude Code de l'hôte :

| Hôte | Conteneur | Rôle |
|---|---|---|
| `$HOST_HOME/.claude` | `/root/.claude` | identifiants de session |
| `$HOST_HOME/.local/share/claude` | `/root/.local/share/claude` | binaire Claude Code |

Le binaire est détecté automatiquement, y compris après une mise à jour de
version. Alternative : passer `CLAUDE_MODE=api` et fournir `ANTHROPIC_API_KEY`.

## Sécurité

- Mots de passe : 12 caractères minimum, majuscule, minuscule, chiffre et caractère spécial, hachés en **Argon2id** (64 Mo, 3 passes)
- **2FA TOTP** obligatoire, codes de secours à usage unique
- Sessions : cookie signé HttpOnly + SameSite, `Secure` automatique en HTTPS, 14 jours
- Changer de mot de passe invalide toutes les sessions existantes
- Anti-bruteforce : 6 tentatives par IP sur 15 minutes
- Toutes les routes `/api/*` sont fermées sans session valide

Derrière un reverse proxy, transmettre les en-têtes suivants, sinon le cookie
`Secure` et la limitation par IP ne fonctionnent pas :

```
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header Host $host;
```

Le fichier `.session_secret` (dans `DATA_DIR`) contient la clé de signature des
sessions : ne pas le supprimer, ne pas le versionner.

## Configuration

| Variable | Rôle |
|---|---|
| `OLLAMA_URL` | URL du serveur Ollama |
| `OLLAMA_MODEL` | modèle local par défaut |
| `CLAUDE_MODE` | `cli` (Claude Code) ou `api` (clé) |
| `CLAUDE_MODEL` | opus / sonnet / haiku |
| `CLAUDE_BIN` | chemin du binaire si la détection échoue |
| `ANTHROPIC_API_KEY` | uniquement si `CLAUDE_MODE=api` |
| `MEMORY_URL` / `MEMORY_API_KEY` | service de mémoire externe (optionnel) |
| `SYNAPSE_URL` / `SYNAPSE_JETON` | mémoire du homelab (optionnel) : Oracle y raconte les verdicts de duel, les modèles téléchargés et les pannes d'Ollama — jamais les conversations |
| `ORACLE_HUB_TOKEN` | jeton de service pour un Hub (optionnel) : ouvre l'API, jamais les comptes |
| `DATA_DIR` | dossier de `oracle.db` et `settings.json` |
| `HOST_HOME` | home de l'hôte, pour monter la session Claude Code |

Un garde-fou rejette tout modèle Claude hors de la liste Opus / Sonnet / Haiku.

## Conversations et projets

Clic droit sur une conversation : ouvrir, renommer, épingler, déplacer dans un
projet, exporter en Markdown, supprimer. Clic droit sur un projet pour le
supprimer (les conversations sont conservées).

## Licence

MIT
