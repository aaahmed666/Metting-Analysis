"""
Module: Logging Configuration
Purpose: Single, idempotent entry point for configuring application-wide logging.
         Call configure_logging() once at process startup (API and worker) so
         every `logging.getLogger(__name__)` in the codebase emits consistently
         formatted, level-controlled output instead of relying on the root
         logger's defaults.
"""
import logging

from config.setting import get_settings

_CONFIGURED = False
_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def configure_logging() -> None:
    """Configure root logging from settings. Safe to call more than once."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    logging.basicConfig(
        level=get_settings().LOG_LEVEL,
        format=_FORMAT,
    )
    _CONFIGURED = True
