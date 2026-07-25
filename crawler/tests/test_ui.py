from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.urls import reverse


def test_secure_proxy_header_is_configured():
    assert settings.SECURE_PROXY_SSL_HEADER == ("HTTP_X_FORWARDED_PROTO", "https")


def test_waitress_trusts_forwarded_scheme_only_from_loopback():
    unit = Path("deploy/systemd/newscrawler-web.service").read_text(encoding="utf-8")
    assert "--listen=127.0.0.1:8000" in unit
    assert "--trusted-proxy=127.0.0.1" in unit
    assert "--trusted-proxy-headers=x-forwarded-proto" in unit


@pytest.mark.django_db
def test_dashboard_requires_login(client):
    response = client.get(reverse("dashboard"))
    assert response.status_code == 302
    assert reverse("login") in response.url


@pytest.mark.django_db
def test_operator_can_open_dashboard(client):
    user = get_user_model().objects.create_user("operator", password="a-secure-test-password")
    client.force_login(user)
    response = client.get(reverse("dashboard"))
    assert response.status_code == 200
    assert "Состояние системы" in response.content.decode()
