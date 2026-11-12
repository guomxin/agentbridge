"""Run due Workspace OA digests without depending on an open page."""

import logging
import threading


_LOG = logging.getLogger(__name__)


class OADigestWorker:
    def __init__(self, settings):
        self.settings = settings
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, name="agentbridge-oa-digest", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stopped.set()
        self._thread.join(timeout=20)

    def _run(self):
        while not self._stopped.wait(15):
            try:
                result = self.settings.run_due(limit=4, should_stop=self._stopped.is_set)
                if result["claimed"]:
                    _LOG.info(
                        "OA digest cycle published=%d partial=%d reused=%d failed=%d discarded=%d",
                        result["published"], result["partial"], result["reused"],
                        result["failed"], result["discarded"],
                    )
            except Exception as exc:
                _LOG.warning("OA digest scheduler deferred: %s", type(exc).__name__)
