"""runner process. Not implemented yet: built in Phase 5 of docs/DEVELOPMENT_PLAN.md."""
import logging
import time

log = logging.getLogger("runner")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    log.warning("runner is a placeholder until Phase 5; idling")
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
