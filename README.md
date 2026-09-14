# LinkSwift-sync

跨网盘高速传输网关 —— 参考 [LinkSwift](https://github.com/hmjz100/LinkSwift) 的「直链 + 多线程传输」原理，
把「从 A 网盘快速下载到本地缓存，再快速上传到 1~N 个网盘」做成一个可部署的 Docker 镜像，
支持 **amd64 与 arm64**，下载/上传均支持**断点续传**。

> **多语言 README**
> 🌐 **简体中文** · [繁體中文](README_zh-TW.md) · [English](README_en.md)

---

## 快速开始

### 拉取镜像

```bash
docker pull ghcr.io/jiushizhu2024/linkswift-sync:latest
```

> 镜像由 **GitHub Actions 自动构建**，覆盖 `linux/amd64` 与 `linux/arm64` 两种架构（见 `.github/workflows/docker-build.yml`）。

### 配置网盘远程 & 任务

> 推荐：启动后用 **Web 控制台**（`http://<host>:8080`）在 UI 里完成全部配置，无需手写配置文件。

1. 编辑 `config/rclone.conf` —— 定义你的网盘远程（百度/阿里/Google Drive/OneDrive/S3/WebDAV 等，支持所有 rclone 后端）。**在控制台「网盘账号」页可直接粘贴/填写，Cookie 类网盘填入对应驱动字段即可。**
2. 编辑 `config/jobs.json` —— 定义任务：一个源（`dl`）+ 一到多个目标（`ups`）。**在控制台「任务」页用 UI 创建**（选源/目标目录、同步模式、方向、定时）。示例见 `config/jobs.example.json`。

### 启动

```bash
docker compose up -d            # 启动 Web 控制台（常驻, 内建定时调度）
docker compose run --rm linkswift-sync once   # 跑一轮后退出(命令行)
```

然后浏览器打开：**http://<host>:8080**

---

## Web 控制台

内置 Web 控制台（无第三方依赖，Python 标准库实现），参考 [taoSync](https://github.com/dr34m-cn/taosync) 的功能设计：

| 功能 | 说明 |
|------|------|
| **网盘账号** | 在 UI 直接编辑 `rclone.conf`，填写各网盘账号/Cookie；保存后自动识别远程 |
| **目录浏览** | 选择远程后，可视化浏览源/目标目录结构，一键填入路径 |
| **任务管理** | 新建/编辑/删除/启停同步任务，手动触发运行 |
| **同步模式** | 全量（目标=源，删除多余）/ 增量（仅新增更新）/ 仅新增 |
| **同步方向** | 单向；双向（源↔目标互为镜像） |
| **定时** | 手动一次性 / 周期 interval（秒）/ cron（6 段表达式） |
| **过滤** | 文件大小上限、排除规则（rclone exclude 模式） |
| **运行日志** | 实时查看每次运行输出；状态实时刷新（运行中/成功/失败） |
| **断点续传** | rclone `.partial` / aria2 `.aria2`，缓存卷持久化 |

API 前缀 `/api/`：`GET /api/remotes`、`GET /api/list?remote=&path=`、`GET/POST/PUT/DELETE /api/jobs`、`POST /api/jobs/<name>/run`、`GET /api/logs` 等。

---

## 原理（源自 LinkSwift）

LinkSwift 是浏览器脚本：通过各网盘公开 API 解析「直链」，再交给 IDM / Aria2 等多线程下载器获得高速传输。
本项目把同一思路工程化为自动化服务：

```
源网盘(直链/远程) --多线程快速--> 本地高速缓存 --多线程快速--> 目标网盘₁
                                                                └--> 目标网盘₂ …
```

| 环节 | 引擎 | 断点续传 |
|------|------|----------|
| 直链下载 | **aria2**（16 线程分片，`-c`） | `.aria2` 控制文件 |
| remote 下载/上传 | **rclone**（分块 + 并行） | `.partial` / 目录级 checkers |
| 流水线编排 | **Python**（下载→缓存→多目标并行上传） | 缓存卷持久化 |

## 多语言文档

- [繁體中文 README](README_zh-TW.md)
- [English README](README_en.md)

## 致谢

本项目原理与思路受 **[LinkSwift](https://github.com/hmjz100/LinkSwift)**（网盘直链下载助手）启发，
LinkSwift 由 [hmjz100](https://github.com/hmjz100) 基于 [网盘直链下载助手](https://github.com/syhyz1990/baiduyun) 修改维护。
**Web 控制台的功能设计（任务管理 / 同步模式 / 双向 / 定时 / 实时进度）参考了 [TaoSync](https://github.com/dr34m-cn/taosync)** 项目。
在此一并致谢各位作者及其开源贡献。本项目不涉及对任何网盘限速的破解，速度取决于服务商接口策略与本地带宽。

## 安全

- 容器默认以**非 root**（`linkswift`）运行。
- `rclone.conf` 含令牌凭证，请勿提交到公共仓库。