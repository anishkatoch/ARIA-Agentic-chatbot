import logging

from app.config import cfg

logger = logging.getLogger(__name__)

_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        url = cfg.db_url
        if url is None:
            return None
        try:
            from sqlalchemy import create_engine
            _engine = create_engine(url, pool_pre_ping=True)
            logger.info("[DB] Engine created")
        except Exception as e:
            logger.warning(f"[DB] Engine creation failed: {e}")
            _engine = None
    return _engine


def get_session_factory():
    global _SessionLocal
    if _SessionLocal is None:
        engine = get_engine()
        if engine is None:
            return None
        from sqlalchemy.orm import sessionmaker
        _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return _SessionLocal


def get_db():
    factory = get_session_factory()
    if factory is None:
        return None
    db = factory()
    try:
        yield db
    finally:
        db.close()


def is_db_available() -> bool:
    return get_engine() is not None
