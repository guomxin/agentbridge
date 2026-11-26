"""One bounded getUpdates read. Only the owning TelegramPilot processes its page and offset."""
from concurrent.futures import ThreadPoolExecutor
import time


class PiUpdateReader:
    def __init__(self, fetch, *, interrupt=None):
        self.fetch = fetch
        self.interrupt = interrupt
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pi-get-updates")
        self.pending = None
        self.retry_at = 0.0

    def poll(self, **query):
        from .telegram import TelegramUnavailable

        if self.pending is None:
            if time.monotonic() >= self.retry_at:
                self.pending = self.executor.submit(self.fetch, **query)
            return []
        if not self.pending.done():
            return []
        future, self.pending = self.pending, None
        try:
            page = future.result()
            if not isinstance(page, list):
                raise TelegramUnavailable("Update list invalid")
            return page
        except TelegramUnavailable:
            # A broken receive connection must not stop the owner's outbound work.
            self.retry_at = time.monotonic() + 2
            return []

    def close(self):
        if self.interrupt:
            self.interrupt()
        self.executor.shutdown(wait=True, cancel_futures=True)
