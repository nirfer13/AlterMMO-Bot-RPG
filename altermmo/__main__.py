"""Entry point: ``python -m altermmo``.

Exit codes (``run_bot.bat`` restarts the bot after any exit EXCEPT 2 and 3):

``0`` / ``1``  the bot stopped or crashed - starting it again makes sense;
``2``          the configuration is incomplete (no token / no database address);
``3``          Discord refuses the login (wrong token, or a privileged intent is
               switched off in the Developer Portal).  Retrying cannot help, and
               logging in every 15 seconds for hours would make Discord reset the token.
"""

from __future__ import annotations

import logging
import sys

import discord

from .bot import AlterBot
from .logging_setup import setup_logging
from .settings import PROJECT_ROOT, load_settings  # noqa: F401 - PROJECT_ROOT: where run_bot.bat lives


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
    try:
        # log_handler=None: logging is already configured by setup_logging().
        bot.run(settings.token, log_handler=None)
    except discord.PrivilegedIntentsRequired:
        log.error("Discord odrzucił połączenie: w Developer Portal (aplikacja bota -> Bot -> Privileged "
                  "Gateway Intents) włącz MESSAGE CONTENT INTENT oraz SERVER MEMBERS INTENT, zapisz "
                  "i uruchom bota ponownie. Bot NIE będzie wznawiany automatycznie.")
        return 3
    except discord.LoginFailure:
        log.error("Discord odrzucił token bota (TOKEN w pliku .env jest błędny albo został zresetowany). "
                  "Bot NIE będzie wznawiany automatycznie.")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
