"""All-action HTTP coverage keeps original authorization and evidence boundaries."""
import threading

from tests.support.action_matrix_acceptance import ActionMatrixAcceptanceFixture
from tests.support.latency_acceptance import JsonHttpClient
from tests.support.latency_action_matrix import MatrixHttpWorkload
from tests.test_workspace import PASSWORD


def test_all_28_actions_and_both_batches_use_real_http_cards_and_workspace(tmp_path):
    fixture = ActionMatrixAcceptanceFixture(tmp_path, seed=False)
    servers = fixture.servers()
    threads = []
    for server in servers:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        threads.append(thread)
    try:
        report = MatrixHttpWorkload(fixture, JsonHttpClient, PASSWORD).run("all")
        assert report["uniqueActions"] == 28
        assert len(report["rows"]) == 44
        assert report["writes"] == 44
        assert report["decisions"] == 41
        assert len([row for row in report["rows"] if "synthetic OA prepare/commit boundary" in row["evidenceBoundary"]]) == 8
        assert all(row["status"] == "succeeded" for row in report["rows"])
    finally:
        for server, thread in zip(servers, threads):
            server.shutdown()
            server.server_close()
            thread.join(2)
        fixture.close()
