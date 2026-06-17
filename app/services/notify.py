"""Notification senders for email (SMTP or provider API) and Telegram.

Sending config is resolved from the database via :mod:`app.services.settings_store`
and passed in as ``cfg``; a destination (recipient email or Telegram chat id)
comes from the AlertAccount. These power "Send test" now and the alert engine in
a later milestone. When something isn't configured the senders return a clear,
actionable message rather than failing silently.
"""
from __future__ import annotations

import smtplib
from email.mime.text import MIMEText

import httpx

from app.models import AlertAccount, AlertChannel
from app.services import settings_store


# ---- email ----

def _send_email_smtp(cfg: dict, to: str, subject: str, body: str) -> tuple[bool, str]:
    host = cfg.get("smtp_host")
    if not host:
        return False, "SMTP isn't set up yet. Add your mail server on the Alerts page."
    sender = cfg.get("smtp_from") or cfg.get("smtp_user") or "priceorbit@localhost"
    subtype = "html" if cfg.get("email_html") == "1" else "plain"
    msg = MIMEText(body, subtype, "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, sender, to
    try:
        port = int(cfg.get("smtp_port") or 587)
        with smtplib.SMTP(host, port, timeout=20) as server:
            if cfg.get("smtp_use_tls") == "1":
                server.starttls()
            if cfg.get("smtp_user"):
                server.login(cfg["smtp_user"], cfg.get("smtp_password", ""))
            server.sendmail(sender, [to], msg.as_string())
        return True, f"Email sent to {to}."
    except Exception as exc:  # noqa: BLE001
        return False, f"Email failed: {exc}"


def _send_email_api(cfg: dict, to: str, subject: str, body: str) -> tuple[bool, str]:
    provider = (cfg.get("email_api_provider") or "sendgrid").lower()
    key = cfg.get("email_api_key")
    sender = cfg.get("email_api_from") or cfg.get("smtp_from")
    is_html = cfg.get("email_html") == "1"
    if not key:
        return False, "Email API key isn't set. Add it on the Alerts page."
    if not sender:
        return False, "Set a 'from' address for the email API."
    try:
        if provider == "sendgrid":
            r = httpx.post("https://api.sendgrid.com/v3/mail/send", timeout=20,
                headers={"Authorization": f"Bearer {key}"},
                json={"personalizations": [{"to": [{"email": to}]}], "from": {"email": sender},
                      "subject": subject, "content": [{"type": "text/html" if is_html else "text/plain", "value": body}]})
            ok = r.status_code in (200, 201, 202)
        elif provider == "mailgun":
            domain = cfg.get("email_api_domain")
            if not domain:
                return False, "Mailgun needs a sending domain."
            data = {"from": sender, "to": to, "subject": subject, ("html" if is_html else "text"): body}
            r = httpx.post(f"https://api.mailgun.net/v3/{domain}/messages", auth=("api", key), data=data, timeout=20)
            ok = r.status_code == 200
        elif provider == "resend":
            payload = {"from": sender, "to": [to], "subject": subject, ("html" if is_html else "text"): body}
            r = httpx.post("https://api.resend.com/emails", timeout=20,
                           headers={"Authorization": f"Bearer {key}"}, json=payload)
            ok = r.status_code in (200, 201)
        elif provider == "postmark":
            payload = {"From": sender, "To": to, "Subject": subject,
                       ("HtmlBody" if is_html else "TextBody"): body}
            r = httpx.post("https://api.postmarkapp.com/email", timeout=20,
                           headers={"X-Postmark-Server-Token": key, "Accept": "application/json"}, json=payload)
            ok = r.status_code == 200
        else:
            return False, f"Unknown email provider: {provider}"
        return (True, f"Email sent to {to}.") if ok else (False, f"Email API failed: HTTP {r.status_code} {r.text[:160]}")
    except Exception as exc:  # noqa: BLE001
        return False, f"Email API failed: {exc}"


def send_email(cfg: dict, to: str, subject: str, body: str) -> tuple[bool, str]:
    if not to:
        return False, "This account has no recipient email address."
    if (cfg.get("email_method") or "smtp") == "api":
        return _send_email_api(cfg, to, subject, body)
    return _send_email_smtp(cfg, to, subject, body)


# ---- telegram ----

def send_telegram(cfg: dict, chat_id: str, text: str) -> tuple[bool, str]:
    token = cfg.get("telegram_bot_token")
    if not token:
        return False, "Telegram isn't set up yet. Add a bot token on the Alerts page."
    if not chat_id:
        return False, "This account has no Telegram chat id."
    try:
        resp = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat_id, "text": text}, timeout=20)
        if resp.status_code == 200 and resp.json().get("ok"):
            return True, "Telegram message sent."
        return False, f"Telegram failed: HTTP {resp.status_code}."
    except Exception as exc:  # noqa: BLE001
        return False, f"Telegram failed: {exc}"


def fetch_telegram_chats(cfg: dict) -> tuple[bool, str, list[dict]]:
    """Read recent bot updates so the user can pick a chat id without hunting."""
    token = cfg.get("telegram_bot_token")
    if not token:
        return False, "Add a bot token first, then message your bot and try again.", []
    try:
        resp = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20)
        data = resp.json()
        if not data.get("ok"):
            return False, "Telegram rejected the request. Check the bot token.", []
        seen, chats = set(), []
        for upd in data.get("result", []):
            chat = (upd.get("message") or upd.get("channel_post") or {}).get("chat")
            if chat and chat.get("id") not in seen:
                seen.add(chat["id"])
                name = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) or chat.get("username") or "chat"
                chats.append({"id": str(chat["id"]), "name": name, "type": chat.get("type", "")})
        if not chats:
            return False, "No chats found. Send your bot a message first, then retry.", []
        return True, f"Found {len(chats)} chat(s).", chats
    except Exception as exc:  # noqa: BLE001
        return False, f"Couldn't reach Telegram: {exc}", []


# ---- account-level send ----

def send_via_account(cfg: dict, account: AlertAccount, subject: str, body: str) -> tuple[bool, str]:
    if not account.enabled:
        return False, "This account is disabled."
    if account.channel == AlertChannel.EMAIL:
        return send_email(cfg, account.destination or "", subject, body)
    if account.channel == AlertChannel.TELEGRAM:
        return send_telegram(cfg, account.destination or "", f"{subject}\n\n{body}")
    return False, f"Unknown channel: {account.channel}"


def send_with_fallback(cfg: dict, account: AlertAccount, fallback: AlertAccount | None,
                       subject: str, body: str) -> tuple[bool, str, AlertAccount]:
    ok, msg = send_via_account(cfg, account, subject, body)
    if ok or fallback is None:
        return ok, msg, account
    fok, fmsg = send_via_account(cfg, fallback, subject, body)
    return fok, (f"Primary failed ({msg}); fallback: {fmsg}" if fok else f"Both failed: {msg} / {fmsg}"), fallback


def send_test(cfg: dict, account: AlertAccount) -> tuple[bool, str]:
    """Send a rendered sample of the price template so formatting is visible."""
    ctx = settings_store.sample_context()
    subject = "[TEST] " + settings_store.render_template(cfg.get("tpl_price_subject", ""), ctx)
    body = settings_store.render_template(cfg.get("tpl_price_body", ""), ctx)
    return send_via_account(cfg, account, subject, body)
