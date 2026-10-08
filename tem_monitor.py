#!/usr/bin/env python3
"""
Glacios 2 -> Slack monitor
===========================

Tails a TEM interface log file and posts Slack alerts when lines match
configured rules (vacuum system faults, LN2/autoloader fill issues,
general errors/warnings, etc.)

Designed to run as a long-lived process (systemd service on Linux,
or a scheduled/background task on Windows) on a machine that has
access to the log file -- see README.md for deployment options.

Usage:
    python tem_monitor.py --config config.yaml
    python tem_monitor.py --config config.yaml --dry-run   # force dry-run
    python tem_monitor.py --config config.yaml --once      # process once and exit (for testing)

State (file read offset + recent alert timestamps) is persisted to
`state_file` so the monitor can be restarted without either replaying
the entire log or losing rule cooldowns.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    sys.exit("Missing dependency PyYAML. Run: pip install -r requirements.txt")

try:
    import requests
except ImportError:
    sys.exit("Missing dependency requests. Run: pip install -r requirements.txt")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("tem_monitor")


SEVERITY_EMOJI = {
    "critical": ":red_circle:",
    "warning": ":large_orange_circle:",
    "info": ":large_blue_circle:",
}


@dataclass
class Rule:
    name: str
    pattern: str
    severity: str = "warning"
    cooldown_minutes: float = 15
    compiled: re.Pattern = field(init=False, repr=False)

    def __post_init__(self):
        self.compiled = re.compile(self.pattern, re.IGNORECASE)


@dataclass
class Config:
    log_path: str
    poll_interval_seconds: float
    state_file: str
    slack_webhook_url: str
    slack_bot_token: str
    slack_channel: str
    dry_run: bool
    rules: list

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        def env_override(value, env_name):
            return os.environ.get(env_name, value)

        rules = [Rule(**r) for r in raw.get("rules", [])]

        return cls(
            log_path=env_override(raw.get("log_path", ""), "TEM_LOG_PATH"),
            poll_interval_seconds=float(raw.get("poll_interval_seconds", 5)),
            state_file=raw.get("state_file", "./state/tem_monitor_state.json"),
            slack_webhook_url=env_override(raw.get("slack_webhook_url", ""), "SLACK_WEBHOOK_URL"),
            slack_bot_token=env_override(raw.get("slack_bot_token", ""), "SLACK_BOT_TOKEN"),
            slack_channel=env_override(raw.get("slack_channel", ""), "SLACK_CHANNEL"),
            dry_run=bool(raw.get("dry_run", True)),
            rules=rules,
        )


class State:
    """Persisted offset + per-rule cooldown tracking."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.offset = 0
        self.inode = None
        self.last_alert_ts: dict[str, float] = {}
        self.suppressed_count: dict[str, int] = {}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                self.offset = data.get("offset", 0)
                self.inode = data.get("inode")
                self.last_alert_ts = data.get("last_alert_ts", {})
                self.suppressed_count = data.get("suppressed_count", {})
            except (json.JSONDecodeError, OSError) as e:
                log.warning("Could not read state file (%s); starting fresh.", e)

    def save(self):
        data = {
            "offset": self.offset,
            "inode": self.inode,
            "last_alert_ts": self.last_alert_ts,
            "suppressed_count": self.suppressed_count,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(self.path)


def get_inode(path: str) -> Optional[int]:
    try:
        return os.stat(path).st_ino
    except OSError:
        return None


class LogTailer:
    """
    Tails a text log file, resuming from a persisted offset and
    detecting rotation (file replaced / truncated).
    """

    def __init__(self, path: str, state: State):
        self.path = path
        self.state = state

    def read_new_lines(self) -> list[str]:
        try:
            size = os.path.getsize(self.path)
        except OSError:
            log.warning("Log file not found or unreadable: %s", self.path)
            return []

        current_inode = get_inode(self.path)

        rotated = False
        if self.state.inode is not None and current_inode != self.state.inode:
            rotated = True
        elif size < self.state.offset:
            # File shrank without inode change (e.g. truncated in place,
            # or a Windows rotation scheme) -> treat as rotation.
            rotated = True

        if rotated:
            log.info("Log rotation detected for %s; reading from start of new file.", self.path)
            self.state.offset = 0

        self.state.inode = current_inode

        new_lines: list[str] = []
        with open(self.path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(self.state.offset)
            for line in f:
                new_lines.append(line.rstrip("\n"))
            self.state.offset = f.tell()

        return new_lines


class SlackNotifier:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def send(self, text: str) -> bool:
        if self.cfg.dry_run:
            log.info("[DRY RUN] Would post to Slack: %s", text)
            return True

        if self.cfg.slack_webhook_url:
            return self._send_webhook(text)
        if self.cfg.slack_bot_token and self.cfg.slack_channel:
            return self._send_bot_token(text)

        log.error(
            "No Slack destination configured (set slack_webhook_url, or "
            "slack_bot_token + slack_channel) and dry_run is false. "
            "Message not sent: %s",
            text,
        )
        return False

    def _send_webhook(self, text: str) -> bool:
        try:
            resp = requests.post(self.cfg.slack_webhook_url, json={"text": text}, timeout=10)
            if resp.status_code != 200:
                log.error("Slack webhook post failed (%s): %s", resp.status_code, resp.text)
                return False
            return True
        except requests.RequestException as e:
            log.error("Slack webhook post raised an exception: %s", e)
            return False

    def _send_bot_token(self, text: str) -> bool:
        try:
            resp = requests.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {self.cfg.slack_bot_token}"},
                json={"channel": self.cfg.slack_channel, "text": text},
                timeout=10,
            )
            data = resp.json()
            if not data.get("ok"):
                log.error("Slack chat.postMessage failed: %s", data)
                return False
            return True
        except (requests.RequestException, ValueError) as e:
            log.error("Slack chat.postMessage raised an exception: %s", e)
            return False


def format_alert(rule: Rule, line: str, suppressed: int) -> str:
    emoji = SEVERITY_EMOJI.get(rule.severity, ":warning:")
    header = f"{emoji} *Glacios 2 - {rule.name.replace('_', ' ')}* ({rule.severity})"
    body = f"> {line.strip()}"
    if suppressed:
        body += f"\n_({suppressed} similar alert(s) suppressed during cooldown)_"
    return f"{header}\n{body}"


class Monitor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.state = State(cfg.state_file)
        self.tailer = LogTailer(cfg.log_path, self.state)
        self.notifier = SlackNotifier(cfg)

    def process_line(self, line: str):
        if not line.strip():
            return

        for rule in self.cfg.rules:
            if rule.compiled.search(line):
                self._maybe_alert(rule, line)
                return  # first matching rule wins

    def _maybe_alert(self, rule: Rule, line: str):
        now = time.time()
        last = self.state.last_alert_ts.get(rule.name, 0)
        cooldown_seconds = rule.cooldown_minutes * 60

        if now - last < cooldown_seconds:
            self.state.suppressed_count[rule.name] = self.state.suppressed_count.get(rule.name, 0) + 1
            log.info("Rule '%s' matched but is within cooldown; suppressing. Line: %s", rule.name, line)
            return

        suppressed = self.state.suppressed_count.pop(rule.name, 0)
        text = format_alert(rule, line, suppressed)

        if self.notifier.send(text):
            self.state.last_alert_ts[rule.name] = now
        else:
            log.error("Failed to deliver alert for rule '%s'; will retry on next match.", rule.name)

    def run_once(self):
        lines = self.tailer.read_new_lines()
        for line in lines:
            self.process_line(line)
        self.state.save()
        return len(lines)

    def run_forever(self):
        log.info(
            "Starting Glacios 2 monitor. Watching: %s (dry_run=%s)",
            self.cfg.log_path,
            self.cfg.dry_run,
        )
        if not os.path.exists(self.cfg.log_path):
            log.warning(
                "Log path does not exist yet: %s -- will keep polling in case it "
                "appears (e.g. instrument not writing a log right now).",
                self.cfg.log_path,
            )
        while True:
            try:
                n = self.run_once()
                if n:
                    log.debug("Processed %d new line(s).", n)
            except Exception:
                log.exception("Unexpected error while processing log; will retry after poll interval.")
            time.sleep(self.cfg.poll_interval_seconds)


def main():
    parser = argparse.ArgumentParser(description="Tail Glacios 2 TEM interface logs and alert Slack.")
    parser.add_argument("--config", default="config.yaml", help="Path to config YAML file.")
    parser.add_argument("--once", action="store_true", help="Process available new lines once, then exit.")
    parser.add_argument("--dry-run", action="store_true", help="Force dry-run regardless of config.")
    args = parser.parse_args()

    if not os.path.exists(args.config):
        sys.exit(
            f"Config file not found: {args.config}\n"
            f"Copy config.example.yaml to config.yaml and edit it first."
        )

    cfg = Config.load(args.config)
    if args.dry_run:
        cfg.dry_run = True

    if not cfg.log_path or cfg.log_path == "/path/to/tem_interface.log":
        sys.exit("Please set log_path in your config to the real TEM interface log path.")

    monitor = Monitor(cfg)

    if args.once:
        n = monitor.run_once()
        log.info("Processed %d line(s). Exiting (--once).", n)
    else:
        monitor.run_forever()


if __name__ == "__main__":
    main()
