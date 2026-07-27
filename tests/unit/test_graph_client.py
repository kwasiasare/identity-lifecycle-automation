"""Tests the real GraphClient's check-before-write behaviour against a mocked
HTTP layer (respx) — separate from the flow tests, which use FakeGraphClient.
This is what proves FakeGraphClient's idempotency semantics actually match
what the real Graph wrapper does.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from identity_lifecycle.config import Settings
from identity_lifecycle.graph_client import GraphApiError, GraphClient


class _FakeToken:
    token = "fake-token"


class _FakeCredential:
    def get_token(self, *scopes, **kwargs):
        return _FakeToken()


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def client(settings) -> GraphClient:
    return GraphClient(settings, credential=_FakeCredential(), http_client=httpx.Client())


@respx.mock
def test_get_user_by_upn_returns_none_on_404(client, settings):
    respx.get(f"{settings.graph_base_url}/users/missing@contoso.onmicrosoft.com").mock(
        return_value=httpx.Response(404, json={"error": "not found"})
    )
    assert client.get_user_by_upn("missing@contoso.onmicrosoft.com") is None


@respx.mock
def test_get_user_by_upn_returns_user_on_200(client, settings):
    respx.get(f"{settings.graph_base_url}/users/a@contoso.onmicrosoft.com").mock(
        return_value=httpx.Response(200, json={"id": "user-1", "userPrincipalName": "a@contoso.onmicrosoft.com"})
    )
    user = client.get_user_by_upn("a@contoso.onmicrosoft.com")
    assert user["id"] == "user-1"


@respx.mock
def test_ensure_user_exists_skips_creation_when_present(client, settings):
    respx.get(f"{settings.graph_base_url}/users/a@contoso.onmicrosoft.com").mock(
        return_value=httpx.Response(200, json={"id": "user-1"})
    )
    create_route = respx.post(f"{settings.graph_base_url}/users")

    user, created = client.ensure_user_exists("a@contoso.onmicrosoft.com", {"displayName": "A"})

    assert created is False
    assert user["id"] == "user-1"
    assert create_route.call_count == 0


@respx.mock
def test_ensure_user_exists_creates_when_absent(client, settings):
    respx.get(f"{settings.graph_base_url}/users/new@contoso.onmicrosoft.com").mock(
        return_value=httpx.Response(404)
    )
    respx.post(f"{settings.graph_base_url}/users").mock(
        return_value=httpx.Response(201, json={"id": "user-2"})
    )
    user, created = client.ensure_user_exists("new@contoso.onmicrosoft.com", {"displayName": "New"})
    assert created is True
    assert user["id"] == "user-2"


@respx.mock
def test_ensure_account_disabled_skips_when_already_disabled(client, settings):
    respx.get(f"{settings.graph_base_url}/users/u1").mock(
        return_value=httpx.Response(200, json={"accountEnabled": False})
    )
    patch_route = respx.patch(f"{settings.graph_base_url}/users/u1")

    changed = client.ensure_account_disabled("u1")

    assert changed is False
    assert patch_route.call_count == 0


@respx.mock
def test_ensure_account_disabled_patches_when_enabled(client, settings):
    respx.get(f"{settings.graph_base_url}/users/u1").mock(
        return_value=httpx.Response(200, json={"accountEnabled": True})
    )
    respx.patch(f"{settings.graph_base_url}/users/u1").mock(return_value=httpx.Response(204))

    changed = client.ensure_account_disabled("u1")

    assert changed is True


@respx.mock
def test_is_group_member_true_on_200(client, settings):
    respx.get(f"{settings.graph_base_url}/groups/g1/members/u1/$ref").mock(
        return_value=httpx.Response(200)
    )
    assert client.is_group_member("g1", "u1") is True


@respx.mock
def test_is_group_member_false_on_404(client, settings):
    respx.get(f"{settings.graph_base_url}/groups/g1/members/u1/$ref").mock(
        return_value=httpx.Response(404)
    )
    assert client.is_group_member("g1", "u1") is False


@respx.mock
def test_ensure_group_member_noop_when_already_member(client, settings):
    respx.get(f"{settings.graph_base_url}/groups/g1/members/u1/$ref").mock(
        return_value=httpx.Response(200)
    )
    post_route = respx.post(f"{settings.graph_base_url}/groups/g1/members/$ref")

    changed = client.ensure_group_member("g1", "u1")

    assert changed is False
    assert post_route.call_count == 0


@respx.mock
def test_ensure_group_member_adds_when_absent(client, settings):
    respx.get(f"{settings.graph_base_url}/groups/g1/members/u1/$ref").mock(
        return_value=httpx.Response(404)
    )
    respx.post(f"{settings.graph_base_url}/groups/g1/members/$ref").mock(
        return_value=httpx.Response(204)
    )
    changed = client.ensure_group_member("g1", "u1")
    assert changed is True


@respx.mock
def test_unexpected_status_code_raises_graph_api_error(client, settings):
    respx.get(f"{settings.graph_base_url}/users/u1").mock(
        return_value=httpx.Response(500, text="internal error")
    )
    with pytest.raises(GraphApiError):
        client._request("GET", f"{settings.graph_base_url}/users/u1", expected=(200,))


@respx.mock
def test_issue_temporary_access_pass_noop_when_one_exists(client, settings):
    respx.get(
        f"{settings.graph_base_url}/users/u1/authentication/temporaryAccessPassMethods"
    ).mock(return_value=httpx.Response(200, json={"value": [{"id": "tap-1"}]}))
    post_route = respx.post(
        f"{settings.graph_base_url}/users/u1/authentication/temporaryAccessPassMethods"
    )

    result = client.issue_temporary_access_pass("u1")

    assert result is None
    assert post_route.call_count == 0
