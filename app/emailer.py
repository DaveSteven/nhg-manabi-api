"""邮件发送（T-028）：注册验证 / 密码重置，走 Resend HTTP API（纯 HTTPS，无出站 SMTP 依赖）。

- 环境变量 `RESEND_API_KEY`、`MAIL_FROM`；未配置 key 时降级为日志打印，
  本地开发 / 测试 / CI 无需真实发信（AC-08：断网 / 错 key 不产生死账号，可重发兜底）。
- 发送失败抛 `EmailSendError`，由调用方决定处理方式。
"""

import os
import json
import urllib.error
import urllib.request

RESEND_API_URL = "https://api.resend.com/emails"
# 域名发信（jpn.manabi.cc 配 SPF/DKIM 后）前用 Resend 测试发件地址兜底。
DEFAULT_MAIL_FROM = "NHG Manabi <onboarding@resend.dev>"
REQUEST_TIMEOUT_SECONDS = 10


class EmailSendError(Exception):
    """Resend API 调用失败（网络错误 / 非 2xx 响应）。"""


def email_configured() -> bool:
    return bool(os.getenv("RESEND_API_KEY", "").strip())


def send_email(to: str, subject: str, html: str) -> None:
    """发送一封 HTML 邮件。未配置 RESEND_API_KEY 时仅打印日志（降级模式）。"""
    api_key = os.getenv("RESEND_API_KEY", "").strip()
    if not api_key:
        print(f"[emailer] RESEND_API_KEY not set; skip sending to {to!r}: {subject!r}")
        return

    from_addr = os.getenv("MAIL_FROM", "").strip() or DEFAULT_MAIL_FROM
    payload = json.dumps({"from": from_addr, "to": [to], "subject": subject, "html": html}).encode("utf-8")
    request = urllib.request.Request(
        RESEND_API_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # Resend API 在 Cloudflare 后面，默认 Python-urllib UA 会被 WAF 拦（403 code 1010）
            "User-Agent": "nhg-manabi-api/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            if response.status >= 300:
                raise EmailSendError(f"resend responded with HTTP {response.status}")
    except urllib.error.HTTPError as error:
        raise EmailSendError(f"resend responded with HTTP {error.code}: {error.read().decode('utf-8', 'replace')[:500]}") from error
    except urllib.error.URLError as error:
        raise EmailSendError(f"resend request failed: {error.reason}") from error
