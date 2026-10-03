"""Process-local drain and latched retirement of legacy DERIVED writers only.

Raw/source ingestion does not enter this gate. Failure never re-enables writers.
"""

from contextlib import contextmanager
from pathlib import Path

from .runtime import LakeError
from .serving import _SELECTION_LOCK, check_selected, selected_config

_LOCK = _SELECTION_LOCK
_RETIRED = {}


def _token(config):
    return config


def activate_retirement(path):
    """Explicit activation/renewal; serialized behind any in-flight local writer."""
    with _LOCK:
        config = selected_config(path)
        if not config.retire_legacy_writers or config.backend != "ducklake":
            raise LakeError("lake_retirement_not_requested")
        check_selected(path)
        if selected_config(path) != config:
            raise LakeError("lake_retirement_renewal_required")
        _RETIRED[Path(path).resolve()] = _token(config)


@contextmanager
def legacy_derived_write(path):
    with _LOCK:
        config = selected_config(path)
        retired = _RETIRED.get(Path(path).resolve())
        if retired is not None and retired != _token(config):
            raise LakeError("lake_retirement_renewal_required")
        if config.retire_legacy_writers:
            if retired is None:
                raise LakeError("lake_retirement_not_activated")
            check_selected(path)
            yield False
        else:
            yield True


def fence_derived_writer(path_argument, retired_result):
    """Serialize the entire derived write, including its read of legacy state."""
    import functools
    import inspect

    def decorate(function):
        signature = inspect.signature(function)

        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            arguments = signature.bind(*args, **kwargs)
            arguments.apply_defaults()
            path = arguments.arguments[path_argument]
            if path is None:
                return function(*args, **kwargs)
            with legacy_derived_write(path) as allowed:
                if not allowed:
                    return retired_result()
                return function(*args, **kwargs)

        return wrapped

    return decorate
