import json
import logging
import re
from datetime import datetime, timezone


class SafeJSONFormatter(logging.Formatter):
    def __init__(self, token: str = ""):
        super().__init__()
        self.token = token

    def format(self, record):
        message = record.getMessage()
        if self.token:
            message = message.replace(self.token, "[REDACTED]")
        message = re.sub(r"(?i)(authorization|token)[=: ]+\S+", r"\1=[REDACTED]", message)
        # Do not format exception messages/tracebacks: HTTP errors can contain private payloads.
        return json.dumps({"at": datetime.now(timezone.utc).isoformat(), "level": record.levelname,
                           "logger": record.name, "event": message}, ensure_ascii=False)


def configure_logging(level: str, token: str = ""):
    handler = logging.StreamHandler()
    handler.setFormatter(SafeJSONFormatter(token))
    logging.basicConfig(level=level, handlers=[handler], force=True)
    logging.getLogger("discord").setLevel(logging.WARNING)
