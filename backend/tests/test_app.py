from fastapi.testclient import TestClient

from app.main import create_app


class _Probe:
    def __init__(self, ok: bool):
        self.ok = ok

    async def ping(self):
        if not self.ok:
            raise ConnectionError("down")
        return True


def test_health_and_readiness():
    app = create_app(with_lifespan=False)
    app.state.db, app.state.redis = _Probe(True), _Probe(True)
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/ready").status_code == 200
        app.state.redis = _Probe(False)
        not_ready = client.get("/ready")
        assert not_ready.status_code == 503
        assert not_ready.json()["checks"] == {"postgres": "ok", "redis": "unavailable"}
