from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from typing import Any

import requests

from .utils import ensure_list


def send_report_email(
    config: dict[str, Any],
    subject: str,
    html: str,
    text: str,
) -> str:
    recipients = ensure_list(os.getenv("MAIL_TO")) or config.get("email", {}).get("to", [])
    sender = os.getenv("MAIL_FROM", "").strip()

    if not recipients:
        return "Skipped email: MAIL_TO is not configured."

    resend_key = os.getenv("RESEND_API_KEY", "").strip()
    if resend_key:
        return _send_with_resend(resend_key, sender, recipients, subject, html, text)

    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_pass = os.getenv("SMTP_PASS", "").strip()
    if smtp_host and smtp_user and smtp_pass:
        return _send_with_smtp(
            host=smtp_host,
            port=int(os.getenv("SMTP_PORT", "587")),
            username=smtp_user,
            password=smtp_pass,
            sender=sender or smtp_user,
            recipients=recipients,
            subject=subject,
            html=html,
            text=text,
        )

    return "Skipped email: configure RESEND_API_KEY + MAIL_FROM, or SMTP_HOST + SMTP_USER + SMTP_PASS."


def _send_with_resend(
    api_key: str,
    sender: str,
    recipients: list[str],
    subject: str,
    html: str,
    text: str,
) -> str:
    if not sender:
        return "Skipped email: MAIL_FROM is required for Resend."

    response = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "from": sender,
            "to": recipients,
            "subject": subject,
            "html": html,
            "text": text,
        },
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Resend failed: HTTP {response.status_code} {response.text}")
    return "Email sent with Resend."


def _send_with_smtp(
    host: str,
    port: int,
    username: str,
    password: str,
    sender: str,
    recipients: list[str],
    subject: str,
    html: str,
    text: str,
) -> str:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message.set_content(text)
    message.add_alternative(html, subtype="html")

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
            smtp.login(username, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(username, password)
            smtp.send_message(message)
    return "Email sent with SMTP."

