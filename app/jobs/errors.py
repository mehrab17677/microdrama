class TransientError(Exception):
    """HTTP 429, 5xx or a timeout: the worker retries in place with backoff."""


class PermanentError(Exception):
    """The attempt failed. The repair logic (guide section 11) decides what happens next."""
