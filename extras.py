"""Вход по паролю и разовая сверка файлов без ID с плейлистом.

Вынесено из core, чтобы ядро оставалось про yt-dlp. Зависимость односторонняя:
здесь импортируется core, обратно — никогда.
"""

import difflib
import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

import core

# ------------------------------------------------------------------- вход

AUTH_PATH = Path(os.environ.get("YTSYNC_AUTH", "/config/auth.json"))
SESSION_DAYS = 180          # полгода, как просил пользователь
COOKIE_NAME = "ytsync_session"
_ITERATIONS = 200_000


def _read() -> dict:
    if not AUTH_PATH.exists():
        return {}
    try:
        return json.loads(AUTH_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write(data: dict) -> None:
    AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = AUTH_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.chmod(tmp, 0o600)
    tmp.replace(AUTH_PATH)


def configured() -> bool:
    a = _read()
    return bool(a.get("hash") and a.get("salt"))


def set_password(pw: str) -> None:
    """Пароль задаёт пользователь при первом входе. Хранится только хеш."""
    if len(pw) < 6:
        raise ValueError("пароль должен быть не короче 6 символов")
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, _ITERATIONS)
    old = _read()
    _write({
        "salt": salt.hex(),
        "hash": dk.hex(),
        "iterations": _ITERATIONS,
        # Секрет подписи сессий: меняется только при «выйти на всех устройствах»
        "secret": old.get("secret") or secrets.token_hex(32),
    })
    core.log("", "пароль установлен" if not old else "пароль изменён")


def check_password(pw: str) -> bool:
    a = _read()
    if not a.get("hash"):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(a["salt"]),
                             int(a.get("iterations", _ITERATIONS)))
    return hmac.compare_digest(dk.hex(), a["hash"])


def issue_token() -> str:
    a = _read()
    exp = int(datetime.now(timezone.utc).timestamp()) + SESSION_DAYS * 86400
    sig = hmac.new(a["secret"].encode(), str(exp).encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def verify_token(tok: str | None) -> bool:
    if not tok or "." not in tok:
        return False
    a = _read()
    if not a.get("secret"):
        return False
    exp_s, _, sig = tok.partition(".")
    if not exp_s.isdigit() or int(exp_s) < datetime.now(timezone.utc).timestamp():
        return False
    good = hmac.new(a["secret"].encode(), exp_s.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(good, sig)


def revoke_sessions() -> None:
    """Смена секрета — все ранее выданные куки разом перестают работать."""
    a = _read()
    if not a:
        return
    a["secret"] = secrets.token_hex(32)
    _write(a)
    core.log("", "сессии сброшены на всех устройствах")


# ------------------------------------- сверка файлов без ID с плейлистом

_NOISE = re.compile(r"[^0-9a-zа-яё]+", re.I)
_PREFIX = re.compile(r"^\s*\d{1,4}\s*[-–—.]\s*")


def _norm(s: str) -> str:
    return _NOISE.sub(" ", _PREFIX.sub("", s or "").lower()).strip()


def reconcile_candidates(folder: str, limit: int = 400) -> list[dict]:
    """Пары «файл без ID на диске» ↔ «видео из плейлиста, которого нет локально».

    Сопоставление только по названию, поэтому решение всегда за человеком: для
    YouTube нет базы хешей вроде AniDB, гарантировать совпадение автоматически
    невозможно.
    """
    have = core.downloaded_ids(folder)
    with core._db_lock, core.db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT video_id, title, last_seen FROM items WHERE folder=?", (folder,))]
    latest = max((r["last_seen"] for r in rows), default=None)
    pool = [(r, _norm(r["title"])) for r in rows if r["video_id"] not in have]
    files = core.files_without_id(folder)[:limit]
    if not pool:
        return [{"file": f, "suggestions": []} for f in files]

    out = []
    for rel in files:
        target = _norm(Path(rel).stem)
        scored = []
        if target:
            for r, nt in pool:
                if not nt:
                    continue
                score = difflib.SequenceMatcher(None, target, nt).ratio()
                if score >= 0.45:
                    scored.append({
                        "video_id": r["video_id"], "title": r["title"],
                        "score": round(score, 3),
                        "in_playlist": bool(latest and r["last_seen"] == latest),
                    })
            scored.sort(key=lambda x: -x["score"])
        out.append({"file": rel, "suggestions": scored[:3]})
    return out


def reconcile_apply(folder: str, pairs: list[dict]) -> int:
    """Дописывает подтверждённые ID в archive.txt, чтобы они больше не качались."""
    known = core.archive_ids(folder)
    ids = []
    for p in pairs:
        vid = (p.get("video_id") or "").strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid) and vid not in known:
            ids.append(vid)
            known.add(vid)
    if not ids:
        return 0
    ap = core.archive_path(folder)
    ap.parent.mkdir(parents=True, exist_ok=True)
    body = ap.read_text(errors="replace") if ap.exists() else ""
    if body and not body.endswith("\n"):
        body += "\n"
    ap.write_text(body + "".join(f"youtube {v}\n" for v in ids))
    core.log(folder, f"сверка: в archive.txt добавлено записей — {len(ids)}")
    return len(ids)
