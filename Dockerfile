FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir .

ENV ICE_STATE_FILE=/data/ice_monitor_state.json
ENV ESI_TOKEN_FILE=/data/esi_tokens.json

VOLUME ["/data"]

CMD ice-monitor --system ${ICE_SYSTEM:-Riavayed} --interval ${ICE_INTERVAL:-300}
