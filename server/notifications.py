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
from urllib import parse as urlparse
from urllib import request as urlrequest

SECRET_RE = re.compile(r"(?i)(password|passwd|token|secret|apikey|api_key|authorization)([=: ]+)(\S+)")
TRUTHY = {"1", "true", "yes", "on"}
FALSY = {"0", "false", "no", "off"}

STATUS_ICONS = {"ok": "🟢", "warn": "🟡", "fail": "🔴"}
NBSP = "\u00a0"  # non-breaking space
DIVIDER = "─" * 26


def status_icon(status: str | None) -> str:
    return STATUS_ICONS.get(str(status or "").strip().lower(), "⚪")


def field_label(text: str) -> str:
    # Form relay backends (PHP-style) rewrite spaces and dots in field names
    # to underscores; non-breaking spaces survive and render as plain spaces.
    return text.replace(" ", NBSP).replace(".", NBSP)


def friendly_time(ts: float) -> str:
    moment = datetime.fromtimestamp(ts)
    clock = moment.strftime("%I:%M %p").lstrip("0")
    return f"{moment.strftime('%A, %B')} {moment.day} · {clock}"


def friendly_stamp(value: object) -> str:
    try:
        return friendly_time(datetime.fromisoformat(str(value)).timestamp())
    except (TypeError, ValueError):
        return str(value or "unknown")


def pluralize(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


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
    if total and not warn and not fail:
        return f"🟢 All {pluralize(total, 'check')} passing"
    parts = []
    if fail:
        parts.append(f"🔴 {fail} failing")
    if warn:
        parts.append(f"🟡 {pluralize(warn, 'warning')}")
    parts.append(f"🟢 {ok} passing")
    return f"{' · '.join(parts)} ({pluralize(total, 'check')})"


def format_check(row: dict) -> str:
    icon = status_icon(row.get("status"))
    host = row.get("host") or "Unknown host"
    label = row.get("label") or row.get("id") or "Unknown check"
    message = row.get("message") or "no detail"
    cadence = row.get("cadenceLabel") or row.get("cadenceId") or "unknown cadence"
    return redact(f"{icon} {host} · {label} — {message} ({cadence})")


def limited_lines(rows: list[dict], limit: int = 20) -> list[str]:
    lines = [format_check(row) for row in rows[:limit]]
    if len(rows) > limit:
        lines.append(f"… plus {len(rows) - limit} more on the dashboard")
    return lines


def morning_subject(ops: dict) -> str:
    summary = ops.get("summary", {}) or {}
    total = int(summary.get("total") or 0)
    fail = int(summary.get("fail") or 0)
    warn = int(summary.get("warn") or 0)
    if fail:
        detail = f"{fail} failing" + (f", {pluralize(warn, 'warning')}" if warn else "")
        return f"🔴 Cabrera Network · Morning report — {detail}"
    if warn:
        return f"🟡 Cabrera Network · Morning report — {pluralize(warn, 'warning')} to review"
    checks = f" ({pluralize(total, 'check')})" if total else ""
    return f"🟢 Cabrera Network · Morning report — all green{checks}"


def critical_subject(failures: list[dict]) -> str:
    count = len(failures)
    if count == 1:
        item = failures[0]
        label = item.get("label", item.get("id", "check"))
        host = str(item.get("host") or "").strip()
        where = f" on {host}" if host else ""
        return f"🚨 Cabrera Network · {label}{where} is failing"
    return f"🚨 Cabrera Network · {count} checks failing"


def split_unhealthy(ops: dict) -> tuple[list[dict], list[dict]]:
    unhealthy = unhealthy_checks(ops)
    fails = [row for row in unhealthy if row.get("status") == "fail"]
    warns = [row for row in unhealthy if row.get("status") == "warn"]
    return fails, warns


def is_retry_run(due_configs: list[dict]) -> bool:
    return any("retry" in str(cfg.get("label", "")).lower() for cfg in due_configs)


ALL_CLEAR_TEXT = "Every check reported healthy overnight. Nothing needs your attention today."


def render_morning_body(ops: dict, due_configs: list[dict], now: float, link: str | None = None) -> str:
    morning_rows = [row for row in iter_checks(ops) if row.get("cadenceId") == "morning"]
    fails, warns = split_unhealthy(ops)
    title = "MORNING REPORT" + (" · RETRY" if is_retry_run(due_configs) else "")

    lines = [
        f"🛰️  CABRERA NETWORK — {title}",
        friendly_time(now),
        DIVIDER,
        "",
        f"📊 Scorecard — {summary_line(ops)}",
    ]
    if fails:
        lines.extend(["", "🔴 Failing now"])
        lines.extend(limited_lines(fails, limit=12))
    if warns:
        lines.extend(["", "🟡 Warnings"])
        lines.extend(limited_lines(warns, limit=12))
    if not fails and not warns:
        lines.extend(["", f"✅ {ALL_CLEAR_TEXT}"])
    if morning_rows:
        lines.extend(["", "🌅 Morning checks"])
        lines.extend(limited_lines(morning_rows, limit=10))
    lines.extend(["", DIVIDER])
    if link:
        lines.append(f"🔗 Dashboard: {link}")
    lines.append(f"Snapshot {friendly_stamp(ops.get('updatedAt'))} · pi4-noc ops center")

    return redact("\n".join(lines).strip() + "\n")


def render_critical_body(ops: dict, failures: list[dict], now: float, link: str | None = None) -> str:
    all_failures = unhealthy_checks(ops, {"fail"})
    lines = [
        "🚨 CABRERA NETWORK — CRITICAL ALERT",
        friendly_time(now),
        DIVIDER,
        "",
        "🔥 What broke",
    ]
    lines.extend(limited_lines(failures, limit=20))
    lines.extend(["", f"📊 Scorecard — {summary_line(ops)}"])

    remaining = [row for row in all_failures if row.get("key") not in {item.get("key") for item in failures}]
    if remaining:
        lines.extend(["", "⏳ Still failing from earlier"])
        lines.extend(limited_lines(remaining, limit=20))

    lines.extend(["", DIVIDER])
    if link:
        lines.append(f"🔗 Dashboard: {link}")
    lines.append(f"Snapshot {friendly_stamp(ops.get('updatedAt'))} · pi4-noc ops center")

    return redact("\n".join(lines).strip() + "\n")


def morning_email_fields(ops: dict, due_configs: list[dict], now: float, link: str | None = None) -> dict[str, str]:
    """Structured fields for form relay providers (FormSubmit's table/box
    templates render each field as its own labeled row)."""
    morning_rows = [row for row in iter_checks(ops) if row.get("cadenceId") == "morning"]
    fails, warns = split_unhealthy(ops)
    retry = " · retry" if is_retry_run(due_configs) else ""

    fields: dict[str, str] = {
        field_label("🛰️ Report"): f"Morning ops digest · {friendly_time(now)}{retry}",
        field_label("📊 Scorecard"): summary_line(ops),
    }
    if fails:
        fields[field_label("🔴 Failing now")] = "\n".join(limited_lines(fails, limit=12))
    if warns:
        fields[field_label("🟡 Warnings")] = "\n".join(limited_lines(warns, limit=12))
    if not fails and not warns:
        fields[field_label("✅ All clear")] = ALL_CLEAR_TEXT
    if morning_rows:
        fields[field_label("🌅 Morning checks")] = "\n".join(limited_lines(morning_rows, limit=10))
    fields[field_label("🕐 Snapshot")] = friendly_stamp(ops.get("updatedAt"))
    if link:
        fields[field_label("🔗 Dashboard")] = link
    return fields


def critical_email_fields(ops: dict, failures: list[dict], now: float, link: str | None = None) -> dict[str, str]:
    all_failures = unhealthy_checks(ops, {"fail"})
    remaining = [row for row in all_failures if row.get("key") not in {item.get("key") for item in failures}]

    fields: dict[str, str] = {
        field_label("🚨 What broke"): "\n".join(limited_lines(failures, limit=20)),
        field_label("📊 Scorecard"): summary_line(ops),
    }
    if remaining:
        fields[field_label("⏳ Still failing")] = "\n".join(limited_lines(remaining, limit=20))
    fields[field_label("🕐 Detected")] = friendly_time(now)
    fields[field_label("🔍 Snapshot")] = friendly_stamp(ops.get("updatedAt"))
    if link:
        fields[field_label("🔗 Dashboard")] = link
    return fields


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
        return self.ntfy_configured() or self.email_configured() or self.webhook_configured() or self.dry_run()

    def dry_run(self) -> bool:
        return env_bool(self.env.get("PI4_NOC_NOTIFY_DRY_RUN"), default=False)

    def email_configured(self) -> bool:
        return bool(split_csv(self.env.get("PI4_NOC_NOTIFY_EMAIL_TO")) and self.env.get("PI4_NOC_SMTP_HOST"))

    def webhook_configured(self) -> bool:
        return bool(self.env.get("PI4_NOC_NOTIFY_WEBHOOK_URL"))

    def ntfy_configured(self) -> bool:
        return bool(self.env.get("PI4_NOC_NOTIFY_NTFY_URL"))

    def dashboard_link(self) -> str:
        return (
            self.env.get("PI4_NOC_NOTIFY_LINK_URL", "").strip()
            or self.env.get("PI4_NOC_NOTIFY_NTFY_CLICK_URL", "").strip()
        )

    def critical_cooldown_seconds(self) -> float:
        try:
            return max(0.0, float(self.env.get("PI4_NOC_NOTIFY_CRITICAL_COOLDOWN_SECONDS", "1800")))
        except ValueError:
            return 1800.0

    def morning_retry_seconds(self) -> float:
        try:
            return max(300.0, float(self.env.get("PI4_NOC_NOTIFY_MORNING_RETRY_SECONDS", "1800")))
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

        morning_due = any(cfg.get("id") == "morning" for cfg in due_configs)
        morning_retry = self._morning_retry_due(state, now)
        if morning_due or morning_retry:
            morning_configs = due_configs if morning_due else [{"id": "morning", "label": "Every morning (retry)"}]
            sent_morning, morning_changed = self._send_morning_if_needed(ops, morning_configs, state, now)
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

    def _morning_retry_due(self, state: dict, now: float) -> bool:
        morning = state.get("morning") if isinstance(state.get("morning"), dict) else {}
        today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        if morning.get("pendingDate") != today or morning.get("lastDate") == today:
            return False
        try:
            last_attempt = datetime.fromisoformat(str(morning.get("lastAttemptAt") or "")).timestamp()
        except Exception:
            return True
        return now - last_attempt >= self.morning_retry_seconds()

    def _record_delivery(self, state: dict, kind: str, delivered: bool, now: float) -> None:
        delivery = state.setdefault("delivery", {})
        attempt_at = datetime.fromtimestamp(now).isoformat()
        delivery["lastAttemptAt"] = attempt_at
        delivery["lastType"] = kind
        if delivered:
            delivery["lastSuccessAt"] = attempt_at
        else:
            delivery["lastFailureAt"] = attempt_at

    def _send_morning_if_needed(self, ops: dict, due_configs: list[dict], state: dict, now: float) -> tuple[dict | None, bool]:
        morning = state.setdefault("morning", {})
        today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        if morning.get("lastDate") == today:
            return None, False
        if morning.get("pendingDate") == today and not self._morning_retry_due(state, now):
            return None, False
        subject = morning_subject(ops)
        link = self.dashboard_link() or None
        body = render_morning_body(ops, due_configs, now, link=link)
        fields = morning_email_fields(ops, due_configs, now, link=link)
        delivered = self.send(subject, body, severity="morning", fields=fields)
        morning["lastAttemptAt"] = datetime.fromtimestamp(now).isoformat()
        self._record_delivery(state, "morning", delivered, now)
        if delivered:
            morning["lastDate"] = today
            morning["lastSuccessAt"] = morning["lastAttemptAt"]
            morning.pop("pendingDate", None)
            return {"type": "morning", "subject": subject}, True
        morning["lastFailureAt"] = morning["lastAttemptAt"]
        morning["pendingDate"] = today
        return None, True

    def _send_critical_if_needed(self, ops: dict, state: dict, now: float) -> tuple[dict | None, bool]:
        failures = unhealthy_checks(ops, {"fail"})
        critical = state.setdefault("critical", {})
        active = critical.get("active", {})
        if not isinstance(active, dict):
            active = {}
        recent = critical.get("recent", {})
        if not isinstance(recent, dict):
            recent = {}

        by_key = {row["key"]: row for row in failures}
        cooldown = self.critical_cooldown_seconds()
        alert_rows = []
        next_active = {}
        next_recent = {}
        changed = False

        def last_activity(entry: dict) -> float:
            last_sent = float(entry.get("lastSentAt") or 0)
            last_attempt = float(entry.get("lastAttemptEpoch") or 0)
            if not last_attempt and entry.get("lastAttemptAt"):
                try:
                    last_attempt = datetime.fromisoformat(str(entry["lastAttemptAt"])).timestamp()
                except (TypeError, ValueError):
                    last_attempt = 0
            return max(last_sent, last_attempt)

        for key, entry in recent.items():
            if not isinstance(entry, dict):
                continue
            activity = last_activity(entry)
            if activity and now - activity < cooldown:
                next_recent[key] = dict(entry)

        for key, row in by_key.items():
            previous = active.get(key, {}) if isinstance(active.get(key), dict) else next_recent.pop(key, {})
            activity = last_activity(previous)
            if not previous or now - activity >= cooldown:
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
            if previous.get("lastAttemptAt"):
                entry["lastAttemptAt"] = previous.get("lastAttemptAt")
            if previous.get("lastAttemptEpoch"):
                entry["lastAttemptEpoch"] = previous.get("lastAttemptEpoch")
            next_active[key] = entry

        recovered_at = datetime.fromtimestamp(now).isoformat()
        for key, entry in active.items():
            if key in by_key or not isinstance(entry, dict):
                continue
            activity = last_activity(entry)
            if activity and now - activity < cooldown:
                recovered = dict(entry)
                recovered["recoveredAt"] = recovered.get("recoveredAt") or recovered_at
                next_recent[key] = recovered

        if set(active.keys()) != set(next_active.keys()):
            changed = True

        result = None
        if alert_rows:
            subject = critical_subject(alert_rows)
            link = self.dashboard_link() or None
            body = render_critical_body(ops, alert_rows, now, link=link)
            fields = critical_email_fields(ops, alert_rows, now, link=link)
            delivered = self.send(subject, body, severity="critical", fields=fields)
            attempt_at = datetime.fromtimestamp(now).isoformat()
            self._record_delivery(state, "critical", delivered, now)
            for row in alert_rows:
                key = row["key"]
                next_active[key]["lastAttemptAt"] = attempt_at
                next_active[key]["lastAttemptEpoch"] = now
                if delivered:
                    next_active[key]["lastSentAt"] = now
            if delivered:
                result = {"type": "critical", "subject": subject, "count": len(alert_rows)}
            changed = True

        if active != next_active:
            changed = True
        if recent != next_recent:
            changed = True
        critical["active"] = next_active
        critical["recent"] = next_recent
        critical["lastCheckedAt"] = datetime.fromtimestamp(now).isoformat()
        return result, changed

    def send(self, subject: str, body: str, *, severity: str = "info", fields: dict[str, str] | None = None) -> bool:
        subject = redact(subject)
        body = redact(body)
        if fields:
            fields = {key: redact(str(value)) for key, value in fields.items() if str(value).strip()}
        if self.dry_run():
            print(f"pi4-noc notification dry-run [{severity}]: {subject}", flush=True)
            return True

        attempted = False
        delivered = False
        if self.ntfy_configured():
            attempted = True
            try:
                self.send_ntfy(subject, body, severity=severity)
                delivered = True
            except Exception as exc:
                print(f"pi4-noc ntfy notification failed: {redact(str(exc))}", flush=True)
        if self.email_configured():
            attempted = True
            try:
                self.send_email(subject, body)
                delivered = True
            except Exception as exc:
                print(f"pi4-noc email notification failed: {redact(str(exc))}", flush=True)
        if self.webhook_configured() and not delivered:
            attempted = True
            try:
                self.send_webhook(subject, body, severity=severity, fields=fields)
                delivered = True
            except Exception as exc:
                print(f"pi4-noc webhook notification failed: {redact(str(exc))}", flush=True)
        return delivered if attempted else False

    def send_ntfy(self, subject: str, body: str, *, severity: str) -> None:
        topic_url = self.env.get("PI4_NOC_NOTIFY_NTFY_URL", "").strip().rstrip("/")
        parsed = urlparse.urlsplit(topic_url)
        topic = parsed.path.strip("/")
        if parsed.scheme != "https" or not parsed.netloc or not re.fullmatch(r"[-_A-Za-z0-9]{16,64}", topic):
            raise ValueError("PI4_NOC_NOTIFY_NTFY_URL must be an HTTPS URL with a private 16-64 character topic")

        def limited(value: str, max_bytes: int) -> str:
            raw = value.encode("utf-8")
            if len(raw) <= max_bytes:
                return value
            return raw[: max_bytes - 3].decode("utf-8", "ignore") + "..."

        priority = 5 if severity == "critical" else 3
        payload = {
            "topic": topic,
            "title": limited(subject, 200),
            "message": limited(body, 3000),
            "priority": priority,
            "tags": ["rotating_light" if severity == "critical" else "house"],
        }
        click_url = self.env.get("PI4_NOC_NOTIFY_NTFY_CLICK_URL", "").strip()
        if click_url:
            payload["click"] = click_url

        root_url = urlparse.urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "pi4-noc/notifications",
        }
        timeout = float(self.env.get("PI4_NOC_NOTIFY_NTFY_TIMEOUT_SECONDS", "10"))
        req = urlrequest.Request(root_url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(65536)
            status = resp.getcode() if hasattr(resp, "getcode") else 200
            if status >= 400:
                raise RuntimeError(f"ntfy returned HTTP {status}")
            response = json.loads(raw.decode("utf-8", "replace") or "{}")
            if not isinstance(response, dict) or response.get("event") not in {None, "message"}:
                raise RuntimeError("ntfy returned an unexpected response")

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

    def send_webhook(self, subject: str, body: str, *, severity: str, fields: dict[str, str] | None = None) -> None:
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
        if fields:
            payload["fields"] = fields
        headers = {"Accept": "application/json", "User-Agent": "pi4-noc/notifications"}
        if self.env.get("PI4_NOC_NOTIFY_WEBHOOK_REFERER", "").strip():
            headers["Referer"] = self.env["PI4_NOC_NOTIFY_WEBHOOK_REFERER"].strip()
        if self.env.get("PI4_NOC_NOTIFY_WEBHOOK_FORMAT", "json").strip().lower() in {"form", "form-urlencoded", "x-www-form-urlencoded"}:
            form_payload: dict[str, str] = {}
            if fields:
                # Each field renders as its own labeled row in the provider's
                # email template; plumbing values stay out of the visible email.
                form_payload.update(fields)
            else:
                form_payload.update(
                    {
                        "name": self.env.get("PI4_NOC_NOTIFY_WEBHOOK_NAME", "Cabrera Network Ops Center"),
                        "email": self.env.get("PI4_NOC_NOTIFY_WEBHOOK_FROM", "pi4-noc@cabrera-network.local"),
                        "subject": subject,
                        "message": body,
                        "source": payload["source"],
                        "severity": severity,
                        "sentAt": payload["sentAt"],
                    }
                )
            form_payload["_subject"] = subject
            form_payload["_captcha"] = self.env.get("PI4_NOC_NOTIFY_WEBHOOK_CAPTCHA", "false")
            form_payload["_template"] = self.env.get("PI4_NOC_NOTIFY_WEBHOOK_TEMPLATE", "table")
            data = urlparse.urlencode(form_payload).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        timeout = float(self.env.get("PI4_NOC_NOTIFY_WEBHOOK_TIMEOUT_SECONDS", "30"))
        req = urlrequest.Request(url, data=data, headers=headers, method="POST")
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(65536)
            status = resp.getcode() if hasattr(resp, "getcode") else 200
            if status >= 400:
                raise RuntimeError(f"webhook returned HTTP {status}")
            try:
                response = json.loads(raw.decode("utf-8", "replace") or "{}")
            except Exception:
                response = {}
            if isinstance(response, dict):
                success = str(response.get("success", "")).strip().lower()
                if success in {"false", "0", "no"}:
                    raise RuntimeError(str(response.get("message") or "webhook reported failure"))
                if response.get("error"):
                    raise RuntimeError(str(response.get("error")))
