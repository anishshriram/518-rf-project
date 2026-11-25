"""Test clients that look like a browser on the station computer, or elsewhere."""

from fastapi.testclient import TestClient


def local_client(app, **options) -> TestClient:
    return TestClient(
        app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000), **options
    )


def remote_client(app, host="192.168.1.5:8000", ip="192.168.1.9", headers=None):
    return TestClient(
        app, base_url=f"http://{host}", client=(ip, 50000), headers=headers
    )
