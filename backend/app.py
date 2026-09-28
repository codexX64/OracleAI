import os, time, asyncio, json, shutil, pathlib, re, datetime, html as html_mod, urllib.parse
import httpx
from fastapi import FastAPI, HTTPException, Request, Response, Depends, Cookie
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from typing import Optional, List
from pydantic import BaseModel
from . import judge as judge_mod
from . import db
from . import auth
from . import synapse

# ---- config (jamais d'IP en dur : tout via .env) ----
OLLAMA_URL      = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL", "qwen3:14b")
CLAUDE_MODE     = os.getenv("CLAUDE_MODE", "cli").lower()   # "cli" = Claude Code (abo Max) | "api" = clé API
ANTHROPIC_KEY   = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL    = os.getenv("CLAUDE_MODEL", "claude-opus-4-8")
CLAUDE_BIN      = os.getenv("CLAUDE_BIN", "claude")


def claude_env():
    """Environnement du sous-processus Claude Code : HOME correct pour lire
    ~/.claude/.credentials.json, et surtout PAS de ANTHROPIC_API_KEY (sinon
    Claude Code bascule en facturation API au lieu de l'abonnement)."""
    env = dict(os.environ)
    env.setdefault("HOME", os.path.expanduser("~"))
    env["PATH"] = env.get("PATH", "") + ":" + env["HOME"] + "/.local/bin:/usr/local/bin"
    if CLAUDE_MODE == "cli":
        env.pop("ANTHROPIC_API_KEY", None)
    return env


def find_claude():
    """Localise le binaire Claude Code, même si le PATH du service est minimal
    ou si l'installation est montée depuis l'hôte (Docker)."""
    if os.path.isabs(CLAUDE_BIN) and os.access(CLAUDE_BIN, os.X_OK):
        return CLAUDE_BIN
    found = shutil.which(CLAUDE_BIN)
    if found:
        return found
    home = os.path.expanduser("~")
    for p in (f"{home}/.local/bin/claude", f"{home}/.claude/local/claude",
              "/usr/local/bin/claude", "/usr/bin/claude", "/opt/homebrew/bin/claude"):
        if os.access(p, os.X_OK):
            return p
    # installation native : ~/.local/share/claude/versions/<version>
    import glob
    cands = []
    for base in (f"{home}/.local/share/claude/versions/*", "/root/.local/share/claude/versions/*"):
        cands += [c for c in glob.glob(base) if os.path.isfile(c) and os.access(c, os.X_OK)]
    if cands:
        return sorted(cands)[-1]      # la plus récente
    return None
MEMORY_URL     = os.getenv("MEMORY_URL", "").rstrip("/")
MEMORY_KEY     = os.getenv("MEMORY_API_KEY", "")
MAX_TOKENS      = int(os.getenv("MAX_TOKENS", "1024"))

PERSONA_BASE = "Tu es un assistant expert (code et culture générale). Réponds de façon concise et directe."

_JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
_MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
         "août", "septembre", "octobre", "novembre", "décembre"]


def persona():
    """Prompt système + date/heure réelles du serveur (évite que le modèle
    réponde avec la date de son entraînement)."""
    n = datetime.datetime.now()
    d = f"{_JOURS[n.weekday()]} {n.day} {_MOIS[n.month-1]} {n.year}"
    return (f"{PERSONA_BASE}\n\n"
            f"Date et heure actuelles : {d}, {n.strftime('%H:%M')} "
            f"(format court {n.strftime('%d/%m/%Y')}, ISO {n.strftime('%Y-%m-%d')}). "
            f"Utilise TOUJOURS cette date si on te demande la date, le jour, l'année ou l'heure — "
            f"jamais celle de tes données d'entraînement.")


# compat : certains appels utilisent encore PERSONA
PERSONA = PERSONA_BASE

FRONT = pathlib.Path(__file__).resolve().parent.parent / "frontend"
def _data_dir():
    """Dossier persistant : DATA_DIR, sinon /app/data s'il existe (volume Docker),
    sinon la racine du projet. Évite de perdre settings.json à chaque recreate."""
    env = os.getenv("DATA_DIR")
    if env:
        return pathlib.Path(env)
    if os.path.isdir("/app/data"):
        return pathlib.Path("/app/data")
    return pathlib.Path(__file__).resolve().parent.parent


DATA_DIR = _data_dir()
CONFIG = DATA_DIR / "settings.json"

# catalogue de modèles téléchargeables (adapté 16 Go), + embeddings
CATALOG = [
    {"name": "qwen3:14b",         "desc": "généraliste ~9 Go — reco 16 Go", "size": "9.3 GB"},
    {"name": "qwen3:8b",          "desc": "généraliste léger ~6 Go",         "size": "5.2 GB"},
    {"name": "qwen2.5-coder:7b",  "desc": "code-first, rapide",              "size": "4.7 GB"},
    {"name": "gpt-oss:20b",       "desc": "raisonnement +, serré en 16 Go",  "size": "13 GB"},
    {"name": "llama3.1:8b",       "desc": "généraliste Meta",                "size": "4.9 GB"},
    {"name": "deepseek-r1:8b",    "desc": "raisonnement",                    "size": "5.2 GB"},
    {"name": "nomic-embed-text",  "desc": "embeddings (École)",              "size": "274 MB"},
]


def load_cfg():
    base = {"ollama_model": OLLAMA_MODEL, "claude_model": CLAUDE_MODEL, "mode": "duel",
            "thinking": False, "num_ctx": 8192, "num_predict": 2048, "keep_alive": -1}
    if CONFIG.exists():
        try:
            base.update(json.loads(CONFIG.read_text()))
        except Exception:
            pass
    return base


def save_cfg(d):
    cur = load_cfg(); cur.update({k: v for k, v in d.items() if v is not None})
    CONFIG.write_text(json.dumps(cur, indent=2)); return cur


app = FastAPI(title="Oracle AI")
db.init_db()
auth.init_auth()


class Attachment(BaseModel):
    name: str
    type: str = "text"
    data: str = ""
    size: int = 0


class DuelReq(BaseModel):
    q: str
    noclaude: bool = False
    ollama_model: Optional[str] = None
    claude_model: Optional[str] = None
    conv_id: Optional[int] = None
    web: bool = False
    attachments: List[Attachment] = []


def guard(model: str) -> str:
    """Garde-fou : jamais fable ni mythos."""
    low = model.lower()
    if "fable" in low or "mythos" in low:
        raise HTTPException(400, f"Modèle interdit par le garde-fou : {model}")
    ok = (low.startswith("claude-opus") or low.startswith("claude-sonnet")
          or low.startswith("claude-haiku") or low in ("opus", "sonnet", "haiku"))
    if not ok:
        raise HTTPException(400, f"Modèle Claude hors liste blanche : {model}")
    return model


async def ask_claude_cli(prompt, ctx):
    """Claude via Claude Code (mode headless -p) — utilise l'abo Max, aucune clé."""
    cbin = find_claude()
    if not cbin:
        return {"model": CLAUDE_MODEL, "error": "Binaire 'claude' introuvable — mets CLAUDE_BIN=/chemin/vers/claude dans .env", "ms": 0}
    model = guard(CLAUDE_MODEL)
    full = (f"Contexte externe :\n{ctx}\n\n{prompt}") if ctx else prompt
    t0 = time.time()
    try:
        proc = await asyncio.create_subprocess_exec(
            cbin, "-p", full,
            "--output-format", "json",
            "--model", model,
            "--append-system-prompt", persona(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=claude_env(),
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=240)
        ms = int((time.time() - t0) * 1000)
        if proc.returncode != 0:
            return {"model": model, "error": f"claude -p code {proc.returncode}: {err.decode()[:180]}", "ms": ms}
        j = json.loads(out.decode())
        return {"model": model, "text": j.get("result", ""),
                "tokens": (j.get("usage") or {}).get("output_tokens"), "ms": ms}
    except asyncio.TimeoutError:
        return {"model": model, "error": "timeout (240s)", "ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        return {"model": model, "error": str(e), "ms": int((time.time() - t0) * 1000)}


async def ask_ollama(client, prompt, ctx):
    sys = "Tu es un assistant expert (code et culture générale). Réponds de façon concise et directe."
    if ctx:
        sys += f"\n\nContexte externe :\n{ctx}"
    t0 = time.time()
    try:
        r = await client.post(f"{OLLAMA_URL}/api/chat", json={
            "model": OLLAMA_MODEL,
            "messages": [{"role": "system", "content": sys},
                         {"role": "user", "content": prompt}],
            "stream": False,
        }, timeout=180)
        r.raise_for_status()
        j = r.json()
        return {"model": OLLAMA_MODEL,
                "text": j.get("message", {}).get("content", ""),
                "tokens": j.get("eval_count"),
                "ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        synapse.panne(e)
        return {"model": OLLAMA_MODEL, "error": f"Ollama injoignable ({e})",
                "ms": int((time.time() - t0) * 1000)}


async def ask_claude_api(client, prompt, ctx):
    if not ANTHROPIC_KEY:
        return {"model": CLAUDE_MODEL, "error": "Clé ANTHROPIC_API_KEY manquante (voir .env)", "ms": 0}
    model = guard(CLAUDE_MODEL)
    sys = "Tu es un assistant expert (code et culture générale). Réponds de façon concise et directe."
    if ctx:
        sys += f"\n\nContexte externe :\n{ctx}"
    t0 = time.time()
    try:
        r = await client.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": model, "max_tokens": MAX_TOKENS,
                  "system": sys,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=180)
        if r.status_code >= 400:
            return {"model": model, "error": f"API {r.status_code}: {r.text[:180]}",
                    "ms": int((time.time() - t0) * 1000)}
        j = r.json()
        text = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
        usage = j.get("usage", {})
        return {"model": model, "text": text,
                "tokens": usage.get("output_tokens"),
                "ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        return {"model": model, "error": str(e), "ms": int((time.time() - t0) * 1000)}


async def get_context(client, q):
    """Contexte fourni par un service de mémoire externe (optionnel)."""
    if not MEMORY_URL:
        return ""
    try:
        r = await client.post(f"{MEMORY_URL}/api/ask",
            headers={"X-API-Key": MEMORY_KEY, "Content-Type": "application/json"},
            json={"q": q}, timeout=30)
        if r.status_code < 400:
            j = r.json()
            return j.get("answer") or j.get("context") or ""
    except Exception:
        pass
    return ""


VERSION = "1.1.0"


@app.get("/healthz")
async def healthz():
    """Sonde publique et minimale (Hub, Docker) — l'état détaillé reste sur /api/health, protégé."""
    return {"ok": True, "version": VERSION}


@app.post("/api/duel")
async def duel(req: DuelReq):
    if not req.q.strip():
        raise HTTPException(400, "Question vide")
    async with httpx.AsyncClient() as client:
        ctx = await get_context(client, req.q)
        tasks = [ask_ollama(client, req.q, ctx)]
        if not req.noclaude:
            if CLAUDE_MODE == "cli":
                tasks.append(ask_claude_cli(req.q, ctx))
            else:
                tasks.append(ask_claude_api(client, req.q, ctx))
        res = await asyncio.gather(*tasks)
    qwen = res[0]
    claude = res[1] if not req.noclaude and len(res) > 1 else {"model": CLAUDE_MODEL, "text": ""}
    verdict = judge_mod.judge(qwen, claude, noclaude=req.noclaude)
    if not req.noclaude:
        synapse.duel(req.q, verdict, qwen.get("model", ""), claude.get("model", ""))
    return {"context": (ctx[:80] + "…") if ctx else "", "qwen": qwen, "claude": claude,
            "verdict": verdict}


def build_attachments(atts):
    """Transforme les pièces jointes en texte injectable dans le prompt."""
    if not atts:
        return "", []
    parts, images = [], []
    for a in atts:
        if a.type == "image":
            images.append(a)
            parts.append(f"[Image jointe : {a.name}]")
        else:
            body = a.data
            if len(body) > 40000:
                body = body[:40000] + "\n…(tronqué)"
            parts.append(f"--- Fichier joint : {a.name} ---\n{body}\n--- fin ---")
    return "\n\n".join(parts), images


SEARCH_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"


def _clean_html(s):
    s = re.sub(r"<[^>]+>", "", s or "")
    return html_mod.unescape(s).strip()


def _fix_ddg_url(u):
    """DDG enveloppe les liens : //duckduckgo.com/l/?uddg=<url encodée>"""
    if "uddg=" in u:
        m = re.search(r"uddg=([^&]+)", u)
        if m:
            return urllib.parse.unquote(m.group(1))
    if u.startswith("//"):
        return "https:" + u
    return u


async def _search_ddg(client, query, n):
    r = await client.post("https://html.duckduckgo.com/html/", data={"q": query})
    h = r.text
    items = re.findall(
        r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>(.*?)(?=class="result__a"|</html>)',
        h, re.S)
    res = []
    for url, title, rest in items[:n]:
        sm = re.search(r'result__snippet[^>]*>(.*?)</a>', rest, re.S)
        res.append({"url": _fix_ddg_url(url), "title": _clean_html(title),
                    "snippet": _clean_html(sm.group(1))[:300] if sm else ""})
    return res


async def _search_lite(client, query, n):
    """Repli : version 'lite' de DuckDuckGo, HTML plus simple."""
    r = await client.post("https://lite.duckduckgo.com/lite/", data={"q": query})
    h = r.text
    urls = re.findall(r'<a[^>]+class="result-link"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', h, re.S)
    snips = re.findall(r'class="result-snippet"[^>]*>(.*?)</td>', h, re.S)
    res = []
    for i, (url, title) in enumerate(urls[:n]):
        res.append({"url": _fix_ddg_url(url), "title": _clean_html(title),
                    "snippet": _clean_html(snips[i])[:300] if i < len(snips) else ""})
    return res


async def web_search(query, n=5):
    """Recherche web sans clé API. Essaie DDG html puis lite."""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                     headers={"User-Agent": SEARCH_UA,
                                              "Accept-Language": "fr-FR,fr;q=0.9"}) as client:
            for fn in (_search_ddg, _search_lite):
                try:
                    res = await fn(client, query, n)
                    res = [r for r in res if r.get("url", "").startswith("http")]
                    if res:
                        return res
                except Exception:
                    continue
        return []
    except Exception:
        return []


def format_web(results):
    if not results:
        return ""
    lines = ["Résultats de recherche web (utilise-les et cite les sources) :"]
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r['title']}\n{r['snippet']}\nSource : {r['url']}")
    return "\n\n".join(lines)


async def stream_ollama(q, ctx, out, model=None, history=None):
    """Producteur : pousse les deltas Qwen dans la queue `out`. `history` = mémoire du fil."""
    model = model or OLLAMA_MODEL
    sys = persona() + (f"\n\nContexte externe :\n{ctx}" if ctx else "")
    messages = [{"role": "system", "content": sys}]
    if history:
        messages += history
    messages.append({"role": "user", "content": q})
    t0 = time.time(); full = []
    cfg = load_cfg()
    payload = {
        "model": model, "messages": messages, "stream": True,
        # garde le modèle en mémoire : plus de rechargement de 9 Go entre deux questions
        "keep_alive": cfg.get("keep_alive", -1),
        "options": {
            "num_ctx": int(cfg.get("num_ctx", 8192)),
            "num_predict": int(cfg.get("num_predict", 2048)),
        },
    }
    # qwen3 & co réfléchissent avant de répondre : très lent pour du bavardage.
    # think=False coupe ce raisonnement interne (réglable dans Paramètres).
    if not cfg.get("thinking", False):
        payload["think"] = False
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            async with client.stream("POST", f"{OLLAMA_URL}/api/chat", json=payload) as r:
                if r.status_code >= 400:
                    body = (await r.aread()).decode(errors="ignore")[:120]
                    # certains modèles ne supportent pas "think" → on réessaie sans
                    if "think" in body.lower() and "think" in payload:
                        payload.pop("think")
                        async with client.stream("POST", f"{OLLAMA_URL}/api/chat", json=payload) as r2:
                            async for line in r2.aiter_lines():
                                if not line.strip():
                                    continue
                                j = json.loads(line)
                                piece = j.get("message", {}).get("content", "")
                                if piece:
                                    full.append(piece)
                                    await out.put(("delta", {"side": "qwen", "text": piece}))
                                if j.get("done"):
                                    break
                        await out.put(("meta", {"side": "qwen", "model": model,
                                                "text": "".join(full), "ms": int((time.time()-t0)*1000)}))
                        return
                    await out.put(("meta", {"side": "qwen", "model": model,
                                            "error": f"Ollama {r.status_code} : {body}", "ms": 0})); return
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    j = json.loads(line)
                    piece = j.get("message", {}).get("content", "")
                    if piece:
                        full.append(piece)
                        await out.put(("delta", {"side": "qwen", "text": piece}))
                    if j.get("done"):
                        break
        await out.put(("meta", {"side": "qwen", "model": model,
                                "text": "".join(full), "ms": int((time.time()-t0)*1000)}))
    except Exception as e:
        synapse.panne(e)
        await out.put(("meta", {"side": "qwen", "model": model,
                                "error": f"Ollama injoignable ({e})", "ms": int((time.time()-t0)*1000)}))


async def stream_claude(q, ctx, out, model=None):
    """Producteur : pousse les deltas Claude (Claude Code, abo Max) dans la queue `out`."""
    cbin = find_claude()
    if not cbin:
        await out.put(("meta", {"side": "claude", "model": model or CLAUDE_MODEL,
                                "error": "Binaire 'claude' introuvable — mets CLAUDE_BIN=/chemin/vers/claude dans .env", "ms": 0})); return
    model = guard(model or CLAUDE_MODEL)
    full_prompt = (f"Contexte externe :\n{ctx}\n\n{q}") if ctx else q
    t0 = time.time(); full = []
    try:
        proc = await asyncio.create_subprocess_exec(
            cbin, "-p", full_prompt,
            "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--model", model, "--append-system-prompt", persona(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=claude_env())
        async for raw in proc.stdout:
            line = raw.decode(errors="ignore").strip()
            if not line:
                continue
            try:
                j = json.loads(line)
            except Exception:
                continue
            if j.get("type") == "stream_event":
                ev = j.get("event", {})
                if ev.get("type") == "content_block_delta" and ev.get("delta", {}).get("type") == "text_delta":
                    piece = ev["delta"].get("text", "")
                    if piece:
                        full.append(piece)
                        await out.put(("delta", {"side": "claude", "text": piece}))
            elif j.get("type") == "result":
                if j.get("is_error") or j.get("api_error_status"):
                    msg = j.get("result") or f"erreur API {j.get('api_error_status')}"
                    await out.put(("meta", {"side": "claude", "model": model,
                                            "error": str(msg)[:200],
                                            "ms": int((time.time()-t0)*1000)}))
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    return
                if not full:
                    full.append(j.get("result", ""))
                    await out.put(("delta", {"side": "claude", "text": j.get("result", "")}))
        rc = await proc.wait()
        if rc != 0 and not full:
            err = (await proc.stderr.read()).decode(errors="ignore").strip()
            await out.put(("meta", {"side": "claude", "model": model,
                                    "error": f"claude a quitté (code {rc}) : {err[:180] or 'aucun détail'}",
                                    "ms": int((time.time()-t0)*1000)}))
            return
        await out.put(("meta", {"side": "claude", "model": model,
                                "text": "".join(full), "ms": int((time.time()-t0)*1000)}))
    except Exception as e:
        await out.put(("meta", {"side": "claude", "model": model,
                                "error": str(e), "ms": int((time.time()-t0)*1000)}))


@app.post("/api/duel/stream")
async def duel_stream(req: DuelReq, request: Request):
    user = require_user(request)
    if not req.q.strip():
        raise HTTPException(400, "Question vide")

    async def gen():
        out = asyncio.Queue()
        async with httpx.AsyncClient() as client:
            ctx = await get_context(client, req.q)
        if ctx:
            yield f'data: {json.dumps({"t":"ctx","text":ctx[:80]+"…"})}\n\n'

        cfg = load_cfg()
        om = req.ollama_model or cfg.get("ollama_model")
        cm = req.claude_model or cfg.get("claude_model")

        # pièces jointes → injectées dans le contexte
        att_text, att_images = build_attachments(req.attachments)
        if att_text:
            ctx = (ctx + "\n\n" if ctx else "") + att_text

        # recherche web → résultats injectés dans le contexte
        if req.web:
            yield f'data: {json.dumps({"t":"status","text":"Recherche web en cours…"})}\n\n'
            results = await web_search(req.q)
            if results:
                ctx = (ctx + "\n\n" if ctx else "") + format_web(results)
                yield f'data: {json.dumps({"t":"web","results":results})}\n\n'
            else:
                yield f'data: {json.dumps({"t":"status","text":"Aucun résultat web trouvé"})}\n\n'
        # mémoire du fil (mode Qwen seul uniquement)
        history = None
        if req.noclaude and req.conv_id:
            conv = db.get_conversation(req.conv_id)
            if conv:
                history = []
                for dd in conv["duels"]:
                    history.append({"role": "user", "content": dd["question"]})
                    if dd.get("qwen_text"):
                        history.append({"role": "assistant", "content": dd["qwen_text"]})
        producers = [asyncio.create_task(stream_ollama(req.q, ctx, out, om, history))]
        if not req.noclaude:
            producers.append(asyncio.create_task(stream_claude(req.q, ctx, out, cm)))

        qwen_meta, claude_meta = None, None
        expected = 1 + (0 if req.noclaude else 1)
        metas = 0
        while metas < expected:
            kind, data = await out.get()
            if kind == "delta":
                yield f'data: {json.dumps({"t":"delta","side":data["side"],"text":data["text"]})}\n\n'
            elif kind == "meta":
                metas += 1
                if data["side"] == "qwen": qwen_meta = data
                else: claude_meta = data
                yield f'data: {json.dumps({"t":"meta",**data})}\n\n'

        await asyncio.gather(*producers, return_exceptions=True)
        qwen_meta = qwen_meta or {"model": om, "text": "", "ms": 0}
        claude_meta = claude_meta or {"model": cm, "text": ""}
        verdict = judge_mod.judge(qwen_meta, claude_meta, noclaude=req.noclaude, question=req.q)
        if not req.noclaude:
            synapse.duel(req.q, verdict, qwen_meta.get("model", ""), claude_meta.get("model", ""))
        conv_id = req.conv_id or db.new_conversation(req.q, "qwen" if req.noclaude else "duel", user_id=user["id"])
        did = db.save_duel(conv_id, req.q, "qwen" if req.noclaude else "duel", qwen_meta, claude_meta, verdict)
        yield f'data: {json.dumps({"t":"verdict","verdict":verdict,"id":did,"conv_id":conv_id})}\n\n'
        yield f'data: {json.dumps({"t":"done"})}\n\n'

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


import re as _re
_MODEL_RE = _re.compile(r"^[a-zA-Z0-9._:\-]+$")


@app.get("/api/models")
async def models():
    installed = []
    ollama_error = None
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(f"{OLLAMA_URL}/api/tags", timeout=5)
            installed = [m["name"] for m in r.json().get("models", [])]
        except Exception as e:
            ollama_error = f"Ollama injoignable sur {OLLAMA_URL} ({type(e).__name__})"
    return {"installed": installed, "catalog": CATALOG,
            "ollama_error": ollama_error, "ollama_url": OLLAMA_URL,
            "claude": ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"],
            "config": load_cfg()}


class Cfg(BaseModel):
    ollama_model: Optional[str] = None
    claude_model: Optional[str] = None
    mode: Optional[str] = None
    thinking: Optional[bool] = None
    num_ctx: Optional[int] = None
    num_predict: Optional[int] = None


@app.post("/api/config")
async def set_config(c: Cfg):
    if c.claude_model:
        guard(c.claude_model)
    avant = load_cfg()
    apres = save_cfg(c.model_dump())
    changes = {k: apres[k] for k in ("ollama_model", "claude_model", "mode")
               if k in apres and apres.get(k) != avant.get(k)}
    if changes:
        synapse.reglage("Oracle : réglage changé — "
                        + ", ".join(f"{k} → {v}" for k, v in changes.items()), changes)
    return apres


class PullReq(BaseModel):
    model: str


@app.post("/api/pull")
async def pull(req: PullReq):
    if not _MODEL_RE.match(req.model or ""):
        raise HTTPException(400, "Nom de modèle invalide")

    async def gen():
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("POST", f"{OLLAMA_URL}/api/pull",
                                         json={"model": req.model, "stream": True}) as r:
                    async for line in r.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            j = json.loads(line)
                        except Exception:
                            continue
                        pct = None
                        if j.get("total"):
                            pct = round(100 * j.get("completed", 0) / j["total"], 1)
                        yield f'data: {json.dumps({"status": j.get("status",""), "pct": pct})}\n\n'
            synapse.modele(req.model)
            yield f'data: {json.dumps({"status":"success","pct":100,"done":True})}\n\n'
        except Exception as e:
            yield f'data: {json.dumps({"status":f"erreur: {e}","done":True})}\n\n'

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/conversations")
async def conversations(request: Request, limit: int = 200):
    u = require_user(request)
    return {"conversations": db.list_conversations(limit, u["id"])}


@app.get("/api/conversation/{cid}")
async def conversation_one(cid: int):
    d = db.get_conversation(cid)
    if not d:
        raise HTTPException(404, "conversation introuvable")
    return d


@app.get("/api/search")
async def search(request: Request, q: str = ""):
    u = require_user(request)
    return {"conversations": db.search_conversations(q, user_id=u["id"]) if q.strip()
            else db.list_conversations(40, u["id"])}


@app.get("/api/stats")
async def api_stats():
    return db.stats()


# ==========================================================
#                     AUTHENTIFICATION
# ==========================================================
PUBLIC_PATHS = {"/api/auth/status", "/api/auth/setup", "/api/auth/login",
                "/api/auth/login/2fa", "/api/auth/check-password"}


def client_ip(request: Request):
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def current_user(request: Request):
    return auth.read_session(request.cookies.get(auth.SESSION_COOKIE))


def require_user(request: Request):
    u = current_user(request)
    if not u:
        raise HTTPException(401, "Non authentifié")
    return u


# Jeton de service posé par le Hub : il ouvre l'API (état, modèles, duels)
# mais jamais les comptes — /api/auth/* reste réservé aux sessions humaines.
HUB_TOKEN = os.getenv("ORACLE_HUB_TOKEN", "")


def _jeton_hub_valide(request: Request) -> bool:
    if not HUB_TOKEN:
        return False
    h = request.headers.get("authorization", "")
    import hmac as _hmac
    return h.startswith("Bearer ") and _hmac.compare_digest(h[7:], HUB_TOKEN)


# nommé porte_auth, pas guard : un middleware homonyme du garde-fou de modèle
# le masquerait, et chaque appel guard(modèle) planterait au lieu de filtrer.
@app.middleware("http")
async def porte_auth(request: Request, call_next):
    path = request.url.path
    # Ressources publiques : page de login, assets, endpoints d'auth
    if (path in PUBLIC_PATHS or path == "/" or path.startswith("/static")
            or path.startswith("/login") or path == "/favicon.ico"
            or path == "/healthz"):
        return await call_next(request)
    if path.startswith("/api/"):
        if (not path.startswith("/api/auth/") and not path.startswith("/api/users")
                and _jeton_hub_valide(request)):
            return await call_next(request)
        if not auth.has_users():
            return JSONResponse({"detail": "Setup requis"}, status_code=401)
        if not current_user(request):
            return JSONResponse({"detail": "Non authentifié"}, status_code=401)
    return await call_next(request)


def set_session_cookie(resp: Response, user, secure: bool):
    resp.set_cookie(auth.SESSION_COOKIE, auth.make_session(user),
                    max_age=auth.SESSION_MAX_AGE, httponly=True,
                    samesite="lax", secure=secure, path="/")


def is_https(request: Request):
    return (request.headers.get("x-forwarded-proto", "").lower() == "https"
            or request.url.scheme == "https")


@app.get("/api/auth/status")
async def auth_status(request: Request):
    u = current_user(request)
    return {"setup_needed": not auth.has_users(),
            "authenticated": bool(u),
            "user": {"id": u["id"], "username": u["username"],
                     "totp_enabled": bool(u["totp_enabled"]), "is_admin": bool(u["is_admin"])} if u else None}


class PwCheck(BaseModel):
    password: str
    username: str = ""


@app.post("/api/auth/check-password")
async def check_password(b: PwCheck):
    return {"score": auth.password_score(b.password),
            "issues": auth.password_issues(b.password, b.username)}


class SetupReq(BaseModel):
    username: str
    password: str


@app.post("/api/auth/setup")
async def auth_setup(b: SetupReq, request: Request):
    """Création du premier compte (admin). Possible seulement si aucun compte."""
    if auth.has_users():
        raise HTTPException(403, "Un compte existe déjà")
    try:
        uid, secret = auth.create_user(b.username, b.password, is_admin=True)
    except ValueError as e:
        raise HTTPException(400, str(e))
    uri = auth.totp_uri(b.username, secret)
    return {"ok": True, "user_id": uid, "secret": secret,
            "qr": auth.qr_data_uri(uri), "uri": uri}


class LoginReq(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
async def auth_login(b: LoginReq, request: Request, response: Response):
    ip = client_ip(request)
    if auth.rate_limited(ip):
        raise HTTPException(429, "Trop de tentatives — réessaie dans 15 minutes")
    u = auth.get_user(b.username)
    if not u or not auth.verify_password(u, b.password):
        auth.record_fail(ip)
        raise HTTPException(401, "Identifiants incorrects")
    if u["totp_enabled"]:
        # étape 2 requise : jeton court, ne vaut pas session
        tok = auth.serializer().dumps({"pending": u["id"]})
        return {"need_2fa": True, "pending": tok}
    auth.clear_fails(ip)
    auth.touch_login(u["id"])
    set_session_cookie(response, u, is_https(request))
    return {"ok": True, "user": {"id": u["id"], "username": u["username"],
                                 "totp_enabled": False, "is_admin": bool(u["is_admin"])}}


class TwoFA(BaseModel):
    pending: str
    code: str
    backup: bool = False


@app.post("/api/auth/login/2fa")
async def auth_2fa(b: TwoFA, request: Request, response: Response):
    ip = client_ip(request)
    if auth.rate_limited(ip):
        raise HTTPException(429, "Trop de tentatives — réessaie dans 15 minutes")
    try:
        data = auth.serializer().loads(b.pending, max_age=300)
    except Exception:
        raise HTTPException(401, "Session expirée, recommence la connexion")
    u = auth.get_user_by_id(data.get("pending"))
    if not u:
        raise HTTPException(401, "Utilisateur introuvable")
    ok = auth.use_backup_code(u["id"], b.code) if b.backup else auth.verify_totp(u["totp_secret"], b.code)
    if not ok:
        auth.record_fail(ip)
        raise HTTPException(401, "Code incorrect")
    auth.clear_fails(ip)
    auth.touch_login(u["id"])
    set_session_cookie(response, u, is_https(request))
    return {"ok": True, "user": {"id": u["id"], "username": u["username"],
                                 "totp_enabled": True, "is_admin": bool(u["is_admin"])}}


@app.post("/api/auth/logout")
async def auth_logout(response: Response):
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/auth/totp/setup")
async def totp_setup(request: Request):
    u = require_user(request)
    if u["totp_enabled"]:
        raise HTTPException(400, "2FA déjà activée")
    uri = auth.totp_uri(u["username"], u["totp_secret"])
    return {"secret": u["totp_secret"], "qr": auth.qr_data_uri(uri), "uri": uri}


class CodeReq(BaseModel):
    code: str


@app.post("/api/auth/totp/enable")
async def totp_enable(b: CodeReq, request: Request):
    u = require_user(request)
    try:
        codes = auth.enable_totp(u["id"], b.code)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "backup_codes": codes}


class PwChange(BaseModel):
    old: str
    new: str


@app.post("/api/auth/password")
async def change_pw(b: PwChange, request: Request, response: Response):
    u = require_user(request)
    try:
        auth.change_password(u["id"], b.old, b.new)
    except ValueError as e:
        raise HTTPException(400, str(e))
    fresh = auth.get_user_by_id(u["id"])
    set_session_cookie(response, fresh, is_https(request))
    return {"ok": True}


# ---------- gestion des comptes (admin) ----------
class NewUser(BaseModel):
    username: str
    password: str
    is_admin: bool = False


@app.get("/api/users")
async def users_list(request: Request):
    require_user(request)
    return {"users": auth.list_users()}


@app.post("/api/users")
async def users_create(b: NewUser, request: Request):
    u = require_user(request)
    if not u["is_admin"]:
        raise HTTPException(403, "Réservé à l'administrateur")
    try:
        uid, secret = auth.create_user(b.username, b.password, b.is_admin)
    except ValueError as e:
        raise HTTPException(400, str(e))
    uri = auth.totp_uri(b.username, secret)
    return {"ok": True, "user_id": uid, "qr": auth.qr_data_uri(uri), "secret": secret}


@app.delete("/api/users/{uid}")
async def users_delete(uid: int, request: Request):
    u = require_user(request)
    if not u["is_admin"]:
        raise HTTPException(403, "Réservé à l'administrateur")
    try:
        auth.delete_user(uid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


# ==========================================================
#             PROJETS & ACTIONS SUR LES CONVERSATIONS
# ==========================================================
class ProjReq(BaseModel):
    name: str
    color: str = "#7fa4d0"


@app.get("/api/projects")
async def projects_list(request: Request):
    u = require_user(request)
    return {"projects": db.list_projects(u["id"])}


@app.post("/api/projects")
async def projects_create(b: ProjReq, request: Request):
    u = require_user(request)
    return {"id": db.create_project(b.name, b.color, u["id"])}


@app.patch("/api/projects/{pid}")
async def projects_rename(pid: int, b: ProjReq, request: Request):
    require_user(request)
    db.rename_project(pid, b.name)
    return {"ok": True}


@app.delete("/api/projects/{pid}")
async def projects_delete(pid: int, request: Request):
    require_user(request)
    db.delete_project(pid)
    return {"ok": True}


class ConvPatch(BaseModel):
    title: Optional[str] = None
    project_id: Optional[int] = None
    pinned: Optional[bool] = None
    clear_project: bool = False


@app.patch("/api/conversation/{cid}")
async def conv_patch(cid: int, b: ConvPatch, request: Request):
    require_user(request)
    if b.title is not None:
        db.rename_conversation(cid, b.title)
    if b.clear_project:
        db.move_conversation(cid, None)
    elif b.project_id is not None:
        db.move_conversation(cid, b.project_id)
    if b.pinned is not None:
        db.pin_conversation(cid, b.pinned)
    return {"ok": True}


@app.delete("/api/conversation/{cid}")
async def conv_delete(cid: int, request: Request):
    require_user(request)
    db.delete_conversation(cid)
    return {"ok": True}


@app.get("/api/health")
async def health():
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(f"{OLLAMA_URL}/api/tags", timeout=5)
            models = [m["name"] for m in r.json().get("models", [])]
            ollama_ok = True
        except Exception:
            models, ollama_ok = [], False
    return {"version": VERSION,
            "ollama": ollama_ok, "ollama_url": OLLAMA_URL, "models": models,
            "ollama_model": OLLAMA_MODEL, "claude_model": CLAUDE_MODEL,
            "claude_mode": CLAUDE_MODE,
            "claude_cli": bool(find_claude()) if CLAUDE_MODE == "cli" else None,
            "claude_path": find_claude() if CLAUDE_MODE == "cli" else None,
            "anthropic_key": bool(ANTHROPIC_KEY) if CLAUDE_MODE == "api" else None,
            "memory": bool(MEMORY_URL)}


@app.get("/")
async def index():
    return FileResponse(FRONT / "index.html")

app.mount("/", StaticFiles(directory=FRONT), name="static")
