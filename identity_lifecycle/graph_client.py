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

Every path segment derived from user input (a UPN or an object id) is
URL-encoded via `_q()` before being interpolated into a Graph URL — defense in
depth against a crafted UPN (e.g. "a@b.com/../../groups") breaking out of its
path segment, on top of the stricter UPN regex enforced in models.py.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from identity_lifecycle.config import Settings

if TYPE_CHECKING:
    # Type-only: `from __future__ import annotations` (above) makes every
    # annotation in this module a string, so this import never actually runs
    # at module-import time — it exists purely for static type checkers.
    from azure.core.credentials import TokenCredential

logger = logging.getLogger("identity_lifecycle.graph_client")

# Status codes GraphClient will retry, with bounded backoff honoring
# Retry-After when Graph supplies one.
_RETRYABLE_STATUS_CODES = frozenset({429, 503})
_DEFAULT_RETRY_AFTER_SECONDS = 2.0

# Graph's known "duplicate write" responses for concurrent replays — treated
# as success (the desired state already holds) rather than propagated as an
# error, since two workers racing to process a redelivered/duplicated message
# is an expected, tolerable outcome, not a real failure.
_ALREADY_EXISTS_MARKERS = (
    "already exist",  # "One or more added object references already exist..."
    "already exists",
)


def _q(value: str) -> str:
    """URL-encodes a single path segment (UPN, object id). `@` is left
    unescaped (it's a normal, expected character in every UPN this system
    handles, and Microsoft's own Graph docs note it never needs encoding) —
    everything else, notably `/ \\ ? # %` and whitespace, is escaped. Those
    are exactly the characters the UPN regex in models.py already rejects at
    intake; this is defense in depth for any id that reaches GraphClient by
    another path (e.g. a manager/group/device id)."""
    return quote(str(value), safe="@")


def _escape_odata_string(value: str) -> str:
    """Escapes a literal single-quote for embedding inside an OData string
    literal (`'` -> `''`), per the OData/Graph $filter string-literal rule."""
    return value.replace("'", "''")


class GraphApiError(RuntimeError):
    """Raised when Graph returns an unexpected error response."""

    def __init__(self, method: str, url: str, status_code: int, body: str):
        self.method = method
        self.url = url
        self.status_code = status_code
        self.body = body
        super().__init__(f"Graph {method} {url} failed: {status_code} {body}")

    def looks_like_already_exists(self) -> bool:
        body_lower = self.body.lower()
        return self.status_code in (400, 409) and any(
            marker in body_lower for marker in _ALREADY_EXISTS_MARKERS
        )


class GraphClient:
    """Synchronous Graph REST client. One instance per function invocation is fine —
    token acquisition is cached internally by the credential."""

    def __init__(
        self,
        settings: Settings,
        credential: TokenCredential | None = None,
        http_client: httpx.Client | None = None,
        sleep_func: Any = None,
    ) -> None:
        self._settings = settings
        if credential is None:
            # Lazy import: azure-identity pulls in cryptography/msal, the
            # heaviest branch of this app's dependency graph. Importing it
            # only when a GraphClient is actually constructed (i.e. inside a
            # trigger invocation) rather than at module load keeps
            # function_app.py's own top-level import graph light, since
            # every trigger in function_app.py imports GraphClient
            # transitively (directly or via identity_lifecycle.flows.*) and
            # that import graph is exactly what Flex Consumption's function
            # indexing has to load. See README "Known gap on Flex
            # Consumption" / CI history for the indexing symptom this is
            # hedging against, and
            # https://learn.microsoft.com/azure/azure-functions/python-build-options
            # ("reduce top-level imports or use lazy imports").
            from azure.identity import DefaultAzureCredential

            credential = DefaultAzureCredential()
        self._credential = credential
        self._http = http_client or httpx.Client(timeout=settings.graph_request_timeout_seconds)
        self._owns_http = http_client is None
        self._sleep = sleep_func or time.sleep

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
        attempt = 0
        while True:
            response = self._http.request(method, url, headers=headers, json=json_body, params=params)
            if (
                response.status_code in _RETRYABLE_STATUS_CODES
                and attempt < self._settings.graph_max_retries
            ):
                delay = _parse_retry_after(response.headers.get("Retry-After"))
                attempt += 1
                logger.warning(
                    "Graph %s %s returned %s; retrying (attempt %d/%d) after %.1fs",
                    method, url, response.status_code, attempt,
                    self._settings.graph_max_retries, delay,
                )
                self._sleep(delay)
                continue
            if response.status_code not in expected:
                raise GraphApiError(method, url, response.status_code, response.text)
            return response

    def _get_all_pages(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        treat_404_as_empty: bool = False,
    ) -> list[dict[str, Any]]:
        """GETs a Graph collection URL, following @odata.nextLink until Graph
        stops returning one. Used for every collection read (memberOf, group
        lookup, managed devices) so a directory with more than one page of
        results (Graph's default page size is typically 100-999 depending on
        the resource) is never silently truncated.

        `treat_404_as_empty` covers `/users/{id}/managedDevices`, which some
        Graph environments 404 on rather than returning an empty collection
        for a user with no managed devices.
        """
        results: list[dict[str, Any]] = []
        next_url: str | None = url
        next_params = params
        first_request = True
        while next_url:
            expected = (200, 404) if (first_request and treat_404_as_empty) else (200,)
            response = self._request("GET", next_url, params=next_params, expected=expected)
            first_request = False
            if response.status_code == 404:
                return []
            body = response.json()
            results.extend(body.get("value", []))
            next_url = body.get("@odata.nextLink")
            next_params = None  # nextLink is a fully-qualified URL with its own query string
        return results

    def _dry_run_skip(self, action: str, **context: Any) -> None:
        detail = " ".join(f"{k}={v}" for k, v in context.items())
        logger.info("DRY_RUN: skipping write — %s (%s)", action, detail)

    # -- users ------------------------------------------------------------------

    def get_user_by_upn(self, upn: str) -> dict[str, Any] | None:
        """Returns the user object, or None if no user with this UPN exists."""
        url = f"{self._settings.graph_base_url}/users/{_q(upn)}"
        response = self._request("GET", url, expected=(200, 404))
        if response.status_code == 404:
            return None
        return response.json()

    def ensure_user_exists(self, upn: str, create_payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Idempotent user creation. Returns (user, created).

        Concurrency-tolerant: if two workers race to create the same UPN
        (e.g. a redelivered queue message processed twice near-simultaneously)
        Graph's second POST fails with a 409/400 "already exists" — that is
        treated as success (re-fetch and return the now-existing user) rather
        than propagated as an error.
        """
        existing = self.get_user_by_upn(upn)
        if existing is not None:
            return existing, False
        if self._settings.dry_run:
            self._dry_run_skip("create_user", upn=upn)
            return {"id": f"dry-run:{upn}", **create_payload}, True
        url = f"{self._settings.graph_base_url}/users"
        try:
            response = self._request("POST", url, json_body=create_payload, expected=(201,))
        except GraphApiError as exc:
            if exc.looks_like_already_exists():
                logger.info(
                    "ensure_user_exists: concurrent create for %s detected as already-exists; "
                    "re-fetching instead of failing",
                    upn,
                )
                refetched = self.get_user_by_upn(upn)
                if refetched is not None:
                    return refetched, False
            raise
        return response.json(), True

    def ensure_user_attributes(self, user_id: str, desired: dict[str, Any]) -> bool:
        """Patches only attributes that differ from current state. Returns True if changed."""
        url = f"{self._settings.graph_base_url}/users/{_q(user_id)}"
        current = self._request(
            "GET", url, params={"$select": ",".join(desired.keys())}
        ).json()
        diff = {k: v for k, v in desired.items() if current.get(k) != v}
        if not diff:
            return False
        if self._settings.dry_run:
            self._dry_run_skip("update_attributes", user_id=user_id, diff=sorted(diff))
            return True
        self._request("PATCH", url, json_body=diff, expected=(204,))
        return True

    def ensure_account_disabled(self, user_id: str) -> bool:
        url = f"{self._settings.graph_base_url}/users/{_q(user_id)}"
        current = self._request("GET", url, params={"$select": "accountEnabled"}).json()
        if current.get("accountEnabled") is False:
            return False
        if self._settings.dry_run:
            self._dry_run_skip("disable_account", user_id=user_id)
            return True
        self._request("PATCH", url, json_body={"accountEnabled": False}, expected=(204,))
        return True

    def revoke_sign_in_sessions(self, user_id: str) -> None:
        if self._settings.dry_run:
            self._dry_run_skip("revoke_sign_in_sessions", user_id=user_id)
            return
        url = f"{self._settings.graph_base_url}/users/{_q(user_id)}/revokeSignInSessions"
        self._request("POST", url, expected=(200,))

    def get_manager_id(self, user_id: str) -> str | None:
        url = f"{self._settings.graph_base_url}/users/{_q(user_id)}/manager"
        response = self._request("GET", url, params={"$select": "id"}, expected=(200, 404))
        if response.status_code == 404:
            return None
        return response.json().get("id")

    def ensure_manager_set(self, user_id: str, manager_id: str) -> bool:
        """Idempotent manager assignment. Returns True if changed."""
        if self.get_manager_id(user_id) == manager_id:
            return False
        if self._settings.dry_run:
            self._dry_run_skip("set_manager", user_id=user_id, manager_id=manager_id)
            return True
        url = f"{self._settings.graph_base_url}/users/{_q(user_id)}/manager/$ref"
        payload = {"@odata.id": f"{self._settings.graph_base_url}/users/{_q(manager_id)}"}
        self._request("PUT", url, json_body=payload, expected=(204,))
        return True

    def list_temporary_access_pass_methods(self, user_id: str) -> list[dict[str, Any]]:
        url = f"{self._settings.graph_base_url}/users/{_q(user_id)}/authentication/temporaryAccessPassMethods"
        return self._get_all_pages(url)

    def issue_temporary_access_pass(
        self, user_id: str, lifetime_minutes: int = 480
    ) -> dict[str, Any] | None:
        """Idempotent: no-ops (returns None) if a *usable* access pass already
        exists for this user. A method that exists but is expired/already
        consumed (isUsable=False) does not block issuing a fresh one."""
        existing = self.list_temporary_access_pass_methods(user_id)
        if any(method.get("isUsable") for method in existing):
            return None
        if self._settings.dry_run:
            self._dry_run_skip("issue_temporary_access_pass", user_id=user_id)
            return None
        url = f"{self._settings.graph_base_url}/users/{_q(user_id)}/authentication/temporaryAccessPassMethods"
        response = self._request(
            "POST", url, json_body={"lifetimeInMinutes": lifetime_minutes}, expected=(201,)
        )
        return response.json()

    def send_mail(self, sender: str, subject: str, body_html: str, to_addresses: list[str]) -> None:
        """Sends mail as `sender` (a UPN or object id) — for the joiner welcome
        mail this must be a dedicated service/no-reply mailbox, never the
        just-created user (see flows/joiner.py: the new mailbox isn't
        provisioned yet and sendMail as it fails with
        MailboxNotEnabledForRESTAPI)."""
        if self._settings.dry_run:
            self._dry_run_skip("send_mail", sender=sender, to=to_addresses)
            return
        url = f"{self._settings.graph_base_url}/users/{_q(sender)}/sendMail"
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
        values = self._get_all_pages(
            url, params={"$filter": f"displayName eq '{_escape_odata_string(display_name)}'"}
        )
        return values[0] if values else None

    def get_member_of_group_ids(self, user_id: str) -> set[str]:
        """Returns the full set of group ids this user is a direct member of,
        via a single paginated GET /users/{id}/memberOf. This replaces the
        old per-group `GET /groups/{id}/members/{user-id}/$ref` check, which
        is not a valid Graph read (it returns 400, not a clean 404-for-absent)
        — one memberOf read covers every group membership question for the
        whole event, instead of one Graph call per candidate group."""
        url = f"{self._settings.graph_base_url}/users/{_q(user_id)}/memberOf"
        items = self._get_all_pages(url, params={"$select": "id"})
        return {item["id"] for item in items if "id" in item}

    def ensure_group_member(
        self, group_id: str, user_id: str, *, member_of_ids: set[str] | None = None
    ) -> bool:
        """Adds user_id to group_id unless already a member. Returns True if changed.

        `member_of_ids`, if supplied, is a pre-fetched result of
        `get_member_of_group_ids(user_id)` — callers processing multiple
        groups for the same user in one flow (joiner's group application,
        mover's reconciliation) should fetch it once and pass it in, rather
        than triggering a fresh memberOf read per group.
        """
        if member_of_ids is None:
            member_of_ids = self.get_member_of_group_ids(user_id)
        if group_id in member_of_ids:
            return False
        if self._settings.dry_run:
            self._dry_run_skip("add_group_member", group_id=group_id, user_id=user_id)
            return True
        url = f"{self._settings.graph_base_url}/groups/{_q(group_id)}/members/$ref"
        payload = {
            "@odata.id": f"{self._settings.graph_base_url}/directoryObjects/{_q(user_id)}"
        }
        try:
            self._request("POST", url, json_body=payload, expected=(204,))
        except GraphApiError as exc:
            if exc.looks_like_already_exists():
                logger.info(
                    "ensure_group_member: concurrent add of %s to %s detected as "
                    "already-exists; treating as success",
                    user_id, group_id,
                )
                return True
            raise
        return True

    def ensure_group_member_removed(
        self, group_id: str, user_id: str, *, member_of_ids: set[str] | None = None
    ) -> bool:
        if member_of_ids is None:
            member_of_ids = self.get_member_of_group_ids(user_id)
        if group_id not in member_of_ids:
            return False
        if self._settings.dry_run:
            self._dry_run_skip("remove_group_member", group_id=group_id, user_id=user_id)
            return True
        url = f"{self._settings.graph_base_url}/groups/{_q(group_id)}/members/{_q(user_id)}/$ref"
        self._request("DELETE", url, expected=(204,))
        return True

    # -- Intune devices (beta) -----------------------------------------------------

    def list_owned_managed_devices(self, user_id: str) -> list[dict[str, Any]]:
        url = f"{self._settings.graph_beta_url}/users/{_q(user_id)}/managedDevices"
        return self._get_all_pages(url, treat_404_as_empty=True)

    def ensure_device_retired(self, device_id: str) -> bool:
        url = f"{self._settings.graph_beta_url}/deviceManagement/managedDevices/{_q(device_id)}"
        current = self._request("GET", url, expected=(200, 404))
        if current.status_code == 404:
            return False  # already gone
        state = current.json().get("managementState")
        if state == "retirePending":
            return False  # already retiring — nothing to do
        if self._settings.dry_run:
            self._dry_run_skip("retire_device", device_id=device_id)
            return True
        retire_url = f"{url}/retire"
        self._request("POST", retire_url, expected=(204, 200))
        return True


def _parse_retry_after(header_value: str | None) -> float:
    """Parses a Retry-After header (seconds form only — Graph always sends
    the numeric-seconds form for 429/503, never the HTTP-date form) with a
    safe fallback when absent or malformed."""
    if not header_value:
        return _DEFAULT_RETRY_AFTER_SECONDS
    try:
        return max(0.0, float(header_value))
    except ValueError:
        return _DEFAULT_RETRY_AFTER_SECONDS
