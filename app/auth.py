#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LinkSwift-sync 管理员认证模块 (无第三方依赖)
============================================
- 凭据存储于 {CONFIG_DIR}/admin.json: 用户名 + PBKDF2-SHA256 加盐哈希
- 默认账号: admin / admin123 (首次启动自动创建, must_change_password=true)
- 首次登录强制改密(可同时修改用户名)
- session token: secrets.token_urlsafe, 内存会话表, 8h 过期

安全说明:
  - 密码不明文落盘, 使用 200k 轮 PBKDF2 + 随机盐
  - 会话 token 不持久化, 容器重启需重新登录
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/data/config"))
AUTH_FILE = Path(os.environ.get("AUTH_FILE", str(CONFIG_DIR / "admin.json")))

DEFAULT_USER = "admin"
DEFAULT_PASS = "admin123"
PBKDF2_ITER = 200_000
SESSION_TTL = 8 * 3600  # 8 小时


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITER
    ).hex()


class Auth:
    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self._ensure_default()

    # ---------------- 存储 ----------------
    def _load(self) -> dict[str, Any]:
        if AUTH_FILE.exists():
            try:
                return json.loads(AUTH_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _save(self, data: dict[str, Any]) -> None:
        AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        AUTH_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        try:
            os.chmod(AUTH_FILE, 0o600)
        except OSError:
            pass

    def _ensure_default(self) -> None:
        """首次启动: 若不存在凭据文件, 创建默认 admin/admin123。"""
        data = self._load()
        if data.get("username"):
            return
        salt = secrets.token_hex(16)
        data = {
            "username": DEFAULT_USER,
            "salt": salt,
            "password_hash": _hash_password(DEFAULT_PASS, salt),
            "must_change_password": True,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "changed_at": None,
        }
        self._save(data)

    # ---------------- 认证 ----------------
    def login(self, username: str, password: str) -> dict[str, Any]:
        """校验用户名密码, 成功返回 token, 否则抛 ValueError。"""
        data = self._load()
        stored_user = data.get("username", "")
        if not hmac.compare_digest(stored_user, username or ""):
            raise ValueError("用户名或密码错误")
        expected = data.get("password_hash", "")
        actual = _hash_password(password or "", data.get("salt", ""))
        if not hmac.compare_digest(actual, expected):
            raise ValueError("用户名或密码错误")

        token = secrets.token_urlsafe(32)
        self.sessions[token] = {
            "username": username,
            "expiry": time.time() + SESSION_TTL,
        }
        return {
            "token": token,
            "username": username,
            "must_change_password": bool(data.get("must_change_password", False)),
        }

    def check_token(self, token: str | None) -> str | None:
        """校验 token, 返回用户名; 无效/过期返回 None。"""
        if not token:
            return None
        sess = self.sessions.get(token)
        if not sess:
            return None
        if sess["expiry"] < time.time():
            self.sessions.pop(token, None)
            return None
        return sess.get("username")

    def logout(self, token: str | None) -> None:
        if token:
            self.sessions.pop(token, None)

    def change_password(
        self, username: str, old_password: str, new_password: str,
        new_username: str | None = None,
    ) -> dict[str, Any]:
        """改密(可同时改用户名)。old 校验通过才允许。"""
        data = self._load()
        stored_user = data.get("username", "")
        if not hmac.compare_digest(stored_user, username or ""):
            raise ValueError("当前用户名不正确")
        expected = data.get("password_hash", "")
        actual = _hash_password(old_password or "", data.get("salt", ""))
        if not hmac.compare_digest(actual, expected):
            raise ValueError("当前密码不正确")

        if new_password != new_password.strip() or len(new_password.strip()) < 6:
            raise ValueError("新密码至少 6 位且不能包含首尾空格")
        if new_password.strip() == DEFAULT_PASS:
            raise ValueError("新密码不能与初始密码相同")

        target_user = (new_username or stored_user).strip()
        if not target_user:
            raise ValueError("用户名不能为空")

        salt = secrets.token_hex(16)
        data.update({
            "username": target_user,
            "salt": salt,
            "password_hash": _hash_password(new_password.strip(), salt),
            "must_change_password": False,
            "changed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        self._save(data)
        # 使所有会话失效, 需重新登录
        self.sessions.clear()
        return {"ok": True, "username": target_user}

    def status(self, token: str | None) -> dict[str, Any]:
        user = self.check_token(token)
        if not user:
            return {"authenticated": False}
        data = self._load()
        return {
            "authenticated": True,
            "username": user,
            "must_change_password": bool(data.get("must_change_password", False)),
        }


AUTH = Auth()
