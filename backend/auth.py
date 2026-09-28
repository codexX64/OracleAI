"""Authentification Oracle AI : Argon2id + TOTP (2FA) + sessions signées."""
import os, time, json, base64, secrets, sqlite3, pathlib, io, threading

import pyotp, qrcode
from argon2 import PasswordHasher, Type
from argon2.exceptions import VerifyMismatchError, VerificationError
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

def _data_dir():
    env = os.getenv("DATA_DIR")
    if env:
        return pathlib.Path(env)
    if os.path.isdir("/app/data"):
        return pathlib.Path("/app/data")
    return pathlib.Path(__file__).resolve().parent.parent


DATA_DIR = _data_dir()
DB = DATA_DIR / "oracle.db"
SECRET_FILE = DATA_DIR / ".session_secret"

SESSION_COOKIE = "oracle_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 14          # 14 jours
LOGIN_WINDOW = 15 * 60                       # fenêtre anti-bruteforce
LOGIN_MAX_TRIES = 6

_lock = threading.Lock()
_attempts = {}                               # ip -> [timestamps]

# Argon2id — paramètres solides pour un service exposé
ph = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=4,
                    hash_len=32, salt_len=16, type=Type.ID)


def _secret():
    """Clé de signature des sessions, générée une fois et persistée."""
    if SECRET_FILE.exists():
        return SECRET_FILE.read_bytes()
    s = secrets.token_bytes(64)
    SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    SECRET_FILE.write_bytes(s)
    try:
        os.chmod(SECRET_FILE, 0o600)
    except Exception:
        pass
    return s


_serializer = None


def serializer():
    global _serializer
    if _serializer is None:
        _serializer = URLSafeTimedSerializer(_secret(), salt="oracle-session")
    return _serializer


def _conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_auth():
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            pw_hash TEXT NOT NULL,
            totp_secret TEXT,
            totp_enabled INTEGER DEFAULT 0,
            created REAL,
            last_login REAL,
            is_admin INTEGER DEFAULT 0,
            session_epoch INTEGER DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS backup_codes(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, code_hash TEXT, used INTEGER DEFAULT 0)""")


def has_users():
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0


# ---------- robustesse du mot de passe ----------
COMMON = {"password", "motdepasse", "azerty", "qwerty", "123456", "admin",
          "oracle", "letmein", "welcome", "iloveyou", "abc123", "000000"}


def password_issues(pw, username=""):
    """Renvoie la liste des problèmes ; vide = mot de passe accepté."""
    p = []
    if len(pw) < 12:
        p.append("au moins 12 caractères")
    if not any(c.islower() for c in pw):
        p.append("une minuscule")
    if not any(c.isupper() for c in pw):
        p.append("une majuscule")
    if not any(c.isdigit() for c in pw):
        p.append("un chiffre")
    if not any(not c.isalnum() for c in pw):
        p.append("un caractère spécial")
    low = pw.lower()
    if any(w in low for w in COMMON):
        p.append("pas de mot courant (password, azerty…)")
    if username and username.lower() in low:
        p.append("ne doit pas contenir le nom d'utilisateur")
    if len(set(pw)) < 6:
        p.append("trop peu de caractères différents")
    return p


def password_score(pw):
    """Score indicatif 0-4 pour la jauge du formulaire."""
    s = 0
    if len(pw) >= 12: s += 1
    if len(pw) >= 16: s += 1
    classes = sum([any(c.islower() for c in pw), any(c.isupper() for c in pw),
                   any(c.isdigit() for c in pw), any(not c.isalnum() for c in pw)])
    if classes >= 3: s += 1
    if classes == 4 and len(set(pw)) >= 10: s += 1
    return min(4, s)


# ---------- utilisateurs ----------
def create_user(username, password, is_admin=False):
    username = (username or "").strip()
    if not (3 <= len(username) <= 32) or not username.replace("_", "").replace("-", "").isalnum():
        raise ValueError("Nom d'utilisateur : 3-32 caractères alphanumériques, - et _ autorisés")
    issues = password_issues(password, username)
    if issues:
        raise ValueError("Mot de passe trop faible — il faut : " + ", ".join(issues))
    secret = pyotp.random_base32()
    with _lock, _conn() as c:
        if c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise ValueError("Ce nom d'utilisateur existe déjà")
        cur = c.execute(
            "INSERT INTO users(username,pw_hash,totp_secret,totp_enabled,created,is_admin) VALUES(?,?,?,0,?,?)",
            (username, ph.hash(password), secret, time.time(), 1 if is_admin else 0))
        return cur.lastrowid, secret


def get_user(username):
    with _conn() as c:
        r = c.execute("SELECT * FROM users WHERE username=?", ((username or "").strip(),)).fetchone()
        return dict(r) if r else None


def get_user_by_id(uid):
    with _conn() as c:
        r = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return dict(r) if r else None


def list_users():
    with _conn() as c:
        rows = c.execute(
            "SELECT id,username,totp_enabled,created,last_login,is_admin FROM users ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def delete_user(uid):
    with _lock, _conn() as c:
        n = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if n <= 1:
            raise ValueError("Impossible de supprimer le dernier compte")
        c.execute("DELETE FROM users WHERE id=?", (uid,))
        c.execute("DELETE FROM backup_codes WHERE user_id=?", (uid,))


def verify_password(user, password):
    try:
        ph.verify(user["pw_hash"], password)
    except (VerifyMismatchError, VerificationError):
        return False
    # ré-hash si les paramètres ont changé
    try:
        if ph.check_needs_rehash(user["pw_hash"]):
            with _lock, _conn() as c:
                c.execute("UPDATE users SET pw_hash=? WHERE id=?", (ph.hash(password), user["id"]))
    except Exception:
        pass
    return True


def change_password(uid, old, new):
    u = get_user_by_id(uid)
    if not u or not verify_password(u, old):
        raise ValueError("Mot de passe actuel incorrect")
    issues = password_issues(new, u["username"])
    if issues:
        raise ValueError("Nouveau mot de passe trop faible — il faut : " + ", ".join(issues))
    with _lock, _conn() as c:
        # session_epoch +1 => invalide toutes les sessions existantes
        c.execute("UPDATE users SET pw_hash=?, session_epoch=session_epoch+1 WHERE id=?",
                  (ph.hash(new), uid))


# ---------- TOTP ----------
def totp_uri(username, secret):
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name="Oracle AI")


def qr_data_uri(uri):
    """QR code en SVG encodé base64 — pas besoin de Pillow."""
    import qrcode.image.svg
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=11, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()


def verify_totp(secret, code):
    if not secret or not code:
        return False
    return pyotp.TOTP(secret).verify(str(code).replace(" ", "").strip(), valid_window=1)


def enable_totp(uid, code):
    u = get_user_by_id(uid)
    if not u:
        raise ValueError("Utilisateur introuvable")
    if not verify_totp(u["totp_secret"], code):
        raise ValueError("Code incorrect — vérifie l'heure de ton téléphone")
    codes = [secrets.token_hex(4) for _ in range(8)]
    with _lock, _conn() as c:
        c.execute("UPDATE users SET totp_enabled=1 WHERE id=?", (uid,))
        c.execute("DELETE FROM backup_codes WHERE user_id=?", (uid,))
        for code_ in codes:
            c.execute("INSERT INTO backup_codes(user_id,code_hash) VALUES(?,?)", (uid, ph.hash(code_)))
    return codes


def use_backup_code(uid, code):
    code = (code or "").replace(" ", "").replace("-", "").strip().lower()
    with _lock, _conn() as c:
        rows = c.execute("SELECT id,code_hash FROM backup_codes WHERE user_id=? AND used=0", (uid,)).fetchall()
        for r in rows:
            try:
                ph.verify(r["code_hash"], code)
            except Exception:
                continue
            c.execute("UPDATE backup_codes SET used=1 WHERE id=?", (r["id"],))
            return True
    return False


# ---------- anti-bruteforce ----------
def rate_limited(ip):
    now = time.time()
    with _lock:
        tries = [t for t in _attempts.get(ip, []) if now - t < LOGIN_WINDOW]
        _attempts[ip] = tries
        return len(tries) >= LOGIN_MAX_TRIES


def record_fail(ip):
    with _lock:
        _attempts.setdefault(ip, []).append(time.time())


def clear_fails(ip):
    with _lock:
        _attempts.pop(ip, None)


# ---------- sessions ----------
def make_session(user):
    return serializer().dumps({"uid": user["id"], "u": user["username"],
                               "e": user.get("session_epoch", 0)})


def read_session(token):
    if not token:
        return None
    try:
        data = serializer().loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    u = get_user_by_id(data.get("uid"))
    if not u:
        return None
    if data.get("e", 0) != u.get("session_epoch", 0):
        return None                     # mot de passe changé → session invalide
    return u


def touch_login(uid):
    with _lock, _conn() as c:
        c.execute("UPDATE users SET last_login=? WHERE id=?", (time.time(), uid))
