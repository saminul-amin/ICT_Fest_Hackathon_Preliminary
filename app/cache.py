"""Consistency-first cache hooks for reporting endpoints.

The hackathon contract requires reports and availability to reflect the current
database state immediately. These helpers intentionally behave as no-ops so the
routes can keep a small abstraction without serving stale API responses.
"""

_report_cache: dict[tuple, dict] = {}
_availability_cache: dict[tuple, dict] = {}


def get_report(org_id: int, frm: str, to: str):
    return None


def set_report(org_id: int, frm: str, to: str, value: dict) -> None:
    return None


def invalidate_report(org_id: int) -> None:
    for key in [k for k in _report_cache if k[0] == org_id]:
        _report_cache.pop(key, None)


def get_availability(room_id: int, date: str):
    return None


def set_availability(room_id: int, date: str, value: dict) -> None:
    return None


def invalidate_availability(room_id: int, date: str) -> None:
    _availability_cache.pop((room_id, date), None)
