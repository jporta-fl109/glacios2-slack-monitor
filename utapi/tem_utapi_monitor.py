#!/usr/bin/env python3

"""
Glacios 2 -> Slack monitor (UTAPI gRPC version)
==================================================

Connects directly to Thermo's UTAPI server (utapi_server.exe, running
on the instrument control PC, port 46699) over gRPC and watches:

  - vacuum system state (VacuumService.WatchVacuumState -- streaming)
  - per-chamber vacuum pressure (VacuumChambersService.WatchVacuumChambersPressure
    -- streaming)
  - LN2 dewar level, estimated time until refill, and whether it's
    currently filling (TemperatureService -- polled; UTAPI has no
    streaming "watch" RPC for these)

This was built and tested against the real .proto files from the
Glacios 2's UTAPI API folder (compiled with compile_protos.py) and
verified end-to-end against a local fake UTAPI server standing in for
the real instrument -- see PROJECT README "UTAPI version" for what was
and wasn't verified against your actual hardware.

Setup:
    pip install -r requirements_utapi.txt
    python compile_protos.py --api-dir "/path/to/Thermo UTAPI/API"
    cp config_utapi.example.yaml config_utapi.yaml   # edit host/thresholds
    python tem_utapi_monitor.py --config config_utapi.yaml --dry-run

Then point it at the real UTAPI server (host = instrument PC's IP,
port 46699) and flip dry_run off once you have a Slack destination.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    sys.exit("Missing dependency PyYAML. Run: pip install -r requirements_utapi.txt")

try:
    import requests
except ImportError:
    sys.exit("Missing dependency requests. Run: pip install -r requirements_utapi.txt")

GENERATED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "generated")
if os.path.isdir(GENERATED_DIR):
    sys.path.insert(0, GENERATED_DIR)

try:
    import grpc
    from vacuum import vacuum_pb2, vacuum_pb2_grpc
    from vacuum import vacuum_chambers_pb2, vacuum_chambers_pb2_grpc
    from sample.v1 import temperature_pb2, temperature_pb2_grpc
except ImportError as e:
    sys.exit(
        f"Missing generated gRPC stubs or grpcio ({e}).\n"
        f"Run: pip install -r requirements_utapi.txt\n"
        f"Then: python compile_protos.py --api-dir \"/path/to/Thermo UTAPI/API\""
    )


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("tem_utapi_monitor")

SEVERITY_EMOJI = {
    "critical": ":rotating_light:",
    "warning": ":warning:",
    "info": ":information_source:",
}

# Subsystem icon shown alongside the severity icon, so an alert's topic is
# recognizable at a glance in a busy channel. Matched by alert key prefix
# (chamber_pressure_<chamber> varies per chamber, so it's a prefix match).
CHECK_EMOJI = {
    "vacuum_state": ":cyclone:",
    "chamber_pressure": ":cyclone:",
    "dewar_level": ":ice_cube:",
    "dewar_refill_due": ":hourglass_flowing_sand:",
    "dewar_filling_stuck": ":droplet:",
}


def get_check_emoji(key: str) -> str:
    if key in CHECK_EMOJI:
        return CHECK_EMOJI[key]
    for prefix, emoji in CHECK_EMOJI.items():
        if key.startswith(prefix):
            return emoji
    return ""


# ---------------------------------------------------------------------------
# Config / state / Slack (same shape as the other two monitors in this
# project, so switching between them is just a different script + config)
# ---------------------------------------------------------------------------

@dataclass
class Config:
    host: str
    port: int
    poll_interval_seconds: float
    max_chamber_pressure_pa: float
    dewar_min_percent: float
    dewar_refill_warn_minutes: float
    dewar_filling_stuck_max_minutes: float
    cooldowns: dict
    state_file: str
    slack_webhook_url: str
    slack_bot_token: str
    slack_channel: str
    dry_run: bool

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        def env(value, name):
            return os.environ.get(name, value)

        cooldowns = raw.get("cooldown_minutes", {})

        return cls(
            host=env(raw.get("host", ""), "UTAPI_HOST"),
            port=int(env(raw.get("port", 46699), "UTAPI_PORT")),
            poll_interval_seconds=float(raw.get("poll_interval_seconds", 30)),
            max_chamber_pressure_pa=float(raw.get("max_chamber_pressure_pa", 1.0e-4)),
            dewar_min_percent=float(raw.get("dewar_min_percent", 20)),
            dewar_refill_warn_minutes=float(raw.get("dewar_refill_warn_minutes", 60)),
            dewar_filling_stuck_max_minutes=float(raw.get("dewar_filling_stuck_max_minutes", 20)),
            cooldowns={
                "vacuum_state": cooldowns.get("vacuum_state", 15),
                "chamber_pressure": cooldowns.get("chamber_pressure", 15),
                "dewar_level": cooldowns.get("dewar_level", 30),
                "dewar_refill_due": cooldowns.get("dewar_refill_due", 60),
                "dewar_filling_stuck": cooldowns.get("dewar_filling_stuck", 30),
            },
            state_file=raw.get("state_file", "./state/tem_utapi_monitor_state.json"),
            slack_webhook_url=env(raw.get("slack_webhook_url", ""), "SLACK_WEBHOOK_URL"),
            slack_bot_token=env(raw.get("slack_bot_token", ""), "SLACK_BOT_TOKEN"),
            slack_channel=env(raw.get("slack_channel", ""), "SLACK_CHANNEL"),
            dry_run=bool(raw.get("dry_run", True)),
        )


class State:
    """Cross-thread cooldown tracking + the dewar-filling timer, persisted to disk."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
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
        with self.lock:
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


class AlertManager:
    """Shared cooldown + Slack dispatch, usable from multiple threads."""

    def __init__(self, cfg: Config, state: State, notifier: SlackNotifier):
        self.cfg = cfg
        self.state = state
        self.notifier = notifier

    def maybe_alert(self, key: str, severity: str, title: str, detail: str):
        cooldown_minutes = self.cfg.cooldowns.get(key, 15)
        now = time.time()
        with self.state.lock:
            last = self.state.last_alert_ts.get(key, 0)
            if now - last < cooldown_minutes * 60:
                self.state.suppressed_count[key] = self.state.suppressed_count.get(key, 0) + 1
                log.info("'%s' triggered but within cooldown; suppressing. %s", key, detail)
                return
            suppressed = self.state.suppressed_count.pop(key, 0)
            self.state.last_alert_ts[key] = now

        severity_emoji = SEVERITY_EMOJI.get(severity, ":warning:")
        check_emoji = get_check_emoji(key)
        icons = f"{severity_emoji} {check_emoji}".strip() if check_emoji else severity_emoji
        text = f"{icons} *Glacios 2 - {title}* ({severity})\n> {detail}"
        if suppressed:
            text += f"\n_({suppressed} similar alert(s) suppressed during cooldown)_"

        if not self.notifier.send(text):
            log.error("Failed to deliver alert for '%s'; cooldown still recorded (will retry on next trigger).", key)
        self.state.save()


# ---------------------------------------------------------------------------
# Watchers
# ---------------------------------------------------------------------------

def watch_vacuum_state(channel, alerts: AlertManager, stop_event: threading.Event):
    stub = vacuum_pb2_grpc.VacuumServiceStub(channel)
    while not stop_event.is_set():
        try:
            for resp in stub.WatchVacuumState(vacuum_pb2.VacuumStateRequest()):
                state_name = vacuum_pb2.VacuumStateResponse.VacuumState.Name(resp.state)
                log.debug("vacuum_state=%s", state_name)
                if resp.state != vacuum_pb2.VacuumStateResponse.VACUUM_STATE_READY:
                    alerts.maybe_alert(
                        "vacuum_state", "critical", "vacuum state",
                        f"Vacuum state is {state_name} (not READY).",
                    )
                if stop_event.is_set():
                    break
        except grpc.RpcError as e:
            log.warning("WatchVacuumState stream dropped (%s); reconnecting in 5s.", e)
            time.sleep(5)


def watch_chamber_pressure(channel, cfg: Config, alerts: AlertManager, stop_event: threading.Event):
    stub = vacuum_chambers_pb2_grpc.VacuumChambersServiceStub(channel)
    while not stop_event.is_set():
        try:
            for resp in stub.WatchVacuumChambersPressure(vacuum_chambers_pb2.WatchVacuumChambersPressureRequest()):
                for cp in resp.chamber_pressures:
                    pressure = cp.pressure_collection.pressure
                    log.debug("chamber=%s pressure=%.3e Pa", cp.chamber, pressure)
                    if pressure and pressure > cfg.max_chamber_pressure_pa:
                        alerts.maybe_alert(
                            f"chamber_pressure_{cp.chamber}", "critical", f"{cp.chamber} chamber pressure",
                            f"{cp.chamber} chamber pressure is {pressure:.2e} Pa "
                            f"(above {cfg.max_chamber_pressure_pa:.1e} Pa threshold).",
                        )
                if stop_event.is_set():
                    break
        except grpc.RpcError as e:
            log.warning("WatchVacuumChambersPressure stream dropped (%s); reconnecting in 5s.", e)
            time.sleep(5)


def poll_temperature(channel, cfg: Config, state: State, alerts: AlertManager, stop_event: threading.Event):
    stub = temperature_pb2_grpc.TemperatureServiceStub(channel)
    while not stop_event.is_set():
        try:
            level_resp = stub.GetDewarRefrigerantLevel(temperature_pb2.GetDewarRefrigerantLevelRequest())
            level_pct = level_resp.level * 100

            refill_resp = stub.GetTimeUntilDewarRefill(temperature_pb2.GetTimeUntilDewarRefillRequest())
            has_remaining = refill_resp.HasField("remaining_time")

            filling_resp = stub.IsFillingDewar(temperature_pb2.IsFillingDewarRequest())
            is_filling = filling_resp.is_filling

            log.debug(
                "dewar_level=%.0f%% remaining_time=%s is_filling=%s",
                level_pct, refill_resp.remaining_time if has_remaining else "n/a", is_filling,
            )

            if level_pct < cfg.dewar_min_percent:
                alerts.maybe_alert(
                    "dewar_level", "warning", "LN2 dewar level low",
                    f"LN2 dewar level is {level_pct:.0f}% (below {cfg.dewar_min_percent:g}% threshold).",
                )

            if has_remaining:
                remaining_minutes = refill_resp.remaining_time / 60.0
                if remaining_minutes < cfg.dewar_refill_warn_minutes:
                    alerts.maybe_alert(
                        "dewar_refill_due", "info", "LN2 refill due soon",
                        f"~{remaining_minutes:.0f} min until next required LN2 refill.",
                    )

            with state.lock:
                if is_filling:
                    if state.filling_since is None:
                        state.filling_since = time.time()
                    elapsed_minutes = (time.time() - state.filling_since) / 60.0
                else:
                    state.filling_since = None
                    elapsed_minutes = 0

            if is_filling and elapsed_minutes > cfg.dewar_filling_stuck_max_minutes:
                alerts.maybe_alert(
                    "dewar_filling_stuck", "critical", "LN2 dewar filling stuck",
                    f"LN2 dewar has been filling for {elapsed_minutes:.0f} min "
                    f"(> {cfg.dewar_filling_stuck_max_minutes:g} min threshold).",
                )

        except grpc.RpcError as e:
            log.warning("Temperature poll failed (%s); will retry.", e)

        stop_event.wait(cfg.poll_interval_seconds)


def main():
    parser = argparse.ArgumentParser(description="Watch Glacios 2 vacuum/LN2 via UTAPI and alert Slack.")
    parser.add_argument("--config", default="config_utapi.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Force dry-run regardless of config.")
    args = parser.parse_args()

    if not os.path.exists(args.config):
        sys.exit(f"Config file not found: {args.config}\nCopy config_utapi.example.yaml to config_utapi.yaml first.")

    cfg = Config.load(args.config)
    if args.dry_run:
        cfg.dry_run = True
    if not cfg.host:
        sys.exit("Please set 'host' in your config to the instrument PC's IP/hostname (UTAPI server).")

    state = State(cfg.state_file)
    notifier = SlackNotifier(cfg)
    alerts = AlertManager(cfg, state, notifier)

    log.info("Connecting to UTAPI server at %s:%s (dry_run=%s)", cfg.host, cfg.port, cfg.dry_run)
    channel = grpc.insecure_channel(f"{cfg.host}:{cfg.port}")

    stop_event = threading.Event()
    threads = [
        threading.Thread(target=watch_vacuum_state, args=(channel, alerts, stop_event), daemon=True),
        threading.Thread(target=watch_chamber_pressure, args=(channel, cfg, alerts, stop_event), daemon=True),
        threading.Thread(target=poll_temperature, args=(channel, cfg, state, alerts, stop_event), daemon=True),
    ]
    for t in threads:
        t.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutting down...")
        stop_event.set()
        state.save()


if __name__ == "__main__":
    main()
