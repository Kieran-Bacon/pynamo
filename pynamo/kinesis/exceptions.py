class HandlerNotFoundError(Exception):
    """No registered model and event pair accepts a change record."""


class UnhandledUserCodeException(Exception):
    """A registered handler raised while `raise_on_error` was set."""


class LambdaTimeoutException(Exception):
    """The Lambda invocation is about to time out."""
