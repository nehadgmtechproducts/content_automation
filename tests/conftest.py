"""Safety net for the whole suite: no test may reach NVIDIA's image API.

generate_art() reads the real .env (which holds a live key), so without this a test that runs
produce_assets would spend real credits and wait on the network. Tests that exercise the image
client install their own fake transport, which overrides this one.
"""
import httpx
import pytest

from app.providers import image_nvidia


@pytest.fixture(autouse=True)
def _no_real_image_requests(monkeypatch):
    def refuse(request):
        raise AssertionError(f"test attempted a real request to {request.url}")

    monkeypatch.setattr(image_nvidia, "_http_client", httpx.Client(transport=httpx.MockTransport(refuse)))
