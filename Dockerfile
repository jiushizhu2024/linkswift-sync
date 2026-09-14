# syntax=docker/dockerfile:1
# ============================================================================
# LinkSwift-sync — 跨网盘高速传输网关  (amd64 / arm64)
#
# 原理 (源自 LinkSwift / 网盘直链下载助手):
#   1) 从各网盘公开 API 解析出「直链」(临时下载/上传地址)
#   2) 用多线程 / 分块把源文件"快"地落到本地高速缓存   (下载/断点)
#   3) 再通过各网盘 API 把缓存里的文件"快"地上传到目标网盘 (上传/断点)
#
# 技术选型:
#   - rclone : 直连网盘官方 API; 分块 + _transfers/_checkers 并行传输;
#              单文件断点续传(.partial)与目录断点续传天然支持
#   - aria2  : 收到直链后用多线程分片下载, 断点续传(-c + .aria2 控制文件)
#   - Python : 编排 下载→缓存→上传 流水线, 支持多目标网盘并行上传
#
# 多架构: 一条 Dockerfile 由 buildx(qemu/原生 binfmt)同时构建 amd64 + arm64。
# 依赖全部来自 Alpine 官方仓库, 无需源码编译, 构建简单可靠。
# ============================================================================
FROM alpine:3.20

# --- 安装运行时, 合并到一个 RUN 以减小层数 ---
# aria2 / rclone 位于 Alpine community 仓库, 其余在 main; 基础镜像默认已启用两者
RUN set -eux; \
    apk add --no-cache \
        aria2 \
        rclone \
        python3 \
        ca-certificates \
        bash \
        curl \
        wget \
        tzdata \
        py3-pip \
        proxychains-ng; \
    \
    # 非 root 运行(安全第一)
    addgroup -S linkswift; \
    adduser -S -G linkswift -h /data linkswift; \
    mkdir -p /data/cache /data/config /data/state /data/scripts; \
    chown -R linkswift:linkswift /data; \
    \
    # 版本信息
    aria2c --version | head -1; \
    rclone version | head -1

# --- 复制编排器、引擎、启动脚本与控制台前端 ---
COPY --chown=linkswift:linkswift app/*.py /data/scripts/
COPY --chown=linkswift:linkswift app/web /data/scripts/web
COPY --chown=linkswift:linkswift entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER linkswift
WORKDIR /data

# Web 控制台: 运行后端(内含常驻调度器)+ 提供前端
EXPOSE 8080

# 缓存卷可持久化: 断点续传依赖缓存里的 .aria2 控制文件与 .part 文件
VOLUME ["/data/cache", "/data/config"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["web"]