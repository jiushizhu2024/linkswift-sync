#!/usr/bin/env bash
set -euo pipefail

# LinkSwift-sync 容器入口
# 负责: 初始化配置占位(若为空) -> 修正目录属主 -> 降权运行 Web 服务
#
# 权限设计(安全第一):
#   容器以 root 启动(便于修正 bind-mount 来自宿主 root 的 config/cache 目录属主),
#   初始化完成后用 setpriv 降权为非 root 的 linkswift 用户运行, 最小权限。
#
# 说明: bind-mount(如 ./config:.../data/config)的目录属主是宿主用户(root),
#       若容器直接以 linkswift 运行将无写权限; 故在此 chown 后降权。

export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

CONFIG_DIR="${CONFIG_DIR:-/data/config}"
CACHE_DIR="${CACHE_DIR:-/data/cache}"
JOBS_FILE="${JOBS_FILE:-$CONFIG_DIR/jobs.json}"
REMOTES_FILE="${REMOTES_FILE:-$CONFIG_DIR/rclone.conf}"

# 修正属主(允许失败, 某些挂载只读时会忽略)
chown -R linkswift:linkswift "${CACHE_DIR}" 2>/dev/null || true
chown -R linkswift:linkswift "${CONFIG_DIR}" 2>/dev/null || true
# 目录本身需可写(尤其非 bind-mount 的首启场景)
chmod -R u+rwx "${CONFIG_DIR}" "${CACHE_DIR}" 2>/dev/null || true

# 若挂载卷为空, 写入示例配置, 便于用户直接编辑
init_placeholder() {
    if [ ! -f "${REMOTES_FILE}" ]; then
        echo "[entrypoint] init example rclone config: ${REMOTES_FILE}"
        printf '%s\n' \
          '# rclone remotes. You can edit here, or use the Web console "Drives" tab.' \
          '' > "${REMOTES_FILE}"
    fi
    if [ ! -f "${JOBS_FILE}" ]; then
        echo "[entrypoint] init example jobs config: ${JOBS_FILE}"
        printf '%s\n' \
          '{' \
          '  "jobs": [' \
          '  ]' \
          '}' > "${JOBS_FILE}"
    fi
}

init_placeholder

# 确保占位文件属主正确
chown linkswift:linkswift "${REMOTES_FILE}" "${JOBS_FILE}" 2>/dev/null || true

CMD_MODE="${1:-web}"
if [ "$CMD_MODE" = "web" ]; then
    # 降低到 linkswift 用户并常驻运行 Web 控制台
    exec setpriv --reuid=linkswift --regid=linkswift --init-groups \
        python3 /data/scripts/server.py
elif [ "$CMD_MODE" = "once" ] || [ "$CMD_MODE" = "sync" ] || [ "$CMD_MODE" = "check" ] || [ "$CMD_MODE" = "debug" ]; then
    # 命令行模式: 同样降权运行(debug 时以 root 运行便于排查权限)
    if [ "$CMD_MODE" = "debug" ]; then
        exec python3 /data/scripts/sync.py "${@:2}"
    else
        exec setpriv --reuid=linkswift --regid=linkswift --init-groups \
            python3 /data/scripts/sync.py "$@"
    fi
else
    echo "[entrypoint] unknown mode '$CMD_MODE'; supported: web|once|check|sync|debug" >&2
    exit 2
fi