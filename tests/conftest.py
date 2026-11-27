import os
import tempfile

import pytest

# Must be set before app.db is imported (engine is created at import time).
os.environ.setdefault("CONFIG_DIR", tempfile.mkdtemp(prefix="cmass-test-"))
os.environ.setdefault("TOKEN_ENCRYPTION_KEY", "x2Qx9yN0S2H5nM6b3m3Hk2pZ6x8oYyq0yX9W1kq3l2A=")


@pytest.fixture()
def db():
    from app import models  # noqa: F401
    from app.db import Base, SessionLocal, engine
    from app.models import Corporation

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session = SessionLocal()
    session.add(Corporation(corporation_id=98000001, name="Conserve Mass", ticker="CMASS",
                            is_tracked=True))
    session.commit()
    yield session
    session.close()
