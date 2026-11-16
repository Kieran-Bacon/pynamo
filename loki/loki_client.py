import logging
import queue
import time
import typing
from functools import wraps
from typing import Callable, Dict, List, Optional, Union

from logging_loki import LokiHandler, LokiQueueHandler

internal_logger = logging.getLogger(__name__)

lambda_cold_start = True


class LokiClient:

    logger = internal_logger

    def __init__(
        self,
        app_name: str,
        env: str,
        level: str,
        *,
        stack: Optional[str] = None,
        loggers: Optional[List[logging.Logger]] = None,
        url: Optional[str] = None,
        loki: Optional[LokiHandler] = None,
        tags: Optional[Dict] = None,
        version: str = "1",
        format: Optional[logging.Formatter] = None,
    ):

        self.stack = stack
        self.url = url
        self.tags = {"app": app_name, "env": env, "stack": self.stack}



        if tags is not None:
            self.tags.update(tags)

        self._loggers = []

        if loki is None:

            if url is None:
                raise ValueError("URL must be set if no loki handler is supplied")

            loki_args = {"url": url, "tags": self.tags, "version": version}

            loki = LokiHandler(**loki_args)

        # loki.setFormatter(logging.Formatter("%(asctime)s::%(name)-30s::%(levelname)s::%(request_id)s %(user_id)s %(message)s"))

        log_prefix = f"{stack}::{app_name}" if stack else app_name
        loki.setFormatter(
            logging.Formatter("%(levelname)s: " + log_prefix + "::%(module)s - %(message)s")
            if format is None
            else format
        )
        loki.setLevel(level)

        self.level = level
        self.loki = loki

        self.add(internal_logger)  # register our own internal logger
        if loggers is not None:
            for logger in loggers:
                self.add(logger)

        return

    def add(self, logger: Union[str, logging.Logger]):

        if isinstance(logger, str):
            logger = logging.getLogger(logger)

        logger.setLevel(self.level)
        logger.addHandler(self.loki)

        self._loggers.append(logger)

        return

    def lambda_handler(self):
        def wrapper(func: Callable):
            @wraps(func)
            def wrapped(event, lambda_context):
                global lambda_cold_start

                # this is a bit ugly but there you go....
                self.loki.metadata["lambda_request_id"] = lambda_context.aws_request_id
                self.loki.metadata["lambda_cold_start"] = lambda_cold_start

                lambda_cold_start = False

                internal_logger.info(
                        "Lambda invocation started",
                    )

                internal_logger.debug("Lambda event: \n%s", event)

                try:
                    started_timestamp = time.time()
                    result = func(event, lambda_context)
                    completed_timestamp = time.time()

                    internal_logger.info(
                        "Lambda invocation completed - time_taken=%s",
                        completed_timestamp - started_timestamp,
                    )

                    return result

                except:
                    internal_logger.exception(
                        "Invocation failed - lambda handler experienced an unhandled exception and has terminated"
                    )
                    raise

                finally:
                    self.loki.flush()

            return wrapped

        return wrapper
