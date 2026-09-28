import re

# ---- détection de features de qualité (déterministe, pas d'IA) ----
FEATURES = {
    "gestion d'erreur":      r"try\b|except\b|catch\b|\.catch\(|Result<|err\s*!=\s*nil|throw\b|raise\b",
    "cross-browser -webkit-": r"-webkit-|-moz-|-ms-",
    "accessibilité (aria)":  r"aria-[a-z]+|role\s*=|alt\s*=",
    "commentaires":          r"//|/\*|<!--|\n\s*#\s",
    "transitions / anim":    r"transition\s*:|@keyframes|animation\s*:",
    "responsive":            r"@media|clamp\(|min\(|max\(|\d+vw|\d+vh",
    "variables / tokens":    r"var\(--|:root|--[a-z-]+\s*:",
    "cas limites":           r"if\s*\(|guard\b|is None|== null|=== undefined|isEmpty|len\(",
    "typage":                r":\s*(string|number|int|str|bool|float)\b|<[A-Z]\w+>|interface\b|type\b",
}

CODE_HINT = re.compile(r"```|<[a-z]+[ />]|def\s|func\s|class\s|SELECT\s|const\s|=>", re.I)


def _extract_code(text):
    blocks = re.findall(r"```[a-zA-Z0-9]*\n(.*?)```", text, re.S)
    return "\n".join(blocks) if blocks else text


def detect_kind(text):
    return "code" if CODE_HINT.search(text or "") else "prose"


def _features(text):
    code = _extract_code(text)
    return {name for name, rx in FEATURES.items() if re.search(rx, code, re.I)}


def _balanced(text):
    """heuristique d'exécutabilité : accolades/parenthèses équilibrées."""
    for o, c in [("{", "}"), ("(", ")"), ("[", "]")]:
        if text.count(o) != text.count(c):
            return False
    return True


def score_one(text, ms, kind):
    text = text or ""
    n = len(text.strip())
    code = _extract_code(text)
    feats = _features(text)

    substance = 2 + (n > 120) * 2 + (n > 350) * 2 + (n > 700) * 2 + (n > 1400) * 1
    richesse  = min(10, 2 + len(feats) * 1.3)
    concision = 10 if 120 <= n <= 2600 else (6 if n < 120 else 5)
    if kind == "code":
        structure = 3 + ("```" in text) * 3 + _balanced(code) * 3 + ("//" in code or "#" in code or "/*" in code) * 1
    else:
        structure = 3 + (text.count("\n") > 2) * 2 + bool(re.search(r"^\s*[-*\d]", text, re.M)) * 3 + (n > 300) * 2

    crit = {
        "Substance":   round(min(10, substance), 1),
        "Richesse":    round(min(10, richesse), 1),
        "Structure":   round(min(10, structure), 1),
        "Concision":   round(min(10, concision), 1),
    }
    total = round(sum(crit.values()) / len(crit), 2)
    return {"crit": crit, "total": total, "features": sorted(feats), "chars": n}


GREETINGS = re.compile(r"^\s*(salut|bonjour|coucou|hello|hey|hi|yo|ça va|ca va|test|merci|ok|d'accord)\b", re.I)


def _similar(a, b):
    """similarité grossière par recouvrement de mots (0-1)."""
    wa = set(re.findall(r"\w+", (a or "").lower()))
    wb = set(re.findall(r"\w+", (b or "").lower()))
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def is_trivial(question, q_text, c_text):
    """Vrai si la question/les réponses ne méritent pas d'être notées (bavardage)."""
    q = (question or "").strip()
    if len(q) < 12 or GREETINGS.match(q):
        return True
    longest = max(len(q_text or ""), len(c_text or ""))
    if longest < 140 and detect_kind(q_text or c_text) == "prose":
        return True
    return False


def judge(qwen, claude, noclaude=False, question=""):
    """Compare les deux sorties, ajoute la latence relative, désigne le gagnant + delta."""
    q_text = qwen.get("text", "") if not qwen.get("error") else ""
    c_text = claude.get("text", "") if not claude.get("error") else ""
    kind = detect_kind(q_text or c_text)

    # bavardage / réponses quasi identiques → non départagé
    trivial = is_trivial(question, q_text, c_text)
    near_dup = (not noclaude) and _similar(q_text, c_text) >= 0.82
    if trivial or near_dup:
        return {"kind": kind, "qwen": None, "claude": None,
                "winner": None, "gap": 0, "delta": [],
                "skipped": "trivial" if trivial else "identique"}

    q = score_one(q_text, qwen.get("ms", 0), kind)
    c = None if noclaude else score_one(c_text, claude.get("ms", 0), kind)

    # latence relative (le plus rapide = 10)
    if not noclaude:
        qm, cm = qwen.get("ms", 1) or 1, claude.get("ms", 1) or 1
        q["crit"]["Latence"] = 10.0 if qm <= cm else round(10 * cm / qm, 1)
        c["crit"]["Latence"] = 10.0 if cm <= qm else round(10 * qm / cm, 1)
        for s in (q, c):
            s["total"] = round(sum(s["crit"].values()) / len(s["crit"]), 2)

    if noclaude:
        return {"kind": kind, "qwen": q, "claude": None, "winner": "qwen",
                "delta": [], "gap": 0}

    winner = "claude" if c["total"] >= q["total"] else "qwen"
    gap = round(abs(c["total"] - q["total"]), 2)
    win_feats = set(c["features"]) if winner == "claude" else set(q["features"])
    lose_feats = set(q["features"]) if winner == "claude" else set(c["features"])
    delta = sorted(win_feats - lose_feats)  # ce que le gagnant a et l'autre n'a pas

    return {"kind": kind, "qwen": q, "claude": c,
            "winner": winner, "gap": gap, "delta": delta}
