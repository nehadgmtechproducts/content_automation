"""Safety net for the whole suite: no test may reach the real internet for pictures.

The providers read the real .env (which holds a live NVIDIA key) and would happily call Wikipedia, Wikimedia Commons, news
sites and NVIDIA's image API from a test that runs produce_assets. That would spend credits, depend on the network and make
results vary. Each provider has an `_http_client` hook; tests that exercise one install their own fake transport, which
overrides the refusing one below."""
import httpx
import pytest

from app.providers import commons_photo, image_nvidia, source_photo


def _refusing_client():
    def refuse(request):
        raise AssertionError(f"test attempted a real request to {request.url}")
    return httpx.Client(transport=httpx.MockTransport(refuse))


@pytest.fixture(autouse=True)
def _no_real_picture_requests(monkeypatch):
    for module in (image_nvidia, source_photo, commons_photo):
        monkeypatch.setattr(module, "_http_client", _refusing_client())
