# Ice Monitor

A Python CLI monitor for EVE Online ice belt activity in a target system, with optional Discord webhook notifications.

## Features
- Resolves solar system ID from system name via ESI.
- Polls system jump/kill activity from public ESI endpoints.
- Maintains a persisted baseline and respawn estimate in `ice_monitor_state.json`.
- Sends Discord alerts for likely spawn signals and upcoming respawn windows.

## Quick Start
1. Create a virtual environment and activate it.
2. Install dependencies:

```bash
pip install -e .
```

3. Copy `.env.example` to `.env` and update values.
4. Run monitor:

```bash
ice-monitor --system Riavayed --interval 300
```

## Optional Test Run
```bash
pytest
```

## Notes
- ESI does not expose direct public anomaly listings, so this monitor uses activity heuristics.
- Keep thresholds conservative to avoid alert noise.
