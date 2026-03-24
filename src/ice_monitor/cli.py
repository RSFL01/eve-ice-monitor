from __future__ import annotations

import argparse
import logging
import time

from .config import load_config
from .monitor import IceMonitor



def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="EVE ice monitor")
    parser.add_argument("--system", default="Riavayed", help="Solar system to monitor")
    parser.add_argument("--interval", type=int, default=300, help="Poll interval in seconds")
    parser.add_argument("--once", action="store_true", help="Run a single poll cycle")
    parser.add_argument("--log-level", default="INFO", help="Logging level (DEBUG, INFO, WARNING)")
    parser.add_argument("-T", "--test", action="store_true", help="Send a test message to the Discord webhook and exit")
    parser.add_argument("--login", action="store_true", help="Authenticate with EVE SSO and save tokens")
    parser.add_argument("--bot", action="store_true", help="Run the Discord bot listener")
    return parser.parse_args()



def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    config = load_config()

    if args.bot:
        import os
        from .bot import run_bot
        token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
        if not token:
            print("Error: DISCORD_BOT_TOKEN must be set")
            return 1
        run_bot(token, config.state_file, config.respawn_hours)
        return 0

    if args.login:
        from .auth import do_login
        if not config.esi_client_id or not config.esi_client_secret:
            print("Error: ESI_CLIENT_ID and ESI_CLIENT_SECRET must be set")
            return 1
        token_data = do_login(config.esi_client_id, config.esi_client_secret, config.esi_token_file)
        print(f"Logged in as: {token_data.character_name} (ID: {token_data.character_id})")
        return 0

    if args.test:
        from .discord import send_discord_alert
        send_discord_alert(config.discord_webhook_url, "Test Message", "Ice monitor is configured and working.")
        return 0

    monitor = IceMonitor(system_name=args.system, config=config)

    if args.once:
        monitor.run_once()
        return 0

    while True:
        monitor.run_once()
        time.sleep(max(5, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
