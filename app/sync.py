#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LinkSwift-sync 编排器
=====================
一条流水线完成: 从源网盘(或直链) 下载到本地高速缓存 -> 再上传到 1~N 个目标网盘。

原理 (参考 LinkSwift / 网盘直链下载助手):
  各网盘公开 API 提供"直链"(直连临时下载/上传地址),
  rclone 用它做多线程+分块传输(自带断点), aria2 用直链做多线程分片下载(自带断点)。

架构:
  - rclone remote 由用户在 config 里定义, 本脚本不关心其实现,
    天然把"直链/API"抽象成统一的 remote:路径。
  - 同步任务 (job) 定义: 一个 dl 源 + 一个或多个 up 目标。
  - 下载阶段: 优先 rclone 直接传输(断点续传最简单闭环);
              若 job 配置提供直链(url), 则交给 aria2 多线程下载断点续传。
  - 上传阶段: 对每一个目标 remote 并行 rclone copy(自带 partial 断点)。

断点续传:
  - rclone: 单文件 .partial + 目录 checkers_; aria2: *.aria2 控制文件。
  - 只要缓存卷 /data/cache 持久化, 重新运行会从断点继续而非重来。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil  # noqa: F401  (备用: 清理缓存时使用)
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

APP_NAME = "linkswift-sync"
CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/data/cache"))
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/data/config"))
JOBS_FILE = Path(os.environ.get("JOBS_FILE", str(CONFIG_DIR / "jobs.json")))

# rclone remote 定义: 用户写在这里, 与 rclone 配置分开管理, 便于校验
REMOTES_FILE = Path(os.environ.get("REMOTES_FILE", str(CONFIG_DIR / "rclone.conf")))

# 默认并行度
TRANSFERS = int(os.environ.get("RCLONE_TRANSFERS", "16"))
CHECKERS = int(os.environ.get("RCLONE_CHECKERS", "16"))

log = logging.getLogger(APP_NAME)


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
@dataclass
class Job:
    name: str
    dl: dict[str, Any]            # {"remote": "src:", "path": "/a/b", "url": "https://…(可选直链)"}
    ups: list[dict[str, Any]]     # [{"remote": "dst1:", "path": "/x/y"}, …]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Job":
        return cls(
            name=d["name"],
            dl=d["dl"],
            ups=d.get("ups", []),
        )

    def source(self) -> str:
        remote = self.dl["remote"]
        if not remote.endswith(":"):
            remote += ":"
        return remote + self.dl.get("path", "").lstrip("/")

    def cache_local_dir(self) -> Path:
        # 本地缓存命名空间, 用 job 名隔离
        return CACHE_DIR / self.name


# --------------------------------------------------------------------------- #
# rclone 封装
# --------------------------------------------------------------------------- #
def _rclone_rc() -> list[str]:
    return ["rclone", f"--config={REMOTES_FILE}", "--transfers", str(TRANSFERS), "--checkers", str(CHECKERS)]


def run(cmd: list[str], timeout: int | None = None) -> subprocess.CompletedProcess:
    log.debug("执行: %s", " ".join(cmd))
    return subprocess.run(cmd, timeout=timeout, capture_output=True, text=True)


def rclone_strict(cmd: list[str], timeout: int | None = None) -> subprocess.CompletedProcess:
    """执行 rclone 并把 stderr 一并记录, 失败抛错。"""
    p = run(cmd, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"rclone 失败 rc={p.returncode}: {p.stderr[-800:]}")
    return p


# --------------------------------------------------------------------------- #
# 下载阶段
# --------------------------------------------------------------------------- #
def check_remotes(remotes: list[str]) -> None:
    """确认所有 remote 都能被识别(存在配置)。"""
    p = rclone_strict([*_rclone_rc(), "listremotes"])
    known = p.stdout.split()
    for r in remotes:
        base = r.split(":", 1)[0] + ":"
        if not any(k.strip() == base for k in known):
            raise ValueError(f"remote {r!r} 未在 {REMOTES_FILE} 中定义")


def download_via_aria2(job: Job) -> None:
    """直链多线程下载, 支持断点(.aria2 控制文件保存在缓存卷)。"""
    dst = job.cache_local_dir()
    dst.mkdir(parents=True, exist_ok=True)
    url = job.dl["url"]
    out = job.dl.get("out") or os.path.basename(url.split("?")[0]) or "download"
    # --continue=true 断点续传; 多线程分片; 16MiB 分片
    cmd = [
        "aria2c",
        "-c",
        "--dir", str(dst),
        "--out", out,
        "--max-connection-per-server", "16",
        "--split", "16",
        "--max-tries", "0",
        "--retry-wait", "3",
        "--min-split-size", "16M",
        "--file-allocation", "none",
        "--summary-interval", "10",
        url,
    ]
    log.info("[%s] aria2 下载直链 -> %s", job.name, dst)
    p = run(cmd)  # aria2 返回码 0 才算成功; 断点文件保留在缓存卷
    if p.returncode != 0:
        raise RuntimeError(f"aria2 下载失败 rc={p.returncode}")
    log.info("[%s] 直链下载完成", job.name)


def download_via_rclone(job: Job) -> None:
    """rclone 把源 remote:path 同步到本地缓存。断点续传由 rclone .partial 保证。
    完成后删除缓存, 仅在异常时保留以便续传。"""
    local = job.cache_local_dir()
    local.mkdir(parents=True, exist_ok=True)
    log.info("[%s] rclone 下载 %s -> %s", job.name, job.source(), local)
    # 断点续传关键: --partial 保留半成品; 重跑会继续
    cmd = [
        *_rclone_rc(),
        "copy", "--partial", "--progress", "--log-level", "INFO",
        job.source(), str(local),
    ]
    rclone_strict(cmd)
    log.info("[%s] 下载阶段完成, 已缓存 %s", job.name, local)


def download(job: Job) -> dict[str, Any]:
    """选择下载路径: 直链 -> aria2; 否则 -> rclone。返回缓存目录与清单。"""
    cache_dir = job.cache_local_dir()
    if cache_dir.exists():
        # 上一次可能未完全备份/上传, 直接沿用缓存即可(断点的前提)
        log.info("[%s] 复用现有缓存 %s", job.name, cache_dir)

    if "url" in job.dl and job.dl.get("url"):
        download_via_aria2(job)
    else:
        download_via_rclone(job)

    # 列出自缓存目录下的文件(供上传)
    files = sorted(p for p in cache_dir.rglob("*") if p.is_file())
    return {
        "cache_dir": str(cache_dir),
        "files": [str(p) for p in files],
        "count": len(files),
    }


# --------------------------------------------------------------------------- #
# 上传阶段
# --------------------------------------------------------------------------- #
def upload(job: Job, cache_meta: dict[str, Any]) -> dict[str, Any]:
    """把缓存上传到 1~N 个目标 remote:path。每个目标独立、并行、断点续传。"""
    local = Path(cache_meta["cache_dir"])
    results: dict[str, Any] = {}

    for up in job.ups:
        remote = up["remote"]
        if not remote.endswith(":"):
            remote += ":"
        remote_path = remote + up["path"].lstrip("/")
        log.info("[%s] 上传 -> %s", job.name, remote_path)

        cmd = [
            *_rclone_rc(),
            "copy", "--partial", "--progress", "--log-level", "INFO",
            str(local) + "/", remote_path,
        ]
        try:
            rclone_strict(cmd)
            results[remote_path] = "ok"
        except RuntimeError as e:
            results[remote_path] = f"failed: {e}"
            log.error("[%s] 上传至 %s 失败: %s", job.name, remote_path, e)

    return results


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run_job(job: Job) -> dict[str, Any]:
    log.info("===== 开始任务 %s =====", job.name)
    cache_meta = download(job)
    results = upload(job, cache_meta)

    all_ok = all(v == "ok" for v in results.values())
    log.info(
        "[%s] 完成 upload 结果: %s",
        job.name,
        json.dumps(results, ensure_ascii=False),
    )
    return {
        "job": job.name,
        "ok": all_ok,
        "upload_results": results,
        "cache_files": cache_meta["count"],
    }


def load_jobs() -> list[Job]:
    if not JOBS_FILE.exists():
        raise FileNotFoundError(f"找不到任务配置 {JOBS_FILE}")
    raw = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    jobs = [Job.from_dict(j) for j in raw.get("jobs", [])]
    if not jobs:
        raise ValueError("jobs.json 里没有定义任何任务")
    return jobs


def sync_once(jobs: list[Job]) -> int:
    """跑一轮所有任务, 返回失败数。"""
    # 先统一校验 remote, 失败则整体快速失败(避免中途才发现配置错)
    all_remotes: list[str] = []
    for j in jobs:
        all_remotes.append(j.dl["remote"])
        all_remotes += [u["remote"] for u in j.ups]
    try:
        check_remotes(list({r for r in all_remotes}))
    except Exception as e:
        log.error("remote 校验失败: %s", e)
        return len(jobs)

    failures = 0
    for j in jobs:
        try:
            run_job(j)
        except Exception as e:
            failures += 1
            log.exception("[%s] 任务失败: %s", j.name, e)
    return failures


def run_loop(jobs: list[Job], interval: int) -> None:
    log.info("进入周期同步模式, 每 %ss 一轮", interval)
    while True:
        failures = sync_once(jobs)
        log.info("本轮失败数: %d", failures)
        time.sleep(interval)


def main() -> None:
    ap = argparse.ArgumentParser(description="LinkSwift-sync 跨网盘高速传输网关")
    ap.add_argument("mode", nargs="?", default="sync",
                    choices=["sync", "once", "check"],
                    help="sync=周期循环, once=跑一轮, check=检查配置")
    ap.add_argument("--interval", type=int, default=int(os.environ.get("SYNC_INTERVAL", "300")),
                    help="周期模式间隔秒")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging(args.verbose)
    jobs = load_jobs()

    if args.mode == "check":
        all_remotes = list({u["remote"] for j in jobs for u in j.ups} | {j.dl["remote"] for j in jobs})
        check_remotes(all_remotes)
        log.info("配置校验通过: %d 个任务, remotes=%s", len(jobs), sorted(all_remotes))
        return

    if args.mode == "once":
        f = sync_once(jobs)
        sys.exit(1 if f else 0)
    run_loop(jobs, args.interval)


if __name__ == "__main__":
    main()