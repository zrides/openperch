"""Cooperative cancellation shared by every long-running stage."""


class Cancelled(Exception):
    pass


def check_cancel(event):
    if event.is_set():
        raise Cancelled()
