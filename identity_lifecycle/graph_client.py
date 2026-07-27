"""Thin wrapper around Microsoft Graph REST calls used by the joiner/mover/leaver flows.

Deliberately plain httpx + DefaultAzureCredential rather than the full msgraph SDK:
  - keeps the dependency footprint small
  - makes every call easy to mock in unit tests (respx, or a hand-rolled fake
    implementing the same method signatures — see tests/unit/conftest.py)
  - Graph's REST surface for these operations is small and stable enough that
    the SDK's extra abstraction isn't earning its keep for this project.

Every mutating method here is check-before-write: it reads current state first
and no-ops if the desired state already holds, so flows are safe to replay
(queue at-least-once delivery, manual re-runs after a partial failure, etc.).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from azure.core.credentials import TokenCredential
from azure.identity import DefaultAzureCredential

from identity_lifecycle.config import Settings

logger = logging.getLogger("identity_lifecycle.graph_client")


class GraphApiError(RuntimeError):
    """Raised when Graph returns an unexpected error response."""

    def __init__(self, method: str, url: str, status_code: int, body: str):
        self.method = method
        self.url = url
        self.status_code = status_code
        self.body = body
        super().__init__(f"Graph {method} {url} failed: {status_code} {body}")


class GraphClient:
    """Synchronous Graph REST client. One instance per function invocation is fine —
    token acquisition is cached internally by the credential."""

    def __init__(
        self,
        settings: Settings,
        credential: TokenCredential | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._settings = settings
        self._credential = credential or DefaultAzureCredential()
        self._http = http_client or httpx.Client(timeout=settings.graph_request_timeout_seconds)
        self._owns_http = http_client is None

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> GraphClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- auth -----------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        token = self._credential.get_token(self._settings.graph_scope)
        return {
            "Authorization": f"Bearer {token.token}",
            "Content-Type": "application/json",
        }

    def _request(
        self, method: str, url: str, *, json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None, expected: tuple[int, ...] = (200,),
    ) -> httpx.Response:
        headers = self._auth_headers()
        response = self._http.request(method, url, headers=headers, json=json_body, params=params)
        if response.status_code not in expected:
            raise GraphApiError(method, url, response.status_code, response.text)
        return response

    # -- users ------------------------------------------------------------------

    def get_user_by_upn(self, upn: str) -> dict[str, Any] | None:
        """Returns the user object, or None if no user with this UPN exists."""
        url = f"{self._settings.graph_base_url}/users/{upn}"
        response = self._request("GET", url, expected=(200, 404))
        if response.status_code == 404:
            return None
        return response.json()

    def ensure_user_exists(self, upn: str, create_payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Idempotent user creation. Returns (user, created)."""
        existing = self.get_user_by_upn(upn)
        if existing is not None:
            return existing, False
        url = f"{self._settings.graph_base_url}/users"
        response = self._request("POST", url, json_body=create_payload, expected=(201,))
        return response.json(), True

    def ensure_user_attributes(self, user_id: str, desired: dict[str, Any]) -> bool:
        """Patches only attributes that differ from current state. Returns True if changed."""
        url = f"{self._settings.graph_base_url}/users/{user_id}"
        current = self._request(
            "GET", url, params={"$select": ",".join(desired.keys())}
        ).json()
        diff = {k: v for k, v in desired.items() if current.get(k) != v}
        if not diff:
            return False
        self._request("PATCH", url, json_body=diff, expected=(204,))
        return True

    def ensure_account_disabled(self, user_id: str) -> bool:
        url = f"{self._settings.graph_base_url}/users/{user_id}"
        current = self._request("GET", url, params={"$select": "accountEnabled"}).json()
        if current.get("accountEnabled") is False:
            return False
        self._request("PATCH", url, json_body={"accountEnabled": False}, expected=(204,))
        return True

    def revoke_sign_in_sessions(self, user_id: str) -> None:
        url = f"{self._settings.graph_base_url}/users/{user_id}/revokeSignInSessions"
        self._request("POST", url, expected=(200,))

    def get_manager_id(self, user_id: str) -> str | None:
        url = f"{self._settings.graph_base_url}/users/{user_id}/manager"
        response = self._request("GET", url, params={"$select": "id"}, expected=(200, 404))
        if response.status_code == 404:
            return None
        return response.json().get("id")

    def ensure_manager_set(self, user_id: str, manager_id: str) -> bool:
        """Idempotent manager assignment. Returns True if changed."""
        if self.get_manager_id(user_id) == manager_id:
            return False
        url = f"{self._settings.graph_base_url}/users/{user_id}/manager/$ref"
        payload = {"@odata.id": f"{self._settings.graph_base_url}/users/{manager_id}"}
        self._request("PUT", url, json_body=payload, expected=(204,))
        return True

    def list_temporary_access_pass_methods(self, user_id: str) -> list[dict[str, Any]]:
        url = f"{self._settings.graph_base_url}/users/{user_id}/authentication/temporaryAccessPassMethods"
        response = self._request("GET", url, expected=(200,))
        return response.json().get("value", [])

    def issue_temporary_access_pass(
        self, user_id: str, lifetime_minutes: int = 480
    ) -> dict[str, Any] | None:
        """Idempotent: no-ops (returns None) if an access pass already exists for this user."""
        if self.list_temporary_access_pass_methods(user_id):
            return None
        url = f"{self._settings.graph_base_url}/users/{user_id}/authentication/temporaryAccessPassMethods"
        response = self._request(
            "POST", url, json_body={"lifetimeInMinutes": lifetime_minutes}, expected=(201,)
        )
        return response.json()

    def send_mail(self, user_id: str, subject: str, body_html: str, to_addresses: list[str]) -> None:
        url = f"{self._settings.graph_base_url}/users/{user_id}/sendMail"
        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML", "content": body_html},
                "toRecipients": [{"emailAddress": {"address": a}} for a in to_addresses],
            },
            "saveToSentItems": "true",
        }
        self._request("POST", url, json_body=payload, expected=(202,))

    # -- groups -------------------------------------------------------------------

    def get_group_by_name(self, display_name: str) -> dict[str, Any] | None:
        url = f"{self._settings.graph_base_url}/groups"
        response = self._request(
            "GET", url, params={"$filter": f"displayName eq '{display_name}'"}
        )
        values = response.json().get("value", [])
        return values[0] if values else None

    def is_group_member(self, group_id: str, user_id: str) -> bool:
        url = f"{self._settings.graph_base_url}/groups/{group_id}/members/{user_id}/$ref"
        response = self._request("GET", url, expected=(200, 404))
        return response.status_code == 200

    def ensure_group_member(self, group_id: str, user_id: str) -> bool:
        """Adds user_id to group_id unless already a member. Returns True if changed."""
        if self.is_group_member(group_id, user_id):
            return False
        url = f"{self._settings.graph_base_url}/groups/{group_id}/members/$ref"
        payload = {
            "@odata.id": f"{self._settings.graph_base_url}/directoryObjects/{user_id}"
        }
        self._request("POST", url, json_body=payload, expected=(204,))
        return True

    def ensure_group_member_removed(self, group_id: str, user_id: str) -> bool:
        if not self.is_group_member(group_id, user_id):
            return False
        url = f"{self._settings.graph_base_url}/groups/{group_id}/members/{user_id}/$ref"
        self._request("DELETE", url, expected=(204,))
        return True

    def list_member_of_group_ids(self, user_id: str, candidate_group_ids: set[str]) -> set[str]:
        """Returns the subset of candidate_group_ids the user currently belongs to."""
        url = f"{self._settings.graph_base_url}/users/{user_id}/memberOf"
        response = self._request("GET", url, params={"$select": "id"})
        member_ids = {item["id"] for item in response.json().get("value", [])}
        return member_ids & candidate_group_ids

    # -- Intune devices (beta) -----------------------------------------------------

    def list_owned_managed_devices(self, user_id: str) -> list[dict[str, Any]]:
        url = f"{self._settings.graph_beta_url}/users/{user_id}/managedDevices"
        response = self._request("GET", url, expected=(200, 404))
        if response.status_code == 404:
            return []
        return response.json().get("value", [])

    def ensure_device_retired(self, device_id: str) -> bool:
        url = f"{self._settings.graph_beta_url}/deviceManagement/managedDevices/{device_id}"
        current = self._request("GET", url, expected=(200, 404))
        if current.status_code == 404:
            return False  # already gone
        state = current.json().get("managementState")
        if state in {"retirePending", "retireFailed", "wipePending", "discovered"} and state == "retirePending":
            return False
        retire_url = f"{url}/retire"
        self._request("POST", retire_url, expected=(204, 200))
        return True
