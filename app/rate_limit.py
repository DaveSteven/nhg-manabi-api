"""进程内滑动窗口限流（线上为单个 uvicorn 进程，足够用；多进程/多实例需换成 Redis）。

用途：
- 登录防暴力破解：按 IP+用户名、按 IP 统计失败次数。
- 防抓取：按用户统计取题接口的调用次数；按 IP 统计所有 /api 请求。
"""

import ipaddress
import os
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "1") != "0"

MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR

# 登录：同一 IP+用户名 15 分钟内失败 5 次、同一 IP 15 分钟内失败 20 次即锁定。
LOGIN_WINDOW = 15 * MINUTE
LOGIN_MAX_FAILURES_PER_ACCOUNT = 5
LOGIN_MAX_FAILURES_PER_IP = 20

# 所有 /api 请求：每个 IP 每分钟 300 次。
IP_REQUESTS_PER_MINUTE = int(os.getenv("RATE_LIMIT_IP_PER_MINUTE", "300"))

# 按用户的配额。听力随机练习会一次拉取整个级别的试卷（N1 约 31 套），练习提交时每题判一次，
# 所以“取题”和“判题”分开计数，额度按正常使用留足余量。
QUOTAS = {
    # 取题：返回题目、选项、解析
    "content": (
        int(os.getenv("RATE_LIMIT_CONTENT_PER_MINUTE", "120")),
        int(os.getenv("RATE_LIMIT_CONTENT_PER_DAY", "2000")),
    ),
    # 判题：返回正确答案
    "check": (
        int(os.getenv("RATE_LIMIT_CHECK_PER_MINUTE", "300")),
        int(os.getenv("RATE_LIMIT_CHECK_PER_DAY", "5000")),
    ),
}


class SlidingWindowCounter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, window: float, now: float) -> deque[float]:
        hits = self._hits[key]
        while hits and hits[0] <= now - window:
            hits.popleft()
        return hits

    def count(self, key: str, window: float) -> int:
        with self._lock:
            hits = self._prune(key, window, time.monotonic())
            if not hits:
                self._hits.pop(key, None)
                return 0
            return len(hits)

    def retry_after(self, key: str, window: float) -> int:
        with self._lock:
            hits = self._hits.get(key)
            if not hits:
                return 0
            return max(1, int(hits[0] + window - time.monotonic()) + 1)

    def add(self, key: str) -> None:
        with self._lock:
            self._hits[key].append(time.monotonic())

    def hit(self, key: str, limit: int, window: float) -> bool:
        """记录一次并返回是否仍在限额内。超限的请求不计数，避免越刷越久。"""
        with self._lock:
            now = time.monotonic()
            hits = self._prune(key, window, now)
            if len(hits) >= limit:
                return False
            hits.append(now)
            return True

    def clear(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


login_failures = SlidingWindowCounter()
ip_requests = SlidingWindowCounter()
content_requests = SlidingWindowCounter()


def reset_all() -> None:
    for counter in (login_failures, ip_requests, content_requests):
        counter.reset()


def _is_internal(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback


def client_ip(request: Request) -> str:
    """只有请求来自内网代理（nginx）时才信任 X-Real-IP / X-Forwarded-For，防止伪造。"""
    host = request.client.host if request.client else ""
    if _is_internal(host):
        forwarded = request.headers.get("x-real-ip") or (
            request.headers.get("x-forwarded-for", "").split(",")[0]
        )
        forwarded = forwarded.strip()
        if forwarded:
            return forwarded
    return host or "unknown"


def too_many(retry_after: int, detail: str = "Too many requests, please try again later") -> HTTPException:
    return HTTPException(
        status_code=429,
        detail=detail,
        headers={"Retry-After": str(max(1, retry_after))},
    )


def check_login_allowed(ip: str, username: str) -> None:
    if not RATE_LIMIT_ENABLED:
        return
    account_key = f"{ip}|{username.lower()}"
    if login_failures.count(account_key, LOGIN_WINDOW) >= LOGIN_MAX_FAILURES_PER_ACCOUNT:
        raise too_many(login_failures.retry_after(account_key, LOGIN_WINDOW), "Too many failed sign-in attempts")
    if login_failures.count(ip, LOGIN_WINDOW) >= LOGIN_MAX_FAILURES_PER_IP:
        raise too_many(login_failures.retry_after(ip, LOGIN_WINDOW), "Too many failed sign-in attempts")


def record_login_failure(ip: str, username: str) -> None:
    login_failures.add(f"{ip}|{username.lower()}")
    login_failures.add(ip)


def record_login_success(ip: str, username: str) -> None:
    login_failures.clear(f"{ip}|{username.lower()}")


def check_ip_quota(ip: str) -> int | None:
    """返回 None 表示放行，否则返回 Retry-After 秒数。"""
    if not RATE_LIMIT_ENABLED:
        return None
    if ip_requests.hit(ip, IP_REQUESTS_PER_MINUTE, MINUTE):
        return None
    return ip_requests.retry_after(ip, MINUTE)


def check_user_quota(user_id: str, kind: str) -> None:
    if not RATE_LIMIT_ENABLED:
        return
    per_minute, per_day = QUOTAS[kind]
    minute_key = f"{kind}|m|{user_id}"
    day_key = f"{kind}|d|{user_id}"
    if content_requests.count(day_key, DAY) >= per_day:
        raise too_many(content_requests.retry_after(day_key, DAY), "Daily limit reached")
    if not content_requests.hit(minute_key, per_minute, MINUTE):
        raise too_many(content_requests.retry_after(minute_key, MINUTE))
    content_requests.add(day_key)
