"""Classify transient document replacement errors for bounded read retries."""


def is_navigation_race(exc):
    # Only transient read/navigation failures are retryable. Authentication,
    # closed targets, permission errors and programming errors must propagate.
    message = str(exc).lower()
    return any(marker in message for marker in (
        "frame was detached", "frame has been detached", "execution context was destroyed",
        "cannot find context with specified id",
        "unable to retrieve content because the page is navigating",
    ))

