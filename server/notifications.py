from __future__ import annotations

import json
import os
import re
import smtplib
import socket
import time
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path
from urllib import request as urlrequest

SECRET_RE = re.compile(r"(?i)(password|passwd|token|secret|apikey|api_key|authorization)([=: ]+)(\S+)")
TRUTHY = {"1", "true", "yes", "on"}
FALSY = {"0", "false", "no", "off"}


def redact(text: str) -> str:
    return SECRET_RE.sub(r"\1\2<redacted>", text or "")


def env_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    normalized = value.strip().lower()
    if normalized in TRUTHY:
        return True
    if normalized in FALSY:
        return False
    return default


def split_csv(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def check_key(cadence: dict, check: dict) -> str:
    return f"{cadence.get('id', 'unknown')}:{check.get('id', 'unknown')}"


def iter_checks(ops: dict) -> list[dict]:
    rows = []
    for cadence in ops.get("cadences", []) or []:
        for check in cadence.get("checks", []) or []:
            rows.append(
                {
                    "key": check_key(cadence, check),
                    "cadenceId": cadence.get("id", "unknown"),
                    "cadenceLabel": cadence.get("label", "Unknown cadence"),
                    "id": check.get("id", "unknown"),
                    "label": check.get("label", "Unknown check"),
                    "host": check.get("host", cadence.get("label", "Unknown host")),
                    "status": check.get("status", "unknown"),
                    "message": redact(str(check.get("message", ""))),
                }
            )
    return rows


def unhealthy_checks(ops: dict, statuses: set[str] | None = None) -> list[dict]:
    wanted = statuses or {"warn", "fail"}
    return [row for row in iter_checks(ops) if row["status"] in wanted]


def summary_line(ops: dict) -> str:
    summary = ops.get("summary", {}) or {}
    total = int(summary.get("total") or 0)
    ok = int(summary.get("ok") or 0)
    warn = int(summary.get("warn") or 0)
    fail = int(summary.get("fail") or 0)
    status = str(summary.get("status") or "unknown").upper()
    return f"{status}: {ok} ok, {warn} warn, {fail} fail across {total} checks"


def format_check(row: dict) -> str:
    prefix = f"[{str(row.get('status', 'unknown')).upper()}]"
    host = row.get("host") or "Unknown host"
    label = row.get("label") or row.get("id") or "Unknown check"
    message = row.get("message") or "no detail"
    cadence = row.get("cadenceLabel") or row.get("cadenceId") or "unknown cadence"
    return redact(f"- {prefix} {host} / {label} ({cadence}): {message}")


def limited_lines(rows: list[dict], limit: int = 20) -> list[str]:
    lines = [format_check(row) for row in rows[:limit]]
    if len(rows) > limit:
        lines.append(f"- ... {len(rows) - limit} more")
    return lines


def morning_subject(ops: dict) -> str:
    summary = ops.get("summary", {}) or {}
    fail = int(summary.get("fail") or 0)
    warn = int(summary.get("warn") or 0)
    if fail:
        return f"Cabrera Network morning ops: {fail} critical failure(s)"
    if warn:
        return f"Cabrera Network morning ops: {warn} warning(s)"
    return "Cabrera Network morning ops: all green"


def critical_subject(failures: list[dict]) -> str:
    count = len(failures)
    if count == 1:
        item = failures[0]
        return f"Cabrera Network critical alert: {item.get('label', item.get('id', 'check'))}"
    return f"Cabrera Network critical alert: {count} checks failing"


def render_morning_body(ops: dict, due_configs: list[dict], now: float) -> str:
    due_labels = ", ".join(cfg.get("label", cfg.get("id", "unknown")) for cfg in due_configs) or "unknown"
    checks = iter_checks(ops)
    morning_rows = [row for row in checks if row.get("cadenceId") == "morning"]
    unhealthy = unhealthy_checks(ops)

    lines = [
        "Cabrera Network morning operations digest",
        f"Generated: {datetime.fromtimestamp(now).isoformat()}",
        f"Snapshot: {ops.get('updatedAt', 'unknown')}",
        f"Cadence run: {due_labels}",
        "",
        summary_line(ops),
    ]

    if morning_rows:
        lines.extend(["", "Morning checks:"])
        lines.extend(limited_lines(morning_rows, limit=10))

    if unhealthy:
        lines.extend(["", "Current warn/fail checks:"])
        lines.extend(limited_lines(unhealthy, limit=20))
    else:
        lines.extend(["", "No warn/fail checks are currently active."])

    return redact("\n".join(lines).strip() + "\n")


def render_critical_body(ops: dict, failures: list[dict], now: float) -> str:
    all_failures = unhealthy_checks(ops, {"fail"})
    lines = [
        "Cabrera Network critical red alert",
        f"Generated: {datetime.fromtimestamp(now).isoformat()}",
        f"Snapshot: {ops.get('updatedAt', 'unknown')}",
        "",
        summary_line(ops),
        "",
        "New or repeated critical failures:",
    ]
    lines.extend(limited_lines(failures, limit=20))

    remaining = [row for row in all_failures if row.get("key") not in {item.get("key") for item in failures}]
    if remaining:
        lines.extend(["", "Other active critical failures:"])
        lines.extend(limited_lines(remaining, limit=20))

    return redact("\n".join(lines).strip() + "\n")


class NotificationManager:
    def __init__(self, env: dict[str, str] | None = None) -> None:
        self.env = env if env is not None else os.environ
        state_path = self.env.get("PI4_NOC_NOTIFY_STATE", "").strip()
        if state_path:
            self.state_path = Path(state_path)
        else:
            state_dir = Path(self.env.get("PI4_NOC_STATE_DIR", "/var/lib/pi4-noc"))
            self.state_path = state_dir / "notification-state.json"

    def configured(self) -> bool:
        return self.email_configured() or self.webhook_configured() or self.dry_run()

    def dry_run(self) -> bool:
        return env_bool(self.env.get("PI4_NOC_NOTIFY_DRY_RUN"), default=False)

    def email_configured(self) -> bool:
        return bool(split_csv(self.env.get("PI4_NOC_NOTIFY_EMAIL_TO")) and self.env.get("PI4_NOC_SMTP_HOST"))

    def webhook_configured(self) -> bool:
        return bool(self.env.get("PI4_NOC_NOTIFY_WEBHOOK_URL"))

    def critical_cooldown_seconds(self) -> float:
        try:
            return max(0.0, float(self.env.get("PI4_NOC_NOTIFY_CRITICAL_COOLDOWN_SECONDS", "1800")))
        except ValueError:
            return 1800.0

    def load_state(self) -> dict:
        try:
            if not self.state_path.exists():
                return {}
            raw = json.loads(self.state_path.read_text(encoding="utf-8") or "{}")
            return raw if isinstance(raw, dict) else {}
        except Exception as exc:
            print(f"pi4-noc notification state read failed: {redact(str(exc))}", flush=True)
            return {}

    def save_state(self, state: dict) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(f"{self.state_path.name}.tmp")
            tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            tmp.replace(self.state_path)
            try:
                self.state_path.chmod(0o600)
            except OSError:
                pass
        except Exception as exc:
            print(f"pi4-noc notification state write failed: {redact(str(exc))}", flush=True)

    def dispatch_operation_notifications(self, ops: dict, due_configs: list[dict], *, force: bool = False, now: float | None = None) -> list[dict]:
        if force or not self.configured():
            return []

        now = now if now is not None else time.time()
        sent: list[dict] = []
        state = self.load_state()
        state.setdefault("schemaVersion", 1)
        changed = False

        if any(cfg.get("id") == "morning" for cfg in due_configs):
            sent_morning, morning_changed = self._send_morning_if_needed(ops, due_configs, state, now)
            if sent_morning:
                sent.append(sent_morning)
            changed = changed or morning_changed

        critical_result, critical_changed = self._send_critical_if_needed(ops, state, now)
        if critical_result:
            sent.append(critical_result)
        changed = changed or critical_changed

        if changed:
            state["updatedAt"] = datetime.fromtimestamp(now).isoformat()
            self.save_state(state)
        return sent

    def _send_morning_if_needed(self, ops: dict, due_configs: list[dict], state: dict, now: float) -> tuple[dict | None, bool]:
        morning = state.setdefault("morning", {})
        today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        if morning.get("lastDate") == today:
            return None, False
        subject = morning_subject(ops)
        body = render_morning_body(ops, due_configs, now)
        delivered = self.send(subject, body, severity="morning")
        morning["lastAttemptAt"] = datetime.fromtimestamp(now).isoformat()
        if delivered:
            morning["lastDate"] = today
            return {"type": "morning", "subject": subject}, True
        return None, True

    def _send_critical_if_needed(self, ops: dict, state: dict, now: float) -> tuple[dict | None, bool]:
        failures = unhealthy_checks(ops, {"fail"})
        critical = state.setdefault("critical", {})
        active = critical.get("active", {})
        if not isinstance(active, dict):
            active = {}

        by_key = {row["key"]: row for row in failures}
        cooldown = self.critical_cooldown_seconds()
        alert_rows = []
        next_active = {}
        changed = False

        for key, row in by_key.items():
            previous = active.get(key, {}) if isinstance(active.get(key), dict) else {}
            last_sent = float(previous.get("lastSentAt") or 0)
            if not previous or now - last_sent >= cooldown:
                alert_rows.append(row)
            entry = {
                "id": row.get("id"),
                "cadenceId": row.get("cadenceId"),
                "label": row.get("label"),
                "host": row.get("host"),
                "message": row.get("message"),
                "lastSeenAt": datetime.fromtimestamp(now).isoformat(),
            }
            if previous.get("lastSentAt"):
                entry["lastSentAt"] = previous.get("lastSentAt")
            next_active[key] = entry

        if set(active.keys()) != set(next_active.keys()):
            changed = True

        result = None
        if alert_rows:
            subject = critical_subject(alert_rows)
            body = render_critical_body(ops, alert_rows, now)
            delivered = self.send(subject, body, severity="critical")
            attempt_at = datetime.fromtimestamp(now).isoformat()
            for row in alert_rows:
                key = row["key"]
                next_active[key]["lastAttemptAt"] = attempt_at
                if delivered:
                    next_active[key]["lastSentAt"] = now
            if delivered:
                result = {"type": "critical", "subject": subject, "count": len(alert_rows)}
            changed = True

        if active != next_active:
            changed = True
        critical["active"] = next_active
        critical["lastCheckedAt"] = datetime.fromtimestamp(now).isoformat()
        return result, changed

    def send(self, subject: str, body: str, *, severity: str = "info") -> bool:
        subject = redact(subject)
        body = redact(body)
        if self.dry_run():
            print(f"pi4-noc notification dry-run [{severity}]: {subject}", flush=True)
            return True

        attempted = False
        delivered = False
        if self.email_configured():
            attempted = True
            try:
                self.send_email(subject, body)
                delivered = True
            except Exception as exc:
                print(f"pi4-noc email notification failed: {redact(str(exc))}", flush=True)
        if self.webhook_configured():
            attempted = True
            try:
                self.send_webhook(subject, body, severity=severity)
                delivered = True
            except Exception as exc:
                print(f"pi4-noc webhook notification failed: {redact(str(exc))}", flush=True)
        return delivered if attempted else False

    def send_email(self, subject: str, body: str) -> None:
        host = self.env.get("PI4_NOC_SMTP_HOST", "").strip()
        if not host:
            raise ValueError("PI4_NOC_SMTP_HOST is required")
        recipients = split_csv(self.env.get("PI4_NOC_NOTIFY_EMAIL_TO"))
        if not recipients:
            raise ValueError("PI4_NOC_NOTIFY_EMAIL_TO is required")

        use_ssl = env_bool(self.env.get("PI4_NOC_SMTP_SSL"), default=False)
        use_tls = env_bool(self.env.get("PI4_NOC_SMTP_TLS"), default=not use_ssl)
        port = int(self.env.get("PI4_NOC_SMTP_PORT") or (465 if use_ssl else 587))
        timeout = float(self.env.get("PI4_NOC_SMTP_TIMEOUT_SECONDS", "10"))
        sender = self.env.get("PI4_NOC_NOTIFY_EMAIL_FROM", "").strip() or f"Cabrera Network <pi4-noc@{socket.gethostname()}>"
        username = self.env.get("PI4_NOC_SMTP_USER", "").strip()
        password = self.smtp_password()

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = sender
        message["To"] = ", ".join(recipients)
        message["Date"] = formatdate(localtime=True)
        message.set_content(body)

        smtp_cls = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
        with smtp_cls(host, port, timeout=timeout) as smtp:
            if not use_ssl:
                try:
                    smtp.ehlo()
                except smtplib.SMTPException:
                    pass
            if use_tls and not use_ssl:
                smtp.starttls()
                try:
                    smtp.ehlo()
                except smtplib.SMTPException:
                    pass
            if username and password:
                smtp.login(username, password)
            smtp.send_message(message)

    def smtp_password(self) -> str:
        direct = self.env.get("PI4_NOC_SMTP_PASSWORD", "")
        if direct:
            return direct
        password_file = self.env.get("PI4_NOC_SMTP_PASSWORD_FILE", "").strip()
        if not password_file:
            return ""
        return Path(password_file).read_text(encoding="utf-8").strip()

    def send_webhook(self, subject: str, body: str, *, severity: str) -> None:
        url = self.env.get("PI4_NOC_NOTIFY_WEBHOOK_URL", "").strip()
        if not url:
            raise ValueError("PI4_NOC_NOTIFY_WEBHOOK_URL is required")
        payload = {
            "source": "pi4-noc",
            "severity": severity,
            "subject": subject,
            "body": body,
            "sentAt": datetime.now().isoformat(),
        }
        data = json.dumps(payload).encode("utf-8")
        timeout = float(self.env.get("PI4_NOC_NOTIFY_WEBHOOK_TIMEOUT_SECONDS", "10"))
        req = urlrequest.Request(url, data=data, headers={"Content-Type": "application/json", "User-Agent": "pi4-noc/notifications"}, method="POST")
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            resp.read(1024)
