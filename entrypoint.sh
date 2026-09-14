#!/usr/bin/env bash
set -euo pipefail

# LinkSwift-sync 容器入口
# 负责: 确保依赖存在 -> 初始化配置占位(如果为空) -> 调用编排器
# 权限: 以非 root (linkswift) 运行, 保持最小权限。

export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

CONFIG_DIR="${CONFIG_DIR:-/data/config}"
JOBS_FILE="${JOBS_FILE:-$CONFIG_DIR/jobs.json}"
REMOTES_FILE="${REMOTES_FILE:-$CONFIG_DIR/rclone.conf}"

# 若挂载卷为空, 写入示例配置, 便于用户直接编辑
init_placeholder() {
    if [ ! -f "${REMOTES_FILE}" ]; then
        echo "[entrypoint] 初始化示例 rclone 配置: ${REMOTES_FILE}"
        printf '%s\n' \
          '# 在此定义你的网盘远程(参考 rclone 官方文档配置)' \
          '# 例:' \
          '# [aliyun]' \
          '#   type = alist' \
          '#   url = https://your-alist.example.com' \
          '#   password = <...>' \
          '' > "${REMOTES_FILE}"
    fi
    if [ ! -f "${JOBS_FILE}" ]; then
        echo "[entrypoint] 初始化示例任务配置: ${JOBS_FILE}"
        printf '%s\n' \
          '{' \
          '  "jobs": [' \
          '  ]' \
          '}' > "${JOBS_FILE}"
    fi
}

init_placeholder

CMD_MODE="${1:-web}"
if [ "$CMD_MODE" = "web" ]; then
    exec python3 /data/scripts/server.py
elif [ "$CMD_MODE" = "sync" ] || [ "$CMD_MODE" = "once" ] || [ "$CMD_MODE" = "check" ]; then
    # 兼容旧的 CLI 模式(直接调用编排器)
    exec python3 /data/scripts/sync.py "$@"
else
    echo "[entrypoint] 未知模式 '$CMD_MODE'; 支持 web|once|check|sync" >&2
    exit 2
fi