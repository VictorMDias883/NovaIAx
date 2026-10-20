#!/usr/bin/env python
"""End-to-end smoke test against a running NovaIAx API.

Walks the exact path a mobile client takes on first launch, in order, so a
release can be verified without a device:

    register -> login -> me -> refresh -> objectives -> roadmap days
    -> assistant -> general chat -> device command -> cache flush -> logout

Every step asserts on the real response contract (status code and the fields
the app reads), so a field rename or a changed status code fails here instead
of on a phone.  Steps that call the AI provider report their outcome without
aborting the run: an exhausted quota (``ai_rate_limited``), a saturated queue
(``ai_busy``) or a missing API key are expected in local setups and are not
bugs — but they are reported as ``SKIPPED`` so they cannot pass unnoticed.

Usage::

    python scripts/smoke_test.py http://localhost:8000
    python scripts/smoke_test.py http://localhost:8000 --email me@example.com --password 'S3nh@Forte!'

Only ``httpx`` is required — the script talks to the API over HTTP, so it also
verifies a real deployment, not just the Python objects.

Exit code is ``0`` when every step passed or was legitimately skipped, ``1``
otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import string
import sys
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

try:
    import httpx
except ImportError:  # pragma: no cover - dependency guard
    print("httpx is required: pip install httpx", file=sys.stderr)
    raise SystemExit(2) from None

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

#: Statuses the AI endpoints legitimately return when the provider cannot be
#: used.  Anything else is a real failure.
AI_TOLERATED_STATUSES = {400, 429, 502, 503, 504}


class StepError(Exception):
    """A step did not behave as the contract requires."""


class Runner:
    """Drives the flow and records the outcome of every step."""

    def __init__(self, base_url: str, email: str, password: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.password = password
        self.timeout = timeout
        self.client = httpx.Client(base_url=self.base_url, timeout=timeout)
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.user_id: int | None = None
        self.objective_id: int | None = None
        self.results: list[tuple[str, str, str]] = []
        self.failures = 0

    # -- plumbing ---------------------------------------------------------

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"} if self.access_token else {}

    def call(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
        expect: tuple[int, ...] = (200,),
        label: str = "",
    ) -> Any:
        """Perform a request and return the decoded JSON body.

        Raises:
            StepError: If the status is not in ``expect``.
        """
        try:
            response = self.client.request(
                method,
                path,
                json=json_body,
                params=params,
                headers=self.auth_headers(),
            )
        except httpx.HTTPError as exc:
            raise StepError(f"request failed: {exc}") from exc

        if response.status_code not in expect:
            raise StepError(f"expected {expect}, got {response.status_code}: {_body(response)}")

        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    def record(self, name: str, status: str, detail: str = "") -> None:
        self.results.append((name, status, detail))
        if status == FAIL:
            self.failures += 1
        icon = {PASS: "ok  ", FAIL: "FAIL", SKIP: "skip"}[status]
        print(f"  [{icon}] {name}" + (f" - {detail}" if detail else ""), flush=True)

    def run(self, name: str, func) -> None:
        """Execute one step, recording PASS/FAIL/SKIP instead of aborting."""
        print(f"\n> {name}", flush=True)
        try:
            detail = func()
        except StepError as exc:
            self.record(name, FAIL, str(exc))
        except _Skipped as exc:
            self.record(name, SKIP, str(exc))
        else:
            self.record(name, PASS, detail or "")

    def close(self) -> None:
        self.client.close()

    # -- steps ------------------------------------------------------------

    def step_health(self) -> str:
        body = self.call("GET", "/health", label="health")
        status = body.get("status") if isinstance(body, dict) else None
        if status not in {"ok", "healthy"}:
            raise StepError(f"unexpected health status: {status!r}")
        return f"status={status}"

    def step_register(self) -> str:
        """Register the account; an existing email is not a failure."""
        body = {
            "full_name": "Smoke Test",
            "email": self.email,
            "password": self.password,
            "birth_date": "1990-01-01",
        }
        response = self.client.post("/api/v1/auth/register", json=body)
        if response.status_code == 409:
            # Already registered: log in instead so the flow can continue.
            raise StepError("account already exists (409); delete it or pass a different --email")
        if response.status_code != 201:
            raise StepError(f"expected 201, got {response.status_code}: {_body(response)}")

        data = response.json()
        for field in ("user", "access_token", "refresh_token"):
            if not data.get(field):
                raise StepError(f"register response is missing '{field}'")
        self.access_token = data["access_token"]
        self.refresh_token = data["refresh_token"]
        self.user_id = data["user"]["id"]
        return f"user_id={self.user_id}"

    def step_login(self) -> str:
        data = self.call(
            "POST",
            "/api/v1/auth/login",
            json_body={"email": self.email, "password": self.password},
            expect=(200,),
            label="login",
        )
        for field in ("access_token", "refresh_token"):
            if not data.get(field):
                raise StepError(f"login response is missing '{field}'")
        self.access_token = data["access_token"]
        self.refresh_token = data["refresh_token"]
        self.user_id = data["user"]["id"]
        return f"user_id={self.user_id}"

    def step_me(self) -> str:
        data = self.call("GET", "/api/v1/auth/me", label="me")
        if data.get("email") != self.email:
            raise StepError(f"/me returned a different account: {data.get('email')!r}")
        return f"email={data['email']}"

    def step_refresh(self) -> str:
        data = self.call(
            "POST",
            "/api/v1/auth/refresh",
            json_body={"refresh_token": self.refresh_token},
            label="refresh",
        )
        if not data.get("access_token"):
            raise StepError("refresh response is missing 'access_token'")
        self.access_token = data["access_token"]
        self.refresh_token = data.get("refresh_token", self.refresh_token)
        return "new access token issued"

    def step_objectives(self) -> str:
        """Create one objective; this is the step that spends an AI call."""
        due = (datetime.now(UTC) + timedelta(days=30)).isoformat()
        response = self.client.post(
            "/api/v1/objectives/register",
            json={
                "title": f"Smoke test {uuid.uuid4().hex[:6]}",
                "description": "Created by scripts/smoke_test.py",
                "due_date": due,
            },
            headers=self.auth_headers(),
        )
        if response.status_code != 200:
            if response.status_code in AI_TOLERATED_STATUSES:
                code = _ai_code(response)
                if response.status_code == 429 and code is None:
                    raise _Skipped("the API's own rate limit (per-IP) was hit; wait a minute and re-run")
                raise _Skipped(f"AI unavailable ({response.status_code} {code or 'no code'})")
            raise StepError(f"expected 200, got {response.status_code}: {_body(response)}")

        data = response.json()
        self.objective_id = data.get("id")
        if not self.objective_id:
            raise StepError("register response is missing 'id'")
        days = data.get("days") or []
        return f"objective_id={self.objective_id}, days={len(days)}"

    def step_roadmap_days(self) -> str:
        if self.objective_id is None:
            raise _Skipped("no objective to list days for")
        data = self.call(
            "GET",
            f"/api/v1/objectives/{self.objective_id}/roadmap/days",
            label="roadmap days",
        )
        if "days" not in data:
            raise StepError("days response is missing 'days'")
        days = data["days"]
        if len(days) != 7:
            raise StepError(f"expected 7 roadmap days, got {len(days)}")
        missing = [key for key in ("id", "day_number", "day_date", "status") if key not in days[0]]
        if missing:
            raise StepError(f"roadmap day is missing {missing}")
        return f"{len(days)} days, first status={days[0]['status']}"

    def step_update_day(self) -> str:
        """Mark the first day complete, the way the app does when ticking it."""
        if self.objective_id is None:
            raise _Skipped("no objective to update")
        days = self.call(
            "GET",
            f"/api/v1/objectives/{self.objective_id}/roadmap/days",
            label="roadmap days",
        )["days"]
        first = days[0]
        updated = self.call(
            "PATCH",
            f"/api/v1/objectives/{self.objective_id}/roadmap/days/{first['id']}",
            json_body={"status": "COMPLETED"},
            label="update day",
        )
        if updated.get("status") != "COMPLETED":
            raise StepError(f"day status was not applied: {updated.get('status')!r}")
        return f"day {first['day_number']} -> COMPLETED"

    def step_assistant(self) -> str:
        response = self.client.post(
            "/api/v1/objectives/assistant",
            json={"user_message": "Quero aprender Python assíncrono esta semana."},
            headers=self.auth_headers(),
        )
        return _ai_step(response, "objective assistant")

    def step_general_chat(self) -> str:
        response = self.client.post(
            "/api/v1/agents/general/chat",
            json={"user_message": "Como está meu progresso hoje?"},
            headers=self.auth_headers(),
        )
        return _ai_step(response, "general agent")

    def step_device_command(self) -> str:
        response = self.client.post(
            "/api/v1/agents/device/command",
            json={"user_message": "Ligue a luz da sala"},
            headers=self.auth_headers(),
        )
        return _ai_step(response, "device agent")

    def step_community_report(self) -> str:
        """Create, list and delete a community report (no AI involved)."""
        created = self.call(
            "POST",
            "/api/v1/community/reports",
            json_body={
                "category": "infrastructure",
                "title": "Relato do smoke test",
                "description": "Criado por scripts/smoke_test.py",
            },
            expect=(201,),
            label="community report",
        )
        report_id = created.get("id")
        if report_id is None:
            raise StepError("report create response is missing 'id'")

        page = self.call("GET", "/api/v1/community/reports", label="community list")
        if not any(item.get("id") == report_id for item in page.get("items", [])):
            raise StepError("the created report is not in the paginated list")

        self.call(
            "DELETE",
            f"/api/v1/community/reports/{report_id}",
            expect=(204,),
            label="community delete",
        )
        return f"report_id={report_id} created, listed and deleted"

    def step_data_export(self) -> str:
        """Download the portable account bundle and check its typed envelope."""
        data = self.call("GET", "/api/v1/me/export", label="data export")
        if data.get("schema_version") is None:
            raise StepError("export bundle is missing 'schema_version'")
        if not isinstance(data.get("user"), dict):
            raise StepError("export bundle is missing the typed 'user' section")
        return f"schema_version={data['schema_version']}"

    def step_consent_withdraw(self) -> str:
        """Grant then withdraw an opt-in consent (exercises the typed DELETE)."""
        self.call(
            "PUT",
            "/api/v1/me/consents",
            json_body={"type": "learning"},
            label="grant consent",
        )
        data = self.call("DELETE", "/api/v1/me/consents/learning", label="withdraw consent")
        if not data.get("message"):
            raise StepError("withdraw response is missing 'message'")
        return "learning consent granted then withdrawn"

    def step_cache_flush(self) -> str:
        data = self.call(
            "POST",
            "/api/v1/cache/flush",
            params={"include_conversations": "false"},
            label="cache flush",
        )
        cleared = data.get("cleared")
        if not isinstance(cleared, dict):
            raise StepError(f"flush response has no 'cleared' counts: {data}")
        total = sum(v for v in cleared.values() if isinstance(v, int))
        return f"{total} keys cleared"

    def step_logout(self) -> str:
        self.call(
            "POST",
            "/api/v1/auth/logout",
            json_body={"refresh_token": self.refresh_token},
            label="logout",
        )
        # The refresh token is denied now, so a refresh must fail.
        response = self.client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": self.refresh_token},
        )
        if response.status_code != 401:
            raise StepError(f"a revoked refresh token must be rejected with 401, got {response.status_code}")
        self.access_token = None
        return "refresh token revoked and rejected afterwards"

    # -- driver -----------------------------------------------------------

    def execute(self) -> int:
        print(f"NovaIAx smoke test -> {self.base_url}")
        print(f"account           -> {self.email}")

        self.run("1. Health", self.step_health)
        self.run("2. Register", self.step_register)
        self.run("3. Login", self.step_login)
        self.run("4. Me", self.step_me)
        self.run("5. Refresh token", self.step_refresh)
        self.run("6. Register objective", self.step_objectives)
        self.run("7. List roadmap days", self.step_roadmap_days)
        self.run("8. Update roadmap day", self.step_update_day)
        self.run("9. Objective assistant", self.step_assistant)
        self.run("10. General agent", self.step_general_chat)
        self.run("11. Device agent", self.step_device_command)
        self.run("12. Community report", self.step_community_report)
        self.run("13. Data export", self.step_data_export)
        self.run("14. Consent withdraw", self.step_consent_withdraw)
        self.run("15. Cache flush", self.step_cache_flush)
        self.run("16. Logout", self.step_logout)

        print("\n" + "=" * 62)
        passed = sum(1 for _, status, _ in self.results if status == PASS)
        skipped = sum(1 for _, status, _ in self.results if status == SKIP)
        failed = self.failures
        print(f"{passed} passed, {failed} failed, {skipped} skipped")
        if skipped:
            print("Skipped steps need a configured GROQ_API_KEY with quota to be exercised,")
            print(
                "and no per-IP rate limit (60/min by default) — this script makes "
                f"{len(self.results)} requests."
            )
        if failed:
            print("\nFailures:")
            for name, status, detail in self.results:
                if status == FAIL:
                    print(f"  - {name}: {detail}")
        print("=" * 62)
        return 1 if failed else 0


class _Skipped(Exception):
    """The step could not run here (usually a missing AI quota)."""


def _body(response: httpx.Response) -> str:
    """Return a short, safe rendering of a response for error messages."""
    text = response.text
    return text if len(text) <= 300 else text[:300] + "..."


def _ai_code(response: httpx.Response) -> str | None:
    """Return the machine-readable ``code`` from an AI error body, if any.

    ``None`` means the 429 came from the API's own rate limiter rather than
    from the AI provider — a very different thing to report.
    """
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    code = payload.get("code")
    return str(code) if code else None


def _ai_step(response: httpx.Response, what: str) -> str:
    """Shared handling for the three AI-backed steps."""
    if response.status_code == 200:
        data = response.json()
        message = data.get("assistant_message")
        if not isinstance(message, str) or not message.strip():
            raise StepError(f"{what}: response has no 'assistant_message'")
        return f"assistant replied ({len(message)} chars)"
    if response.status_code == 429 and _ai_code(response) is None:
        raise _Skipped(
            "the API's own rate limit (per-IP) was hit; wait a minute and re-run "
            "to exercise this step"
        )
    if response.status_code in AI_TOLERATED_STATUSES:
        raise _Skipped(f"AI unavailable ({response.status_code} {_ai_code(response) or 'no code'})")
    raise StepError(f"{what}: expected 200, got {response.status_code}: {_body(response)}")


def generate_password() -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%&*"
    required = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
    ]
    chars = required + [secrets.choice(alphabet) for _ in range(13)]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("base_url", nargs="?", default="http://localhost:8000", help="API base URL")
    parser.add_argument("--email", default=f"smoke-{uuid.uuid4().hex[:8]}@novaiax.dev", help="account to create")
    parser.add_argument("--password", default=None, help="account password (default: $SMOKE_PASSWORD or random)")
    parser.add_argument("--timeout", type=float, default=90.0, help="per-request timeout in seconds")
    parser.add_argument("--json", action="store_true", help="print the results as JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    password = args.password or os.getenv("SMOKE_PASSWORD") or generate_password()

    runner = Runner(args.base_url, args.email, password, args.timeout)
    try:
        code = runner.execute()
    finally:
        runner.close()

    if args.json:
        print(json.dumps([{"step": n, "status": s, "detail": d} for n, s, d in runner.results], indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
