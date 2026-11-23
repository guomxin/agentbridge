"""Keep provider diagnostics outside SDK text, history and Workspace events."""
from nanobot.providers.factory import make_provider


class CheckedProvider:
    def __init__(self, config):
        self.provider = make_provider(config)
        self.failed = False

    def __getattr__(self, name):
        return getattr(self.provider, name)

    async def chat_stream_with_retry(self, **kwargs):
        try:
            response = await self.provider.chat_stream_with_retry(**kwargs)
        except Exception:
            self.failed = True
            raise RuntimeError("Model provider unavailable") from None
        if response.finish_reason == "error":
            self.failed = True
            # The upstream runner otherwise uses response.content as its assistant reply.
            raise RuntimeError("Model provider unavailable")
        return response

    def result(self, run_result):
        if self.failed or run_result.error or run_result.stop_reason=="error":
            raise RuntimeError("Model provider unavailable")
        return run_result.content
