"""ytsync — планировщик синхронизации плейлистов YouTube + дашборд.

Одна фоновая очередь: параллельных скачиваний нет намеренно, чтобы не грузить
диск и слабый процессор. Планировщик раз в минуту смотрит расписание и тихие часы.
"""

import threading
import time
from datetime import datetime

from flask import (Flask, jsonify, make_response, redirect, render_template,
                   request)

import core
import extras

app = Flask(__name__)

# Очередь — список под Condition, а не queue.Queue: её надо показывать,
# чистить и защищать от повторного добавления того же плейлиста.
_q: list[dict] = []
_qlock = threading.Condition()
_current: dict | None = None


# ------------------------------------------------------------------ вход

OPEN_PATHS = {"/healthz", "/login", "/api/auth/state", "/api/auth/setup",
              "/api/auth/login"}


def _authed() -> bool:
    return extras.verify_token(request.cookies.get(extras.COOKIE_NAME))


@app.before_request
def _guard():
    p = request.path
    if p in OPEN_PATHS or p.startswith("/static/"):
        return None
    if not extras.configured():
        # Пароль ещё не задан — гоним на первичную настройку
        return (redirect("/login") if not p.startswith("/api/")
                else (jsonify({"error": "не задан пароль", "setup": True}), 401))
    if not _authed():
        return (redirect("/login") if not p.startswith("/api/")
                else (jsonify({"error": "нужен вход"}), 401))
    return None


def _with_cookie(payload, status=200):
    r = make_response(jsonify(payload), status)
    r.set_cookie(extras.COOKIE_NAME, extras.issue_token(),
                 max_age=extras.SESSION_DAYS * 86400,
                 httponly=True, samesite="Lax")
    return r


@app.get("/login")
def login_page():
    if extras.configured() and _authed():
        return redirect("/")
    return render_template("login.html", setup=not extras.configured())


@app.get("/api/auth/state")
def auth_state():
    return jsonify({"configured": extras.configured(), "authed": _authed(),
                    "days": extras.SESSION_DAYS})


@app.post("/api/auth/setup")
def auth_setup():
    if extras.configured():
        return jsonify({"error": "пароль уже задан"}), 409
    pw = (request.get_json(force=True) or {}).get("password", "")
    try:
        extras.set_password(pw)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return _with_cookie({"ok": True})


@app.post("/api/auth/login")
def auth_login():
    pw = (request.get_json(force=True) or {}).get("password", "")
    if not extras.check_password(pw):
        core.logk("", "log_badlogin", "warn")
        return jsonify({"error": "неверный пароль"}), 401
    return _with_cookie({"ok": True})


@app.post("/api/auth/logout")
def auth_logout():
    r = make_response(jsonify({"ok": True}))
    r.delete_cookie(extras.COOKIE_NAME)
    return r


@app.post("/api/auth/password")
def auth_change():
    d = request.get_json(force=True) or {}
    if not extras.check_password(d.get("current", "")):
        return jsonify({"error": "текущий пароль неверен"}), 401
    try:
        extras.set_password(d.get("password", ""))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return _with_cookie({"ok": True})


@app.post("/api/auth/revoke")
def auth_revoke():
    extras.revoke_sessions()
    r = make_response(jsonify({"ok": True}))
    r.delete_cookie(extras.COOKIE_NAME)
    return r


# ------------------------------------------------------------- очередь

def _snapshot() -> dict:
    with _qlock:
        return {"current": dict(_current) if _current else None,
                "queue": [dict(j) for j in _q], "queued": len(_q)}


def _set_current(v) -> None:
    global _current
    with _qlock:
        _current = v


def _find(folder: str) -> dict | None:
    for pl in core.load_config()["playlists"]:
        if pl["folder"] == folder:
            return pl
    return None


def _enqueue(kind: str, folder: str, ids: list | None = None) -> tuple[bool, str]:
    with _qlock:
        if _current and _current["folder"] == folder:
            return False, "уже выполняется"
        for j in _q:
            if j["folder"] == folder:
                if kind == "sync" and j["kind"] == "index":
                    j["kind"] = "sync"
                    return True, "уже в очереди, повышено до синка"
                return False, "уже в очереди"
        _q.append({"kind": kind, "folder": folder, "added": core.now(), "ids": ids})
        _qlock.notify()
        return True, "добавлено в очередь"


def _enqueue_all(kind: str) -> int:
    return sum(int(_enqueue(kind, pl["folder"])[0])
               for pl in core.load_config()["playlists"] if pl.get("enabled", True))


def _run_job(job: dict, pl: dict) -> None:
    kind, folder = job["kind"], job["folder"]
    started = core.now()
    ok, added, msg, reason = False, 0, "", ""
    cur = {"folder": folder, "kind": kind, "started": started, "stage": "index",
           "item": None, "percent": 0.0, "done": 0, "expected": 0}

    def status(upd: dict) -> None:
        cur.update(upd)
        _set_current(dict(cur))

    try:
        if kind == "retry":
            ids = job.get("ids") or core.playlist_stats(pl)["lost_ids"]
            status({"stage": "retry", "expected": len(ids)})
            core.logk(folder, "log_retry", n=len(ids))
            ok, added, reason = core.retry_lost(pl, ids, on_status=status)
        else:
            status({})
            ok, entries = core.index_playlist(pl)
            if ok:
                core.store_index(folder, entries)
                core.logk(folder, "log_indexed", n=len(entries))
            else:
                msg = "msg_index_failed"
                core.logk(folder, "log_index_failed", "error")

            if ok and kind == "sync":
                expected = core.pending_count(pl)
                status({"stage": "download", "expected": expected})
                core.logk(folder, "log_pending", n=expected)
                ok, added, reason = core.download_playlist(pl, on_status=status)
                core.logk(folder, "log_downloaded", n=added)

        if reason:
            # Отдаём ключ, а не готовый текст: перевод делает интерфейс
            msg = {"stop": "msg_stopped", "pause": "msg_paused",
                   "nospace": "msg_nospace",
                   "timeout": "msg_timeout"}.get(reason, reason)
        # После явной остановки не пересчитываем: «стоп» должен останавливать.
        if kind in ("sync", "retry") and reason != "stop" and ok:
            status({"stage": "recount", "item": None, "percent": 0.0})
            ok2, entries = core.index_playlist(pl)
            if ok2:
                core.store_index(folder, entries)
    except Exception as e:                                    # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"
        core.log(folder, msg, "error")
        ok = False
    finally:
        with core._db_lock, core.db() as c:
            c.execute("INSERT INTO runs (folder, kind, started, finished, ok, added, message)"
                      " VALUES (?,?,?,?,?,?,?)",
                      (folder, kind, started, core.now(), int(ok), added, msg))
        core.cancel_control()
        _set_current(None)

    if reason == "stop":
        with _qlock:
            dropped, _q[:] = len(_q), []
        if dropped:
            core.logk("", "log_queue_cleared", "warn", n=dropped)


def _worker() -> None:
    while True:
        job = None
        with _qlock:
            while not _q:
                _qlock.wait()
            if core.in_quiet_hours():
                # В тихие часы качать нельзя, но индексировать можно
                for i, j in enumerate(_q):
                    if j["kind"] == "index":
                        job = _q.pop(i)
                        break
                if job is None:
                    _qlock.wait(timeout=60)
                    continue
            else:
                job = _q.pop(0)
        pl = _find(job["folder"])
        if pl:
            _run_job(job, pl)


def _scheduler() -> None:
    fired: set[str] = set()
    quiet_notified = False
    while True:
        try:
            n = datetime.now()
            cfg = core.load_config()
            key = f"{n:%Y-%m-%d}-{n.hour}"
            if n.hour in (cfg.get("schedule_hours") or []) and key not in fired:
                fired.add(key)
                if len(fired) > 48:
                    fired.clear()
                core.logk("", "log_schedule", h=n.hour, n=_enqueue_all("sync"))

            # Наступили тихие часы во время скачивания — доработать файл и встать
            if core.in_quiet_hours():
                cur = _snapshot()["current"]
                if cur and cur.get("stage") in ("download", "retry"):
                    if not quiet_notified:
                        core.logk("", "log_quiet", "warn", h=n.hour)
                        quiet_notified = True
                    core.request_pause()
            else:
                quiet_notified = False
        except Exception as e:                                # noqa: BLE001
            core.logk("", "log_sched_err", "error", err=str(e))
        time.sleep(60)


# ------------------------------------------------------------------ API

@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/state")
def api_state():
    cfg = core.load_config()
    snap = _snapshot()
    qf = {j["folder"] for j in snap["queue"]}
    out = []
    for pl in cfg["playlists"]:
        st = core.playlist_stats(pl)
        st["queued"] = pl["folder"] in qf
        st["running"] = bool(snap["current"] and snap["current"]["folder"] == pl["folder"])
        out.append(st)
    free_ok, free_why = core.space_ok()
    return jsonify({
        "playlists": out,
        "config": {k: cfg[k] for k in
                   ("schedule_hours", "output_template", "write_thumbnail", "format",
                    "cookies_file", "min_free_gb", "limit_rate", "quiet_hours")},
        "cookies": core.cookies_status(),
        "disk": core.disk_usage(),
        "space_ok": free_ok, "space_warning": free_why,
        "quiet_now": core.in_quiet_hours(),
        "runner": snap,
        "known_folders": core.existing_folders(),
    })


@app.post("/api/run/<kind>")
def api_run(kind):
    if kind not in ("sync", "index"):
        return jsonify({"error": "kind: sync|index"}), 400
    folder = request.args.get("folder", "")
    if folder:
        if not _find(folder):
            return jsonify({"error": "плейлист не найден"}), 404
        ok, why = _enqueue(kind, folder)
        return jsonify({"ok": ok, "message": why, **_snapshot()}), (200 if ok else 409)
    return jsonify({"ok": True, "message": f"добавлено: {_enqueue_all(kind)}", **_snapshot()})


@app.post("/api/retry/<folder>")
def api_retry(folder):
    pl = _find(folder)
    if not pl:
        return jsonify({"error": "плейлист не найден"}), 404
    ids = core.playlist_stats(pl)["lost_ids"]
    if not ids:
        return jsonify({"ok": False, "message": "упущенных нет"}), 409
    ok, why = _enqueue("retry", folder, ids)
    return jsonify({"ok": ok, "message": why, "count": len(ids), **_snapshot()}), \
        (200 if ok else 409)


@app.post("/api/control/<action>")
def api_control(action):
    if action == "stop":
        hit = core.request_stop()
        return jsonify({"ok": True,
                        "message": "останавливаю" if hit else "сейчас ничего не выполняется"})
    if action == "pause":
        hit = core.request_pause()
        return jsonify({"ok": True, "message":
                        "встану после текущего файла" if hit else "сейчас ничего не выполняется"})
    return jsonify({"error": "action: stop|pause"}), 400


@app.delete("/api/queue")
def api_queue_clear():
    folder = request.args.get("folder", "")
    with _qlock:
        before = len(_q)
        _q[:] = [j for j in _q if j["folder"] != folder] if folder else []
        removed = before - len(_q)
    if removed:
        core.logk(folder, "log_unqueued", n=removed)
    return jsonify({"ok": True, "removed": removed, **_snapshot()})


@app.get("/api/reconcile/<folder>")
def api_reconcile_get(folder):
    if not _find(folder):
        return jsonify({"error": "плейлист не найден"}), 404
    return jsonify({"items": extras.reconcile_candidates(folder)})


@app.post("/api/reconcile/<folder>")
def api_reconcile_apply(folder):
    if not _find(folder):
        return jsonify({"error": "плейлист не найден"}), 404
    pairs = (request.get_json(force=True) or {}).get("pairs") or []
    return jsonify({"ok": True, "added": extras.reconcile_apply(folder, pairs)})


@app.post("/api/playlists")
def api_add():
    d = request.get_json(force=True)
    url = (d.get("url") or "").strip()
    folder = (d.get("folder") or "").strip().strip("/")
    if not url or not folder:
        return jsonify({"error": "нужны ссылка и папка"}), 400
    if ".." in folder or folder.startswith("."):
        return jsonify({"error": "недопустимое имя папки"}), 400
    cfg = core.load_config()
    if any(p["folder"] == folder for p in cfg["playlists"]):
        return jsonify({"error": "такая папка уже добавлена"}), 409
    cfg["playlists"].append({"name": (d.get("name") or folder).strip(), "url": url,
                             "folder": folder, "enabled": True,
                             "output_template": (d.get("output_template") or "").strip(),
                             "format": (d.get("format") or "").strip()})
    core.save_config(cfg)
    try:
        made = core.ensure_folder(folder)
    except OSError as e:
        return jsonify({"error": f"не удалось создать папку: {e}"}), 500
    if made["archive_created"]:
        core.logk(folder, "log_added_new")
    else:
        core.logk(folder, "log_added_existing", n=made["archive_entries"])
    _enqueue("index", folder)
    return jsonify({"ok": True, **made})


@app.patch("/api/playlists/<folder>")
def api_patch(folder):
    d = request.get_json(force=True)
    cfg = core.load_config()
    for pl in cfg["playlists"]:
        if pl["folder"] == folder:
            for k in ("name", "url", "enabled", "output_template", "format"):
                if k in d:
                    pl[k] = d[k]
            core.save_config(cfg)
            return jsonify({"ok": True})
    return jsonify({"error": "не найдено"}), 404


@app.delete("/api/playlists/<folder>")
def api_delete(folder):
    """Удаляет только запись в конфиге. Файлы и archive.txt не трогаются."""
    cfg = core.load_config()
    cfg["playlists"] = [p for p in cfg["playlists"] if p["folder"] != folder]
    core.save_config(cfg)
    with _qlock:
        _q[:] = [j for j in _q if j["folder"] != folder]
    core.logk(folder, "log_removed")
    return jsonify({"ok": True})


@app.post("/api/config")
def api_config():
    d = request.get_json(force=True)
    cfg = core.load_config()
    for key in ("schedule_hours", "quiet_hours"):
        if key in d:
            try:
                cfg[key] = sorted({int(h) for h in d[key] if 0 <= int(h) <= 23})
            except (TypeError, ValueError):
                return jsonify({"error": f"{key}: часы — числа от 0 до 23"}), 400
    if "min_free_gb" in d:
        try:
            cfg["min_free_gb"] = max(0, float(d["min_free_gb"] or 0))
        except (TypeError, ValueError):
            return jsonify({"error": "порог места — число"}), 400
    for k in ("output_template", "format", "limit_rate"):
        if k in d:
            cfg[k] = str(d[k]).strip()
    if "write_thumbnail" in d:
        cfg["write_thumbnail"] = bool(d["write_thumbnail"])
    core.save_config(cfg)
    return jsonify({"ok": True})


@app.post("/api/cookies")
def api_cookies_set():
    f = request.files.get("file")
    text = (f.read().decode("utf-8", errors="replace") if f is not None
            else (request.get_json(silent=True) or {}).get("text", ""))
    if not text.strip():
        return jsonify({"error": "пустой файл"}), 400
    if len(text) > 4_000_000:
        return jsonify({"error": "слишком большой файл"}), 400
    try:
        return jsonify({"ok": True, "cookies": core.save_cookies(text)})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except OSError as e:
        return jsonify({"error": f"не удалось сохранить: {e}"}), 500


@app.delete("/api/cookies")
def api_cookies_clear():
    core.clear_cookies()
    return jsonify({"ok": True})


@app.get("/api/log")
def api_log():
    return jsonify(core.recent_log(limit=int(request.args.get("limit", 300)),
                                   folder=request.args.get("folder") or None))


@app.get("/api/orphans/<folder>")
def api_orphans(folder):
    return jsonify({"files": core.files_without_id(folder)})


@app.get("/healthz")
def healthz():
    return "ok", 200


core.init_db()
threading.Thread(target=_worker, daemon=True).start()
threading.Thread(target=_scheduler, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8099)
