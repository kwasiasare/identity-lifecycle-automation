"""Tests the real GraphClient's check-before-write behaviour against a mocked
HTTP layer (respx) — separate from the flow tests, which use FakeGraphClient.
This is what proves FakeGraphClient's idempotency semantics actually match
what the real Graph wrapper does. Every method gets at least one test that
asserts the exact URL/method/body sent to Graph.
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
    return GraphClient(
        settings,
        credential=_FakeCredential(),
        http_client=httpx.Client(),
        sleep_func=lambda _seconds: None,  # never actually sleep in tests
    )


# -- users --------------------------------------------------------------------


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
def test_get_user_by_upn_url_encodes_upn(client, settings):
    # A UPN containing a character that must be percent-encoded in a path
    # segment (space, from a legacy/edge-case value) — proves _q() is applied.
    # "@" itself is left literal (see _q() docstring).
    route = respx.get(f"{settings.graph_base_url}/users/a%20b@contoso.onmicrosoft.com").mock(
        return_value=httpx.Response(200, json={"id": "user-1"})
    )
    client.get_user_by_upn("a b@contoso.onmicrosoft.com")
    assert route.call_count == 1


@respx.mock
def test_get_user_by_upn_url_encodes_path_traversal_attempt(client, settings):
    # Defense in depth: even though models.py's UPN regex already rejects
    # this shape at intake, GraphClient itself must not let a "/" reach the
    # URL unescaped if one ever got this far.
    route = respx.get(
        f"{settings.graph_base_url}/users/a@b.com%2F..%2F..%2Fgroups"
    ).mock(return_value=httpx.Response(404))
    client.get_user_by_upn("a@b.com/../../groups")
    assert route.call_count == 1


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
    create_route = respx.post(f"{settings.graph_base_url}/users").mock(
        return_value=httpx.Response(201, json={"id": "user-2"})
    )
    user, created = client.ensure_user_exists("new@contoso.onmicrosoft.com", {"displayName": "New"})
    assert created is True
    assert user["id"] == "user-2"
    assert create_route.calls.last.request.method == "POST"


@respx.mock
def test_ensure_user_exists_treats_concurrent_409_as_success(client, settings):
    """A second worker racing to create the same UPN gets a 409 "already
    exists" from Graph — GraphClient must re-fetch and report created=False
    rather than raising."""
    get_route = respx.get(f"{settings.graph_base_url}/users/race@contoso.onmicrosoft.com")
    get_route.side_effect = [
        httpx.Response(404),
        httpx.Response(200, json={"id": "user-3", "userPrincipalName": "race@contoso.onmicrosoft.com"}),
    ]
    respx.post(f"{settings.graph_base_url}/users").mock(
        return_value=httpx.Response(
            409, json={"error": {"message": "Another object with the same value for property userPrincipalName already exists."}}
        )
    )

    user, created = client.ensure_user_exists("race@contoso.onmicrosoft.com", {"displayName": "Race"})

    assert created is False
    assert user["id"] == "user-3"


@respx.mock
def test_ensure_user_exists_reraises_non_conflict_errors(client, settings):
    respx.get(f"{settings.graph_base_url}/users/bad@contoso.onmicrosoft.com").mock(
        return_value=httpx.Response(404)
    )
    respx.post(f"{settings.graph_base_url}/users").mock(
        return_value=httpx.Response(403, json={"error": {"message": "Insufficient privileges"}})
    )
    with pytest.raises(GraphApiError):
        client.ensure_user_exists("bad@contoso.onmicrosoft.com", {"displayName": "Bad"})


@respx.mock
def test_ensure_user_attributes_noop_when_current(client, settings):
    respx.get(f"{settings.graph_base_url}/users/u1").mock(
        return_value=httpx.Response(200, json={"jobTitle": "Engineer"})
    )
    patch_route = respx.patch(f"{settings.graph_base_url}/users/u1")

    changed = client.ensure_user_attributes("u1", {"jobTitle": "Engineer"})

    assert changed is False
    assert patch_route.call_count == 0


@respx.mock
def test_ensure_user_attributes_patches_diff_only(client, settings):
    respx.get(f"{settings.graph_base_url}/users/u1").mock(
        return_value=httpx.Response(200, json={"jobTitle": "Old Title"})
    )
    patch_route = respx.patch(f"{settings.graph_base_url}/users/u1").mock(
        return_value=httpx.Response(204)
    )

    changed = client.ensure_user_attributes("u1", {"jobTitle": "New Title"})

    assert changed is True
    assert patch_route.calls.last.request.method == "PATCH"
    import json as _json

    assert _json.loads(patch_route.calls.last.request.content) == {"jobTitle": "New Title"}


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
def test_revoke_sign_in_sessions_calls_expected_endpoint(client, settings):
    route = respx.post(f"{settings.graph_base_url}/users/u1/revokeSignInSessions").mock(
        return_value=httpx.Response(200, json={"value": True})
    )
    client.revoke_sign_in_sessions("u1")
    assert route.call_count == 1


@respx.mock
def test_get_manager_id_returns_none_on_404(client, settings):
    respx.get(f"{settings.graph_base_url}/users/u1/manager").mock(return_value=httpx.Response(404))
    assert client.get_manager_id("u1") is None


@respx.mock
def test_ensure_manager_set_noop_when_already_set(client, settings):
    respx.get(f"{settings.graph_base_url}/users/u1/manager").mock(
        return_value=httpx.Response(200, json={"id": "mgr-1"})
    )
    put_route = respx.put(f"{settings.graph_base_url}/users/u1/manager/$ref")

    changed = client.ensure_manager_set("u1", "mgr-1")

    assert changed is False
    assert put_route.call_count == 0


@respx.mock
def test_ensure_manager_set_puts_when_different(client, settings):
    respx.get(f"{settings.graph_base_url}/users/u1/manager").mock(return_value=httpx.Response(404))
    put_route = respx.put(f"{settings.graph_base_url}/users/u1/manager/$ref").mock(
        return_value=httpx.Response(204)
    )

    changed = client.ensure_manager_set("u1", "mgr-2")

    assert changed is True
    import json as _json

    body = _json.loads(put_route.calls.last.request.content)
    assert body["@odata.id"] == f"{settings.graph_base_url}/users/mgr-2"


@respx.mock
def test_send_mail_posts_expected_payload(client, settings):
    route = respx.post(f"{settings.graph_base_url}/users/no-reply@contoso.onmicrosoft.com/sendMail").mock(
        return_value=httpx.Response(202)
    )
    client.send_mail(
        sender="no-reply@contoso.onmicrosoft.com",
        subject="Welcome",
        body_html="<p>hi</p>",
        to_addresses=["a@example.com"],
    )
    assert route.call_count == 1
    import json as _json

    body = _json.loads(route.calls.last.request.content)
    assert body["message"]["subject"] == "Welcome"
    assert body["message"]["toRecipients"][0]["emailAddress"]["address"] == "a@example.com"


# -- groups -------------------------------------------------------------------


@respx.mock
def test_get_group_by_name_builds_filter_and_escapes_quotes(client, settings):
    route = respx.get(f"{settings.graph_base_url}/groups").mock(
        return_value=httpx.Response(200, json={"value": [{"id": "g1", "displayName": "O'Brien's Team"}]})
    )
    group = client.get_group_by_name("O'Brien's Team")
    assert group["id"] == "g1"
    sent_filter = route.calls.last.request.url.params["$filter"]
    assert sent_filter == "displayName eq 'O''Brien''s Team'"


@respx.mock
def test_get_group_by_name_returns_none_when_no_match(client, settings):
    respx.get(f"{settings.graph_base_url}/groups").mock(return_value=httpx.Response(200, json={"value": []}))
    assert client.get_group_by_name("nonexistent") is None


@respx.mock
def test_get_member_of_group_ids_follows_pagination(client, settings):
    # respx matches by path when no query is specified on the pattern, so one
    # route (matched regardless of query string) covers both the initial
    # $select=id request and the follow-up nextLink request.
    next_url = f"{settings.graph_base_url}/users/u1/memberOf?$skiptoken=abc"
    route = respx.get(f"{settings.graph_base_url}/users/u1/memberOf")
    route.side_effect = [
        httpx.Response(200, json={"value": [{"id": "g1"}], "@odata.nextLink": next_url}),
        httpx.Response(200, json={"value": [{"id": "g2"}]}),
    ]

    ids = client.get_member_of_group_ids("u1")

    assert ids == {"g1", "g2"}
    assert route.call_count == 2


@respx.mock
def test_ensure_group_member_noop_when_already_member(client, settings):
    respx.get(f"{settings.graph_base_url}/users/u1/memberOf").mock(
        return_value=httpx.Response(200, json={"value": [{"id": "g1"}]})
    )
    post_route = respx.post(f"{settings.graph_base_url}/groups/g1/members/$ref")

    changed = client.ensure_group_member("g1", "u1")

    assert changed is False
    assert post_route.call_count == 0


@respx.mock
def test_ensure_group_member_adds_when_absent(client, settings):
    respx.get(f"{settings.graph_base_url}/users/u1/memberOf").mock(
        return_value=httpx.Response(200, json={"value": []})
    )
    post_route = respx.post(f"{settings.graph_base_url}/groups/g1/members/$ref").mock(
        return_value=httpx.Response(204)
    )
    changed = client.ensure_group_member("g1", "u1")
    assert changed is True
    import json as _json

    body = _json.loads(post_route.calls.last.request.content)
    assert body["@odata.id"] == f"{settings.graph_base_url}/directoryObjects/u1"


@respx.mock
def test_ensure_group_member_uses_supplied_member_of_ids_without_a_graph_read(client, settings):
    memberof_route = respx.get(f"{settings.graph_base_url}/users/u1/memberOf")
    post_route = respx.post(f"{settings.graph_base_url}/groups/g1/members/$ref").mock(
        return_value=httpx.Response(204)
    )

    changed = client.ensure_group_member("g1", "u1", member_of_ids=set())

    assert changed is True
    assert memberof_route.call_count == 0
    assert post_route.call_count == 1


@respx.mock
def test_ensure_group_member_treats_concurrent_already_exists_as_success(client, settings):
    respx.get(f"{settings.graph_base_url}/users/u1/memberOf").mock(
        return_value=httpx.Response(200, json={"value": []})
    )
    respx.post(f"{settings.graph_base_url}/groups/g1/members/$ref").mock(
        return_value=httpx.Response(
            400, json={"error": {"message": "One or more added object references already exist for the following modified properties: 'members'."}}
        )
    )
    changed = client.ensure_group_member("g1", "u1")
    assert changed is True


@respx.mock
def test_ensure_group_member_removed_noop_when_not_a_member(client, settings):
    respx.get(f"{settings.graph_base_url}/users/u1/memberOf").mock(
        return_value=httpx.Response(200, json={"value": []})
    )
    delete_route = respx.delete(f"{settings.graph_base_url}/groups/g1/members/u1/$ref")

    changed = client.ensure_group_member_removed("g1", "u1")

    assert changed is False
    assert delete_route.call_count == 0


@respx.mock
def test_ensure_group_member_removed_deletes_when_member(client, settings):
    respx.get(f"{settings.graph_base_url}/users/u1/memberOf").mock(
        return_value=httpx.Response(200, json={"value": [{"id": "g1"}]})
    )
    delete_route = respx.delete(f"{settings.graph_base_url}/groups/g1/members/u1/$ref").mock(
        return_value=httpx.Response(204)
    )

    changed = client.ensure_group_member_removed("g1", "u1")

    assert changed is True
    assert delete_route.call_count == 1


# -- temporary access pass -----------------------------------------------------


@respx.mock
def test_issue_temporary_access_pass_noop_when_usable_one_exists(client, settings):
    respx.get(
        f"{settings.graph_base_url}/users/u1/authentication/temporaryAccessPassMethods"
    ).mock(return_value=httpx.Response(200, json={"value": [{"id": "tap-1", "isUsable": True}]}))
    post_route = respx.post(
        f"{settings.graph_base_url}/users/u1/authentication/temporaryAccessPassMethods"
    )

    result = client.issue_temporary_access_pass("u1")

    assert result is None
    assert post_route.call_count == 0


@respx.mock
def test_issue_temporary_access_pass_issues_new_when_existing_not_usable(client, settings):
    respx.get(
        f"{settings.graph_base_url}/users/u1/authentication/temporaryAccessPassMethods"
    ).mock(return_value=httpx.Response(200, json={"value": [{"id": "tap-1", "isUsable": False}]}))
    post_route = respx.post(
        f"{settings.graph_base_url}/users/u1/authentication/temporaryAccessPassMethods"
    ).mock(return_value=httpx.Response(201, json={"id": "tap-2", "temporaryAccessPass": "ABC123"}))

    result = client.issue_temporary_access_pass("u1")

    assert result["temporaryAccessPass"] == "ABC123"
    assert post_route.call_count == 1


# -- Intune devices -------------------------------------------------------------


@respx.mock
def test_list_owned_managed_devices_returns_empty_on_404(client, settings):
    respx.get(f"{settings.graph_beta_url}/users/u1/managedDevices").mock(return_value=httpx.Response(404))
    assert client.list_owned_managed_devices("u1") == []


@respx.mock
def test_list_owned_managed_devices_follows_pagination(client, settings):
    next_url = f"{settings.graph_beta_url}/users/u1/managedDevices?$skiptoken=xyz"
    route = respx.get(f"{settings.graph_beta_url}/users/u1/managedDevices")
    route.side_effect = [
        httpx.Response(200, json={"value": [{"id": "d1"}], "@odata.nextLink": next_url}),
        httpx.Response(200, json={"value": [{"id": "d2"}]}),
    ]

    devices = client.list_owned_managed_devices("u1")

    assert {d["id"] for d in devices} == {"d1", "d2"}
    assert route.call_count == 2


@respx.mock
def test_ensure_device_retired_skips_when_already_gone(client, settings):
    respx.get(f"{settings.graph_beta_url}/deviceManagement/managedDevices/d1").mock(
        return_value=httpx.Response(404)
    )
    assert client.ensure_device_retired("d1") is False


@respx.mock
def test_ensure_device_retired_skips_when_already_pending(client, settings):
    respx.get(f"{settings.graph_beta_url}/deviceManagement/managedDevices/d1").mock(
        return_value=httpx.Response(200, json={"managementState": "retirePending"})
    )
    retire_route = respx.post(f"{settings.graph_beta_url}/deviceManagement/managedDevices/d1/retire")

    assert client.ensure_device_retired("d1") is False
    assert retire_route.call_count == 0


@respx.mock
def test_ensure_device_retired_retires_a_normally_managed_device(client, settings):
    """Regression test for the dead skip-condition bug: a device in a normal
    "discovered"/"managed" state must actually get retired, not silently
    skipped."""
    respx.get(f"{settings.graph_beta_url}/deviceManagement/managedDevices/d1").mock(
        return_value=httpx.Response(200, json={"managementState": "managed"})
    )
    retire_route = respx.post(f"{settings.graph_beta_url}/deviceManagement/managedDevices/d1/retire").mock(
        return_value=httpx.Response(204)
    )

    assert client.ensure_device_retired("d1") is True
    assert retire_route.call_count == 1


@respx.mock
def test_ensure_device_retired_retires_a_discovered_device(client, settings):
    respx.get(f"{settings.graph_beta_url}/deviceManagement/managedDevices/d1").mock(
        return_value=httpx.Response(200, json={"managementState": "discovered"})
    )
    retire_route = respx.post(f"{settings.graph_beta_url}/deviceManagement/managedDevices/d1/retire").mock(
        return_value=httpx.Response(204)
    )

    assert client.ensure_device_retired("d1") is True
    assert retire_route.call_count == 1


# -- error handling / retry -----------------------------------------------------


@respx.mock
def test_unexpected_status_code_raises_graph_api_error(client, settings):
    respx.get(f"{settings.graph_base_url}/users/u1").mock(
        return_value=httpx.Response(500, text="internal error")
    )
    with pytest.raises(GraphApiError):
        client._request("GET", f"{settings.graph_base_url}/users/u1", expected=(200,))


@respx.mock
def test_retries_429_honoring_retry_after_then_succeeds(client, settings):
    route = respx.get(f"{settings.graph_base_url}/users/u1")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(200, json={"id": "user-1"}),
    ]
    slept: list[float] = []
    client._sleep = slept.append

    user = client.get_user_by_upn("u1")

    assert user["id"] == "user-1"
    assert route.call_count == 2
    assert slept == [0.0]


@respx.mock
def test_retries_503_then_raises_after_exhausting_bounded_retries(client, settings):
    route = respx.get(f"{settings.graph_base_url}/users/u1").mock(
        return_value=httpx.Response(503, text="service unavailable")
    )
    client._sleep = lambda _seconds: None

    with pytest.raises(GraphApiError) as exc_info:
        client.get_user_by_upn("u1")

    assert exc_info.value.status_code == 503
    # initial attempt + graph_max_retries retries, never unbounded
    assert route.call_count == settings.graph_max_retries + 1


# -- DRY_RUN --------------------------------------------------------------------


@respx.mock
def test_dry_run_skips_user_creation_write():
    settings = Settings(dry_run=True)
    dry_client = GraphClient(
        settings, credential=_FakeCredential(), http_client=httpx.Client(), sleep_func=lambda s: None
    )
    respx.get(f"{settings.graph_base_url}/users/new@contoso.onmicrosoft.com").mock(
        return_value=httpx.Response(404)
    )
    create_route = respx.post(f"{settings.graph_base_url}/users")

    user, created = dry_client.ensure_user_exists("new@contoso.onmicrosoft.com", {"displayName": "New"})

    assert created is True
    assert create_route.call_count == 0


@respx.mock
def test_dry_run_skips_group_member_add_write():
    settings = Settings(dry_run=True)
    dry_client = GraphClient(
        settings, credential=_FakeCredential(), http_client=httpx.Client(), sleep_func=lambda s: None
    )
    respx.get(f"{settings.graph_base_url}/users/u1/memberOf").mock(
        return_value=httpx.Response(200, json={"value": []})
    )
    post_route = respx.post(f"{settings.graph_base_url}/groups/g1/members/$ref")

    changed = dry_client.ensure_group_member("g1", "u1")

    assert changed is True
    assert post_route.call_count == 0
