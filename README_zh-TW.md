# LinkSwift-sync

跨網盤高速傳輸閘道 —— 參考 [LinkSwift](https://github.com/hmjz100/LinkSwift) 的「直鏈 + 多執行緒傳輸」原理，
把「從 A 網盤快速下載到本機快取，再快速上傳到 1~N 個網盤」做成一個可部署的 Docker 映像，
支援 **amd64 與 arm64**，下載/上傳均支援**斷點續傳**。

> **多語言 README**
> 🌐 [简体中文](README.md) · **繁體中文** · [English](README_en.md)

---

## 快速開始

### 拉取映像

```bash
docker pull ghcr.io/jiushizhu2024/linkswift-sync:latest
```

> 映像由 **GitHub Actions 自動建置**，涵蓋 `linux/amd64` 與 `linux/arm64` 兩種架構（見 `.github/workflows/docker-build.yml`）。

### 設定網盤遠端 & 任務

1. 編輯 `config/rclone.conf` —— 定義你的網盤遠端（百度/阿里/Google Drive/OneDrive/S3/WebDAV 等，支援所有 rclone 後端）。
2. 編輯 `config/jobs.json` —— 定義任務：一個來源（`dl`）+ 一到多個目標（`ups`）。範例見 `config/jobs.example.json`。

### 啟動

```bash
docker compose up -d            # 週期同步
docker compose run --rm linkswift-sync once   # 跑一輪後退出
```

---

## 原理（源自 LinkSwift）

LinkSwift 是瀏覽器腳本：透過各網盤公開 API 解析「直鏈」，再交給 IDM / Aria2 等多執行緒下載器獲得高速傳輸。
本專案把同一思路工程化為自動化服務：

```
來源網盤(直鏈/遠端) --多執行緒快速--> 本機高速快取 --多執行緒快速--> 目標網盤₁
                                                                     └--> 目標網盤₂ …
```

| 環節 | 引擎 | 斷點續傳 |
|------|------|----------|
| 直鏈下載 | **aria2**（16 執行緒分片，`-c`） | `.aria2` 控制檔 |
| remote 下載/上傳 | **rclone**（分塊 + 並行） | `.partial` / 目錄級 checkers |
| 流水線編排 | **Python**（下載→快取→多目標並行上傳） | 快取卷持久化 |

## 多語言文件

- [简体中文 README](README.md)
- [English README](README_en.md)

## 致謝

本專案原理與思路受 **[LinkSwift](https://github.com/hmjz100/LinkSwift)**（網盤直鏈下載助手）啟發，
LinkSwift 由 [hmjz100](https://github.com/hmjz100) 基於 [網盤直鏈下載助手](https://github.com/syhyz1990/baiduyun) 修改維護。
在此致謝作者及其開源貢獻。本專案不涉及對任何網盤限速的破解，速度取決於服務商介面策略與本地頻寬。

## 安全

- 容器預設以**非 root**（`linkswift`）執行。
- `rclone.conf` 含權杖憑證，請勿提交到公開儲存庫。