"""수집 실패 알림 (Slack Webhook / 이메일).

환경변수
  SLACK_WEBHOOK_URL                      : 설정 시 Slack 으로 전송
  ALERT_EMAIL_TO, SMTP_HOST[, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, ALERT_EMAIL_FROM]
                                         : 설정 시 이메일 전송
같은 리그·작업에서 같은 오류가 24시간 안에 이미 알림되었으면 다시 보내지 않는다.
"""
from __future__ import annotations

import logging
import os
import smtplib
from abc import ABC, abstractmethod
from email.message import EmailMessage

import requests
from sqlalchemy import Connection, text

log = logging.getLogger(__name__)


class Notifier(ABC):
    @abstractmethod
    def send(self, subject: str, body: str) -> None: ...


class SlackNotifier(Notifier):
    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url

    def send(self, subject: str, body: str) -> None:
        requests.post(self.webhook_url, json={"text": f"*{subject}*\n{body}"}, timeout=10).raise_for_status()


class EmailNotifier(Notifier):
    def __init__(self, host: str, port: int, to: str, sender: str, user: str | None, password: str | None) -> None:
        self.host, self.port, self.to, self.sender = host, port, to, sender
        self.user, self.password = user, password

    def send(self, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, self.sender, self.to
        msg.set_content(body)
        with smtplib.SMTP(self.host, self.port, timeout=10) as smtp:
            smtp.starttls()
            if self.user:
                smtp.login(self.user, self.password or "")
            smtp.send_message(msg)


class CompositeNotifier(Notifier):
    def __init__(self, notifiers: list[Notifier]) -> None:
        self.notifiers = notifiers

    def send(self, subject: str, body: str) -> None:
        for n in self.notifiers:
            try:
                n.send(subject, body)
            except Exception:  # 알림 실패가 수집 결과 기록을 막으면 안 된다
                log.exception("알림 전송 실패: %s", type(n).__name__)


def notifier_from_env() -> Notifier | None:
    notifiers: list[Notifier] = []
    if url := os.environ.get("SLACK_WEBHOOK_URL"):
        notifiers.append(SlackNotifier(url))
    if (to := os.environ.get("ALERT_EMAIL_TO")) and (host := os.environ.get("SMTP_HOST")):
        notifiers.append(EmailNotifier(host, int(os.environ.get("SMTP_PORT", "587")), to,
                                       os.environ.get("ALERT_EMAIL_FROM", "sports-analytics@localhost"),
                                       os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD")))
    return CompositeNotifier(notifiers) if notifiers else None


def alert_run(conn: Connection, run_id: int, notifier: Notifier | None) -> bool:
    """실패·부분 실패 실행을 알린다. 보냈으면 True."""
    run = conn.execute(text("""
        SELECT r.id, r.status, r.job_type, r.error_message, r.counts, r.warnings, r.league_id, r.started_at,
               l.code AS league_code
        FROM ingest.ingest_run r LEFT JOIN core.league l ON l.id = r.league_id WHERE r.id = :id
    """), {"id": run_id}).one()
    if run.status not in ("failed", "partial") or notifier is None:
        return False
    duplicate = conn.execute(text("""
        SELECT 1 FROM ingest.ingest_run
        WHERE id <> :id AND league_id IS NOT DISTINCT FROM :league AND job_type = :job
          AND status = :status AND error_message IS NOT DISTINCT FROM :err
          AND alerted_at > now() - interval '24 hours'
        LIMIT 1
    """), {"id": run.id, "league": run.league_id, "job": run.job_type, "status": run.status,
           "err": run.error_message}).first()
    if duplicate:
        return False
    subject = f"[수집 {'실패' if run.status == 'failed' else '부분 실패'}] {run.league_code} / {run.job_type}"
    lines = [f"실행 ID: {run.id}", f"시작: {run.started_at:%Y-%m-%d %H:%M:%S %Z}"]
    if run.error_message:
        lines.append(f"오류: {run.error_message}")
    if run.counts:
        lines.append("건수: " + ", ".join(f"{k}={v}" for k, v in run.counts.items() if k != "by_table"))
    if run.warnings:
        lines.append(f"경고 {len(run.warnings)}건 (처음 5건):")
        lines += [f"  - {w}" for w in run.warnings[:5]]
    notifier.send(subject, "\n".join(lines))
    conn.execute(text("UPDATE ingest.ingest_run SET alerted_at = now() WHERE id = :id"), {"id": run.id})
    return True
