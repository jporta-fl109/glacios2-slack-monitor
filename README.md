# Glacios 2 -> Slack monitor

This project has **three alternative approaches** to the same goal --
posting Slack alerts for Glacios 2 vacuum system and LN2/autoloader
issues.

- **`utapi/` (built and verified)** -- connects directly to Thermo's
  UTAPI gRPC server (`utapi_server.exe`, the "UtapiServer" Windows
  service, already confirmed running on your Glacios 2's control PC)
  using the actual `.proto` interface files from your instrument's
  UTAPI folder. This is the most direct, best-verified option in this
  project -- see "UTAPI version" below for exactly what was confirmed
  vs. still needs checking against the real hardware.

- **`tem_api_monitor.py` (alternative, less certain)** -- polls the
  instrument via the `pytemscript` Python library instead of talking
  gRPC directly. Kept here as a fallback, but `pytemscript`'s own docs
  flag its UTAPI support as "under development, currently not
  available" -- the `utapi/` version above is the better bet now that
  we have your instrument's real proto files. See "API version
  (pytemscript)" below.

- **`tem_monitor.py` (fallback)** -- tails the TEM interface log file
  and matches lines against regex rules. Useful only if gRPC/UTAPI
  access turns out not to be reachable from wherever this needs to
  run, or as a secondary safety net. See "Log-tailing version" below.

All three share the same Slack-posting logic (webhook or bot token)
and the same cooldown/dry-run design.

## UTAPI version (recommended)

This is in the `utapi/` folder. Unlike the other two approaches, this
one was built directly from your Glacios 2's actual UTAPI `.proto`
files (which you provided from `.../proto_files/API`) and **tested
end-to-end against a local stand-in gRPC server** implementing those
exact interfaces -- so the plumbing (connecting, streaming vacuum
state, streaming chamber pressure, polling LN2/dewar status, cooldown
suppression) is confirmed working, not guessed.

### What's confirmed vs. not yet verified

Confirmed, from your instrument's real `.proto` files and Thermo's own
example scripts (also in your Documentation folder):
- UTAPI server listens on **port 46699**, plain gRPC with **no
  TLS/auth** (`grpc.insecure_channel`) -- keep this on your internal
  network only, same caveat as Thermo's own examples give.
- `VacuumService.WatchVacuumState` -- streams vacuum state
  (`VACUUM_STATE_READY` / `NOT_READY` / `BUSY` / `UNDEFINED`).
- `VacuumChambersService.WatchVacuumChambersPressure` -- streams
  per-chamber pressure in Pa.
- `TemperatureService.GetDewarRefrigerantLevel` -- LN2 dewar level as
  a 0-1 fraction (confirmed comment: "up to 5% imprecision").
- `TemperatureService.GetTimeUntilDewarRefill` -- estimated seconds
  until refill needed (an optional field -- unset if no refill is
  expected, e.g. autoloader at room temperature).
- `TemperatureService.IsFillingDewar` -- whether it's currently
  filling (used here to detect a stuck/failed autofill by timing how
  long it stays true).

Not yet verified against your actual Glacios 2 (only against a fake
server standing in for it):
- Real-world vacuum pressure baselines, to tune
  `max_chamber_pressure_pa` sensibly for your instrument.
- Whether `GetTimeUntilDewarRefill`'s `remaining_time` is actually
  populated under normal operation on your system, or whether it's
  more often unset.
- Whether the licensing/version requirements (TEM server 7.18+ per
  pytemscript's docs, 7.22+ per a SerialEM facility note) are met --
  if `utapi_server.exe` is running at all, this is probably fine, but
  worth knowing if you hit connection errors.
- No dedicated proto for the autoloader *itself* was found among
  acquisition/column/energy_filter/optics/sample/shutter/vacuum --
  LN2 monitoring is entirely through `TemperatureService`, which
  appears to be the right (and only) source for it.

### Setup

```bash
cd utapi
pip install -r requirements_utapi.txt
python compile_protos.py --api-dir "/path/to/Thermo UTAPI/API"
cp config_utapi.example.yaml config_utapi.yaml
# edit config_utapi.yaml: set host to the instrument PC's IP
python tem_utapi_monitor.py --config config_utapi.yaml --dry-run
```

`--api-dir` should point at Thermo's actual API folder (the one with
`vacuum/`, `sample/`, `utapi_types/`, etc.) -- not a copy bundled into
this project, since those `.proto` files are Thermo's confidential
material. `compile_protos.py` only compiles the handful this monitor
actually uses and writes the generated stubs to `utapi/generated/`
(not checked into version control).

This script runs all three watches concurrently (vacuum state and
chamber pressure as live streams, LN2/dewar as a poll loop since UTAPI
has no streaming RPC for temperature) and posts to Slack the same way
as the other two scripts in this project.

### Deployment

Same systemd/Task Scheduler pattern as the other scripts -- point the
service/task at `utapi/tem_utapi_monitor.py --config config_utapi.yaml`.
This can run on the instrument PC itself, or on another machine that
can reach it on port 46699 -- gRPC doesn't require running on the same
box, just network access to it.

## API version (pytemscript)

`tem_api_monitor.py` connects to `pytemscript-server` (bundled with
the `pytemscript` package) over a socket and polls a set of **checks**
defined in `config_api.yaml` -- vacuum status, vacuum gauge pressures,
autoloader availability, LN2 dewar level, a stuck-fill timer, and time
remaining until the next required refill.

### Setup

1. **Confirm licensing.** Ask your Thermo FSE whether Standard/Advanced
   Scripting or UTAPI is enabled on your Glacios 2. There's no way to
   check this from software ahead of time.
2. **On the instrument control PC**, install `pytemscript` and run its
   bundled server so the COM interface is reachable over the network:
   ```
   pip install pytemscript
   pytemscript-server            # defaults to port 39000
   ```
   Keep this restricted to your internal network -- the server has no
   built-in authentication, so don't expose this port beyond your lab
   network/firewall.
3. **Wherever you want the monitor to run** (the instrument PC itself,
   or another machine on the same network):
   ```
   cp config_api.example.yaml config_api.yaml
   # edit config_api.yaml: set host/port to point at pytemscript-server
   pip install -r requirements_api.txt
   python tem_api_monitor.py --config config_api.yaml --once
   ```

### Verifying the API checks

I wrote the checks in `tem_api_monitor.py` (`check_vacuum_status`,
`check_dewar_level`, etc.) against pytemscript's publicly documented
API shape, but **I have not verified the exact attribute/method names
against a real Glacios 2** -- these can vary by TEM server version and
by which scripting interface is active. Each check is wrapped so a
wrong attribute name logs a warning instead of crashing the process;
the first time you run this for real, watch for lines like:

```
Check 'vacuum_status' (vacuum_status) raised AttributeError: ...
```

That tells you exactly which function in `tem_api_monitor.py` needs
its attribute path adjusted for your pytemscript version -- check
https://pytemscript.readthedocs.io/ for your installed version, or
run a quick interactive session against the real microscope:

```python
from pytemscript.microscope import Microscope
mic = Microscope(connection="socket", host="localhost", port=39000)
print(mic.vacuum.status)
print(mic.temperature.dewar_level())
```

Send me what the real attributes/values look like and I'll tighten
the check functions to match.

### Deployment

Same systemd/Task Scheduler pattern as the log-tailing version applies
-- just point the service/task at `tem_api_monitor.py --config
config_api.yaml` instead. If the monitor runs on a different machine
than `pytemscript-server`, that machine just needs `requirements_api.txt`
installed and network access to the instrument PC's port 39000.

## Log-tailing version (fallback)

## How it works

`tem_monitor.py` tails a log file (polls it every few seconds for new
lines), tests each new line against a list of regex rules in
`config.yaml`, and posts a formatted message to Slack when a rule
matches -- with a per-rule cooldown so a flapping fault doesn't flood
the channel. Read position and cooldown state are saved to a small
JSON file so restarts don't replay the whole log or reset cooldowns.

It's a single long-running process, not a Slack app -- it only sends
messages out (via an Incoming Webhook or a bot token), it doesn't need
to receive Slack events. That keeps the Slack-side setup to about two
minutes whenever you're ready for it.

## What you still need to figure out

This was built without a real Glacios 2 log sample, so treat the
regex rules in `config.example.yaml` as a reasonable starting guess
based on common Thermo Fisher TEM interface terminology, not a
tested fit. Before relying on this:

1. **Find the actual log file(s) and format.** On most Thermo Fisher
   TEM installs, the interface software (TUI / TEM Server / Advanced
   user software, depending on your version) writes logs somewhere
   under the instrument PC's install directory or a configured log
   folder -- ask your facility/service engineer or check the
   software's settings/about screen for a "log location". Confirm
   whether it's a single file that grows forever, or one that rotates
   (new file per day, or renamed when it hits a size limit).

2. **Grab a handful of real log lines** that cover: a normal vacuum
   pump cycle, a vacuum fault (if you have one on record, or can
   trigger a benign test condition), a normal LN2/autoloader fill,
   and a failed or low-level LN2 event. Send those to me (with any
   instrument identifiers redacted if you'd rather not share them) and
   I'll tighten the regex rules in `config.yaml` to match the real
   wording and avoid false positives/negatives.

3. **Decide where this runs.** It needs to run on a machine that can
   read the log file continuously -- typically the instrument PC
   itself, or a lab server with the log folder mounted/shared. See
   "Deployment" below for systemd (Linux) and Task Scheduler (Windows)
   setups.

## Dry run (no Slack needed yet)

`config.example.yaml` has `dry_run: true` by default. In this mode,
alerts are printed to the console/log instead of sent to Slack, so you
can validate the rules against a real log file today:

```bash
cp config.example.yaml config.yaml
# edit config.yaml: set log_path to the real (or a copied/sample) log file
pip install -r requirements.txt
python tem_monitor.py --config config.yaml --once
```

`--once` processes whatever's new since the last run and exits, which
is convenient for iterating on the rules. Drop `--once` to have it
keep polling.

There's also a synthetic sample log at `sample_logs/example.log` you
can point `log_path` at immediately to see the monitor work end to end
before you have a real log file.

## Turning on Slack (once you have a channel)

1. Create the Slack channel (e.g. `#glacios2-alerts`).
2. Easiest option -- Incoming Webhook:
   - In Slack, go to https://api.slack.com/apps, create an app (or use
     an existing one), enable "Incoming Webhooks", and add one for
     that channel. Copy the webhook URL.
   - Put it in `config.yaml` as `slack_webhook_url`, or set the
     `SLACK_WEBHOOK_URL` environment variable instead (recommended, so
     the secret isn't sitting in a file).
3. Set `dry_run: false` in `config.yaml`.
4. Run `python tem_monitor.py --config config.yaml --once` again and
   confirm the alert shows up in Slack.

A bot-token option (`slack_bot_token` + `slack_channel`) is also
supported if you'd rather use `chat.postMessage` -- useful if this bot
grows into something that also reacts to messages later.

## Tuning rules against real logs

Each rule in `config.yaml` is:

```yaml
- name: vacuum_fault
  pattern: '(vacuum).*(fault|fail|error|interlock|trip|out of range|too high|too low)'
  severity: critical        # critical | warning | info -- affects the emoji in Slack
  cooldown_minutes: 15       # suppress repeat alerts from this rule for N minutes
```

Rules are checked top to bottom and the **first match wins** per line,
so put more specific/urgent rules above more general catch-alls (the
defaults already do this: specific vacuum/LN2 rules before the generic
`general_error` / `general_warning` rules).

Tips once you have real samples:
- If a line you'd expect to trigger doesn't, the wording probably
  differs from the guessed pattern -- loosen it or add a second rule.
- If you get false positives (e.g. a routine "vacuum OK" status line
  matching `vacuum_warning`), narrow the pattern or add a negative
  lookahead, e.g. `(?!.*\bOK\b)`.
- Keep `general_error` / `general_warning` as a safety net for
  anything the specific rules miss, but expect it to be noisier --
  that's why its cooldown is longer.

## Deployment

### Linux (systemd)

See `systemd/tem-monitor.service`. Typical install:

```bash
sudo mkdir -p /opt/glacios2-slack-monitor
sudo cp -r . /opt/glacios2-slack-monitor
cd /opt/glacios2-slack-monitor
python3 -m venv venv
venv/bin/pip install -r requirements.txt
# edit config.yaml, then:
sudo cp systemd/tem-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tem-monitor
sudo systemctl status tem-monitor
journalctl -u tem-monitor -f   # tail its logs
```

### Windows (Task Scheduler)

See `windows/run_tem_monitor.ps1` for setup steps in the comment
header -- it wraps `tem_monitor.py` in a restart loop and is meant to
be launched by a Task Scheduler task set to run at startup.

## Files in this project

```
utapi/tem_utapi_monitor.py       UTAPI version (recommended): direct gRPC, verified end-to-end
utapi/compile_protos.py          compiles Thermo's .proto files into Python stubs
utapi/config_utapi.example.yaml  copy to config_utapi.yaml and edit
utapi/requirements_utapi.txt     pip dependencies (adds grpcio, grpcio-tools)

tem_api_monitor.py          API version (pytemscript): polls pytemscript-server, posts to Slack
config_api.example.yaml     copy to config_api.yaml and edit
requirements_api.txt        pip dependencies for the API version (adds pytemscript)

tem_monitor.py              log-tailing version: regex rules, posts to Slack
config.example.yaml         copy to config.yaml and edit
requirements.txt            pip dependencies for the log-tailing version
sample_logs/example.log     synthetic log for testing before you have a real one

systemd/tem-monitor.service   Linux service unit (edit ExecStart for whichever script you deploy)
windows/run_tem_monitor.ps1   Windows Task Scheduler launcher (edit for whichever script you deploy)
```

## Not covered yet

- Escalation beyond Slack (paging, email) -- straightforward to add
  as another notifier if needed once the channel exists.
- A "heartbeat" alert if the log file stops updating entirely (could
  indicate the instrument software crashed rather than a vacuum/LN2
  issue) -- worth adding once we know how chatty the real log is
  during normal operation, to set a sensible "no activity" threshold.
- Parsing structured fields (timestamps, error codes) out of each line
  for richer Slack messages -- depends on the real log format.
