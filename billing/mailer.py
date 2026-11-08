"""Transactional email for billing (verification codes).

Reuses the SMTP settings the panel already stores in ``data.json`` under
``settings.mail`` and the panel's own transport, so there is exactly one place
where a mail server is configured.
"""

from __future__ import annotations

import logging
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

logger = logging.getLogger(__name__)


class BillingMailError(RuntimeError):
    pass


def _panel():
    import app as panel
    return panel


def mail_config() -> dict:
    """The panel's mail block, with defaults filled in by the panel itself."""
    try:
        from mail_service import mail_settings

        data = _panel().load_data()
        return mail_settings(data)
    except Exception:  # noqa: BLE001
        logger.exception("billing: reading mail settings failed")
        return {}


def status() -> dict:
    """What the admin UI shows about the mail channel."""
    try:
        from mail_service import validate_mail_settings
        settings = mail_config()
        validate_mail_settings(settings)
    except Exception as exc:  # noqa: BLE001
        message = str(exc) or exc.__class__.__name__
        return {"ready": False, "reason": message, "from_email": "", "host": ""}
    return {
        "ready": True,
        "reason": "",
        "from_email": str(settings.get("from_email") or ""),
        "host": str(settings.get("host") or ""),
    }


def _deliver(to: str, subject: str, text: str, html: str) -> None:
    from mail_service import MailError, SmtpTransport, validate_mail_settings

    settings = mail_config()
    try:
        validate_mail_settings(settings)
    except MailError as exc:
        raise BillingMailError("Почтовый сервер не настроен в панели (Настройки → Почта)") from exc

    from_email = str(settings.get("from_email") or "").strip()
    from_name = str(settings.get("from_name") or "").strip()
    message = EmailMessage()
    message["From"] = formataddr((from_name, from_email)) if from_name else from_email
    message["To"] = to
    reply_to = str(settings.get("reply_to") or "").strip()
    if reply_to:
        message["Reply-To"] = reply_to
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    domain = from_email.split("@")[-1] if "@" in from_email else None
    message["Message-ID"] = make_msgid(domain=domain) if domain else make_msgid()
    message["Auto-Submitted"] = "auto-generated"
    message.set_content(text)
    message.add_alternative(html, subtype="html")

    try:
        with SmtpTransport(settings) as transport:
            transport.send(message)
    except Exception as exc:  # noqa: BLE001 - surface a readable reason to the UI
        logger.exception("billing: sending mail to %s failed: %s", to, exc)
        raise BillingMailError(f"Не удалось отправить письмо: {exc}") from exc


def send_verification_code(to: str, code: str, ttl_minutes: int,
                           brand: str = "TTVPN", purpose: str = "register") -> None:
    if purpose == "password":
        subject = f"Восстановление пароля {brand}: {code}"
        intro = "Код для смены пароля:"
        footer = ("Код действует {ttl} мин. и работает один раз. "
                  "Если вы не запрашивали смену пароля, просто проигнорируйте письмо "
                  "и войдите со старым паролем.")
    else:
        subject = f"Код подтверждения {brand}: {code}"
        intro = "Код подтверждения почты:"
        footer = ("Код действует {ttl} мин. и работает один раз. "
                  "Если вы не регистрировались, просто проигнорируйте письмо.")
    text = (
        f"{intro}\n\n{code}\n\n"
        f"{footer.format(ttl=ttl_minutes)}\n"
    )
    html = f"""<!DOCTYPE html>
<html lang="ru"><body style="margin:0;padding:24px;background:#f5f5f9;
 font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;color:#1a1a2e;">
 <div style="max-width:480px;margin:0 auto;background:#fff;border-radius:12px;padding:28px;">
  <div style="font-size:18px;font-weight:700;margin-bottom:16px;">{brand}</div>
  <div style="font-size:14px;color:#6b6b83;margin-bottom:10px;">{intro}</div>
  <div style="font-size:34px;font-weight:700;letter-spacing:8px;padding:14px 0;">{code}</div>
  <div style="font-size:13px;color:#6b6b83;">{footer.format(ttl=ttl_minutes)}</div>
 </div>
</body></html>"""
    _deliver(to, subject, text, html)


def send_test(to: str, brand: str = "TTVPN") -> None:
    subject = f"Проверка почты {brand}"
    text = (
        "Это тестовое письмо из раздела «Биллинг».\n"
        "Если вы его видите, почтовый сервер настроен верно и коды подтверждения будут доходить.\n"
    )
    html = f"""<!DOCTYPE html>
<html lang="ru"><body style="margin:0;padding:24px;background:#f5f5f9;
 font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;color:#1a1a2e;">
 <div style="max-width:480px;margin:0 auto;background:#fff;border-radius:12px;padding:28px;">
  <div style="font-size:18px;font-weight:700;margin-bottom:12px;">{brand}</div>
  <p style="font-size:14px;margin:0 0 10px;">Это тестовое письмо из раздела «Биллинг».</p>
  <p style="font-size:14px;color:#6b6b83;margin:0;">Почтовый сервер настроен верно —
   коды подтверждения будут доходить.</p>
 </div>
</body></html>"""
    _deliver(to, subject, text, html)
