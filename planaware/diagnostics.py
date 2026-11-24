"""Log operation failures using only trusted error classifications."""

import builtins
import logging
import re


def log_failure(logger, level, event, error, *, expected_types=()):
    """Log a fixed event with a trusted exception name and known SQLSTATE."""
    if not logger.isEnabledFor(level):
        return

    error_type = type(error)
    name = error_type.__name__
    trusted_type = error_type in expected_types or getattr(builtins, name, None) is error_type
    sqlstate = getattr(error, "sqlstate", None)
    safe_sqlstate = None
    if not trusted_type or sqlstate is not None:
        try:
            from psycopg import errors
        except ImportError:
            pass
        else:
            trusted_type = trusted_type or getattr(errors, name, None) is error_type
            if type(sqlstate) is str and re.fullmatch(r"[0-9A-Z]{5}", sqlstate):
                try:
                    errors.lookup(sqlstate)
                except KeyError:
                    pass
                else:
                    safe_sqlstate = sqlstate

    name = name if trusted_type else "Error"
    if safe_sqlstate is None:
        logger.log(level, "%s error_type=%s", event, name)
    else:
        logger.log(level, "%s error_type=%s sqlstate=%s", event, name, safe_sqlstate)
