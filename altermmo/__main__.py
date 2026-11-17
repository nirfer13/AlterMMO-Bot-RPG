"""Entry point: ``python -m altermmo``."""

from __future__ import annotations

import logging
import sys

from .bot import AlterBot
from .logging_setup import setup_logging
from .settings import load_settings


def main() -> int:
    settings = load_settings()
    setup_logging(settings.log_dir, debug=settings.debug)
    log = logging.getLogger("altermmo")

    if not settings.token:
        log.error("Brak tokenu bota: ustaw TOKEN w pliku .env (patrz .env.example).")
        return 2
    if not settings.database_url:
        log.error("Brak adresu bazy danych: ustaw DATABASE_URL w pliku .env "
                  "(w trybie debug: HEROKU_POSTGRESQL_BRONZE_URL).")
        return 2

    log.info("Starting AlterMMO RPG bot (debug=%s, data=%s).", settings.debug, settings.data_dir)
    bot = AlterBot(settings)
    # log_handler=None: logging is already configured by setup_logging().
    bot.run(settings.token, log_handler=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
