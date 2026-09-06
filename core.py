"""Ядро ytsync: индексация плейлистов, сверка с локальными файлами, скачивание.

Источник правды о том, что уже скачано — существующие archive.txt в формате
yt-dlp --download-archive плюс ID, извлечённые из имён файлов. Своей БД
для этого не заводим, чтобы не разъезжалась с реальностью на диске.
"""

import json
import os
import re
import shlex
import signal
import sqlite3
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("YTSYNC_CONFIG", "/config/config.json"))
DB_PATH = Path(os.environ.get("YTSYNC_DB", "/config/ytsync.db"))
DOWNLOAD_ROOT = Path(os.environ.get("YTSYNC_DOWNLOAD_ROOT", "/media"))
COOKIES_PATH = Path(os.environ.get("YTSYNC_COOKIES", "/config/cookies.txt"))

# ID видео в конце имени: "... [dQw4w9WgXcQ].mkv"
ID_IN_NAME = re.compile(r"\[([A-Za-z0-9_-]{11})\]\.[A-Za-z0-9]+$")
VIDEO_EXT = {".mkv", ".mp4", ".webm", ".m4v", ".mov", ".avi", ".flv", ".ts"}

# "ERROR: [youtube] abcdefghijk: Video unavailable"
ERR_LINE = re.compile(r"ERROR:\s*\[[^\]]+\]\s*([A-Za-z0-9_-]{11}):\s*(.+)")
DEST_LINE = re.compile(r"\[download\]\s+Destination:\s+(.+)")
PCT_LINE = re.compile(r"\[download\]\s+([\d.]+)%")
# Заголовки, которыми YouTube помечает мёртвые записи в плейлисте
DEAD_TITLES = {"[deleted video]", "[private video]", "[unavailable video]",
               "deleted video", "private video"}
# Куки, которые реально нужны yt-dlp
COOKIE_DOMAINS = ("youtube.com", "youtu.be", "google.com", "googlevideo.com",
                  "ytimg.com", "accounts.google.com")

DEFAULT_CONFIG = {
    "schedule_hours": [4],
    "output_template": "%(playlist_index)s - %(title)s [%(id)s].%(ext)s",
    "write_thumbnail": True,
    "format": "",
    "extra_args": [],
    "cookies_file": "",
    "playlists": [],
    # Не начинать скачивание, если свободного места меньше (ГБ). 0 — без ограничения
    "min_free_gb": 100,
    # Ограничение скорости для yt-dlp, например "5M". Пусто — без ограничения
    "limit_rate": "",
    # Часы, когда скачивать нельзя. Индексация при этом продолжает работать
    "quiet_hours": [],
}

# Русские варианты сообщений журнала. Хранятся в базе как запасной текст на
# случай, если интерфейс встретит ключ, которого не знает.
_RU_LOG = {
    "log_indexed": "проиндексировано записей: {n}",
    "log_dead": "из них недоступно (удалено или приватно): {n}",
    "log_pending": "к загрузке новых: {n}",
    "log_downloaded": "скачано новых: {n}",
    "log_downloading": "качаю: {name}",
    "log_retry": "перепроверяю упущенных: {n}",
    "log_stopped": "остановлено вручную; недокачанный файл сохранён и продолжится "
                   "при следующем запуске",
    "log_paused": "пауза: текущий файл дописан, остальное отложено",
    "log_timeout": "таймаут {n} с",
    "log_badjson": "не удалось разобрать ответ yt-dlp",
    "log_index_failed": "индексация не удалась",
    "log_nospace": "свободно {free} ГБ при пороге {need} ГБ — скачивание не начато",
    "log_added_new": "плейлист добавлен; создана папка и пустой archive.txt",
    "log_added_existing": "плейлист добавлен; использую существующий archive.txt "
                          "({n} записей)",
    "log_removed": "плейлист убран из конфига, файлы не тронуты",
    "log_unqueued": "снято из очереди: {n}",
    "log_queue_cleared": "очередь очищена после остановки: снято {n}",
    "log_schedule": "расписание {h}:00 — поставлено в очередь: {n}",
    "log_quiet": "тихие часы ({h}:00) — встаю после текущего файла",
    "log_reconciled": "сверка: в archive.txt добавлено записей — {n}",
    "log_cookies_saved": "куки сохранены: оставлено {kept} из {total}, "
                         "отброшено посторонних {dropped}",
    "log_cookies_cleared": "куки удалены",
    "log_pw_set": "пароль установлен",
    "log_pw_changed": "пароль изменён",
    "log_sessions_revoked": "сессии сброшены на всех устройствах",
    "log_badlogin": "неудачная попытка входа",
    "log_sched_err": "планировщик: {err}",
}

_cfg_lock = threading.Lock()
_db_lock = threading.Lock()

# Управление текущим процессом yt-dlp.
# stop  — убить сразу; недокачанный файл останется .part и продолжится позже (--continue)
# pause — доработать текущий файл и встать
_proc_lock = threading.Lock()
_proc: subprocess.Popen | None = None
_ctl = {"stop": False, "pause": False}


def _kill_group(p: subprocess.Popen, sig: int) -> None:
    """Бьём по всей группе процессов, а не только по yt-dlp.

    yt-dlp порождает детей — ffmpeg для склейки и deno для JS-задачи. При
    сигнале одному родителю они переживают его и продолжают писать в файл.
    """
    try:
        os.killpg(os.getpgid(p.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            p.kill()
        except OSError:
            pass


def request_stop() -> bool:
    """Прервать немедленно."""
    with _proc_lock:
        _ctl["stop"] = True
        if _proc and _proc.poll() is None:
            _kill_group(_proc, signal.SIGTERM)
            return True
    return False


def request_pause() -> bool:
    """Встать после текущего файла."""
    with _proc_lock:
        if _proc and _proc.poll() is None:
            _ctl["pause"] = True
            return True
    return False


def cancel_control() -> None:
    with _proc_lock:
        _ctl["stop"] = False
        _ctl["pause"] = False


def control_state() -> dict:
    with _proc_lock:
        return dict(_ctl)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- конфиг

def load_config() -> dict:
    with _cfg_lock:
        if not CONFIG_PATH.exists():
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2))
            return json.loads(json.dumps(DEFAULT_CONFIG))
        cfg = json.loads(CONFIG_PATH.read_text())
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        return cfg


def save_config(cfg: dict) -> None:
    with _cfg_lock:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
        tmp.replace(CONFIG_PATH)


# -------------------------------------------------------------------- БД

def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _db_lock, db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS items (
                folder     TEXT NOT NULL,
                video_id   TEXT NOT NULL,
                title      TEXT,
                position   INTEGER,
                first_seen TEXT,
                last_seen  TEXT,
                PRIMARY KEY (folder, video_id)
            );
            CREATE TABLE IF NOT EXISTS problems (
                folder   TEXT NOT NULL,
                video_id TEXT NOT NULL,
                kind     TEXT,          -- dead | error
                message  TEXT,
                ts       TEXT,
                PRIMARY KEY (folder, video_id)
            );
            CREATE TABLE IF NOT EXISTS runs (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                folder   TEXT,
                kind     TEXT,
                started  TEXT,
                finished TEXT,
                ok       INTEGER,
                added    INTEGER DEFAULT 0,
                message  TEXT
            );
            CREATE TABLE IF NOT EXISTS log (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                ts     TEXT,
                folder TEXT,
                level  TEXT,
                line   TEXT
            );
            CREATE INDEX IF NOT EXISTS log_ts ON log(id DESC);
            """
        )
        # Миграция: раньше в журнал писался готовый текст, из-за чего он
        # оставался русским в английском интерфейсе. Теперь рядом хранятся
        # ключ и параметры, а перевод делает морда. Старые записи остаются
        # как есть — у них key пустой, и показывается сохранённый текст.
        cols = {r[1] for r in c.execute("PRAGMA table_info(log)")}
        if "key" not in cols:
            c.execute("ALTER TABLE log ADD COLUMN key TEXT")
        if "args" not in cols:
            c.execute("ALTER TABLE log ADD COLUMN args TEXT")


def log(folder: str, line: str, level: str = "info") -> None:
    """Сырая строка — для вывода yt-dlp и прочего, что переводить нечего."""
    line = line.rstrip()
    if not line:
        return
    with _db_lock, db() as c:
        c.execute("INSERT INTO log (ts, folder, level, line) VALUES (?,?,?,?)",
                  (now(), folder, level, line[:2000]))
        c.execute("DELETE FROM log WHERE id < (SELECT MAX(id)-5000 FROM log)")


def logk(folder: str, key: str, level: str = "info", **args) -> None:
    """Наше собственное сообщение: храним ключ и параметры, а не готовый текст.

    В `line` кладём русский вариант — он останется запасным, если интерфейс
    встретит незнакомый ключ после обновления.
    """
    with _db_lock, db() as c:
        c.execute("INSERT INTO log (ts, folder, level, line, key, args) VALUES (?,?,?,?,?,?)",
                  (now(), folder, level, _RU_LOG.get(key, key).format(**args)[:2000],
                   key, json.dumps(args, ensure_ascii=False)))
        c.execute("DELETE FROM log WHERE id < (SELECT MAX(id)-5000 FROM log)")


def recent_log(limit: int = 300, folder: str | None = None) -> list[dict]:
    q = "SELECT ts, folder, level, line, key, args FROM log"
    args: list = []
    if folder:
        q += " WHERE folder = ?"
        args.append(folder)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    out = []
    with _db_lock, db() as c:
        for r in c.execute(q, args):
            d = dict(r)
            try:
                d["args"] = json.loads(d["args"]) if d["args"] else {}
            except (json.JSONDecodeError, TypeError):
                d["args"] = {}
            out.append(d)
    return out


def set_problem(folder: str, vid: str, kind: str, message: str) -> None:
    with _db_lock, db() as c:
        c.execute(
            """INSERT INTO problems (folder, video_id, kind, message, ts) VALUES (?,?,?,?,?)
               ON CONFLICT(folder, video_id) DO UPDATE SET
                 kind=excluded.kind, message=excluded.message, ts=excluded.ts""",
            (folder, vid, kind, message[:400], now()))


def clear_problems(folder: str, kind: str) -> None:
    with _db_lock, db() as c:
        c.execute("DELETE FROM problems WHERE folder=? AND kind=?", (folder, kind))


def problems(folder: str) -> dict[str, dict]:
    with _db_lock, db() as c:
        return {r["video_id"]: {"kind": r["kind"], "message": r["message"]}
                for r in c.execute(
                    "SELECT video_id, kind, message FROM problems WHERE folder=?", (folder,))}


# ------------------------------------------------- что уже есть на диске

def folder_path(folder: str) -> Path:
    return DOWNLOAD_ROOT / folder


def archive_path(folder: str) -> Path:
    return folder_path(folder) / "archive.txt"


def archive_ids(folder: str) -> set[str]:
    p = archive_path(folder)
    if not p.exists():
        return set()
    out = set()
    for line in p.read_text(errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            out.add(parts[-1])
    return out


def file_ids(folder: str) -> tuple[set[str], int, int]:
    d = folder_path(folder)
    ids, total, without = set(), 0, 0
    if not d.is_dir():
        return ids, 0, 0
    for f in d.rglob("*"):
        if not f.is_file() or f.suffix.lower() not in VIDEO_EXT:
            continue
        total += 1
        m = ID_IN_NAME.search(f.name)
        if m:
            ids.add(m.group(1))
        else:
            without += 1
    return ids, total, without


def files_without_id(folder: str) -> list[str]:
    d = folder_path(folder)
    if not d.is_dir():
        return []
    return [str(f.relative_to(d)) for f in sorted(d.rglob("*"))
            if f.is_file() and f.suffix.lower() in VIDEO_EXT and not ID_IN_NAME.search(f.name)]


def downloaded_ids(folder: str) -> set[str]:
    return archive_ids(folder) | file_ids(folder)[0]


def ensure_folder(folder: str) -> dict:
    """Создаёт папку плейлиста и пустой archive.txt, если их ещё нет.

    Существующий archive.txt не трогаем ни при каких условиях.
    """
    d = folder_path(folder)
    made_dir = not d.exists()
    d.mkdir(parents=True, exist_ok=True)
    ap = archive_path(folder)
    made_arch = not ap.exists()
    if made_arch:
        ap.touch()
    return {"folder_created": made_dir, "archive_created": made_arch,
            "archive_entries": len(archive_ids(folder))}


# ------------------------------------------------------------- yt-dlp

def _pretty(cmd: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in cmd)


def _ytdlp_json(args: list[str], folder: str, timeout: int = 600) -> tuple[int, str]:
    cmd = ["yt-dlp", *args]
    log(folder, "$ " + _pretty(cmd), "cmd")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        logk(folder, "log_timeout", "error", n=timeout)
        return 124, ""
    for line in (p.stderr or "").splitlines():
        if line.strip():
            log(folder, line, "error" if "ERROR" in line else "warn")
    return p.returncode, p.stdout or ""


def _ytdlp_stream(args: list[str], folder: str, on_status=None,
                  timeout: int = 6 * 3600) -> tuple[int, str]:
    """Запуск с живым выводом: строки уходят в журнал по мере появления,
    прогресс отдаётся наверх через on_status, а не засоряет журнал.

    Возвращает (код возврата, причина остановки): "" | "stop" | "pause" | "timeout".
    """
    global _proc
    cmd = ["yt-dlp", *args]
    log(folder, "$ " + _pretty(cmd), "cmd")
    # Своя группа процессов, чтобы гасить yt-dlp вместе с ffmpeg и deno
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, bufsize=1, errors="replace",
                         start_new_session=True)
    with _proc_lock:
        _proc = p
        _ctl["stop"] = _ctl["pause"] = False

    deadline = datetime.now(timezone.utc).timestamp() + timeout
    done, reason = 0, ""
    try:
        for raw in p.stdout:                                  # type: ignore[union-attr]
            line = raw.rstrip()
            if not line:
                continue
            if datetime.now(timezone.utc).timestamp() > deadline:
                reason = "timeout"
                _kill_group(p, signal.SIGKILL)
                logk(folder, "log_timeout", "error", n=timeout)
                break
            with _proc_lock:
                if _ctl["stop"]:
                    reason = "stop"
                    break

            m = PCT_LINE.search(line)
            if m:                                             # прогресс — только в статус
                if on_status:
                    on_status({"percent": float(m.group(1))})
                continue

            m = DEST_LINE.search(line)
            if m:
                # Начался следующий файл. Если просили паузу — предыдущий уже
                # дописан, самое время остановиться.
                with _proc_lock:
                    paused = _ctl["pause"] and done > 0
                if paused:
                    reason = "pause"
                    _kill_group(p, signal.SIGTERM)
                    break
                name = Path(m.group(1)).name
                done += 1
                if on_status:
                    on_status({"item": name, "done": done, "percent": 0.0})
                logk(folder, "log_downloading", name=name)
                continue

            m = ERR_LINE.search(line)
            if m:
                vid, msg = m.group(1), m.group(2).strip()
                low = msg.lower()
                kind = "dead" if ("unavailable" in low or "private" in low
                                  or "removed" in low or "terminated" in low) else "error"
                set_problem(folder, vid, kind, msg)
                log(folder, f"{vid}: {msg}", "error")
                continue

            if "has already been recorded in the archive" in line:
                continue                                      # шум, и так знаем
            if any(t in line for t in ("[Merger]", "[ExtractAudio]", "[Fixup",
                                       "Deleting original file",
                                       "[download] Downloading item")):
                continue
            log(folder, line, "error" if "ERROR" in line else "info")
        # Терминация закрывает поток вывода, и цикл заканчивается сам — проверка
        # флага внутри цикла до этого просто не успевает выполниться. Поэтому
        # причину доопределяем здесь, после выхода.
        if not reason:
            with _proc_lock:
                if _ctl["stop"]:
                    reason = "stop"
                elif _ctl["pause"]:
                    reason = "pause"
        try:
            p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            _kill_group(p, signal.SIGKILL)
    finally:
        if p.poll() is None:
            _kill_group(p, signal.SIGKILL)
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        with _proc_lock:
            _proc = None
            _ctl["stop"] = _ctl["pause"] = False
    if reason == "stop":
        logk(folder, "log_stopped", "warn")
    elif reason == "pause":
        logk(folder, "log_paused", "warn")
    return (p.returncode or 0), reason


def index_playlist(pl: dict) -> tuple[bool, list[dict]]:
    """Быстрая индексация без скачивания: что сейчас лежит в плейлисте."""
    cfg = load_config()
    args = ["--flat-playlist", "--dump-single-json", "--no-warnings", "--ignore-errors"]
    if cfg.get("cookies_file"):
        args += ["--cookies", cfg["cookies_file"]]
    args.append(pl["url"])
    rc, out = _ytdlp_json(args, pl["folder"])
    if not out.strip():
        return False, []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        logk(pl["folder"], "log_badjson", "error")
        return False, []

    folder = pl["folder"]
    # Чистим только свои метки. Метки, полученные при попытке скачивания
    # ("dead"/"error"), трогать нельзя: в списке плейлиста такие видео
    # выглядят совершенно здоровыми, и стерев их, мы вернули бы недоступное
    # обратно в очередь на скачивание.
    clear_problems(folder, "ghost")
    entries, dead = [], 0
    for i, e in enumerate(data.get("entries") or [], start=1):
        vid = e.get("id")
        if not vid:
            continue
        title = e.get("title") or ""
        entries.append({"id": vid, "title": title, "position": i})
        avail = (e.get("availability") or "").lower()
        if title.strip().lower() in DEAD_TITLES or avail in ("private", "unavailable"):
            # YouTube оставляет удалённые видео в плейлисте как записи-призраки
            set_problem(folder, vid, "ghost", title or avail or "недоступно")
            dead += 1
    if dead:
        logk(folder, "log_dead", "warn", n=dead)
    return True, entries


def store_index(folder: str, entries: list[dict]) -> None:
    ts = now()
    with _db_lock, db() as c:
        for e in entries:
            c.execute(
                """INSERT INTO items (folder, video_id, title, position, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(folder, video_id) DO UPDATE SET
                     title=excluded.title, position=excluded.position, last_seen=excluded.last_seen""",
                (folder, e["id"], e["title"], e["position"], ts, ts))


def download_playlist(pl: dict, on_status=None) -> tuple[bool, int, str]:
    """Скачивание. archive.txt делает дедупликацию — уже скачанное пропускается.

    Возвращает (успех, сколько скачано, причина остановки).
    """
    cfg = load_config()
    folder = pl["folder"]
    dest = folder_path(folder)
    dest.mkdir(parents=True, exist_ok=True)

    free_ok, why = space_ok()
    if not free_ok:
        logk(folder, "log_nospace", "error", free=why["free"], need=why["need"])
        return False, 0, "nospace"

    before = downloaded_ids(folder)
    # Скачивание заново пробует всё нескачанное, поэтому сбрасываем обе свои
    # метки: и отказы формата, и недоступность. Метки индексации ("ghost")
    # остаются — их обновляет сама индексация.
    clear_problems(folder, "error")
    clear_problems(folder, "dead")
    args = [
        "--download-archive", str(archive_path(folder)),
        "--paths", str(dest),
        "-o", pl.get("output_template") or cfg["output_template"],
        "--no-overwrites", "--continue", "--newline",
        "--ignore-errors", "--no-warnings",
        # mkv принимает практически любые кодеки, поэтому ffmpeg просто
        # перекладывает потоки. Другой контейнер может заставить его
        # перекодировать — а это процессор, которого на сервере мало.
        "--merge-output-format", "mkv",
    ]
    if pl.get("write_thumbnail", cfg["write_thumbnail"]):
        args.append("--write-thumbnail")
    fmt = pl.get("format") or cfg.get("format")
    if fmt:
        args += ["-f", fmt]
    if cfg.get("limit_rate"):
        args += ["--limit-rate", str(cfg["limit_rate"])]
    if cfg.get("cookies_file"):
        args += ["--cookies", cfg["cookies_file"]]
    args += list(cfg.get("extra_args") or [])
    args.append(pl["url"])

    rc, reason = _ytdlp_stream(args, folder, on_status)
    added = len(downloaded_ids(folder) - before)
    # rc != 0 при --ignore-errors значит "часть видео не удалась", а не полный провал.
    # Ручная остановка провалом тоже не считается.
    ok = reason in ("stop", "pause") or rc in (0, 1)
    return ok, added, reason


def pending_count(pl: dict) -> int:
    """Сколько новых видео реально предстоит скачать — считаем до старта,
    чтобы показывать «файл 2 из 5», а не внутренний счётчик yt-dlp по всему плейлисту."""
    return playlist_stats(pl)["pending"]


# ------------------------------------------------------------- сводка

def playlist_stats(pl: dict) -> dict:
    folder = pl["folder"]
    have = downloaded_ids(folder)
    arch = archive_ids(folder)
    _, total_files, without = file_ids(folder)
    probs = problems(folder)

    with _db_lock, db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT video_id, title, position, first_seen, last_seen FROM items WHERE folder=?",
            (folder,))]
        last_run = c.execute(
            "SELECT started, finished, kind, ok, added, message FROM runs "
            "WHERE folder=? ORDER BY id DESC LIMIT 1", (folder,)).fetchone()

    latest = max((r["last_seen"] for r in rows), default=None)
    current = [r for r in rows if latest and r["last_seen"] == latest]
    gone = [r for r in rows if latest and r["last_seen"] != latest]

    def annotate(r):
        p = probs.get(r["video_id"])
        return {**r, "problem": p["message"] if p else None}

    # Недоступно = либо помечено как мёртвое/ошибочное, либо вовсе выпало из плейлиста
    broken = [r for r in current if r["video_id"] in probs]
    ok_now = [r for r in current if r["video_id"] not in probs]

    pending = [annotate(r) for r in ok_now if r["video_id"] not in have]
    downloaded = [r for r in current if r["video_id"] in have]
    # Спасено: посмотреть на YouTube уже нельзя, а у нас лежит
    rescued = [annotate(r) for r in broken + gone if r["video_id"] in have]
    # Упущено: пропало и не скачано
    lost = [annotate(r) for r in broken + gone if r["video_id"] not in have]

    avg = folder_avg_size(folder)
    return {
        "folder": folder,
        "name": pl.get("name") or folder,
        "url": pl["url"],
        "enabled": pl.get("enabled", True),
        "indexed": bool(latest),
        "indexed_at": latest,
        "in_playlist": len(current),
        "downloaded": len(downloaded),
        "pending": len(pending),
        "pending_items": pending[:300],
        # Прикидка по среднему размеру уже скачанного в этой же папке.
        # Сеть не нужна, считается мгновенно; для пустой папки честно отдаём null.
        "avg_size_mb": round(avg / 1e6) if avg else None,
        "pending_gb": round(avg * len(pending) / 1e9, 1) if avg else None,
        "lost_ids": [r["video_id"] for r in lost],
        "rescued": len(rescued),
        "rescued_items": rescued[:300],
        "lost": len(lost),
        "lost_items": lost[:300],
        "archive_entries": len(arch),
        "files_total": total_files,
        "files_without_id": without,
        "last_run": dict(last_run) if last_run else None,
    }


def disk_usage() -> dict:
    try:
        st = os.statvfs(DOWNLOAD_ROOT)
    except OSError:
        return {}
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    return {"total_gb": round(total / 1e9, 1), "free_gb": round(free / 1e9, 1),
            "used_pct": round((total - free) / total * 100, 1) if total else 0}


def existing_folders() -> list[str]:
    if not DOWNLOAD_ROOT.is_dir():
        return []
    return sorted(d.name for d in DOWNLOAD_ROOT.iterdir()
                  if d.is_dir() and not d.name.startswith("."))


# ------------------------------------------------------------------ куки

def cookies_status() -> dict:
    """Метаданные файла кук. Содержимое наружу не отдаём никогда."""
    p = COOKIES_PATH
    if not p.exists() or p.stat().st_size == 0:
        return {"present": False}
    st = p.stat()
    domains, count = set(), 0
    for line in p.read_text(errors="replace").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            count += 1
            domains.add(parts[0].lstrip("."))
    return {"present": True, "size": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds"),
            "count": count, "domains": sorted(domains)[:10],
            "has_youtube": any("youtube" in d or "google" in d for d in domains)}


def save_cookies(text: str) -> dict:
    """Принимает Netscape cookies.txt. Всё, что не относится к YouTube/Google,
    отбрасывается: браузерный дамп целиком — это сессии ко всем сайтам подряд,
    и держать их на сервере незачем."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    kept, dropped_domains, total = [], set(), 0
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        total += 1
        dom = parts[0].lstrip(".").lower()
        if any(dom == d or dom.endswith("." + d) for d in COOKIE_DOMAINS):
            kept.append(line)
        else:
            dropped_domains.add(dom)
    if not total:
        raise ValueError(
            "не похоже на Netscape cookies.txt — нет ни одной строки из 7 полей через "
            "табуляцию. Если копировал через буфер, табы могли стать пробелами: грузи файлом.")
    if not kept:
        raise ValueError(
            f"в файле {total} кук, но ни одной для YouTube или Google. "
            "Похоже, экспортирован не тот сайт.")

    COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = COOKIES_PATH.with_suffix(".tmp")
    tmp.write_text("# Netscape HTTP Cookie File\n" + "\n".join(kept) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(COOKIES_PATH)
    cfg = load_config()
    cfg["cookies_file"] = str(COOKIES_PATH)
    save_config(cfg)

    st = cookies_status()
    st["dropped"] = total - len(kept)
    st["dropped_domains"] = sorted(dropped_domains)[:15]
    logk("", "log_cookies_saved", kept=len(kept), total=total, dropped=st["dropped"])
    return st


def clear_cookies() -> None:
    if COOKIES_PATH.exists():
        COOKIES_PATH.unlink()
    cfg = load_config()
    cfg["cookies_file"] = ""
    save_config(cfg)
    logk("", "log_cookies_cleared")


# ------------------------------------------ место, тихие часы, повтор

def folder_avg_size(folder: str) -> float:
    """Средний размер уже скачанного видео в этой папке, байт.

    Нужен для прикидки «98 в очереди ≈ 48 ГБ». Сеть не трогаем: спрашивать
    у YouTube размер каждого ролика долго и незачем.
    """
    d = folder_path(folder)
    if not d.is_dir():
        return 0.0
    sizes = [f.stat().st_size for f in d.rglob("*")
             if f.is_file() and f.suffix.lower() in VIDEO_EXT]
    return (sum(sizes) / len(sizes)) if sizes else 0.0


def space_ok() -> tuple[bool, str]:
    cfg = load_config()
    need = float(cfg.get("min_free_gb") or 0)
    if need <= 0:
        return True, None
    free = disk_usage().get("free_gb")
    if free is None or free >= need:
        return True, None
    return False, {"key": "log_nospace", "free": free, "need": need}


def in_quiet_hours() -> bool:
    """Тихие часы: скачивать нельзя. Индексация при этом разрешена."""
    return datetime.now().hour in (load_config().get("quiet_hours") or [])


def clear_problem(folder: str, vid: str) -> None:
    with _db_lock, db() as c:
        c.execute("DELETE FROM problems WHERE folder=? AND video_id=?", (folder, vid))


def retry_lost(pl: dict, video_ids: list[str], on_status=None) -> tuple[bool, int, str]:
    """Перекачать только упущенные видео, не трогая остальной плейлист.

    Мы уже убеждались, что «недоступно» бывает временным — четыре ролика ожили
    после установки JS-движка, — поэтому перепроверка имеет смысл.
    """
    if not video_ids:
        return True, 0, ""
    ok, why = space_ok()
    if not ok:
        logk(pl["folder"], "log_nospace", "error", free=why["free"], need=why["need"])
        return False, 0, "nospace"

    cfg = load_config()
    folder = pl["folder"]
    dest = folder_path(folder)
    dest.mkdir(parents=True, exist_ok=True)
    before = downloaded_ids(folder)
    for vid in video_ids:
        clear_problem(folder, vid)

    args = [
        "--download-archive", str(archive_path(folder)),
        "--paths", str(dest),
        # playlist_index у одиночного видео не существует — нужен свой шаблон
        "-o", "%(title)s [%(id)s].%(ext)s",
        "--no-overwrites", "--continue", "--newline",
        "--ignore-errors", "--no-warnings",
        "--merge-output-format", "mkv",
    ]
    if pl.get("write_thumbnail", cfg["write_thumbnail"]):
        args.append("--write-thumbnail")
    fmt = pl.get("format") or cfg.get("format")
    if fmt:
        args += ["-f", fmt]
    if cfg.get("limit_rate"):
        args += ["--limit-rate", str(cfg["limit_rate"])]
    if cfg.get("cookies_file"):
        args += ["--cookies", cfg["cookies_file"]]
    args += [f"https://www.youtube.com/watch?v={v}" for v in video_ids]

    rc, reason = _ytdlp_stream(args, folder, on_status)
    added = len(downloaded_ids(folder) - before)
    return (reason in ("stop", "pause") or rc in (0, 1)), added, reason
