# NovaIAx — Gap Audit (feature list vs. codebase)

Re-audit produced at the end of the consolidation work. Status legend:

- **implemented** — endpoint(s) + service + persistence + tests exist and are in the v1 router.
- **partial** — some pieces exist but the feature is not end-to-end usable or is missing parts.
- **missing** — no persistence, no service, no route.

The whole API is layered **Router → Command → Service → Repository**, owner-scoped by `user_id`,
and AI-backed features are plan-gated. Audited files are cited as `path:line`.

This revision supersedes the initial audit: every row below was re-checked against the running
`app.openapi()` schema (121 paths / 179 operations) and the code. The suite is green on SQLite
(`730 passed`) and the migrations + core flows were verified against PostgreSQL.

## Summary

| # | Feature | Status | Files |
|---|---------|--------|-------|
| 1 | Medication care — medications | implemented | `app/api/v1/medication_router.py`, `app/services/medication_service.py`, `app/repositories/health_repository.py`, `app/models/health.py` |
| 2 | Medication care — intake logs | implemented | `app/api/v1/medication_router.py` (`POST /me/medications/{id}/intakes`), `app/models/health.py` (`IntakeStatus`) |
| 3 | Medication care — adherence | implemented | `app/api/v1/medication_router.py` (`GET /me/medications/adherence`) |
| 4 | Personal finance — expenses | implemented | `app/api/v1/finance_router.py`, `app/services/finance_service.py`, `app/models/finance.py` |
| 5 | Personal finance — incomes | implemented | `app/api/v1/finance_router.py`, `app/services/finance_service.py` |
| 6 | Personal finance — bills | implemented | `app/api/v1/finance_router.py`, `app/models/finance.py` (`BillStatus`, `BillRecurrence`) |
| 7 | Personal finance — monthly budget | implemented | `app/api/v1/finance_router.py` (`PUT /me/finance/budgets`) |
| 8 | Personal finance — summary | implemented | `app/api/v1/finance_router.py` (`GET /me/finance/summary`) |
| 9 | Study — plans | implemented | `app/api/v1/study_router.py`, `app/services/study_service.py`, `app/models/study.py` |
| 10 | Study — flashcards (spaced repetition) | implemented | `app/api/v1/study_router.py` (`POST /me/study/cards/{id}/reviews`, SM-2) |
| 11 | Study — quiz generation | implemented | `app/api/v1/study_router.py` (`POST /me/study/quiz`), `app/services/study_service.py` |
| 12 | Study — mind map | implemented | `app/api/v1/study_router.py` (`POST /me/study/mind-map`) |
| 13 | Trips / shifts / child-care log | implemented | `app/api/v1/logbook_router.py`, `app/services/logbook_service.py`, `app/models/logbook.py` (`LogKind`) |
| 14 | Log report generation | implemented | `app/api/v1/logbook_router.py` (`GET /me/logs/report`) |
| 15 | Community — reports | implemented | `app/api/v1/community_router.py`, `app/services/community_service.py`, `app/models/community.py` |
| 16 | Community — confirmations | implemented | `app/api/v1/community_router.py` (`POST/DELETE .../reports/{id}/confirm`) |
| 17 | Community — flags | implemented | `app/api/v1/community_router.py` (`POST .../reports/{id}/flag`), `app/repositories/community_repository.py` |
| 18 | Community — user blocks | implemented | `app/api/v1/community_router.py` (`/community/blocks`) |
| 19 | Community — official contacts | implemented | `app/api/v1/community_router.py` (`GET /community/official-contacts`) |
| 20 | Community — draft-complaint | implemented | `app/api/v1/community_router.py` (`/community/drafts`, `POST /community/drafts/generate`) |
| 21 | Community — photo upload w/ EXIF stripping | implemented | `app/api/v1/community_router.py` (`POST .../photo`), `app/utils/image_exif.py` |
| 22 | Community — admin moderation page | implemented | `app/api/admin.py` (`GET /admin/reports`, `POST /admin/reports/{id}/status`), `app/templates/admin/reports.html` |
| 23 | Community — domestic-violence support endpoint (never stores/publishes) | implemented | `app/api/v1/community_router.py` (`GET /community/support`) |
| 24 | Unified `POST /api/v1/agents/chat` → `{message_id, reply, why, sources, proposed_actions}` | implemented | `app/api/v1/agent_orchestrator_router.py`, `app/schemas/agent_orchestration_schemas.py:45` |
| 25 | Emergency/crisis detection (SAMU 192, Polícia 190, CVV 188, Ligue 180) without a model | implemented | `app/services/agent_safety_service.py:27` (all four numbers + Bombeiros 193, structured) |
| 26 | Stable `message_id` on **every** chat response | implemented | `chat_schemas.py:26`, `general_agent_schemas.py:22`, `device_command_schemas.py:35`, `agent_orchestration_schemas.py:52`; minted in `chat_completion_service.py:96`, `device_action_service.py:203`, `agent_orchestrator_service.py:200` |
| 27 | `POST /ai/feedback` validates `message_id` belongs to caller | implemented | `app/api/v1/ai_feedback_router.py`, `app/repositories/ai_feedback_repository.py` |
| 28 | Device-action whitelist + params as discriminated union | implemented | `app/schemas/device_action_schemas.py` (`DeviceAction` union), `app/models/agent_action.py` |
| 29 | No action sends a message / places a call by itself | implemented | `app/schemas/device_action_schemas.py` (`PREPARES_ONLY` = DIAL, COMPOSE_MESSAGE) |
| 30 | Typed `/agents/actions/{id}/confirm|reject|result` models | implemented | `agent_orchestration_schemas.py` (result supports `executed\|failed\|canceled` + `error_code`/`detail`), migration `ab12cd34ef56` |
| 31 | Typed DELETE-conversation responses | implemented | `ConversationClearResponse` (`chat_schemas.py`), `GeneralAgentConversationClearResponse` |
| 32 | Remove “smart-home” wording | implemented | grep: no `smart-home`/`casa inteligente` strings in `app/` or docs |
| 33 | Rate-limit authenticated AI routes per user id (default 8/min) | implemented | `app/middlewares/rate_limit_middleware.py` (`extract_user_id`, `rate_limit:ai:user:{id}`), `config.py` (`rate_limit_ai_user`) |
| 34 | Per-IP only for unauthenticated routes | implemented | `app/middlewares/rate_limit_middleware.py` (falls back to `rate_limit:ai:{ip}` / `rate_limit:general:{ip}`) |
| 35 | `X-Forwarded-For` trusted only when `TRUST_PROXY_HEADERS` | implemented | `app/core/network.py`, `app/core/config.py` (`trust_proxy_headers`) |
| 36 | Login lockout keyed on **email+IP** | implemented | `app/cache/login_rate_limiter.py` (`login_failed:<email>:<ip>`, `_key(email, ip)`) |
| 37 | Generic login/logout responses (no email-existence leak) | implemented | `app/services/auth_service.py` (uniform 401; lockout now email+IP) |
| 38 | First registrant must NOT become admin in production | implemented | `app/services/auth_service.py`, `app/core/config.py` (`first_user_admin` / `first_registrant_becomes_admin`) |
| 39 | Play package name required in prod, default `br.com.novaiax.agente` | implemented | `app/core/config.py` (default + production validation regex in `_validate_production_secrets`) |
| 40 | RTDN rejects unauthenticated pushes (OIDC / shared secret) | implemented | `app/api/v1/subscription_router.py` (`_authorize_rtdn`); tests in `tests/test_billing_and_rtdn.py` |
| 41 | Every route declares `response_model` + status codes; no untyped `{...}` | implemented | `app.openapi()` scan: **0** API routes without `response_model`; `app/schemas/cache_schemas.py`, `privacy.py` |
| 42 | `GET /objectives/` pagination limits/defaults + bare-array documented | implemented | `app/api/v1/objective_router.py:38` (`offset ge=0`, `limit ge=1 le=100`); documented in `API_REFERENCE.md` §2 |
| 43 | `/cache/flush` only deletes conversations when `include_conversations=true` | implemented | `app/api/v1/cache_router.py:27`, `app/services/cache_flush_service.py`, `app/schemas/cache_schemas.py` |

## Cross-cutting facts

- **AI guardrails already present**: `agent_safety_service.scan` short-circuits crisis turns before
  any model call (`agent_orchestrator_service.py`), and medical/financial/legal disclaimers are
  appended (`agent_safety_service.py`). The finance/medication/study/logbook/community services keep
  the “never diagnose / never change doses / never give financial-legal advice” rule and carry a
  `disclaimer` field on every AI-generated response.
- **Plan gating** vocabulary lives in `app/clients/plan_limits.py` (`FEATURE_*`), enforced by
  `require_plan(feature)` (`app/api/deps.py`). Sub-agent gating is in
  `app/services/subscription_service.py` + `app/models/sub_agent.py` (`min_plan`).
- **Migrations**: single linear chain, head `a2b3c4d5e6f7` (config center: `app_config`, `plan_limit_overrides`, `ai_provider_settings` + `region`/`url`/`source` on `official_contacts`; previous head `f1a2b3c4d5e6` was promotions: `promo_codes`, `promo_redemptions`, `promotions`; before that `e1f2a3b4c5d6` was the agents console: `prompt_versions` + catalog metadata on `sub_agents`/`system_prompts`; before that, `d2b3c4d5e6f7` added the admin console — `users.admin_totp_secret`, `admin_sessions`, `admin_audit_log` — plus RLS enable on all `public` tables). All new tables use FK `ondelete` rules
  and `DROP TYPE` guards. Verified end-to-end on a fresh PostgreSQL database (`alembic upgrade head`).
- **Tests** run on SQLite + in-memory Redis via `TestClient` (`730 passed`, ruff + mypy clean). The
  core flows were additionally exercised against PostgreSQL + Redis with `scripts/seed_dev.py` and
  `scripts/smoke_test.py` (`15 passed, 1 skipped` — the skip needs a Groq key with quota).
