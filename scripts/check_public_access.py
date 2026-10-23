#!/usr/bin/env python
"""Check public API access and auth error contracts against a running server."""

from __future__ import annotations

import argparse
import secrets
import string
import uuid
from typing import Any

import httpx


class PublicAccessCheck:
    def __init__(self, base_url: str, timeout: float) -> None:
        self.client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, follow_redirects=False)
        self.results: list[tuple[str, bool, int, str]] = []
        self.statuses: dict[tuple[str, str], list[int]] = {}

    def request(
        self,
        name: str,
        method: str,
        path: str,
        *,
        expected_status: int | set[int],
        expected_code: str | None = None,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
    ) -> httpx.Response | None:
        try:
            response = self.client.request(method, path, headers=headers, json=json_body, data=data)
        except httpx.HTTPError as exc:
            self.results.append((name, False, 0, str(exc)))
            return None

        try:
            payload = response.json()
        except ValueError:
            payload = {}
        code = payload.get("code", "") if isinstance(payload, dict) else ""
        accepted_statuses = {expected_status} if isinstance(expected_status, int) else expected_status
        passed = response.status_code in accepted_statuses
        if expected_code is not None:
            passed = passed and code == expected_code
        detail = f"status={response.status_code} code={code or '-'}"
        self.results.append((name, passed, response.status_code, code or detail))
        self.statuses.setdefault((method, path), []).append(response.status_code)
        return response

    def public_pair(self, method: str, path: str, expected_status: int) -> None:
        for label, headers in (("no-auth", {}), ("garbage-bearer", {"Authorization": "Bearer garbage"})):
            response = self.request(
                f"{method} {path} [{label}]", method, path, expected_status=expected_status, headers=headers
            )
            if path == "/" and label == "no-auth" and response is not None:
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
                valid_root = payload.get("name") == "NovaIAx API" and payload.get("status") == "ok" and bool(
                    payload.get("version")
                )
                self.results.append(("GET / [body]", valid_root, response.status_code, "root response fields"))

    def run(self) -> int:
        public_gets = (
            ("/", 200),
            ("/health", {200, 503}),
            ("/api/v1/app-config", 200),
            ("/privacy", 200),
            ("/terms", 200),
            ("/account-deletion", 200),
            ("/docs", 200),
            ("/redoc", 200),
            ("/openapi.json", 200),
            ("/admin/login", 200),
            ("/admin/static/no-such-file.css", 404),
        )
        for path, expected in public_gets:
            self.public_pair("GET", path, expected)

        for label, headers in (("no-auth", {}), ("garbage-bearer", {"Authorization": "Bearer garbage"})):
            self.request(f"HEAD /health [{label}]", "HEAD", "/health", expected_status={200, 503}, headers=headers)

        self._check_public_forms()
        self._check_auth_flow()
        self._check_protected_route()
        self._check_trailing_slashes()
        self._print_results()
        return 1 if any(not passed for _, passed, _, _ in self.results) else 0

    def _check_public_forms(self) -> None:
        csrf_response = self.client.get("/account-deletion")
        csrf = self.client.cookies.get("novaiax_public_csrf", "")
        if csrf_response.status_code != 200 or not csrf:
            self.results.append(("GET /account-deletion [CSRF seed]", False, csrf_response.status_code, "missing CSRF cookie"))
            return
        data = {"email": "not-registered@example.invalid", "password": "WrongPassword123", "csrf_token": csrf}
        for label, headers in (("no-auth", {}), ("garbage-bearer", {"Authorization": "Bearer garbage"})):
            self.request(
                f"POST /account-deletion/request [{label}]",
                "POST",
                "/account-deletion/request",
                expected_status=200,
                headers=headers,
                data=data,
            )

        admin_page = self.client.get("/admin/login")
        admin_csrf = self.client.cookies.get("novaiax_admin_login_csrf", "")
        if admin_page.status_code != 200 or not admin_csrf:
            self.results.append(("GET /admin/login [CSRF seed]", False, admin_page.status_code, "missing CSRF cookie"))
            return
        admin_data = {
            "email": "not-an-admin@example.invalid",
            "password": "WrongPassword123",
            "csrf_token": admin_csrf,
        }
        responses = [
            self.client.post("/admin/login", data=admin_data, headers=headers)
            for headers in ({}, {"Authorization": "Bearer garbage"})
        ]
        same_panel_result = responses[0].status_code == responses[1].status_code == 401
        self.results.append(
            ("POST /admin/login [Bearer ignored]", same_panel_result, responses[1].status_code, "panel auth unchanged")
        )

        for label, headers in (("no-auth", {}), ("garbage-bearer", {"Authorization": "Bearer garbage"})):
            response = self.client.post("/api/v1/billing/google/rtdn", json={}, headers=headers)
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            code = payload.get("code", "") if isinstance(payload, dict) else ""
            detail = payload.get("detail", "") if isinstance(payload, dict) else ""
            accepted = response.status_code in {200, 401, 503} and detail != "Autenticação necessária."
            self.results.append((f"POST /api/v1/billing/google/rtdn [{label}]", accepted, response.status_code, code or "Pub/Sub verifier"))

    def _check_auth_flow(self) -> None:
        email = f"public-check-{uuid.uuid4().hex}@example.com"
        password = self._password()
        body = {"full_name": "Public Access Check", "email": email, "password": password, "birth_date": "1990-01-01"}
        registered = self.request("POST /auth/register [no-auth]", "POST", "/api/v1/auth/register", expected_status=201, json_body=body)
        if registered is None or registered.status_code != 201:
            return
        duplicate = self.request(
            "POST /auth/register [garbage-bearer]",
            "POST",
            "/api/v1/auth/register",
            expected_status=409,
            expected_code="conflict",
            headers={"Authorization": "Bearer garbage"},
            json_body=body,
        )
        login = self.request(
            "POST /auth/login [no-auth]",
            "POST",
            "/api/v1/auth/login",
            expected_status=200,
            json_body={"email": email, "password": password},
        )
        garbage_login = self.request(
            "POST /auth/login [garbage-bearer]",
            "POST",
            "/api/v1/auth/login",
            expected_status=200,
            json_body={"email": email, "password": password},
            headers={"Authorization": "Bearer garbage"},
        )
        wrong = self.request(
            "POST /auth/login [wrong password]",
            "POST",
            "/api/v1/auth/login",
            expected_status=401,
            expected_code="invalid_credentials",
            json_body={"email": email, "password": "WrongPassword123"},
            headers={"Authorization": "Bearer garbage"},
        )
        if login is None or login.status_code != 200:
            return

        refresh = self.request(
            "POST /auth/refresh [no-auth]",
            "POST",
            "/api/v1/auth/refresh",
            expected_status=200,
            json_body={"refresh_token": login.json()["refresh_token"]},
        )
        if refresh is None or refresh.status_code != 200:
            return
        refresh_with_stale_header = self.request(
            "POST /auth/refresh [garbage-bearer]",
            "POST",
            "/api/v1/auth/refresh",
            expected_status=200,
            json_body={"refresh_token": refresh.json()["refresh_token"]},
            headers={"Authorization": "Bearer garbage"},
        )
        if refresh_with_stale_header is None or refresh_with_stale_header.status_code != 200:
            return
        logout = self.request(
            "POST /auth/logout [no-auth]",
            "POST",
            "/api/v1/auth/logout",
            expected_status=200,
            json_body={"refresh_token": refresh_with_stale_header.json()["refresh_token"]},
        )
        if logout is not None and logout.status_code == 200:
            self.request(
                "POST /auth/logout [garbage-bearer]",
                "POST",
                "/api/v1/auth/logout",
                expected_status=200,
                json_body={"refresh_token": refresh_with_stale_header.json()["refresh_token"]},
                headers={"Authorization": "Bearer garbage"},
            )
        _ = duplicate, garbage_login, wrong

    def _check_protected_route(self) -> None:
        for label, headers in (("no-auth", {}), ("garbage-bearer", {"Authorization": "Bearer garbage"})):
            self.request(
                f"GET /api/v1/objectives/ [{label}]",
                "GET",
                "/api/v1/objectives/",
                expected_status=401,
                expected_code="authentication_required",
                headers=headers,
            )

    def _check_trailing_slashes(self) -> None:
        candidates = [
            ("GET", path) for path, _ in (
                ("/", 200),
                ("/health", 200),
                ("/api/v1/app-config", 200),
                ("/privacy", 200),
                ("/terms", 200),
                ("/account-deletion", 200),
                ("/docs", 200),
                ("/redoc", 200),
                ("/openapi.json", 200),
                ("/admin/login", 200),
                ("/admin/static/no-such-file.css", 404),
            )
        ]
        candidates.append(("GET", "/api/v1/objectives/"))
        for method, path in candidates:
            if path == "/":
                continue
            variant = path[:-1] if path.endswith("/") else f"{path}/"
            headers = {"X-Forwarded-Proto": "https"}
            slashed = self.client.request(method, variant, headers=headers)
            canonical_statuses = self.statuses.get((method, path), [])
            same_status = bool(canonical_statuses) and slashed.status_code == canonical_statuses[0]
            no_redirect = not 300 <= slashed.status_code < 400
            no_http_location = not slashed.headers.get("location", "").startswith("http://")
            passed = same_status and no_redirect and no_http_location
            code = self._error_code(slashed)
            self.results.append((f"slash {method} {path}", passed, slashed.status_code, code or "no redirect"))

    def _print_results(self) -> None:
        print(f"{'RESULT':<7} {'STATUS':<7} {'CODE':<30} ENDPOINT")
        for name, passed, status, code in self.results:
            print(f"{'PASS' if passed else 'FAIL':<7} {status:<7} {code[:29]:<30} {name}")
        failures = sum(not passed for _, passed, _, _ in self.results)
        print(f"\n{len(self.results) - failures} passed, {failures} failed")

    @staticmethod
    def _error_code(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return ""
        return str(payload.get("code", "")) if isinstance(payload, dict) else ""

    @staticmethod
    def _password() -> str:
        alphabet = string.ascii_letters + string.digits + "!@#$%&*"
        return "Aa1!" + "".join(secrets.choice(alphabet) for _ in range(16))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url", help="API base URL, for example https://novaiax-gateway.onrender.com")
    parser.add_argument("--timeout", type=float, default=10.0, help="per-request timeout in seconds")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    checker = PublicAccessCheck(args.base_url, args.timeout)
    try:
        return checker.run()
    finally:
        checker.client.close()


if __name__ == "__main__":
    raise SystemExit(main())
