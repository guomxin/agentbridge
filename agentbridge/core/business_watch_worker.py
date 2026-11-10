"""A separate bounded observation worker; governance and front-end loops stay free."""
import logging
import threading

_LOG=logging.getLogger(__name__)


class BusinessWatchWorker:
    def __init__(self,service,*,interval_seconds=60,initial_delay_seconds=10):
        self.service=service
        self.interval_seconds=max(float(interval_seconds),15)
        self.initial_delay_seconds=max(float(initial_delay_seconds),0)
        self._stop_event=threading.Event()
        self._thread=None

    def start(self):
        if self._thread is not None:
            raise RuntimeError('business watch worker already started')
        self._thread=threading.Thread(target=self._run,name='agentbridge-business-watch',daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=20)

    def run_cycle(self):
        return self.service.business_watch.run_due_checks(limit=4,should_stop=self._stop_event.is_set)

    def _run(self):
        if self._stop_event.wait(self.initial_delay_seconds): return
        while not self._stop_event.is_set():
            try:
                result=self.run_cycle()
                if result['claimed']:
                    _LOG.info('Business watch cycle checked=%d changed=%d paused=%d failed=%d discarded=%d',
                        result['checked'],result['changed'],result['paused'],result['failed'],result['discarded'])
            except Exception as exc:
                _LOG.warning('Business watch cycle deferred: %s',type(exc).__name__)
            if self._stop_event.wait(self.interval_seconds): return
