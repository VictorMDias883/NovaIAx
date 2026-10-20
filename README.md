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
- **IA (pool de provedores)** — roteia cada chamada por um **router com fallback por prioridade** (Groq + provedores OpenAI-compatíveis adicionais), com pacer/limite de concorrência por provedor, **circuit breaker** por provedor, **quota diária por usuário** em Redis (abate o orçamento só em sucesso, 429 com `Retry-After` ao estourar) e seleção de modelo por plano/tarefa:
  - `POST /ai/chat` — chat completion usando system prompts gerenciáveis; respostas em cache Redis **por usuário** (a chave inclui o `user_id`, então mensagens idênticas de usuários diferentes não compartilham cache).
  - `POST /objectives/assistant` — assistente conversacional que coleta os dados e registra a meta automaticamente quando recebe um JSON completo.
  - `POST /agents/general/chat` — **agente geral**: assistente conversacional com acesso aos objetivos do usuário e aos dias do roadmap cumpridos.
- **Objetivos** — registro de **metas gerais** (ex.: "aprender inglês") com validação de data futura e **roadmap gerado por IA** contido dentro do objetivo, em janelas de 7 dias: ao criar, o roadmap cobre os próximos 7 dias e decompõe a meta geral em uma **meta básica / objetivo mínimo por dia**; a cada 7 dias o dono (ou admin) chama o endpoint de renovação para gerar a próxima janela, até a data limite.
- **Dias do roadmap** — cada janela de roadmap gera automaticamente os dias (1 a 7), cada um com sua **meta básica** (ex.: "Dia 1 — aprender vocabulário básico de saudações"); o dono (ou admin) marca cada dia como cumprido/pendente/pulado ou remove um dia do roadmap via API, e o agente geral usa esses dados para reportar o progresso.
- **System prompts** — CRUD exclusivo para administradores.
- **Personalização do agente** — catálogo de **grupos de audiência** (15) e **sub-agentes** (6), perfil por usuário (nome, tom, idioma, fuso, grupos/sub-agentes ativos, modo simplificado), **memórias de longo prazo** (CRUD por usuário) e interruptor de **aprendizado automático**: afirmações explícitas viram memórias aprendidas (guardas rígidas para saúde/religião/orientação/política/finanças e só quando o aprendizado está ligado), e tanto o agente geral quanto o `/ai/chat` montam o prompt com a personalização e respondem com `why`/`sources`.
- **Produtividade pessoal** — módulo `/me` com **tarefas** (prioridade/status, recorrência diária automática ao concluir, listagem com filtros e paginação), **lembretes** (soneca até a data final suportada), **notas** (marcação e pin), **checklists** (listas com itens e categorias), **hábitos** (check-in diário com cálculo de sequência) e **sessões de foco**; o **plano semanal gera por IA** uma proposta que só é persistida no accept, gated pelo plano **PRO** (recurso `automation`, 402 nos planos inferiores). Todas as datas são armazenadas em UTC (convertidas pelo fuso do perfil do agente) e os endpoints agregam **agenda** e **resumo do dia**.

## Stack

| Camada | Tecnologia |
| --- | --- |
| API | FastAPI + Uvicorn |
| ORM | SQLAlchemy 2 (async) |
| Banco | PostgreSQL (asyncpg), SQLite (aiosqlite p/ testes) |
| Cache | Redis (com fallback em memória) |
| Auth | python-jose (JWT) + passlib (PBKDF2) |
| IA | Pool de provedores compatíveis OpenAI (Groq, OpenAI, OpenRouter) via httpx |
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
        ├── /objectives/{id}/roadmap/days (dias do roadmap: listar, marcar status, remover)
        ├── /agents/general/chat   (agente geral: objetivos + dias cumpridos)
        ├── /ai/chat               (chat completion com pool de provedores)
        ├── /system-prompts        (admin CRUD)
        └── /proxy/{service}/...   (reverse proxy com cache)
```

> **Convenção de transação.** A intenção é que os *repositories* apenas
> usem `flush()` e nunca façam `commit()`; quem controla a transação é o
> *service*. Hoje isso já vale para os repositórios de `objective`,
> `roadmap_day` e `user` (com parâmetro `commit: bool = True` para escrita
> atômica em múltiplas etapas). `productivity_repository`,
> `ai_feedback_repository`, `system_prompt_repository` e
> `agent_action_repository` ainda commitam internamente — dívida técnica
> documentada, sem bug de correção associado (cada escrita é de uma única
> entidade).
>
> **Envelope de erro.** Toda resposta de erro é
> `{"detail": "<pt-BR>", "code": "<estável>"}` (ver
> [API_REFERENCE.md](API_REFERENCE.md)).

### Estrutura do projeto

```
NovaIAx/
├── app/
│   ├── main.py               # Entry point (FastAPI, middlewares, lifespan)
│   ├── api/
│   │   ├── deps.py           # Injeção de dependências (auth, db, redis)
│   │   └── v1/               # Roteadores (auth, users, objectives, roadmap-days, agents, chat, proxy...)
│   ├── cache/redis_client.py # Cliente Redis com fallback em memória
│   ├── clients/              # ai_client (cliente OpenAI-compatível), ai_router (pool/fallback/provedores), ai_quota (limite diário), safety (filtros)
│   ├── commands/             # Objetos de comando (RegisterUser, Login, etc.)
│   ├── core/                 # Config, logging, security (JWT/hash), health, network
│   ├── db/session.py         # Engine assíncrono + Base + init_db (apenas testes)
│   ├── exceptions/           # Handlers globais (handlers.py) + códigos de erro (api_errors.py, ai_errors.py, plan_errors.py)
│   ├── middlewares/          # Auth, RateLimit, Logging, SecurityHeaders
│   ├── models/               # User, Objective, RoadmapDay, SystemPrompt (SQLAlchemy)
│   ├── repositories/         # Camada de acesso a dados
│   ├── schemas/              # Modelos Pydantic
│   ├── services/             # Regras de negócio
│   └── utils/                # Helpers compartilhados (datas, ids, sanetização, coerção)
├── migrations/               # Migrações Alembic (env.py, script.py.mako, versions/)
├── scripts/                  # seed_dev.py, smoke_test.py, export_openapi.py
├── tests/                    # Testes de integração e unitários
├── API_REFERENCE.md          # Referência completa dos endpoints (gerada do openapi.json)
├── FLUTTER_HANDOFF.md        # Contrato HTTP para o app Flutter
├── PRIVACY.md                # Conformidade/LGPD
├── openapi.json              # Schema OpenAPI (python scripts/export_openapi.py)
├── Infra/project.wsd         # Diagrama PlantUML da arquitetura
├── Dockerfile
├── render.yaml               # Deploy Render (Blueprint: web service + Postgres)
├── entrypoint.sh             # Container entrypoint (migrações + uvicorn)
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
- Chave de API de ao menos um provedor de IA — [Groq](https://console.groq.com/), OpenAI ou OpenRouter; provedores extras entram automaticamente no pool de fallback (para os endpoints de IA)

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

## Testando com o app

Além da suíte automatizada, há dois utilitários para exercise a API como um
usuário faria — úteis antes de um teste no aparelho e depois de cada deploy.

### 1. Popular o banco com dados de exemplo

Cria a conta `test@novaiax.dev`, uma meta e os sete dias do roadmap (com status
variado), para que nenhuma tela abra vazia.

```bash
# senha via ambiente (recomendado: não fica no histórico do shell)
export SEED_PASSWORD='S3nh@Forte!2026'
python -m scripts.seed_dev

# ...ou na própria chamada
python -m scripts.seed_dev --password 'S3nh@Forte!2026'

# ...ou deixe o script gerar uma senha forte e imprimi-la uma única vez
python -m scripts.seed_dev
```

O script é idempotente: rodá-lo de novo atualiza a conta existente em vez de
falhar na restrição de e-mail único. Ele funciona sem `GROQ_API_KEY` — o roadmap
é gerado localmente, sem consumir cota do provider.

### 2. Percorrer o fluxo completo contra a API no ar

```bash
python scripts/smoke_test.py http://localhost:8000
```

Executa, nesta ordem: `register → login → me → refresh → objectives → roadmap
days → assistant → general chat → device command → cache flush → logout`.

Cada passo valida o contrato real (status e campos que o app lê), então uma
mudança de nome de campo ou de código de status quebra aqui — e não no celular.
A saída é um resumo por passo:

```
  [ok  ] 7. List roadmap days - 7 days, first status=PENDING
  [ok  ] 8. Update roadmap day - day 1 -> COMPLETED
  [skip] 11. Device agent - AI unavailable (429 ai_busy)
```

Passos que dependem do provider são marcados `skip`, não `fail`: cota
esgotada (`ai_rate_limited`), fila cheia (`ai_busy`), chave ausente ou o
rate limit por IP (o padrão são 10 req/min nos endpoints de IA, e este script
faz 13 requisições em sequência) são situations esperadas em ambiente local e
não devem ser tratadas como regressão. O script sai com código `1` apenas
quando algum passo realmente falhou.

Opções úteis:

```bash
python scripts/smoke_test.py http://localhost:8000 --email me@example.com --password 'S3nh@Forte!'
python scripts/smoke_test.py http://localhost:8000 --json     # saída legível por máquina
python scripts/smoke_test.py http://localhost:8000 --timeout 120
```

### Suíte automatizada

```bash
ruff check app tests scripts
mypy app
pytest -q
```

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
- Em **Render**, o `entrypoint.sh` roda `alembic upgrade head` antes do
  `uvicorn` no próprio container (equivalente in-process do `preDeployCommand`
  pago da Render) — veja [DEPLOY.md](DEPLOY.md).
- `init_db()` em `app/db/session.py` fica restrito aos testes (o `conftest.py` o chama).

### Testes

```bash
pytest
```

> O `RedisClient` cai para o armazenamento em memória automaticamente quando o Redis não está disponível, então a maioria dos testes funciona sem infraestrutura.

## Privacidade e conformidade

Gate de idade, consentimentos, exportação e exclusão de dados, retenção e
bloqueio por tentativas de login — veja [PRIVACY.md](PRIVACY.md).

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
| `RATE_LIMIT_DEFAULT` | `60` | Limite geral (req/min), janela independente |
| `RATE_LIMIT_AI` | `10` | Limite dos endpoints que chamam a IA (req/min), janela separada |
| `CACHE_TTL_DEFAULT` | `60` | TTL de cache (s) |
| `CACHE_TTL_AI` | `300` | TTL de cache para respostas de IA (s) |
| `MAX_PAYLOAD_BYTES` | `1048576` | Tamanho máximo do body (1 MiB) |
| `GROQ_API_KEY` | — | Chave da API Groq (primeiro provedor do pool de IA) |
| `GROQ_API_BASE_URL` | `https://api.groq.com/openai/v1` | Base URL da API Groq |
| `GROQ_API_MODEL` | `openai/gpt-oss-120b` | Modelo usado nos chat completions |
| `GROQ_REQUEST_BUDGET_SECONDS` | `60` | Teto de uma requisição de IA, do início ao fim |
| `GROQ_QUEUE_WAIT_SECONDS` | `25` | Fila interna (pacer + semáforo) dentro do orçamento |
| `GROQ_RATE_LIMIT_RPM` | `20` | Requisições/min enviadas por este processo |
| `GROQ_TOKEN_BUDGET_PER_MINUTE` | `6000` | Tokens/min estimados enviados por este processo |
| `GROQ_MAX_CONCURRENCY` | `2` | Chamadas de IA simultâneas |
| `GROQ_MAX_ATTEMPTS` | `1` | Tentativas por chamada (1 no free tier) |
| `OPENAI_API_KEY` | — | Chave OpenAI (entra no pool de fallback) |
| `OPENAI_API_BASE_URL` | `https://api.openai.com/v1` | Base URL da OpenAI |
| `OPENAI_API_MODEL` | `gpt-4o-mini` | Modelo OpenAI padrão |
| `OPENROUTER_API_KEY` | — | Chave OpenRouter (entra no pool de fallback) |
| `OPENROUTER_API_BASE_URL` | `https://openrouter.ai/api/v1` | Base URL do OpenRouter |
| `OPENROUTER_API_MODEL` | `openai/gpt-4o-mini` | Modelo OpenRouter padrão |
| `AI_PROVIDERS_JSON` | — | Lista JSON substitui os provedores acima (ver `.env.example`) |
| `AI_DEFAULT_DAILY_MESSAGES` | `200` | Quota diária de mensagens por usuário |
| `AI_DEFAULT_DAILY_TOKENS` | `50000` | Quota diária de tokens por usuário |
| `AI_DEFAULT_MAX_OUTPUT_TOKENS` | `1024` | Limite de tokens na resposta de IA |
| `AI_BREAKER_FAILURE_THRESHOLD` | `3` | Falhas consecutivas p/ abrir o circuit breaker do provedor |
| `AI_BREAKER_COOLDOWN_SECONDS` | `30` | Janela em que um provedor aberto fica fora do pool |
| `DB_POOL_SIZE` | `10` | Conexões no pool (PostgreSQL/MySQL) |
| `DB_MAX_OVERFLOW` | `10` | Conexões extras acima do pool |
| `DB_POOL_RECYCLE_SECONDS` | `1800` | Idade máxima de uma conexão do pool |
| `DB_POOL_TIMEOUT_SECONDS` | `10` | Espera máxima por uma conexão livre |
| `DB_POOL_PRE_PING` | `true` | Valida a conexão antes de entregá-la |
| `SEED_PASSWORD` | — | Senha da conta criada por `scripts/seed_dev.py` |

## Endpoints principais

A referência completa (110 operações, com parâmetros, corpos e respostas) está
em [API_REFERENCE.md](API_REFERENCE.md) e no schema [openapi.json](openapi.json)
(regenere com `python scripts/export_openapi.py`). A tabela abaixo é um resumo.

### Formato de erro

Toda resposta de erro tem **sempre** o mesmo envelope:

```json
{ "detail": "<mensagem em pt-BR>", "code": "<codigo_estavel>" }
```

- `detail` é sempre em **pt-BR** e serve para exibição (pode mudar).
- `code` é **estável** e é o que o app deve usar para decidir o fluxo. Está
  presente em *todo* erro, inclusive os `HTTPException` genéricos (o código é
  derivado do status). Códigos de IA (`ai_*`), plano (`plan_*`,
  `subscription_inactive`, `billing_unavailable`) e genéricos
  (`authentication_required`, `invalid_credentials`, `not_found`, `conflict`,
  `validation_error`, `rate_limit_exceeded`, …) estão catalogados em
  [API_REFERENCE.md](API_REFERENCE.md).

| Método | Rota | Descrição | Auth |
| --- | --- | --- | --- |
| `POST` | `/api/v1/auth/register` | Registra usuário e retorna JWT | Pública |
| `POST` | `/api/v1/auth/login` | Login por e-mail/senha | Pública |
| `POST` | `/api/v1/auth/refresh` | Renova access/refresh tokens | Pública |
| `GET` | `/api/v1/auth/me` | Identidade do usuário atual | Bearer |
| `PATCH` | `/api/v1/users/me` | Atualiza o nome do próprio usuário | Bearer |
| `PATCH` | `/api/v1/users/me/password` | Troca a própria senha (invalida refresh tokens) | Bearer |
| `POST` | `/api/v1/objectives/register` | Registra uma meta (gera roadmap IA de 7 dias) | Bearer |
| `POST` | `/api/v1/objectives/{id}/roadmap/renew` | Renova o roadmap (janela de 7 dias) | Bearer |
| `GET` | `/api/v1/objectives/{id}/roadmap/days` | Lista os dias do roadmap (owner/admin) | Bearer |
| `PATCH` | `/api/v1/objectives/{id}/roadmap/days/{day_id}` | Marca o dia como cumprido/pendente/pulado | Bearer |
| `DELETE` | `/api/v1/objectives/{id}/roadmap/days/{day_id}` | Remove um dia do roadmap | Bearer |
| `POST` | `/api/v1/objectives/assistant` | Assistente IA para criar metas | Bearer |
| `POST` | `/api/v1/agents/general/chat` | Agente geral (objetivos + dias do roadmap cumpridos) | Bearer |
| `POST` | `/api/v1/ai/chat` | Chat completion (com histórico por usuário) | Bearer |
| `GET` | `/api/v1/agent-groups` | Cataloga os grupos de personalização | Bearer |
| `GET` | `/api/v1/sub-agents` | Cataloga os sub-agentes de personalização | Bearer |
| `GET` | `/api/v1/me/agent` | Perfil de personalização (nome/tom/grupos... ou padrão) | Bearer |
| `PUT` | `/api/v1/me/agent` | Atualiza o perfil de personalização | Bearer |
| `GET` | `/api/v1/me/memories` | Lista as memórias do usuário | Bearer |
| `POST` | `/api/v1/me/memories` | Salva uma memória (409 se a chave já existe) | Bearer |
| `PATCH` | `/api/v1/me/memories/{key}` | Atualiza valor/habilitada de uma memória | Bearer |
| `DELETE` | `/api/v1/me/memories/{key}` | Exclui uma memória | Bearer |
| `PUT` | `/api/v1/me/learning` | Liga/desliga o aprendizado automático (`{"enabled": bool}`) | Bearer |
| `GET` | `/api/v1/me/tasks` | Lista tarefas (filtros por status/prioridade/vencimento, paginado) | Bearer |
| `POST` | `/api/v1/me/tasks` | Cria tarefa (vence em `due_at`, recorrência diária se `recurrence` preenchido) | Bearer |
| `PATCH/DELETE` | `/api/v1/me/tasks/{task_id}` | Atualiza / exclui tarefa (concluir tarefa recorrente avança a data) | Bearer |
| `GET/POST` | `/api/v1/me/reminders` | Lista / cria lembrete (com `snooze_until` para soneca) | Bearer |
| `PATCH/DELETE` | `/api/v1/me/reminders/{reminder_id}` | Soneca / conclui / exclui lembrete | Bearer |
| `GET/POST` | `/api/v1/me/notes` | Lista (filtro por tag) / cria nota marcadada | Bearer |
| `PATCH` | `/api/v1/me/notes/{note_id}` | Atualiza conteúdo/tags/pin | Bearer |
| `DELETE` | `/api/v1/me/notes/{note_id}` | Exclui nota | Bearer |
| `GET/POST` | `/api/v1/me/checklists` | Lista / cria checklist (categoria `compras`/`casa`/`bebe`/`viagem`) | Bearer |
| `GET/PATCH/DELETE` | `/api/v1/me/checklists/{checklist_id}` | Consulta / atualiza / exclui checklist | Bearer |
| `POST` | `/api/v1/me/checklists/{checklist_id}/items` | Adiciona item ao checklist | Bearer |
| `PATCH/DELETE` | `/api/v1/me/checklists/{checklist_id}/items/{item_id}` | Conclui/soneca/exclui item | Bearer |
| `GET/POST` | `/api/v1/me/habits` | Lista / cria hábito | Bearer |
| `POST` | `/api/v1/me/habits/{habit_id}/check-ins` | Registra check-in diário (retorna sequência/vista) | Bearer |
| `PATCH/DELETE` | `/api/v1/me/habits/{habit_id}` | Atualiza / exclui hábito | Bearer |
| `POST` | `/api/v1/me/focus-sessions` | Inicia sessão de foco | Bearer |
| `PATCH/DELETE` | `/api/v1/me/focus-sessions/{session_id}` | Atualiza (encerra) / exclui uma sessão de foco | Bearer |
| `GET` | `/api/v1/me/focus-sessions` | Lista sessões de foco | Bearer |
| `GET` | `/api/v1/me/agenda` | Agenda agregada: tarefas, lembretes, hábitos e focos do dia (paginada) | Bearer |
| `GET` | `/api/v1/me/today` | Resumo do dia com contagens | Bearer |
| `POST` | `/api/v1/me/weekly-plan` | Gera plano semanal por IA (não persiste) — **PRO** | Bearer |
| `POST` | `/api/v1/me/weekly-plan/accept` | Persiste o plano semanal gerado (tarefas + lembretes) — **PRO** | Bearer |
| `PATCH` | `/api/v1/users/{id}/promote` | Promove usuário a admin | Admin |
| `PATCH` | `/api/v1/users/{id}/demote` | Rebaixa um admin | Admin |
| `DELETE` | `/api/v1/users/{id}` | Exclui usuário | Admin |
| `GET` | `/api/v1/users/` | Lista usuários paginado | Admin |
| `GET/POST` | `/api/v1/system-prompts/` | Lista/cria system prompts | Admin |
| `GET/PUT/DELETE` | `/api/v1/system-prompts/{id}` | Consulta/atualiza/exclui prompt | Admin |
| `*` | `/api/v1/proxy/{service}/...` | Proxy para serviços downstream | Bearer |
| `GET` | `/api/v1/me/subscription` | Assinatura e direitos do usuário (plano, quota) | Bearer |
| `POST` | `/api/v1/billing/google/verify` | Verifica uma compra do Google Play | Bearer |
| `POST` | `/api/v1/billing/google/rtdn` | Webhook de notificações do Google Play (chamado pelo Google) | Pública |
| `GET/PUT` | `/api/v1/me/consents` | Lê / grava consentimentos (LGPD) | Bearer |
| `DELETE` | `/api/v1/me/consents/{consent_type}` | Revoga um consentimento | Bearer |
| `GET` | `/api/v1/me/export` | Exporta os dados do usuário (sem `password_hash`) | Bearer |
| `DELETE` | `/api/v1/me` | Exclui a própria conta (exige senha) | Bearer |
| `GET` | `/health` | Health check de liveness/readiness (DB + Redis) | Pública |

### Produtividade pessoal

Tarefas, lembretes, notas, checklists, hábitos e sessões de foco são **isoladas por usuário** (toda consulta exige `user_id`) e tem as datas normalizadas para **UTC** usando o fuso configurado no perfil do agente (`GET /api/v1/me/agent` → `timezone`, padrão `America/Sao_Paulo`). Datas naïves enviadas pela API são interpretadas nesse fuso e convertidas antes de persistir.

```bash
# Tarefa que vence hoje às 09:00 no fuso do usuário
curl -X POST http://localhost:8000/api/v1/me/tasks \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -d '{"title":"Treinar","due_at":"2026-10-13T09:00:00"}'

# Hábito com check-in do dia (a vista traz a sequência atual)
curl -X POST http://localhost:8000/api/v1/me/habits \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -d '{"title":"Beber água"}'
curl -X POST http://localhost:8000/api/v1/me/habits/1/check-ins \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -d '{"date":"2026-10-13"}'
```

**Plano semanal (`weekly-plan`)** — `POST /api/v1/me/weekly-plan` pede à IA uma proposta para a semana (tarefas e lembretes) e a devolve **sem persistir**; o cliente só persiste tudo ao chamar `POST /api/v1/me/weekly-plan/accept`. O recurso usa o engine de IA e está **gated pelo plano PRO** (feature `automation`): usuários em planos abaixo recebem `402` com `code: "plan_upgrade_required"`. Datas retornadas na proposta são convertidas para UTC antes do armazenamento.

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

# Remover um dia do roadmap (os dias restantes mantêm a numeração original)
curl -X DELETE http://localhost:8000/api/v1/objectives/1/roadmap/days/<day_id> \
  -H "Authorization: Bearer <access_token>"
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

### Personalização do agente

O usuário pode personalizar o próprio agente (nome, tom, grupos de audiência, sub-agentes e memórias) e ligar/desligar o aprendizado automático. O catálogo (grupos e sub-agentes) é semeado via `scripts/seed_agent_catalog.py` (aplicável mais de uma vez).

```bash
# Ativar o perfil "Estudantes" + o sub-agente "Finanças" e dar um nome
curl -X PUT http://localhost:8000/api/v1/me/agent \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -d '{"agent_name":"MeuCompanheiro","active_group_slugs":["estudantes"],"active_sub_agent_slugs":["financas"]}'

# Salvar uma memória explícita
curl -X POST http://localhost:8000/api/v1/me/memories \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -d '{"key":"moradia","value":"Moro em Lisboa desde 2020."}'

# Desligar o aprendizado automático
curl -X PUT http://localhost:8000/api/v1/me/learning \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -d '{"enabled":false}'
```

O nome tem 2 a 30 caracteres e passa por um filtro básico (termos ofensivos e tentativas de se passar pela plataforma/outros assistentes são rejeitados com `422`). Chaves de memória criadas pelo usuário não podem usar o prefixo reservado `auto_` (reservado ao aprendizado automático).

Quando há perfil personalizado, tanto o **agente geral** quanto o **`/ai/chat`** montam o system prompt a partir da base + nome/tom do agente + prompts dos grupos ativos + memórias habilitadas (marcadas como *dados não confiáveis*), e a resposta inclui `why` (motivos curtos) e `sources` (chaves/slugs usados) — ambos `[]` quando não há personalização.

O **aprendizado automático** salva memórias (`key` com prefixo `auto_`, `source="learned"`) apenas quando: (1) a pessoa expressa uma afirmação explícita em primeira pessoa ("Eu moro em ...", "Eu trabalho com ...", "Meu filho ..."), (2) o aprendizado está ligado (`PUT /me/learning`), e (3) o assunto **não** é saúde, religião, orientação sexual, política ou finanças — a menos que a própria frase peça para salvar ("quero que você lembre que ..."). Ocorre somente no agente geral e no `/ai/chat`, nunca em metas/dispositivo.

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
- Erros não expõem detalhes internos: toda falha é `{"detail", "code"}` com `detail` em pt-BR e mensagem genérica; o traceback fica só nos logs.
- Rate limit em endpoints de IA, autenticação e sociais; tentativas de login falhas são limitadas por e-mail (5 / 15 min).
