# LinkSwift-sync

A cross-cloud-drive high-speed transfer gateway. Inspired by the **direct-link + multi-threaded transfer** principle of [LinkSwift](https://github.com/hmjz100/LinkSwift), it turns "quickly download from drive A to a local cache, then quickly upload to 1..N drives" into a deployable Docker image. Supports **amd64 and arm64**, with **resumable** downloads and uploads.

> **Multilingual README**
> 🌐 [简体中文](README.md) · [繁體中文](README_zh-TW.md) · **English**

---

## Quick Start

### Pull the image

```bash
docker pull ghcr.io/jiushizhu2024/linkswift-sync:latest
```

> The image is **built automatically by GitHub Actions** for both `linux/amd64` and `linux/arm64` (see `.github/workflows/docker-build.yml`).

### Configure cloud remotes & jobs

1. Edit `config/rclone.conf` to define your drive remotes (Baidu / Aliyun / Google Drive / OneDrive / S3 / WebDAV — any rclone backend).
2. Edit `config/jobs.json` to define jobs: one source (`dl`) + one or more targets (`ups`). See `config/jobs.example.json`.

### Run

```bash
docker compose up -d                                   # start Web console (resident, built-in scheduler)
docker compose run --rm linkswift-sync once            # run once then exit (CLI)
```

Then open **http://<host>:8080** in your browser.

---

## Web Console

A built-in Web console (no third-party deps, Python stdlib), with its design inspired by [TaoSync](https://github.com/dr34m-cn/taosync):

| Feature | Description |
|---------|-------------|
| **Drives/Accounts** | Edit `rclone.conf` directly in the UI; paste accounts/Cookies; remotes auto-detected |
| **Directory browser** | Visually browse source/target directories on a remote and fill paths |
| **Job management** | Create/edit/delete/enable/disable sync jobs; manually trigger runs |
| **Sync mode** | Full (target=source, delete extras) / Incremental (add/update only) / Add-only |
| **Direction** | One-way; Two-way (source↔target mirror) |
| **Schedule** | Manual one-shot / interval (sec) / cron (6-field) |
| **Filters** | Max file size, exclude rules (rclone exclude patterns) |
| **Logs** | Real-time per-run output; live status (running/success/failed) |
| **Resume** | rclone `.partial` / aria2 `.aria2`, persistent cache volume |

API prefix `/api/`: `GET /api/remotes`, `GET /api/list?remote=&path=`, `GET/POST/PUT/DELETE /api/jobs`, `POST /api/jobs/<name>/run`, `GET /api/logs`, etc.

---

## The Idea (from LinkSwift)

LinkSwift is a browser userscript that resolves **direct links** from each drive's public API and hands them to multi-threaded downloaders (IDM / Aria2) for high-speed transfer. This project turns that same idea into an automated service:

```
Source drive (direct-link/remote) --multi-thread fast--> local cache --multi-thread fast--> target drive 1
                                                                                            \--> target drive 2 ...
```

| Stage        | Engine                            | Resume mechanism              |
|--------------|-----------------------------------|-------------------------------|
| Direct download | **aria2** (16-thread split, `-c`) | `.aria2` control file       |
| remote download/upload | **rclone** (chunked + parallel) | `.partial` / directory checkers |
| Orchestration | **Python** (download → cache → parallel multi-target upload) | persistent cache volume |

## Multilingual docs

- [简体中文 README](README.md)
- [繁體中文 README](README_zh-TW.md)

## Credits

This project's approach is inspired by **[LinkSwift](https://github.com/hmjz100/LinkSwift)** (网盘直链下载助手), which is maintained by [hmjz100](https://github.com/hmjz100) as a fork of [网盘直链下载助手](https://github.com/syhyz1990/baiduyun). Thanks to the authors and their open-source contributions. This project does not attempt to bypass any cloud-drive throttling; transfer speed depends on the provider's API policy and your local bandwidth.

The **Web console design (job management / sync modes / bidirectional / scheduling / real-time progress) is inspired by [TaoSync](https://github.com/dr34m-cn/taosync)**.

## Security

- The container runs as **non-root** (`linkswift`) by default.
- `rclone.conf` holds credentials — do not commit it to a public repository.