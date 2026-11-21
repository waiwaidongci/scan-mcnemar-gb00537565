import logging
import time
from pathlib import Path
from src.services.execution import claim, execute, materialize_due
from src.services.sequential import claim_look, execute_look
from src.utils.wakeup import wait

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
heartbeat = Path('/tmp/analysis-heartbeat')


def main():
    while True:
        try:
            materialize_due()
            lease = claim()
            look = claim_look()
            heartbeat.touch()
            if lease:
                execute(lease)
                heartbeat.touch()
            if look:
                execute_look(look)
                heartbeat.touch()
            if not lease and not look:
                wait()
        except Exception:
            log.exception('Worker cycle failed')
            time.sleep(2)


if __name__ == '__main__':
    main()
