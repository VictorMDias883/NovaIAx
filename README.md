# NovaIAx

API Gateway construído com **FastAPI** que centraliza autenticação, autorização, rate limiting, cache, logs estruturados e proxy reverso para serviços downstream — com integração nativa a IA (Groq Cloud) para chat e criação assistida de objetivos.

## Funcionalidades

- **Autenticação JWT** — access (15 min) e refresh (7 dias) tokens, com fluxo baseado em banco de dados (PostgreSQL) e hash de senha PBKDF2-SHA256.
- **RBAC** — papéis `USER` e `ADMIN`; o primeiro usuário registrado vira admin automaticamente. Gestão completa de usuários (promover, rebaixar, excluir, listar paginado).
- **Rate limiting** — janela deslizante por IP: 60 req/min em endpoints gerais e 10 req/min em endpoints de IA (`/ai/`), com headers `X-RateLimit-*` e `Retry-After`.
- **Reverse proxy** — roteia para múltiplos serviços downstream com cache Redis, injeção de headers (`X-Forwarded-For`, `X-Gateway-User`), limite de payload (1 MiB → 413) e timeout configurável (→ 502).
- **Cache Redis** — com fallback automático em memória para desenvolvimento/CI.
- **Segurança** — middleware de security headers (`X-Content-Type-Options`, `X-Frame-Options`, HSTS, etc.) e CORS configurável.
- **Logs estruturados** — toda requisição é registrada em JSON (método, path, status, duração, IP) pronto para agregadores.
- **IA (Groq Cloud)**:
  - `POST /ai/chat` — chat completion usando system prompts gerenciáveis.
  - `POST /objectives/assistant` — assistente conversacional que coleta os dados e registra a meta automaticamente quando recebe um JSON completo.
  - `POST /agents/general/chat` — **agente geral**: assistente conversacional com acesso aos objetivos do usuário e aos dias do roadmap cumpridos.
- **Objetivos** — registro de **metas gerais** (ex.: "aprender inglês") com validação de data futura e **roadmap gerado por IA** contido dentro do objetivo, em janelas de 7 dias: ao criar, o roadmap cobre os próximos 7 dias e decompõe a meta geral em uma **meta básica / objetivo mínimo por dia**; a cada 7 dias o dono (ou admin) chama o endpoint de renovação para gerar a próxima janela, até a data limite.
- **Dias do roadmap** — cada janela de roadmap gera automaticamente os dias (1 a 7), cada um com sua **meta básica** (ex.: "Dia 1 — aprender vocabulário básico de saudações"); o dono (ou admin) marca cada dia como cumprido/pendente/pulado via API, e o agente geral usa esses dados para reportar o progresso.
- **System prompts** — CRUD exclusivo para administradores.

## Stack

| Camada | Tecnologia |
| --- | --- |
| API | FastAPI + Uvicorn |
| ORM | SQLAlchemy 2 (async) |
| Banco | PostgreSQL (asyncpg), SQLite (aiosqlite p/ testes) |
| Cache | Redis (com fallback em memória) |
| Auth | python-jose (JWT) + passlib (PBKDF2) |
| IA | Groq Cloud (httpx) |
| Infra | Docker Compose |
| Testes | pytest + pytest-asyncio |

## Arquitetura

```
Cliente → [Middlewares: RateLimit → Auth → Logging → SecurityHeaders → CORS] → Rota
                                                                                  │
        Router → Command → Service → Repository → Database                       │
        (chat / objectives / assistant / users / system-prompts / proxy)  ───────┘
```

```
main.py
  └── /api/v1
        ├── /auth                  (login, register, refresh, me)
        ├── /users                 (admin: promote, demote, delete, list)
        ├── /objectives            (register, renovar roadmap)
        ├── /objectives/assistant  (assistente IA de metas)
        ├── /objectives/{id}/roadmap/days (dias do roadmap: listar, marcar status)
        ├── /agents/general/chat   (agente geral: objetivos + dias cumpridos)
        ├── /ai/chat               (chat completion com Groq)
        ├── /system-prompts        (admin CRUD)
        └── /proxy/{service}/...   (reverse proxy com cache)
```

### Estrutura do projeto

```
NovaIAx/
├── app/
│   ├── main.py               # Entry point (FastAPI, middlewares, lifespan)
│   ├── api/
│   │   ├── deps.py           # Injeção de dependências (auth, db, redis)
│   │   └── v1/               # Roteadores (auth, users, objectives, roadmap-days, agents, chat, proxy...)
│   ├── cache/redis_client.py # Cliente Redis com fallback em memória
│   ├── clients/ai_client.py  # Abstração AIClient + GroqAIClient
│   ├── commands/             # Objetos de comando (RegisterUser, Login, etc.)
│   ├── core/                 # Config, logging, security (JWT/hash), health, network
│   ├── db/session.py         # Engine assíncrono + Base + init_db (apenas testes)
│   ├── exceptions/handlers.py# Handlers globais de erro
│   ├── middlewares/          # Auth, RateLimit, Logging, SecurityHeaders
│   ├── models/               # User, Objective, RoadmapDay, SystemPrompt (SQLAlchemy)
│   ├── repositories/         # Camada de acesso a dados
│   ├── schemas/              # Modelos Pydantic
│   └── services/             # Regras de negócio
├── migrations/               # Migrações Alembic (env.py, script.py.mako, versions/)
├── tests/                    # Testes de integração e unitários
├── Infra/project.wsd         # Diagrama PlantUML da arquitetura
├── Dockerfile
├── fly.toml                  # Deploy Fly.io (release_command roda migrações)
├── docker-compose.yml        # Gateway
├── docker-compose.data.yml   # Postgres + Redis
├── docker-compose.dev.yml    # Ambiente de desenvolvimento
├── alembic.ini
├── conftest.py
└── requirements.txt
```

## Requisitos

- Python 3.12+
- Docker + Docker Compose (opcional, para Postgres/Redis)
- Chave de API do [Groq](https://console.groq.com/) (para os endpoints de IA)

## Execução

### Local

```bash
# 1. Copie e ajuste as variáveis de ambiente
cp .env.example .env

# 2. Suba apenas os serviços de dados (Postgres + Redis)
docker compose -f docker-compose.data.yml up -d

# 3. Instale as dependências
pip install -r requirements.txt

# 4. Aplique as migrações do banco
alembic upgrade head

# 5. Execute
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### Docker (stack completa)

O `docker-compose.yml` raiz sobe `postgres`, `redis` e a API `api` (com `alembic upgrade head`
rodando automaticamente antes do `uvicorn`):

```bash
docker compose up -d       # Postgres + Redis + Gateway (aplicando migrações)
```

Para inspeção, os arquivos separados continuam disponíveis:

```bash
docker compose -f docker-compose.data.yml up -d                          # apenas Postgres + Redis
docker compose -f docker-compose.dev.yml run --rm api alembic upgrade head  # apenas migrações
docker compose -f docker-compose.dev.yml up --build                      # apenas o Gateway
```

> **Importante:** dentro dos containers o `DATABASE_URL` precisa apontar para o serviço
> `postgres` (ex.: `postgresql+asyncpg://novaiax:novaiax@postgres:5432/novaiax`, como no
> `.env.example`). Se o `.env` local usa um IP/porta do host (`192.168.x.x`), o Gateway em
> container não alcança o banco — ajuste antes de subir a stack.

Acesse a documentação interativa (Swagger) em `http://localhost:8000/docs`.

## Migrações (Alembic)

O schema é versionado com [Alembic](https://alembic.sqlalchemy.org/) e a aplicação
**não** cria tabelas no startup (o antigo `Base.metadata.create_all()` no lifespan foi
removido). Antes de iniciar o app é preciso aplicar as migrações:

```bash
alembic upgrade head        # aplica migrações pendentes
alembic revision --autogenerate -m "descricao"   # gera nova migração a partir dos modelos
```

- O `migrations/env.py` usa o mesmo `async` engine configurado em `app/db/session.py`
  (`DATABASE_URL`) e o `Base.metadata` com todos os modelos registrados em `app/models/__init__.py`.
- Enums (`UserRole`, `RoadmapDayStatus`) viram tipos nativos do Postgres
  (`user_role`, `roadmap_day_status`).
- Em **Fly.io**, o `release_command = "alembic upgrade head"` no `fly.toml` roda as
  migrações num machine temporário antes do novo deploy — se falhar, o deploy é abortado.
- `init_db()` em `app/db/session.py` fica restrito aos testes (o `conftest.py` o chama).

### Testes

```bash
pytest
```

> O `RedisClient` cai para o armazenamento em memória automaticamente quando o Redis não está disponível, então a maioria dos testes funciona sem infraestrutura.

## Variáveis de ambiente

Veja [.env.example](.env.example) para a lista completa.

| Variável | Padrão | Descrição |
| --- | --- | --- |
| `SECRET_KEY` | `change-me-in-production` | Chave de assinatura dos JWT (**troque em produção**) |
| `JWT_ALGORITHM` | `HS256` | Algoritmo de assinatura JWT |
| `ACCESS_TOKEN_TTL_MINUTES` | `15` | Validade do access token |
| `REFRESH_TOKEN_TTL_DAYS` | `7` | Validade do refresh token |
| `DATABASE_URL` | `postgresql+asyncpg://novaiax:novaiax@postgres:5432/novaiax` | Conexão com o banco |
| `REDIS_URL` | `redis://redis:6379/0` | Conexão com o Redis |
| `ALLOWED_ORIGINS` | `http://localhost:3000,http://127.0.0.1:3000` | Origens permitidas no CORS |
| `DEFAULT_ADMIN_PASSWORD` | `changeme123` | Senha do admin padrão do fluxo legado |
| `MASTER_API_KEY` | — | Chave mestre que contorna a validação de API keys |
| `DOWNSTREAM_SERVICES_JSON` | `[{"name":"ai","base_url":"http://mock-ai-service:8001","timeout_seconds":5}]` | Serviços downstream do proxy |
| `RATE_LIMIT_DEFAULT` | `60` | Limite geral (req/min) |
| `RATE_LIMIT_AI` | `10` | Limite para endpoints `/ai/` (req/min) |
| `CACHE_TTL_DEFAULT` | `60` | TTL de cache (s) |
| `CACHE_TTL_AI` | `300` | TTL de cache para respostas de IA (s) |
| `MAX_PAYLOAD_BYTES` | `1048576` | Tamanho máximo do body (1 MiB) |
| `GROQ_API_KEY` | — | Chave da API Groq (**necessária para IA**) |
| `GROQ_API_BASE_URL` | `https://api.groq.com/v1` | Base URL da API Groq |
| `GROQ_API_MODEL` | `llama-3.3-70b-versatile` | Modelo usado nos chat completions |
| `GROQ_API_TIMEOUT_SECONDS` | `10` | Timeout das chamadas de IA |

## Endpoints principais

| Método | Rota | Descrição | Auth |
| --- | --- | --- | --- |
| `POST` | `/api/v1/auth/register` | Registra usuário e retorna JWT | Pública |
| `POST` | `/api/v1/auth/login` | Login por e-mail/senha | Pública |
| `POST` | `/api/v1/auth/refresh` | Renova access/refresh tokens | Pública |
| `GET` | `/api/v1/auth/me` | Identidade do usuário atual | Bearer |
| `POST` | `/api/v1/objectives/register` | Registra uma meta (gera roadmap IA de 7 dias) | Bearer |
| `POST` | `/api/v1/objectives/{id}/roadmap/renew` | Renova o roadmap (janela de 7 dias) | Bearer |
| `GET` | `/api/v1/objectives/{id}/roadmap/days` | Lista os dias do roadmap (owner/admin) | Bearer |
| `PATCH` | `/api/v1/objectives/{id}/roadmap/days/{day_id}` | Marca o dia como cumprido/pendente/pulado | Bearer |
| `POST` | `/api/v1/objectives/assistant` | Assistente IA para criar metas | Bearer |
| `POST` | `/api/v1/agents/general/chat` | Agente geral (objetivos + dias do roadmap cumpridos) | Bearer |
| `POST` | `/api/v1/ai/chat` | Chat completion com Groq | Bearer |
| `PATCH` | `/api/v1/users/{id}/promote` | Promove usuário a admin | Admin |
| `PATCH` | `/api/v1/users/{id}/demote` | Rebaixa um admin | Admin |
| `DELETE` | `/api/v1/users/{id}` | Exclui usuário | Admin |
| `GET` | `/api/v1/users/` | Lista usuários paginado | Admin |
| `GET/POST` | `/api/v1/system-prompts/` | Lista/cria system prompts | Admin |
| `GET/PUT/DELETE` | `/api/v1/system-prompts/{id}` | Consulta/atualiza/exclui prompt | Admin |
| `*` | `/api/v1/proxy/{service}/...` | Proxy para serviços downstream | Bearer |
| `GET` | `/health` | Health check de liveness | Pública |

### Exemplo — login

```bash
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@example.com","password":"sua-senha"}'
```

### Dias do roadmap

Ao criar ou renovar o roadmap de uma meta (o objetivo geral), o sistema registra automaticamente um dia por data da janela (1 a 7), cada um com sua **meta básica / objetivo mínimo** (ex.: "Dia 1 — aprender vocabulário básico de saudações"). O dono (ou admin) marca o progresso:

```bash
# Listar os dias do roadmap de uma meta (cada dia traz a meta básica)
curl http://localhost:8000/api/v1/objectives/1/roadmap/days \
  -H "Authorization: Bearer <access_token>"

# Marcar o dia 2 como cumprido
curl -X PATCH http://localhost:8000/api/v1/objectives/1/roadmap/days/<day_id> \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -d '{"status":"COMPLETED"}'
```

A resposta de `POST /objectives/register` e `POST /objectives/{id}/roadmap/renew` inclui o campo `days`, com a meta básica de cada dia dentro do objetivo.

### Agente geral

O agente geral conversa com o usuário e tem acesso automático aos objetivos cadastrados e aos dias do roadmap já cumpridos:

```bash
curl -X POST http://localhost:8000/api/v1/agents/general/chat \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -d '{"user_message":"Quais metas eu tenho e quantos dias eu já cumpri?"}'
```

## Adicionar um novo downstream

Inclua um novo item em `DOWNSTREAM_SERVICES_JSON`:

```json
[
  {"name": "ai", "base_url": "http://mock-ai-service:8001", "timeout_seconds": 5},
  {"name": "payments", "base_url": "http://payments:8002", "timeout_seconds": 10}
]
```

Em seguida, as rotas ficam disponíveis em `POST /api/v1/proxy/payments/...`.

## Segurança

- Senhas sempre armazenadas com hash PBKDF2-SHA256 (nunca em texto puro).
- Tokens JWT assinados com `SECRET_KEY`; refresh tokens validados contra `refresh_token_valid_after` (revogáveis ao promover/rebaixar).
- Endpoints administrativos protegidos por `require_admin` (403 para não-admins).
- Headers sensíveis (`Authorization`, `Cookie`, `X-API-Key`) não são repassados a serviços downstream.
- Erros não expõem detalhes internos (respostas genéricas + logs com traceback).
