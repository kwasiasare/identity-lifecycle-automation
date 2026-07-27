"""In-memory GraphClient double used by every flow test.

Implements the same public method surface as identity_lifecycle.graph_client.GraphClient
(duck-typed — flows never import GraphClient directly for type checks) with the same
check-before-write idempotency semantics, so replaying an event against this fake
proves the flow's idempotency the same way it would against real Graph.

`self.mutation_calls` records every method call that would have caused a Graph
write, letting tests assert "second run made zero mutation calls".
"""

from __future__ import annotations

import itertools
from typing import Any

_id_counter = itertools.count(1)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{next(_id_counter):06d}"


class FakeGraphClient:
    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {}  # keyed by upn
        self.users_by_id: dict[str, dict[str, Any]] = {}
        self.groups: dict[str, dict[str, Any]] = {}  # keyed by display name
        self.memberships: dict[str, set[str]] = {}  # group_id -> set of user_id
        self.managers: dict[str, str] = {}  # user_id -> manager_id
        self.tap_methods: dict[str, list[dict[str, Any]]] = {}
        self.sent_mail: list[dict[str, Any]] = []
        self.devices_by_user: dict[str, list[dict[str, Any]]] = {}
        self.device_states: dict[str, str] = {}
        self.mutation_calls: list[str] = []

    # -- test setup helpers ----------------------------------------------------

    def seed_user(self, upn: str, **attrs: Any) -> dict[str, Any]:
        user = {
            "id": _new_id("user"),
            "userPrincipalName": upn,
            "displayName": attrs.get("displayName", upn),
            "accountEnabled": True,
            "department": None,
            "jobTitle": None,
        }
        user.update(attrs)
        self.users[upn] = user
        self.users_by_id[user["id"]] = user
        return user

    def seed_group(self, name: str) -> dict[str, Any]:
        group = {"id": _new_id("group"), "displayName": name}
        self.groups[name] = group
        self.memberships.setdefault(group["id"], set())
        return group

    def seed_device(self, user_id: str, device_name: str, management_state: str = "managed") -> dict[str, Any]:
        device = {"id": _new_id("device"), "deviceName": device_name}
        self.devices_by_user.setdefault(user_id, []).append(device)
        self.device_states[device["id"]] = management_state
        return device

    # -- users -------------------------------------------------------------------

    def get_user_by_upn(self, upn: str) -> dict[str, Any] | None:
        return self.users.get(upn)

    def ensure_user_exists(self, upn: str, create_payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        existing = self.users.get(upn)
        if existing is not None:
            return existing, False
        self.mutation_calls.append(f"create_user:{upn}")
        user = {"id": _new_id("user"), **create_payload}
        self.users[upn] = user
        self.users_by_id[user["id"]] = user
        return user, True

    def ensure_user_attributes(self, user_id: str, desired: dict[str, Any]) -> bool:
        user = self.users_by_id[user_id]
        diff = {k: v for k, v in desired.items() if user.get(k) != v}
        if not diff:
            return False
        self.mutation_calls.append(f"update_attributes:{user_id}:{sorted(diff)}")
        user.update(diff)
        return True

    def ensure_account_disabled(self, user_id: str) -> bool:
        user = self.users_by_id[user_id]
        if user.get("accountEnabled") is False:
            return False
        self.mutation_calls.append(f"disable_account:{user_id}")
        user["accountEnabled"] = False
        return True

    def revoke_sign_in_sessions(self, user_id: str) -> None:
        self.mutation_calls.append(f"revoke_sessions:{user_id}")

    def send_mail(self, user_id: str, subject: str, body_html: str, to_addresses: list[str]) -> None:
        self.mutation_calls.append(f"send_mail:{user_id}:{to_addresses}")
        self.sent_mail.append(
            {"user_id": user_id, "subject": subject, "body_html": body_html, "to": to_addresses}
        )

    # -- groups -----------------------------------------------------------------

    def get_group_by_name(self, display_name: str) -> dict[str, Any] | None:
        return self.groups.get(display_name)

    def is_group_member(self, group_id: str, user_id: str) -> bool:
        return user_id in self.memberships.get(group_id, set())

    def ensure_group_member(self, group_id: str, user_id: str) -> bool:
        if self.is_group_member(group_id, user_id):
            return False
        self.mutation_calls.append(f"add_member:{group_id}:{user_id}")
        self.memberships.setdefault(group_id, set()).add(user_id)
        return True

    def ensure_group_member_removed(self, group_id: str, user_id: str) -> bool:
        if not self.is_group_member(group_id, user_id):
            return False
        self.mutation_calls.append(f"remove_member:{group_id}:{user_id}")
        self.memberships[group_id].discard(user_id)
        return True

    def list_member_of_group_ids(self, user_id: str, candidate_group_ids: set[str]) -> set[str]:
        return {gid for gid, members in self.memberships.items() if user_id in members} & candidate_group_ids

    # -- manager ------------------------------------------------------------------

    def get_manager_id(self, user_id: str) -> str | None:
        return self.managers.get(user_id)

    def ensure_manager_set(self, user_id: str, manager_id: str) -> bool:
        if self.managers.get(user_id) == manager_id:
            return False
        self.mutation_calls.append(f"set_manager:{user_id}:{manager_id}")
        self.managers[user_id] = manager_id
        return True

    # -- temporary access pass -----------------------------------------------------

    def list_temporary_access_pass_methods(self, user_id: str) -> list[dict[str, Any]]:
        return self.tap_methods.get(user_id, [])

    def issue_temporary_access_pass(self, user_id: str, lifetime_minutes: int = 480) -> dict[str, Any] | None:
        if self.tap_methods.get(user_id):
            return None
        self.mutation_calls.append(f"issue_tap:{user_id}")
        method = {"id": _new_id("tap"), "temporaryAccessPass": "TAP-123456", "lifetimeInMinutes": lifetime_minutes}
        self.tap_methods.setdefault(user_id, []).append(method)
        return method

    # -- devices ----------------------------------------------------------------

    def list_owned_managed_devices(self, user_id: str) -> list[dict[str, Any]]:
        return list(self.devices_by_user.get(user_id, []))

    def ensure_device_retired(self, device_id: str) -> bool:
        state = self.device_states.get(device_id)
        if state == "retirePending":
            return False
        self.mutation_calls.append(f"retire_device:{device_id}")
        self.device_states[device_id] = "retirePending"
        return True
