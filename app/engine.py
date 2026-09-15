#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LinkSwift-sync 引擎模块 (可复用库)

把「下载到本地缓存 -> 上传到 1~N 个目标网盘」的流水线封装为可被 CLI 或 Web
后端调用的库。基于 LinkSwift 的直链 + 多线程原理:
  - rclone: 直连网盘 API, 分块 + 并行, 单文件勾点续传(.partial)
  - aria2 : 收到直链后多线程分片下载, 断点续传(.aria2)

新增(参考 taoSync 的设计):
  - 同步模式: full(目标与源一致) / incremental(仅新增与更新)
  - 双向同步: Bidirectional -> 对目标做反向 rclone copy(源同步到目标后反向)
  - 过滤: max_size(单文件上限)、excludes(排除规则)
  - 任务状态: running / last_result / error 查询
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

APP_NAME = "linkswift-sync"
CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/data/cache"))
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/data/config"))
JOBS_FILE = Path(os.environ.get("JOBS_FILE", str(CONFIG_DIR / "jobs.json")))
REMOTES_FILE = Path(os.environ.get("REMOTES_FILE", str(CONFIG_DIR / "rclone.conf")))

TRANSFERS = int(os.environ.get("RCLONE_TRANSFERS", "16"))
CHECKERS = int(os.environ.get("RCLONE_CHECKERS", "16"))

log = logging.getLogger(APP_NAME)

# mode 枚举
MODE_FULL = "full"            # 目标镜像源(删除目标多余)
MODE_INCREMENTAL = "incremental"  # 仅新增/更新, 不删除
MODE_ADD_ONLY = "add_only"    # 仅新增(不更新不删除)


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
@dataclass
class Job:
    name: str
    dl: dict[str, Any]
    ups: list[dict[str, Any]]
    # 新字段(可选)
    mode: str = MODE_FULL
    bidirectional: bool = False
    max_size: Optional[int] = None       # bytes
    excludes: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Job":
        return cls(
            name=d["name"],
            dl=d["dl"],
            ups=d.get("ups", []),
            mode=d.get("mode", MODE_FULL),
            bidirectional=bool(d.get("bidirectional", False)),
            max_size=d.get("max_size"),
            excludes=d.get("excludes", []),
        )

    def source(self) -> str:
        remote = self.dl["remote"]
        if not remote.endswith(":"):
            remote += ":"
        path = self.dl.get("path", "").lstrip("/")
        return remote + path

    def cache_local_dir(self) -> Path:
        return CACHE_DIR / self.name

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "dl": self.dl,
            "ups": self.ups,
            "mode": self.mode,
            "bidirectional": self.bidirectional,
            "max_size": self.max_size,
            "excludes": self.excludes,
        }


# --------------------------------------------------------------------------- #
# rclone 封装
# --------------------------------------------------------------------------- #
def _rc() -> list[str]:
    return ["rclone", f"--config={REMOTES_FILE}", "--transfers", str(TRANSFERS), "--checkers", str(CHECKERS)]


def _filters(max_size: Optional[int], excludes: list[str]) -> list[str]:
    out: list[str] = []
    if max_size:
        out += ["--max-size", str(max_size)]
    for e in excludes:
        if e:
            out += ["--exclude", e]
    return out


def run(cmd: list[str], timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    log.debug("执行: %s", " ".join(cmd))
    return subprocess.run(cmd, timeout=timeout, capture_output=True, text=True)


def rclone_strict(cmd: list[str], timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    p = run(cmd, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"rclone 失败 rc={p.returncode}: {p.stderr[-800:]}")
    return p


def list_remotes() -> list[str]:
    p = rclone_strict([*_rc(), "listremotes"])
    return p.stdout.split()


def obscure_password(password: str) -> str:
    """用 rclone obscure 加密密码, rclone.conf 的 pass 字段必须是混淆后的值。"""
    if not password:
        return ""
    p = run(["rclone", "obscure", password])
    if p.returncode != 0:
        raise RuntimeError(f"rclone obscure 失败: {p.stderr[-200:]}")
    return p.stdout.strip()


def add_webdav_remote(name: str, url: str, user: str, password: str) -> None:
    """向 rclone.conf 追加一个 WebDAV 远程。用于接入 OpenList/AList 的 DAV 服务。"""
    obscured = obscure_password(password) if password else ""
    section = f"\n[{name}]\ntype = webdav\nurl = {url}\nvendor = other\nuser = {user}\npass = {obscured}\n"
    conf = Path(REMOTES_FILE)
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text(conf.read_text(encoding="utf-8") + section, encoding="utf-8")


def test_remote(remote: str, path: str = "") -> dict[str, Any]:
    """测试远程连通性(列出其根或指定目录)。返回 ok / error。"""
    target = remote if remote.endswith(":") else remote + ":"
    if path:
        target = target + path.lstrip("/")
    try:
        _ = lsf(remote, path)
        return {"ok": True, "target": target}
    except Exception as e:
        return {"ok": False, "target": target, "error": str(e)}


def remove_remote(name: str) -> None:
    """从 rclone.conf 移除指定远程段(按 [name] 段落)。"""
    conf = Path(REMOTES_FILE)
    if not conf.exists():
        return
    lines = conf.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    skip = False
    for line in lines:
        if line.startswith("[") and line.endswith("]"):
            skip = line[1:-1].strip() == name
            if not skip:
                out.append(line)
            continue
        if skip:
            continue
        out.append(line)
    conf.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")


def check_remotes(remotes: list[str]) -> None:
    known = list_remotes()
    for r in remotes:
        base = r.split(":", 1)[0] + ":"
        if not any(k.strip() == base for k in known):
            raise ValueError(f"remote {r!r} 未在 {REMOTES_FILE} 中定义")


def lsf(remote: str, path: str = "") -> dict[str, Any]:
    """列出远端目录: 返回 {'dirs': [], 'files': []}。用途: Web UI 目录浏览。"""
    target = remote
    if not target.endswith(":"):
        target += ":"
    if path:
        target = target + path.lstrip("/")
    p = rclone_strict([*_rc(), "lsjson", "--dirs-only", target])
    dirs = json.loads(p.stdout or "[]")
    p2 = rclone_strict([*_rc(), "lsjson", target])
    files = json.loads(p2.stdout or "[]")
    return {
        "path": (target.rstrip("/")),
        "dirs": [d["Path"] for d in dirs],
        "files": [
            {"name": f["Path"], "size": f.get("Size", 0), "isDir": f.get("IsDir", False)}
            for f in files if f.get("IsDir") is False
        ],
    }


# --------------------------------------------------------------------------- #
# 下载阶段
# --------------------------------------------------------------------------- #
def download_via_aria2(job: Job) -> None:
    dst = job.cache_local_dir()
    dst.mkdir(parents=True, exist_ok=True)
    url = job.dl["url"]
    out = job.dl.get("out") or os.path.basename(url.split("?")[0]) or "download"
    cmd = [
        "aria2c", "-c",
        "--dir", str(dst), "--out", out,
        "--max-connection-per-server", "16", "--split", "16",
        "--max-tries", "0", "--retry-wait", "3", "--min-split-size", "16M",
        "--file-allocation", "none", "--summary-interval", "10",
        *(["--max-download-limit", str(job.max_size)] if job.max_size else []),
        url,
    ]
    log.info("[%s] aria2 下载直链 -> %s", job.name, dst)
    p = run(cmd)
    if p.returncode != 0:
        raise RuntimeError(f"aria2 下载失败 rc={p.returncode}: {p.stderr[-400:]}")
    log.info("[%s] 直链下载完成", job.name)


def download_via_rclone(job: Job) -> None:
    local = job.cache_local_dir()
    local.mkdir(parents=True, exist_ok=True)
    log.info("[%s] rclone 下载 %s -> %s", job.name, job.source(), local)
    cmd = [*_rc(), "copy", "--partial", "--progress", "--log-level", "INFO",
           *_filters(job.max_size, job.excludes), job.source(), str(local)]
    rclone_strict(cmd)
    log.info("[%s] 下载阶段完成, 已缓存 %s", job.name, local)


def download(job: Job) -> dict[str, Any]:
    cache_dir = job.cache_local_dir()
    if cache_dir.exists():
        log.info("[%s] 复用现有缓存 %s", job.name, cache_dir)
    if job.dl.get("url"):
        download_via_aria2(job)
    else:
        download_via_rclone(job)
    files = sorted(p for p in cache_dir.rglob("*") if p.is_file())
    return {"cache_dir": str(cache_dir), "files": [str(p) for p in files], "count": len(files)}


# --------------------------------------------------------------------------- #
# 上传阶段
# --------------------------------------------------------------------------- #
def _upsert_remote(job: Job, local: Path, remote_path: str) -> None:
    """按 mode 决定 rclone copy(增量) / sync(全量,删除目标多余)。"""
    file_args = _filters(job.max_size, job.excludes)
    if job.mode == MODE_FULL:
        cmd = [*_rc(), "sync", "--partial", "--progress", "--log-level", "INFO",
               *(file_args), str(local) + "/", remote_path]
    elif job.mode == MODE_ADD_ONLY:
        cmd = [*_rc(), "copy", "--ignore-existing", "--partial", "--progress",
               "--log-level", "INFO", *(file_args), str(local) + "/", remote_path]
    else:  # incremental
        cmd = [*_rc(), "copy", "--update", "--partial", "--progress", "--log-level", "INFO",
               *(file_args), str(local) + "/", remote_path]
    rclone_strict(cmd)
    log.info("[%s] 上传完成 -> %s (mode=%s)", job.name, remote_path, job.mode)


def upload(job: Job, cache_meta: dict[str, Any]) -> dict[str, Any]:
    local = Path(cache_meta["cache_dir"])
    results: dict[str, Any] = {}
    for up in job.ups:
        remote = up["remote"]
        if not remote.endswith(":"):
            remote += ":"
        remote_path = remote + up["path"].lstrip("/")
        log.info("[%s] 上传 -> %s", job.name, remote_path)
        try:
            _upsert_remote(job, local, remote_path)
            results[remote_path] = "ok"
        except RuntimeError as e:
            results[remote_path] = f"failed: {e}"
            log.error("[%s] 上传至 %s 失败: %s", job.name, remote_path, e)
    return results


# --------------------------------------------------------------------------- #
# 双向同步(参考 taoSync 场景: 源<->目标)
# --------------------------------------------------------------------------- #
def reverse_job(job: Job) -> Job:
    """构造反向 job: 原本的源变为目标, 把第一个目标作为反向源。
    仅当只有一个目标且双向开启时使用。"""
    if len(job.ups) != 1:
        raise ValueError("双向同步仅支持单一目标")
    up = job.ups[0]
    return Job(
        name=f"{job.name}__rvs",
        dl={"remote": up["remote"], "path": up["path"]},
        ups=[{"remote": job.dl["remote"], "path": job.dl.get("path", "")}],
        mode=job.mode,
        bidirectional=False,  # 防止无限递归
        max_size=job.max_size,
        excludes=job.excludes,
    )


# --------------------------------------------------------------------------- #
# 任务执行 + 状态
# --------------------------------------------------------------------------- #
def run_job(job: Job, states: dict[str, Any] | None = None) -> dict[str, Any]:
    """执行一个任务(下载->上传, 可选双向), 返回结构化结果。
    states: 可选的任务状态表 {'name': {...}}, 用于 Web 实时状态更新。"""
    if states is not None:
        states[job.name] = {"status": "running", "started_at": time_str()}
    log.info("===== 开始任务 %s (mode=%s, bidir=%s) =====", job.name, job.mode, job.bidirectional)
    try:
        cache_meta = download(job)
        results = upload(job, cache_meta)
        all_ok = all(v == "ok" for v in results.values())

        out = {"job": job.name, "ok": all_ok, "upload_results": results, "cache_files": cache_meta["count"]}
        if job.bidirectional and all_ok and len(job.ups) == 1:
            rvs = reverse_job(job)
            out["reverse"] = run_job(rvs)   # 递归标记 bidirectional=False

        if states is not None:
            states[job.name] = {"status": "success" if all_ok else "error",
                                "finished_at": time_str(), "result": out}
        return out
    except Exception as e:
        log.exception("[%s] 任务失败: %s", job.name, e)
        if states is not None:
            states[job.name] = {"status": "error", "finished_at": time_str(), "error": str(e)}
        return {"job": job.name, "ok": False, "error": str(e)}


def load_jobs() -> list[Job]:
    if not JOBS_FILE.exists():
        raise FileNotFoundError(f"找不到任务配置 {JOBS_FILE}")
    raw = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    jobs = [Job.from_dict(j) for j in raw.get("jobs", [])]
    return jobs


def save_jobs(jobs: list[Job]) -> None:
    data = {"jobs": [j.to_dict() for j in jobs]}
    JOBS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def time_str() -> str:
    import time as _t
    return _t.strftime("%Y-%m-%d %H:%M:%S")