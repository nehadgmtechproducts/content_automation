"""docker-compose.yml must never publish the database, Redis or any other service to the network.

On a server, a port published as "5432:5432" listens on every interface, and Docker adds its own firewall rules that
bypass ufw: the database (default password) and Redis (no password) would be open to the whole internet. Services that
need a host port (your own shell, alembic) bind to 127.0.0.1 only."""
from pathlib import Path

import pytest

import yaml

COMPOSE = yaml.safe_load((Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text())


def published_ports():
    for name, svc in COMPOSE["services"].items():
        for port in svc.get("ports", []):
            yield name, str(port)


def test_every_published_port_is_bound_to_localhost():
    exposed = [(n, p) for n, p in published_ports() if not p.startswith("127.0.0.1:")]
    assert exposed == [], f"published to the network: {exposed}"


def test_the_services_that_need_a_host_port_still_have_one():
    ports = dict(published_ports())
    assert ports["postgres"] == "127.0.0.1:5432:5432" and ports["redis"] == "127.0.0.1:6379:6379"


# ---------- least privilege: each service gets only the secrets it needs ----------

AI = {"FAL_KEY", "NVIDIA_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "FIRECRAWL_API_KEY", "PEXELS_API_KEY", "ELEVENLABS_API_KEY", "REPLICATE_API_TOKEN"}
TELEGRAM = {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}


def env_of(service: str) -> set[str]:
    return set((COMPOSE["services"][service].get("environment") or {}).keys())


def test_the_worker_gets_the_ai_keys_and_telegram():
    assert AI <= env_of("worker") and TELEGRAM <= env_of("worker")


def test_the_approvals_listener_gets_telegram_and_no_ai_key_at_all():
    assert TELEGRAM <= env_of("approvals")
    assert not (AI & env_of("approvals")), AI & env_of("approvals")


@pytest.mark.parametrize("service", ["beat", "render"])
def test_the_scheduler_and_the_shell_get_no_secrets(service):
    assert not ((AI | TELEGRAM) & env_of(service)), service


def test_no_secret_is_left_in_the_shared_environment_block():
    shared = set(COMPOSE["x-app-env"].keys())
    assert not (shared & (AI | TELEGRAM))


def test_every_service_still_gets_what_it_needs_to_run():
    for service in ("worker", "approvals", "beat"):
        assert {"DATABASE_URL", "REDIS_URL", "MEDIA_ROOT", "SYNTHESIS_PROVIDER"} <= env_of(service), service
