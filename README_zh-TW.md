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

> 建議：啟動後用 **Web 控制台**（`http://<host>:8080`）在 UI 裡完成全部設定，無需手寫設定檔。

1. 編輯 `config/rclone.conf` —— 定義你的網盤遠端（百度/阿里/Google Drive/OneDrive/S3/WebDAV 等，支援所有 rclone 後端）。**在控制台「網盤帳號」頁可直接貼上/填寫，Cookie 類網盤填入對應驅動欄位即可。**
2. 編輯 `config/jobs.json` —— 定義任務：一個來源（`dl`）+ 一到多個目標（`ups`）。**在控制台「任務」頁用 UI 建立**（選來源/目標目錄、同步模式、方向、定時）。範例見 `config/jobs.example.json`。

### 啟動

```bash
docker compose up -d            # 啟動 Web 控制台（常駐, 內建排程）
docker compose run --rm linkswift-sync once   # 跑一輪後退出(命令列)
```

然後瀏覽器打開：**http://<host>:8080**

---

## Web 控制台

內建 Web 控制台（無第三方依賴，Python 標準庫實作），參考 [TaoSync](https://github.com/dr34m-cn/taosync) 的功能設計：

| 功能 | 說明 |
|------|------|
| **網盤帳號** | 在 UI 直接編輯 `rclone.conf`，填寫各網盤帳號/Cookie；儲存後自動識別遠端 |
| **目錄瀏覽** | 選擇遠端後，視覺化瀏覽來源/目標目錄結構，一鍵填入路徑 |
| **任務管理** | 新增/編輯/刪除/啟停同步任務，手動觸發執行 |
| **同步模式** | 全量（目標=來源，刪除多餘）/ 增量（僅新增更新）/ 僅新增 |
| **同步方向** | 單向；雙向（來源↔目標互為鏡像） |
| **定時** | 手動一次性 / 週期 interval（秒）/ cron（6 段表達式） |
| **過濾** | 檔案大小上限、排除規則（rclone exclude 模式） |
| **執行日誌** | 即時檢視每次執行輸出；狀態即時更新（執行中/成功/失敗） |
| **斷點續傳** | rclone `.partial` / aria2 `.aria2`，快取卷持久化 |

API 前置 `/api/`：`GET /api/remotes`、`GET /api/list?remote=&path=`、`GET/POST/PUT/DELETE /api/jobs`、`POST /api/jobs/<name>/run`、`GET /api/logs` 等。

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