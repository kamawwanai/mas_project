"""Process-local network prohibition, inherited by project CLI subprocesses via env."""
import os
import sys

_installed = False


class NetworkDisabled(RuntimeError):
    pass


def deny_network():
    global _installed
    os.environ["TRIP_OFFLINE"] = "1"
    if _installed:
        return
    def guard(event, args):
        if event.startswith("socket.") and event in {
            "socket.__new__", "socket.connect", "socket.getaddrinfo", "socket.gethostbyname",
            "socket.gethostbyaddr", "socket.bind", "socket.sendto"}:
            raise NetworkDisabled("Network disabled during local validation: " + event)
    sys.addaudithook(guard)
    _installed = True
