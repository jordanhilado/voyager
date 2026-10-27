import logging

from .base import Backend, Observation, TargetInfo  # noqa: F401

log = logging.getLogger("voyager.backend")


def make_backend(settings) -> Backend:
    """The backend for this machine: native accessibility on macOS, UI Automation on Windows."""
    from ..config import IS_MAC, IS_WINDOWS

    if IS_WINDOWS:
        return _windows(settings)
    if not IS_MAC:
        raise RuntimeError("Voyager runs on macOS and Windows; this platform is not supported")
    try:
        from .macnative import MacNativeBackend, native_available

        ok, why = native_available()
    except Exception as e:  # noqa: BLE001
        ok, why = False, f"pyobjc unavailable ({e})"
    if not ok:
        raise RuntimeError(f"native macOS backend unavailable: {why}")
    log.info("backend: native macOS accessibility")
    return MacNativeBackend(settings)


def _windows(settings) -> Backend:
    try:
        from . import winwin

        winwin.set_dpi_aware()  # before anything reads a coordinate
        from .winnative import WinNativeBackend, native_available

        ok, why = native_available()
    except Exception as e:  # noqa: BLE001
        ok, why = False, f"comtypes/pywin32 unavailable ({e})"
    if not ok:
        raise RuntimeError(f"native Windows backend unavailable: {why}")
    log.info("backend: native Windows UI Automation")
    return WinNativeBackend(settings)
