"""Black-box contract tests for the core CoWork API behavior."""
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["DATABASE_URL"] = f"sqlite:///{(Path(tempfile.gettempdir()) / f'cowork_test_{uuid.uuid4().hex}.db').as_posix()}"
os.environ.setdefault("JWT_SECRET", "cowork-test-secret")

import jwt
from fastapi.testclient import TestClient

from app.config import JWT_ALGORITHM, JWT_SECRET
from app.main import app

client = TestClient(app)


def _future(hours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(
        minute=0, second=0, microsecond=0
    ).isoformat()


def _register(org: str, username: str, password: str = "pw12345") -> dict:
    response = client.post(
        "/auth/register",
        json={"org_name": org, "username": username, "password": password},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _login(org: str, username: str, password: str = "pw12345") -> dict:
    response = client.post(
        "/auth/login",
        json={"org_name": org, "username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_room(headers: dict, rate: int = 1001) -> int:
    response = client.post(
        "/rooms",
        json={"name": f"Focus {uuid.uuid4().hex[:6]}", "capacity": 4, "hourly_rate_cents": rate},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _create_booking(headers: dict, room_id: int, start: str, end: str) -> dict:
    response = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": start, "end_time": end},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_auth_registration_logout_and_refresh_contract():
    assert client.get("/health").json() == {"status": "ok"}

    org = f"auth-{uuid.uuid4().hex}"
    admin = _register(org, "alice")
    assert admin["role"] == "admin"

    duplicate = client.post(
        "/auth/register",
        json={"org_name": org, "username": "alice", "password": "pw12345"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "USERNAME_TAKEN"

    tokens = _login(org, "alice")
    access_payload = jwt.decode(tokens["access_token"], JWT_SECRET, algorithms=[JWT_ALGORITHM])
    assert access_payload["exp"] - access_payload["iat"] == 900

    refresh = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refresh.status_code == 200, refresh.text
    reused = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert reused.status_code == 401

    logout = client.post("/auth/logout", headers=_headers(tokens["access_token"]))
    assert logout.status_code == 200
    after_logout = client.get("/rooms", headers=_headers(tokens["access_token"]))
    assert after_logout.status_code == 401


def test_booking_window_conflict_pagination_detail_and_refund_contract():
    org = f"book-{uuid.uuid4().hex}"
    _register(org, "admin")
    _register(org, "member")
    admin_headers = _headers(_login(org, "admin")["access_token"])
    member_headers = _headers(_login(org, "member")["access_token"])
    room_id = _create_room(admin_headers)

    invalid = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": _future(3), "end_time": _future(3)},
        headers=member_headers,
    )
    assert invalid.status_code == 400
    assert invalid.json()["code"] == "INVALID_BOOKING_WINDOW"

    first = _create_booking(member_headers, room_id, _future(50), _future(51))
    assert first["price_cents"] == 1001

    back_to_back = _create_booking(member_headers, room_id, _future(51), _future(52))
    assert back_to_back["start_time"] > first["start_time"]

    conflict = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": _future(50), "end_time": _future(51)},
        headers=member_headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "ROOM_CONFLICT"

    listing = client.get("/bookings?page=1&limit=1", headers=member_headers)
    assert listing.status_code == 200
    assert listing.json()["items"][0]["id"] == first["id"]
    assert listing.json()["limit"] == 1

    detail = client.get(f"/bookings/{first['id']}", headers=member_headers)
    assert detail.status_code == 200
    assert detail.json()["start_time"] == first["start_time"]

    cancel = client.post(f"/bookings/{first['id']}/cancel", headers=member_headers)
    assert cancel.status_code == 200
    assert cancel.json()["refund_percent"] == 100
    assert cancel.json()["refund_amount_cents"] == 1001

    recancel = client.post(f"/bookings/{first['id']}/cancel", headers=member_headers)
    assert recancel.status_code == 409
    assert recancel.json()["code"] == "ALREADY_CANCELLED"


def test_multi_tenant_export_stats_and_availability_contract():
    org_a = f"orga-{uuid.uuid4().hex}"
    org_b = f"orgb-{uuid.uuid4().hex}"
    _register(org_a, "admin")
    _register(org_b, "admin")
    headers_a = _headers(_login(org_a, "admin")["access_token"])
    headers_b = _headers(_login(org_b, "admin")["access_token"])

    room_a = _create_room(headers_a, rate=2000)
    room_b = _create_room(headers_b, rate=3000)
    booking = _create_booking(headers_a, room_a, _future(30), _future(32))

    stats = client.get(f"/rooms/{room_a}/stats", headers=headers_a)
    assert stats.status_code == 200
    assert stats.json()["total_confirmed_bookings"] == 1
    assert stats.json()["total_revenue_cents"] == 4000

    date = booking["start_time"][:10]
    availability = client.get(f"/rooms/{room_a}/availability?date={date}", headers=headers_a)
    assert availability.status_code == 200
    assert availability.json()["busy"] == [
        {"start_time": booking["start_time"], "end_time": booking["end_time"]}
    ]

    cross_org_export = client.get(
        f"/admin/export?include_all=true&room_id={room_b}",
        headers=headers_a,
    )
    assert cross_org_export.status_code == 404
    assert cross_org_export.json()["code"] == "ROOM_NOT_FOUND"

    client.post(f"/bookings/{booking['id']}/cancel", headers=headers_a)
    stats_after_cancel = client.get(f"/rooms/{room_a}/stats", headers=headers_a)
    assert stats_after_cancel.json()["total_confirmed_bookings"] == 0
    assert stats_after_cancel.json()["total_revenue_cents"] == 0

    availability_after_cancel = client.get(f"/rooms/{room_a}/availability?date={date}", headers=headers_a)
    assert availability_after_cancel.json()["busy"] == []
