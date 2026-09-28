import sqlite3, json, time, pathlib, threading, os

def _data_dir():
    env = os.getenv("DATA_DIR")
    if env:
        return pathlib.Path(env)
    if os.path.isdir("/app/data"):
        return pathlib.Path("/app/data")
    return pathlib.Path(__file__).resolve().parent.parent


DB = _data_dir() / "oracle.db"
_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS conversations(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, title TEXT, mode TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS duels(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conv_id INTEGER, ts REAL, question TEXT, mode TEXT,
            qwen_model TEXT, claude_model TEXT,
            qwen_text TEXT, claude_text TEXT,
            qwen_ms INTEGER, claude_ms INTEGER,
            winner TEXT, gap REAL, kind TEXT,
            qwen_score REAL, claude_score REAL,
            verdict TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS projects(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, color TEXT, ts REAL, user_id INTEGER)""")
        cols = [r[1] for r in c.execute("PRAGMA table_info(duels)").fetchall()]
        if "conv_id" not in cols:
            c.execute("ALTER TABLE duels ADD COLUMN conv_id INTEGER")
        ccols = [r[1] for r in c.execute("PRAGMA table_info(conversations)").fetchall()]
        if "project_id" not in ccols:
            c.execute("ALTER TABLE conversations ADD COLUMN project_id INTEGER")
        if "user_id" not in ccols:
            c.execute("ALTER TABLE conversations ADD COLUMN user_id INTEGER")
        if "pinned" not in ccols:
            c.execute("ALTER TABLE conversations ADD COLUMN pinned INTEGER DEFAULT 0")


# ---------- projets ----------
def create_project(name, color="#7fa4d0", user_id=None):
    with _lock, _conn() as c:
        cur = c.execute("INSERT INTO projects(name,color,ts,user_id) VALUES(?,?,?,?)",
                        ((name or "Projet")[:60], color, time.time(), user_id))
        return cur.lastrowid


def list_projects(user_id=None):
    with _conn() as c:
        rows = c.execute("""
            SELECT p.id,p.name,p.color,
                   (SELECT COUNT(*) FROM conversations co WHERE co.project_id=p.id) AS n
            FROM projects p WHERE (? IS NULL OR p.user_id=? OR p.user_id IS NULL)
            ORDER BY p.name""", (user_id, user_id)).fetchall()
        return [dict(r) for r in rows]


def rename_project(pid, name):
    with _lock, _conn() as c:
        c.execute("UPDATE projects SET name=? WHERE id=?", ((name or "Projet")[:60], pid))


def delete_project(pid):
    with _lock, _conn() as c:
        c.execute("UPDATE conversations SET project_id=NULL WHERE project_id=?", (pid,))
        c.execute("DELETE FROM projects WHERE id=?", (pid,))


# ---------- actions sur les conversations ----------
def rename_conversation(cid, title):
    with _lock, _conn() as c:
        c.execute("UPDATE conversations SET title=? WHERE id=?", ((title or "Sans titre")[:80], cid))


def delete_conversation(cid):
    with _lock, _conn() as c:
        c.execute("DELETE FROM duels WHERE conv_id=?", (cid,))
        c.execute("DELETE FROM conversations WHERE id=?", (cid,))


def move_conversation(cid, project_id):
    with _lock, _conn() as c:
        c.execute("UPDATE conversations SET project_id=? WHERE id=?", (project_id, cid))


def pin_conversation(cid, pinned=True):
    with _lock, _conn() as c:
        c.execute("UPDATE conversations SET pinned=? WHERE id=?", (1 if pinned else 0, cid))


def new_conversation(title, mode="duel", user_id=None, project_id=None):
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT INTO conversations(ts,title,mode,user_id,project_id) VALUES(?,?,?,?,?)",
            (time.time(), (title or "Nouveau duel")[:80], mode, user_id, project_id))
        return cur.lastrowid


def save_duel(conv_id, question, mode, qwen, claude, verdict):
    with _lock, _conn() as c:
        cur = c.execute(
            """INSERT INTO duels(conv_id,ts,question,mode,qwen_model,claude_model,
               qwen_text,claude_text,qwen_ms,claude_ms,winner,gap,kind,
               qwen_score,claude_score,verdict)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (conv_id, time.time(), question, mode,
             qwen.get("model"), claude.get("model"),
             qwen.get("text", ""), claude.get("text", ""),
             qwen.get("ms"), claude.get("ms"),
             verdict.get("winner"), verdict.get("gap"), verdict.get("kind"),
             (verdict.get("qwen") or {}).get("total"),
             (verdict.get("claude") or {}).get("total"),
             json.dumps(verdict)))
        c.execute("UPDATE conversations SET ts=? WHERE id=?", (time.time(), conv_id))
        return cur.lastrowid


def list_conversations(limit=200, user_id=None):
    with _conn() as c:
        rows = c.execute("""
            SELECT co.id, co.ts, co.title, co.project_id, co.pinned, co.mode,
                   p.name AS project_name, p.color AS project_color,
                   (SELECT COUNT(*) FROM duels d WHERE d.conv_id=co.id) AS n,
                   (SELECT winner FROM duels d WHERE d.conv_id=co.id ORDER BY d.id DESC LIMIT 1) AS last_winner
            FROM conversations co
            LEFT JOIN projects p ON p.id=co.project_id
            WHERE (? IS NULL OR co.user_id=? OR co.user_id IS NULL)
            ORDER BY co.pinned DESC, co.ts DESC LIMIT ?""", (user_id, user_id, limit)).fetchall()
        return [dict(r) for r in rows]


def get_conversation(cid):
    with _conn() as c:
        co = c.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
        if not co:
            return None
        rows = c.execute("SELECT * FROM duels WHERE conv_id=? ORDER BY id", (cid,)).fetchall()
    duels = []
    for r in rows:
        d = dict(r)
        try:
            d["verdict"] = json.loads(d["verdict"])
        except Exception:
            d["verdict"] = None
        duels.append(d)
    return {"conversation": dict(co), "duels": duels}


def search_conversations(q, limit=40, user_id=None):
    with _conn() as c:
        rows = c.execute("""
            SELECT DISTINCT co.id, co.ts, co.title, co.project_id,
                   p.name AS project_name,
                   (SELECT winner FROM duels d WHERE d.conv_id=co.id ORDER BY d.id DESC LIMIT 1) AS last_winner
            FROM conversations co
            LEFT JOIN duels d ON d.conv_id=co.id
            LEFT JOIN projects p ON p.id=co.project_id
            WHERE (co.title LIKE ? OR d.question LIKE ?)
              AND (? IS NULL OR co.user_id=? OR co.user_id IS NULL)
            ORDER BY co.ts DESC LIMIT ?""", (f"%{q}%", f"%{q}%", user_id, user_id, limit)).fetchall()
        return [dict(r) for r in rows]


def stats():
    with _conn() as c:
        rows = c.execute(
            "SELECT winner,qwen_score,claude_score,gap FROM duels WHERE winner IS NOT NULL ORDER BY id").fetchall()
        total = c.execute("SELECT COUNT(*) FROM duels").fetchone()[0]
    rows = [dict(r) for r in rows]
    judged = [r for r in rows if r["winner"] in ("qwen", "claude")]
    qwins = sum(1 for r in judged if r["winner"] == "qwen")
    avg_q = round(sum(r["qwen_score"] or 0 for r in judged) / len(judged), 2) if judged else 0
    avg_c = round(sum(r["claude_score"] or 0 for r in judged) / len(judged), 2) if judged else 0
    gaps = [r["gap"] for r in judged if r["gap"] is not None]
    return {
        "total": total, "judged": len(judged),
        "qwen_winrate": round(100 * qwins / len(judged)) if judged else 0,
        "avg_qwen": avg_q, "avg_claude": avg_c,
        "gaps": gaps[-20:], "last_gap": gaps[-1] if gaps else None,
    }
