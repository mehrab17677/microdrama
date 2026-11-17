"""poller process. Not implemented yet: built in Phase 2 of docs/DEVELOPMENT_PLAN.md."""
import logging
import time

log = logging.getLogger("poller")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    log.warning("poller is a placeholder until Phase 2; idling")
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
