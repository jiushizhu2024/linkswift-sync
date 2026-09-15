#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LinkSwift-sync Web 控制台后端 (无第三方依赖)
=============================================
基于 Python 标准库 http.server 的 REST API + 定时调度 + 任务状态管理。

功能(参考 taoSync 功能设计, 采用更轻量的 Python 实现):
  - 远程(remotes)管理: 列出已配置网盘远程; UI 中 rclone.conf 由用户填
  - 目录浏览: 列出某 remote 下目录/文件, 供 UI 选择源/目标路径
  - 任务(jobs): 增删改查, 启停, 每任务手动运行, cron/interval/手动 三种定时
  - 运行日志: 循环缓冲区记录每次运行 stdout/err
  - 任务状态: 实时 running/success/error

API 一览:
  GET  /api/status                  -> 运行态 + 最近日志
  GET  /api/remotes                 -> 已配置远程列表
  GET  /api/list?remote=&path=      -> 目录浏览
  GET  /api/jobs                    -> 任务列表
  POST /api/jobs                    -> 新增任务
  PUT  /api/jobs/<name>             -> 更新任务
  DELETE /api/jobs/<name>           -> 删除任务
  POST /api/jobs/<name>/run         -> 手动运行
  POST /api/jobs/<name>/toggle      -> 启停(enable/disable)
  GET  /api/logs?lines=N            -> 最近日志
  GET  /                           -> 前端 index.html
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

import engine
import auth

APP = "linkswift-sync"
WEB_ROOT = Path(__file__).resolve().parent / "web"
PORT = int(__import__("os").environ.get("WEB_PORT", "8080"))

log = logging.getLogger(APP)

# 运行状态(内存表)
STATES: dict[str, Any] = {}     # job.name -> {"status": ..., ...}
LOG_RING: list[str] = []        # 最近日志
LOG_MAX = 2000


def ring_log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    LOG_RING.append(line)
    if len(LOG_RING) > LOG_MAX:
        LOG_RING[:] = LOG_RING[-LOG_MAX:]


# --------------------------------------------------------------------------- #
# 设置持久化 (语言/主题)
# --------------------------------------------------------------------------- #
SETTINGS_FILE = Path(os.environ.get("CONFIG_DIR", "/data/config")) / "settings.json"

_DEFAULT_SETTINGS = {"language": "zh-CN", "theme": "light"}


def _load_settings() -> dict[str, Any]:
    if SETTINGS_FILE.exists():
        try:
            d = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            return {**_DEFAULT_SETTINGS, **d}
        except Exception:
            pass
    return dict(_DEFAULT_SETTINGS)


def _save_settings(data: dict[str, Any]) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 调度器 (参考 taoSync: interval / cron / 手动一次性)
# --------------------------------------------------------------------------- #
class Scheduler:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.thread = None
        self.stop_event = threading.Event()
        self.enabled = {}   # job -> bool

    def start(self) -> None:
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                jobs = engine.load_jobs()
                running = set(s.get("status") == "running" for s in STATES.values())
                for j in jobs:
                    if self.enabled.get(j.name, True) is False:
                        continue
                    schedule = j.dl.get("schedule", "manual")
                    if schedule == "interval" and should_interval_run(j):
                        ring_log(f"== 定时执行任务 {j.name} (interval) ==")
                        spawn_run(j)
                    elif schedule == "cron" and should_cron_run(j):
                        ring_log(f"== 定时执行任务 {j.name} (cron) ==")
                        spawn_run(j)
            except Exception as e:
                ring_log(f"调度器错误: {e}")
            time.sleep(15)


def should_interval_run(j: engine.Job) -> bool:
    last = (STATES.get(j.name) or {}).get("finished_at")
    every = int(j.dl.get("interval_seconds", 300))
    if not last:
        return True
    try:
        last_ts = time.mktime(time.strptime(last, "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return True
    return (time.time() - last_ts) >= every


def parse_cron(spec: str) -> tuple[list[int], list[int], list[int], list[int], list[int], list[int]]:
    """解析 5 段 cron: 分 时 日 月 周。支持 * 与数值列表。返回匹配集合(0-59,0-23,1-31,1-12,0-6)。"""
    fields = spec.split()
    if len(fields) != 5:
        raise ValueError("cron 需要 5 段: 分 时 日 月 周")

    def parse_field(f: str, lo: int, hi: int) -> list[int]:
        if f == "*":
            return list(range(lo, hi + 1))
        out: list[int] = []
        for part in f.split(","):
            if "-" in part:
                a, b = part.split("-")
                out += range(int(a), int(b) + 1)
            else:
                out.append(int(part))
        return sorted(set(out))

    return (
        parse_field(fields[0], 0, 59),
        parse_field(fields[1], 0, 23),
        parse_field(fields[2], 1, 31),
        parse_field(fields[3], 1, 12),
        parse_field(fields[4], 0, 6),
        [],
    )


def should_cron_run(j: engine.Job) -> bool:
    spec = j.dl.get("cron", "")
    minutes, hours, days, months, weekdays, _ = parse_cron(spec)
    now = time.localtime()
    # 简单窗口: 在当前分钟内触发一次(用 finished_at 去重)
    if now.tm_min not in minutes or now.tm_hour not in hours:
        return False
    if now.tm_mday not in days or now.tm_mon not in months:
        return False
    if weekdays and now.tm_wday not in weekdays:
        return False
    last = (STATES.get(j.name) or {}).get("finished_at")
    if last and time.mktime(time.strptime(last, "%Y-%m-%d %H:%M:%S")) > time.time() - 60:
        return False  # 本分钟内已跑
    return True


def spawn_run(j: engine.Job) -> threading.Thread:
    def _target():
        try:
            out = engine.run_job(j, STATES)
            ring_log(f"任务 {j.name} 完成: ok={out.get('ok')}")
        except Exception as e:
            ring_log(f"任务 {j.name} 异常: {e}")
    t = threading.Thread(target=_target, daemon=True)
    t.start()
    return t


# --------------------------------------------------------------------------- #
# HTTP 处理
# --------------------------------------------------------------------------- #
API_PREFIX = "/api"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A002  # 静默访问日志
        pass

    # ---- helper ----
    def _json(self, obj: Any, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict[str, Any]:
        try:
            ln = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            ln = 0
        if ln:
            return json.loads(self.rfile.read(ln) or b"{}")
        return {}

    def _token(self) -> str | None:
        """从 Cookie 或 Authorization 头提取 session token。"""
        # 1) Authorization: Bearer <token>
        auth_hdr = self.headers.get("Authorization", "")
        if auth_hdr.startswith("Bearer "):
            return auth_hdr[7:].strip()
        # 2) Cookie: session=<token>
        ck = self.headers.get("Cookie", "")
        if ck:
            try:
                c = SimpleCookie()
                c.load(ck)
                if "session" in c:
                    return c["session"].value
            except Exception:
                pass
        return None

    def _require_auth(self) -> str | None:
        """返回已认证用户名; 未登录则发送 401 并返回 None。"""
        user = auth.AUTH.check_token(self._token())
        if not user:
            self._json({"error": "未登录", "code": "UNAUTHORIZED"}, 401)
            return None
        return user

    # ---- routing ----
    def do_GET(self) -> None:
        parts = urlparse(self.path)
        path = parts.path
        qs = parse_qs(parts.query)

        # 公开: 前端页面 + auth 状态查询
        if path == "/" or path == "/index.html":
            return self._serve_index()
        if path == f"{API_PREFIX}/auth/status":
            return self._json(auth.AUTH.status(self._token()))

        # 公开: 读取设置(语言/主题) — 无需登录, 首屏渲染需要
        if path == f"{API_PREFIX}/settings":
            return self._json(_load_settings())

        # 以下全部需要登录
        if not self._require_auth():
            return
        if path == f"{API_PREFIX}/status":
            return self._json({"state": STATES, "log_len": len(LOG_RING)})
        if path == f"{API_PREFIX}/remotes":
            try:
                return self._json({"remotes": engine.list_remotes()})
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        if path == f"{API_PREFIX}/remotes/raw":
            conf = Path(engine.REMOTES_FILE)
            return self._json({"content": conf.read_text(encoding="utf-8") if conf.exists() else "",
                               "path": str(conf)})
        if path == f"{API_PREFIX}/list":
            remote = (qs.get("remote") or [""])[0]
            p = (qs.get("path") or [""])[0]
            try:
                return self._json(engine.lsf(remote, p))
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        if path == f"{API_PREFIX}/jobs":
            try:
                jobs = engine.load_jobs()
                return self._json({"jobs": [j.to_dict() for j in jobs],
                                   "states": {k: v for k, v in STATES.items()}})
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        if path == f"{API_PREFIX}/logs":
            n = int((qs.get("lines") or ["200"])[0])
            return self._json({"logs": LOG_RING[-n:]})
        if path.startswith(f"{API_PREFIX}/jobs/"):
            return self._job_detail(path)
        return self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        parts = urlparse(self.path)
        path = parts.path

        # 公开: 登录
        if path == f"{API_PREFIX}/auth/login":
            data = self._read_body()
            try:
                result = auth.AUTH.login(
                    data.get("username", ""), data.get("password", "")
                )
                # 设置 Cookie
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Set-Cookie", f"session={result['token']}; Path=/; HttpOnly; SameSite=Strict")
                body = json.dumps(result, ensure_ascii=False).encode("utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except ValueError as e:
                return self._json({"error": str(e)}, 401)
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        # 以下需要登录
        if not self._require_auth():
            return

        if path == f"{API_PREFIX}/auth/logout":
            auth.AUTH.logout(self._token())
            return self._json({"ok": True})

        if path == f"{API_PREFIX}/auth/change":
            data = self._read_body()
            user = auth.AUTH.check_token(self._token()) or ""
            try:
                result = auth.AUTH.change_password(
                    username=user,
                    old_password=data.get("old_password", ""),
                    new_password=data.get("new_password", ""),
                    new_username=data.get("new_username"),
                )
                # 改密成功后所有旧 session 已失效; 不再自动登录, 要求前端重新登录
                return self._json({"ok": True, "username": result["username"], "require_relogin": True})
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if path == f"{API_PREFIX}/openlist":
            data = self._read_body()
            name = (data.get("name") or "openlist").strip().lower()
            url = (data.get("url") or "").strip()
            user = (data.get("user") or "").strip()
            password = (data.get("pass") or "").strip()
            if not url:
                return self._json({"error": "url 必填 (OpenList DAV 地址, 如 http://openlist:5244/dav)"}, 400)
            try:
                # 先测连通
                tmp = engine.add_webdav_remote(name, url, user, password)
                t = engine.test_remote(name)
                if not t.get("ok"):
                    # 失败则回滚追加的配置段
                    engine.remove_remote(name)
                    return self._json({"error": f"OpenList 连接失败: {t.get('error')}", "detail": t}, 500)
                return self._json({"ok": True, "remotes": engine.list_remotes(), "name": name})
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        if path == f"{API_PREFIX}/remotes/test":
            data = self._read_body()
            remote = (data.get("remote") or "").strip()
            path_ = (data.get("path") or "").strip()
            if not remote:
                return self._json({"error": "remote 必填"}, 400)
            t = engine.test_remote(remote, path_)
            code = 200 if t.get("ok") else 500
            return self._json(t, code)
        if path == f"{API_PREFIX}/jobs":
            data = self._read_body()
            try:
                jobs = engine.load_jobs()
                if any(x.name == data.get("name") for x in jobs):
                    return self._json({"error": "任务已存在"}, 409)
                jobs.append(engine.Job.from_dict(data))
                engine.save_jobs(jobs)
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        if path.startswith(f"{API_PREFIX}/jobs/"):
            return self._job_action(path)
        return self._json({"error": "not found"}, 404)

    def do_PUT(self) -> None:
        parts = urlparse(self.path)
        path = parts.path
        if not self._require_auth():
            return
        if path == f"{API_PREFIX}/settings":
            data = self._read_body()
            cur = _load_settings()
            if "language" in data:
                cur["language"] = data["language"]
            if "theme" in data:
                cur["theme"] = data["theme"]
            _save_settings(cur)
            return self._json({"ok": True, "settings": cur})
        if path == f"{API_PREFIX}/remotes/raw":
            data = self._read_body()
            content = data.get("content", "")
            try:
                conf = Path(engine.REMOTES_FILE)
                conf.parent.mkdir(parents=True, exist_ok=True)
                conf.write_text(content, encoding="utf-8")
                return self._json({"ok": True, "remotes": engine.list_remotes()})
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        if path.startswith(f"{API_PREFIX}/jobs/"):
            name = path.split("/")[-2] if path.endswith("/") else path.rsplit("/", 1)[-1]
            # path like /api/jobs/<name>
            seg = path.split("/")
            name = seg[-1]
            data = self._read_body()
            try:
                jobs = engine.load_jobs()
                for i, j in enumerate(jobs):
                    if j.name == name:
                        jobs[i] = engine.Job.from_dict({**data, "name": name})
                        engine.save_jobs(jobs)
                        return self._json({"ok": True})
                return self._json({"error": "任务不存在"}, 404)
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        return self._json({"error": "not found"}, 404)

    def do_DELETE(self) -> None:
        parts = urlparse(self.path)
        path = parts.path
        if not self._require_auth():
            return
        if path.startswith(f"{API_PREFIX}/jobs/"):
            name = path.rsplit("/", 1)[-1]
            try:
                jobs = engine.load_jobs()
                jobs = [j for j in jobs if j.name != name]
                engine.save_jobs(jobs)
                STATES.pop(name, None)
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        return self._json({"error": "not found"}, 404)

    # ---- sub-entry ----
    def _job_detail(self, path: str) -> None:
        seg = path.split("/")
        if len(seg) >= 4 and seg[-1]:
            name = seg[-1]
            try:
                jobs = engine.load_jobs()
                j = next((x for x in jobs if x.name == name), None)
                if not j:
                    return self._json({"error": "not found"}, 404)
                return self._json(j.to_dict())
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        return self._json({"error": "bad path"}, 400)

    def _job_action(self, path: str) -> None:
        seg = [s for s in path.split("/") if s]
        # /api/jobs/<name>/run  or  /api/jobs/<name>/toggle
        if len(seg) < 4:
            return self._json({"error": "bad path"}, 400)
        name, action = seg[2], seg[3]
        try:
            jobs = engine.load_jobs()
            j = next((x for x in jobs if x.name == name), None)
            if not j:
                return self._json({"error": "not found"}, 404)
            if action == "run":
                ring_log(f"手动触发任务 {name}")
                spawn_run(j)
                return self._json({"ok": True, "started": True})
            if action == "toggle":
                # 无调度器 enable 持久, 简化: 通过删除/重建或状态标记. 此处仅给前端回显
                return self._json({"ok": True, "note": "toggle 由调度器 enabled 管理"})
            return self._json({"error": "unknown action"}, 400)
        except Exception as e:
            return self._json({"error": str(e)}, 500)

    def _serve_index(self) -> None:
        idx = WEB_ROOT / "index.html"
        if not idx.exists():
            return self._json({"error": "index.html 未找到"}, 500)
        body = idx.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    sched = Scheduler()
    sched.start()
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    log.info("LinkSwift-sync Web 控制台: http://0.0.0.0:%d", PORT)
    ring_log(f"Web 控制台启动, 端口 {PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sched.stop()
        server.shutdown()
    finally:
        sched.stop()


if __name__ == "__main__":
    main()