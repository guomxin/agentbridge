"""Stop and reap only a fixture-owned subprocess."""


def kill_fixture_process(process):
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)
