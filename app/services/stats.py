"""Live per-room booking statistics derived from the booking table."""
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import Booking


def record_create(room_id: int, price_cents: int) -> None:
    return None


def record_cancel(room_id: int, price_cents: int) -> None:
    return None


def get(db: Session, room_id: int) -> dict:
    count, revenue = (
        db.query(func.count(Booking.id), func.coalesce(func.sum(Booking.price_cents), 0))
        .filter(Booking.room_id == room_id, Booking.status == "confirmed")
        .one()
    )
    return {"count": int(count), "revenue": int(revenue)}
