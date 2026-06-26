import logging

logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger("nba_collect_eval")


def log_marketplace_event(
    db,
    event_type: str,
    *,
    user_id: int = None,
    listing_id: int = None,
    details=None,
    level: int = logging.INFO,
) -> None:
    """Log an event and persist it when the database supports event storage."""
    LOGGER.log(
        level,
        "Marketplace event=%s user_id=%s listing_id=%s details=%s",
        event_type,
        user_id,
        listing_id,
        details,
    )
    if db is None or not hasattr(db, "add_marketplace_event"):
        return
    try:
        db.add_marketplace_event(
            event_type,
            user_id=user_id,
            listing_id=listing_id,
            details=details,
        )
    except Exception:
        LOGGER.exception("Could not persist marketplace event %s", event_type)
