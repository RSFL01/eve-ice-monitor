FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir .

# State file lives in /data so Railway can mount a persistent volume there
ENV ICE_STATE_FILE=/data/ice_monitor_state.json

RUN mkdir -p /data

CMD ice-monitor --system ${ICE_SYSTEM:-Riavayed} --interval ${ICE_INTERVAL:-300}
