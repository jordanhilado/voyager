import logging

from .base import Backend, Observation, TargetInfo  # noqa: F401

log = logging.getLogger("voyager.backend")


def make_backend(settings) -> Backend:
    """The backend for this machine: native accessibility on macOS. Windows is not implemented yet."""
    from ..config import IS_MAC

    if not IS_MAC:
        raise RuntimeError("Voyager currently runs on macOS only; Windows support is pending")
    try:
        from .macnative import MacNativeBackend, native_available

        ok, why = native_available()
    except Exception as e:  # noqa: BLE001
        ok, why = False, f"pyobjc unavailable ({e})"
    if not ok:
        raise RuntimeError(f"native macOS backend unavailable: {why}")
    log.info("backend: native macOS accessibility")
    return MacNativeBackend(settings)
