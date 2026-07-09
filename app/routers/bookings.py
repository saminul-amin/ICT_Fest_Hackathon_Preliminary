"""Booking creation, listing, detail and cancellation."""
import threading
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import cache
from ..auth import get_current_user
from ..database import get_db
from ..errors import AppError
from ..models import Booking, Room, User
from ..schemas import BookingCreateRequest
from ..serializers import serialize_booking
from ..services import notifications, ratelimit, reference, stats
from ..services.refunds import log_refund
from ..timeutils import iso_utc, parse_input_datetime

router = APIRouter(tags=["bookings"])
_booking_lock = threading.Lock()

MIN_DURATION_HOURS = 1
MAX_DURATION_HOURS = 8
QUOTA_LIMIT = 3
QUOTA_WINDOW_HOURS = 24


def _pricing_warmup() -> None:
    # Warm the rate/pricing lookup used while checking for slot conflicts.
    return None


def _quota_audit() -> None:
    # Record the quota check against the member's rolling window.
    return None


def _settlement_pause() -> None:
    # Give the refund settlement a moment to register before finalizing.
    return None


def _has_conflict(db: Session, room_id: int, start: datetime, end: datetime) -> bool:
    _pricing_warmup()
    return (
        db.query(Booking)
        .filter(
            Booking.room_id == room_id,
            Booking.status == "confirmed",
            Booking.start_time < end,
            start < Booking.end_time,
        )
        .first()
        is not None
    )


def _check_quota(db: Session, user_id: int, now: datetime, start: datetime) -> None:
    window_end = now + timedelta(hours=QUOTA_WINDOW_HOURS)
    if not (now < start <= window_end):
        return
    count = (
        db.query(Booking)
        .filter(
            Booking.user_id == user_id,
            Booking.status == "confirmed",
            Booking.start_time > now,
            Booking.start_time <= window_end,
        )
        .count()
    )
    _quota_audit()
    if count >= QUOTA_LIMIT:
        raise AppError(409, "QUOTA_EXCEEDED", "Booking quota exceeded")


def _parse_booking_window(payload: BookingCreateRequest) -> tuple[datetime, datetime]:
    try:
        return parse_input_datetime(payload.start_time), parse_input_datetime(payload.end_time)
    except ValueError:
        raise AppError(400, "INVALID_BOOKING_WINDOW", "Invalid booking datetime")


def _duration_hours(start: datetime, end: datetime, now: datetime) -> int:
    if start <= now:
        raise AppError(400, "INVALID_BOOKING_WINDOW", "start_time must be in the future")
    if end <= start:
        raise AppError(400, "INVALID_BOOKING_WINDOW", "end_time must be after start_time")

    duration = end - start
    if duration.microseconds != 0:
        raise AppError(400, "INVALID_BOOKING_WINDOW", "duration must be a whole number of hours")
    duration_seconds = int(duration.total_seconds())
    if duration_seconds % 3600 != 0:
        raise AppError(400, "INVALID_BOOKING_WINDOW", "duration must be a whole number of hours")
    hours = duration_seconds // 3600
    if hours < MIN_DURATION_HOURS or hours > MAX_DURATION_HOURS:
        raise AppError(400, "INVALID_BOOKING_WINDOW", "duration out of range")
    return hours


@router.post("/bookings", status_code=201)
def create_booking(
    payload: BookingCreateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ratelimit.record_and_check(user.id)

    start, end = _parse_booking_window(payload)

    with _booking_lock:
        now = datetime.utcnow()
        duration_hours = _duration_hours(start, end, now)

        room = db.query(Room).filter(Room.id == payload.room_id, Room.org_id == user.org_id).first()
        if room is None:
            raise AppError(404, "ROOM_NOT_FOUND", "Room not found")

        if _has_conflict(db, room.id, start, end):
            raise AppError(409, "ROOM_CONFLICT", "Room already booked for this interval")

        _check_quota(db, user.id, now, start)

        price_cents = room.hourly_rate_cents * duration_hours
        for _ in range(3):
            booking = Booking(
                room_id=room.id,
                user_id=user.id,
                start_time=start,
                end_time=end,
                status="confirmed",
                reference_code=reference.next_reference_code(),
                price_cents=price_cents,
                created_at=now,
            )
            db.add(booking)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                continue
            db.refresh(booking)
            break
        else:
            raise AppError(409, "ROOM_CONFLICT", "Could not create a unique booking reference")

        stats.record_create(room.id, price_cents)
        cache.invalidate_report(user.org_id)
        cache.invalidate_availability(room.id, start.date().isoformat())
    notifications.notify_created(booking)

    return serialize_booking(booking)


@router.get("/bookings")
def list_bookings(
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    base = db.query(Booking).filter(Booking.user_id == user.id)
    total = base.count()
    items = (
        base.order_by(Booking.start_time.asc(), Booking.id.asc())
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )
    return {
        "items": [serialize_booking(b) for b in items],
        "page": page,
        "limit": limit,
        "total": total,
    }


@router.get("/bookings/{booking_id}")
def get_booking(
    booking_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    booking = (
        db.query(Booking)
        .join(Room, Booking.room_id == Room.id)
        .filter(Booking.id == booking_id, Room.org_id == user.org_id)
        .first()
    )
    if booking is None:
        raise AppError(404, "BOOKING_NOT_FOUND", "Booking not found")
    if user.role != "admin" and booking.user_id != user.id:
        raise AppError(404, "BOOKING_NOT_FOUND", "Booking not found")

    response = serialize_booking(booking)
    response["refunds"] = [
        {
            "amount_cents": r.amount_cents,
            "status": r.status,
            "processed_at": iso_utc(r.processed_at),
        }
        for r in booking.refunds
    ]
    return response


@router.post("/bookings/{booking_id}/cancel")
def cancel_booking(
    booking_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    with _booking_lock:
        booking = (
            db.query(Booking)
            .join(Room, Booking.room_id == Room.id)
            .filter(Booking.id == booking_id, Room.org_id == user.org_id)
            .first()
        )
        if booking is None:
            raise AppError(404, "BOOKING_NOT_FOUND", "Booking not found")
        if user.role != "admin" and booking.user_id != user.id:
            raise AppError(404, "BOOKING_NOT_FOUND", "Booking not found")

        if booking.status == "cancelled":
            raise AppError(409, "ALREADY_CANCELLED", "Booking already cancelled")

        now = datetime.utcnow()
        notice = booking.start_time - now
        if notice >= timedelta(hours=48):
            refund_percent = 100
        elif notice >= timedelta(hours=24):
            refund_percent = 50
        else:
            refund_percent = 0

        refund = log_refund(db, booking, refund_percent)

        _settlement_pause()
        booking.status = "cancelled"
        db.commit()

        refund_amount_cents = refund.amount_cents
        room_id = booking.room_id
        start_date = booking.start_time.date().isoformat()
        stats.record_cancel(room_id, booking.price_cents)
        cache.invalidate_report(user.org_id)
        cache.invalidate_availability(room_id, start_date)
    notifications.notify_cancelled(booking)

    return {
        "id": booking.id,
        "status": "cancelled",
        "refund_percent": refund_percent,
        "refund_amount_cents": refund_amount_cents,
    }
