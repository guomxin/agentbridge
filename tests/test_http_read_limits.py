import io
import threading
import unittest
from unittest.mock import patch
import zlib
from agentbridge.browser.http import CentralHttpWorker
from agentbridge.core.capability_runtime import CapabilityRejected

class Assertions(unittest.TestCase):
    def error(self, code, function):
        with self.assertRaises(CapabilityRejected) as raised: function()
        self.assertEqual(raised.exception.code, code)

class HttpBoundTests(Assertions):
    def response(self, body, encoding="identity"):
        stream = io.BytesIO(body)
        stream.status = 200
        stream.headers = {"Content-Type":"application/json", "Content-Encoding":encoding}
        stream.geturl = lambda:"http://example.test/api"
        return stream

    def test_stream_and_compression_limits(self):
        worker = CentralHttpWorker(allowed_origins={"http://example.test"})
        for body,encoding in ((b"x"*101,"identity"),(zlib.compress(b"x"*1000),"deflate")):
            stream = self.response(body,encoding)
            with patch.object(worker,"_open",return_value=stream):
                self.error("RESULT_INCOMPLETE", lambda:worker.request("GET","http://example.test/api",max_response_bytes=100))
            self.assertTrue(stream.closed)

    def test_bounded_json_and_cancel(self):
        worker = CentralHttpWorker(allowed_origins={"http://example.test"})
        with patch.object(worker,"_open",return_value=self.response(b'{"ok":true}')):
            self.assertEqual(worker.request("GET","http://example.test/api",max_response_bytes=100)["json"],{"ok":True})
        canceled = threading.Event(); canceled.set()
        with patch.object(worker,"_open",return_value=self.response(b'{}')):
            self.error("INTERRUPTED", lambda:worker.request("GET","http://example.test/api",max_response_bytes=100,cancellation=canceled))
