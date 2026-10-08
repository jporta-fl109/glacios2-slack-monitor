#!/usr/bin/env python3
"""
Glacios 2 -> Slack monitor (TEM Scripting API version)
=========================================================

Polls the microscope's live status via the TEM Scripting interface
(Standard/Advanced Scripting, or UTAPI) using the `pytemscript` client,
instead of parsing log files. This is the approach the cryo-EM
community actually uses for automation (same interface EPU, Leginon,
and SerialEM build on) -- it gives structured values like vacuum
status and dewar level directly, rather than regex-matching log text.

Architecture
------------
    [Instrument PC]                    [Monitoring machine -- can be
      TEM Server (COM)                  the same PC, or anywhere on
            |                           your network]
      pytemscript-server   <--socket-->   this script (tem_api_monitor.py)
      (bundled with pytemscript)                 |
                                            Slack webhook / bot token

`pytemscript-server` MUST run on the instrument control PC (the COM
interface is local-only). This script is the *client* -- it can run
on that same PC, or on another machine that can reach it over the
network, by polling the socket server pytemscript-server exposes.

REQUIRES: the TEM Scripting interface (Standard/Advanced Scripting or
UTAPI) to be licensed and enabled on your instrument. Confirm this
with your Thermo Fisher Field Service Engineer before relying on this
-- there's no way to check this remotely/in software ahead of time.

IMPORTANT -- attribute names are not yet verified against a real
Glacios 2: pytemscript's exact property/method names and return types
can vary by TEM server version and by which scripting interface
(Standard/Advanced/UTAPI) is active. The CHECKS below are written
defensively (try/except per check, clearly logged) so a wrong
attribute name degrades to a logged warning instead of a crash -- but
you WILL want to verify the real attribute names against the
pytemscript docs for your installed version, or by running
`python -c "from pytemscript.microscope import Microscope; ..."`
interactively against your real instrument, and adjust CHECKS
accordingly. See README.md "Verifying the API checks".

Usage:
    python tem_api_monitor.py --config config_api.yaml
    python tem_api_monitor.py --config config_api.yaml --once
    python tem_api_monitor.py --config config_api.yaml --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

try:
    import yaml
except ImportError:
    sys.exit("Missing dependency PyYAML. Run: pip install -r requirements_api.txt")

try:
    import requests
except ImportError:
    sys.exit("Missing dependency requests. Run: pip install -r requirements_api.txt")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("tem_api_monitor")

SEVERITY_EMOJI = {
    "critical": ":red_circle:",
    "warning": ":large_orange_circle:",
    "info": ":large_blue_circle:",
}


# ---------------------------------------------------------------------------
# Microscope connection
# ---------------------------------------------------------------------------

def connect_microscope(host: str, port: int):
    """
    Connect to a remote pytemscript-server over a socket.

    This function is intentionally isolated so that if your pytemscript
    version's connection signature differs, this is the only place to fix.
    """
    from pytemscript.microscope import Microscope  # imported here so --help works without it installed

    return Microscope(connection="socket", host=host, port=port)


# ---------------------------------------------------------------------------
# Checks: each reads one live value off the microscope and decides whether
# it represents an alertable condition. Written defensively -- any
# attribute/method that doesn't match your pytemscript version's real API
# raises inside the check and is caught + logged, not fatal to the process.
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    triggered: bool
    detail: str = ""


def check_vacuum_status(mic) -> CheckResult:
    """Alert if the vacuum system status is not nominal/ready."""
    status = mic.vacuum.status
    status_str = str(status).lower()
    ok_values = ("ready", "ok", "nominal")
    if any(v in status_str for v in ok_values):
        return CheckResult(False)
    return CheckResult(True, f"Vacuum status: {status}")


def check_vacuum_gauges(mic, max_pressure_pa: float) -> CheckResult:
    """Alert if any reported vacuum gauge exceeds the configured threshold."""
    gauges = mic.vacuum.gauges  # expected: dict-like {gauge_name: pressure}
    bad = {name: val for name, val in dict(gauges).items() if _to_float(val) is not None and _to_float(val) > max_pressure_pa}
    if bad:
        return CheckResult(True, f"Gauge(s) above {max_pressure_pa:g} Pa: {bad}")
    return CheckResult(False)


def check_autoloader_available(mic) -> CheckResult:
    """Alert if the autoloader reports unavailable (often indicates a fault)."""
    available = mic.autoloader.is_available
    if available:
        return CheckResult(False)
    return CheckResult(True, "Autoloader reports NOT available.")


def check_dewar_level(mic, min_percent: float) -> CheckResult:
    """Alert if LN2 dewar level is below the configured threshold."""
    level = mic.temperature.dewar_level()
    level_pct = _to_float(level)
    if level_pct is None:
        return CheckResult(False)
    # Some APIs return 0-1, others 0-100 -- normalize.
    if level_pct <= 1.0:
        level_pct *= 100
    if level_pct < min_percent:
        return CheckResult(True, f"LN2 dewar level {level_pct:.0f}% (below {min_percent:g}% threshold)")
    return CheckResult(False)


def check_dewar_filling_stuck(mic, max_fill_minutes: float) -> CheckResult:
    """
    Alert if the dewar has been reported as 'filling' for longer than
    expected (possible stuck/failed autofill). Requires tracking how long
    is_dewar_filling has been continuously true -- see PollState below.
    """
    is_filling = bool(mic.temperature.is_dewar_filling)
    return CheckResult(False, "is_filling" if is_filling else "")  # handled specially in Poller


def check_dewar_time_to_refill(mic, warn_minutes: float) -> CheckResult:
    """Alert if time remaining until the next required LN2 refill is low."""
    seconds_left = mic.temperature.dewars_time()
    minutes_left = _to_float(seconds_left)
    if minutes_left is None:
        return CheckResult(False)
    minutes_left = minutes_left / 60.0 if minutes_left > 1000 else minutes_left  # guess: seconds vs minutes
    if minutes_left < warn_minutes:
        return CheckResult(True, f"~{minutes_left:.0f} min until next required LN2 refill")
    return CheckResult(False)


def _to_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# Registry: name -> (function, kwargs pulled from config)
CHECK_FUNCTIONS: dict[str, Callable] = {
    "vacuum_status": check_vacuum_status,
    "vacuum_gauges": check_vacuum_gauges,
    "autoloader_available": check_autoloader_available,
    "dewar_level": check_dewar_level,
    "dewar_filling_stuck": check_dewar_filling_stuck,
    "dewar_time_to_refill": check_dewar_time_to_refill,
}


@dataclass
class CheckConfig:
    name: str
    function: str
    severity: str = "warning"
    cooldown_minutes: float = 15
    params: dict = field(default_factory=dict)


@dataclass
class Config:
    host: str
    port: int
    poll_interval_seconds: float
    state_file: str
    slack_webhook_url: str
    slack_bot_token: str
    slack_channel: str
    dry_run: bool
    checks: list

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        def env_override(value, env_name):
            return os.environ.get(env_name, value)

        checks = []
        for c in raw.get("checks", []):
            params = {k: v for k, v in c.items() if k not in ("name", "function", "severity", "cooldown_minutes")}
            checks.append(CheckConfig(
                name=c["name"],
                function=c["function"],
                severity=c.get("severity", "warning"),
                cooldown_minutes=c.get("cooldown_minutes", 15),
                params=params,
            ))

        return cls(
            host=env_override(raw.get("host", "localhost"), "TEM_API_HOST"),
            port=int(env_override(raw.get("port", 39000), "TEM_API_PORT")),
            poll_interval_seconds=float(raw.get("poll_interval_seconds", 30)),
            state_file=raw.get("state_file", "./state/tem_api_monitor_state.json"),
            slack_webhook_url=env_override(raw.get("slack_webhook_url", ""), "SLACK_WEBHOOK_URL"),
            slack_bot_token=env_override(raw.get("slack_bot_token", ""), "SLACK_BOT_TOKEN"),
            slack_channel=env_override(raw.get("slack_channel", ""), "SLACK_CHANNEL"),
            dry_run=bool(raw.get("dry_run", True)),
            checks=checks,
        )


class State:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.last_alert_ts: dict[str, float] = {}
        self.suppressed_count: dict[str, int] = {}
        self.filling_since: Optional[float] = None
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                self.last_alert_ts = data.get("last_alert_ts", {})
                self.suppressed_count = data.get("suppressed_count", {})
                self.filling_since = data.get("filling_since")
            except (json.JSONDecodeError, OSError) as e:
                log.warning("Could not read state file (%s); starting fresh.", e)

    def save(self):
        data = {
            "last_alert_ts": self.last_alert_ts,
            "suppressed_count": self.suppressed_count,
            "filling_since": self.filling_since,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(self.path)


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
        log.error("No Slack destination configured and dry_run is false. Message not sent: %s", text)
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


def format_alert(check: CheckConfig, detail: str, suppressed: int) -> str:
    emoji = SEVERITY_EMOJI.get(check.severity, ":warning:")
    header = f"{emoji} *Glacios 2 - {check.name.replace('_', ' ')}* ({check.severity})"
    body = f"> {detail}" if detail else ""
    if suppressed:
        body += f"\n_({suppressed} similar alert(s) suppressed during cooldown)_"
    return f"{header}\n{body}".strip()


class Poller:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.state = State(cfg.state_file)
        self.notifier = SlackNotifier(cfg)
        self.mic = None

    def connect(self):
        self.mic = connect_microscope(self.cfg.host, self.cfg.port)
        log.info("Connected to TEM Scripting server at %s:%s", self.cfg.host, self.cfg.port)

    def run_once(self):
        if self.mic is None:
            self.connect()

        for check in self.cfg.checks:
            func = CHECK_FUNCTIONS.get(check.function)
            if func is None:
                log.error("Unknown check function '%s' for check '%s'; skipping.", check.function, check.name)
                continue
            try:
                result = func(self.mic, **check.params)
            except Exception as e:
                log.warning(
                    "Check '%s' (%s) raised %s: %s -- this usually means the pytemscript attribute/method "
                    "name doesn't match your installed version or active scripting interface. See README.md "
                    "'Verifying the API checks'.",
                    check.name, check.function, type(e).__name__, e,
                )
                continue

            self._handle_dewar_filling_special_case(check, result)

            if result.triggered:
                self._maybe_alert(check, result.detail)

        self.state.save()

    def _handle_dewar_filling_special_case(self, check: CheckConfig, result: CheckResult):
        if check.function != "dewar_filling_stuck":
            return
        max_minutes = check.params.get("max_fill_minutes", 20)
        now = time.time()
        if result.detail == "is_filling":
            if self.state.filling_since is None:
                self.state.filling_since = now
            elapsed_min = (now - self.state.filling_since) / 60.0
            if elapsed_min > max_minutes:
                self._maybe_alert(check, f"LN2 dewar has been filling for {elapsed_min:.0f} min (> {max_minutes:g} min threshold)")
        else:
            self.state.filling_since = None

    def _maybe_alert(self, check: CheckConfig, detail: str):
        now = time.time()
        last = self.state.last_alert_ts.get(check.name, 0)
        cooldown_seconds = check.cooldown_minutes * 60

        if now - last < cooldown_seconds:
            self.state.suppressed_count[check.name] = self.state.suppressed_count.get(check.name, 0) + 1
            log.info("Check '%s' triggered but is within cooldown; suppressing. Detail: %s", check.name, detail)
            return

        suppressed = self.state.suppressed_count.pop(check.name, 0)
        text = format_alert(check, detail, suppressed)
        if self.notifier.send(text):
            self.state.last_alert_ts[check.name] = now
        else:
            log.error("Failed to deliver alert for check '%s'; will retry on next trigger.", check.name)

    def run_forever(self):
        log.info(
            "Starting Glacios 2 API monitor. TEM Scripting server: %s:%s (dry_run=%s)",
            self.cfg.host, self.cfg.port, self.cfg.dry_run,
        )
        while True:
            try:
                self.run_once()
            except Exception:
                log.exception(
                    "Unexpected error while polling (connection to pytemscript-server may have dropped); "
                    "will retry after poll interval."
                )
                self.mic = None  # force reconnect next cycle
            time.sleep(self.cfg.poll_interval_seconds)


def main():
    parser = argparse.ArgumentParser(description="Poll Glacios 2 TEM Scripting API and alert Slack.")
    parser.add_argument("--config", default="config_api.yaml", help="Path to config YAML file.")
    parser.add_argument("--once", action="store_true", help="Poll once, then exit.")
    parser.add_argument("--dry-run", action="store_true", help="Force dry-run regardless of config.")
    args = parser.parse_args()

    if not os.path.exists(args.config):
        sys.exit(
            f"Config file not found: {args.config}\n"
            f"Copy config_api.example.yaml to config_api.yaml and edit it first."
        )

    cfg = Config.load(args.config)
    if args.dry_run:
        cfg.dry_run = True

    poller = Poller(cfg)

    if args.once:
        poller.run_once()
        log.info("Poll complete. Exiting (--once).")
    else:
        poller.run_forever()


if __name__ == "__main__":
    main()
