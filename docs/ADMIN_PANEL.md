# Painel administrativo (`/admin`)

Guia operacional do console server-rendered do NovaIAx: como provisionar um
administrador (inclusive em Supabase), quais variáveis de ambiente importam,
como o painel se protege e o mapa de cada tela.

O painel é **HTML/Jinja2** montado em `/admin` (`app/api/admin.py`), com
autenticação própria — ele **não** usa os JWTs do app. Ele é para operadores,
não para o app Flutter (ver `FLUTTER_HANDOFF.md` §15).

---

## 1. Criando um administrador

O painel **não tem registro**: não existe endpoint de cadastro de admin e, em
produção, o bootstrap de startup é um no-op (`app/db/bootstrap.py`). Um
administrador é simplesmente uma linha em `users` com `role = 'ADMIN'`, criada
direto no banco.

### 1.1 Produção (Supabase) — via `scripts/make_admin_sql.py`

O script apenas **gera** o SQL (não conecta ao banco). Gere localmente:

```bash
python -m scripts.make_admin_sql --name "Ops" --email ops@example.com
# a senha vem de $ADMIN_PASSWORD ou do prompt interativo (getpass)
```

Ele imprime dois trechos — rode o que se aplica:

1. `UPDATE` que promove uma conta existente com o e-mail informado (também
   invalida os refresh tokens);
2. `INSERT … ON CONFLICT (email) DO UPDATE` que cria a conta do zero e, se o
   e-mail já existir, sobrescreve hash e papel para `ADMIN`.

Cole o SQL no editor do Supabase (ou rode via `psql`) e faça login em
`https://<host>/admin/login`. O hash da senha é gerado com o mesmo contexto
passlib (PBKDF2-SHA256) que a aplicação usa, então o login verifica normalmente.

> Qualquer tentativa de login de admin sem conta de admin (ou e-mail
> desconhecido) recebe um `401` genérico — não há enumeração de admins.

### 1.2 Desenvolvimento local — via `scripts/seed_dev.py`

O seed cria (idempotente) um admin de desenvolvimento junto com os dados de
exemplo:

```bash
python -m scripts.seed_dev
# admin padrão: admin@novaiax.dev; senha de $SEED_ADMIN_PASSWORD ou gerada
python -m scripts.seed_dev --admin-email dev@novaiax.dev --admin-password '...'
```

A API também cria a conta de operador sozinha, a cada boot, a partir de
`ADMIN_BOOTSTRAP_EMAIL` e `ADMIN_BOOTSTRAP_PASSWORD`: o insert é idempotente
(`ON CONFLICT (email) DO NOTHING`, sem locks) e a senha só é aplicada
**quando a conta é criada** — uma conta existente nunca tem a senha trocada
nem é rebaixada. Em produção uma senha curta (menos de 12 caracteres) ou uma
senha conhecida é recusada: o app continua no ar e registra o motivo no log.
Os nomes antigos `ADMIN_DEFAULT_EMAIL` / `ADMIN_DEFAULT_PASSWORD` continuam
funcionando como aliases depreciados (com um aviso no log).

---

## 2. Variáveis de ambiente

| Variável | Padrão | Propósito |
| --- | --- | --- |
| `ADMIN_PANEL_ENABLED` | `true` | Quando `false`, toda rota `/admin` responde `404`. |
| `ADMIN_PANEL_COOKIE_NAME` | `novaiax_admin_panel` | Nome do cookie de sessão do painel (path `/admin`). |
| `ADMIN_REQUIRE_TOTP` | `false` | Exige TOTP; admins sem segredo são redirecionados para enrolar. |
| `ADMIN_TOTP_ISSUER` | `NovaIAx Admin` | Rótulo exibido no app autenticador. |
| `ADMIN_ALLOWED_IPS` | *(unset)* | Allowlist de IP (CSV). Fora dela `/admin` responde `403`. Com proxy, use `TRUST_PROXY_HEADERS=true`. |
| `ADMIN_SESSION_IDLE_MINUTES` | `60` | Timeout por inatividade. |
| `ADMIN_SESSION_ABSOLUTE_HOURS` | `8` | Vida máxima da sessão. |
| `ADMIN_BOOTSTRAP_EMAIL` | *(unset)* | E-mail da conta de operador criada pelo bootstrap da API. |
| `ADMIN_BOOTSTRAP_PASSWORD` | *(unset)* | Senha da conta — aplicada somente quando a conta é criada. |
| `ADMIN_BOOTSTRAP_NAME` | `Administrador` | Nome exibido da conta de operador. |
| `ADMIN_BOOTSTRAP_PROMOTE_EXISTING` | `false` | Promove para ADMIN uma conta existente com o mesmo e-mail (padrão: não toca em conta existente). |
| `ADMIN_DEFAULT_EMAIL` / `ADMIN_DEFAULT_PASSWORD` | *(unset)* | Aliases depreciados das duas primeiras variáveis (funcionam, mas logam um aviso na subida). |

---

## 3. Supabase: RLS e pooler

Notas completas em `DEPLOY.md` §10; o resumo para o painel:

- **Alembic** (`entrypoint.sh` roda `alembic upgrade head`): use a conexão
  **direta** ou a **session pooler** (`5432`). O transaction pooler do
  PgBouncer (`6543`) não sustenta as sessões/prepared statements que o
  Alembic precisa.
- **Aplicação**: o engine async tolera o transaction pooler (`6543`) —
  `app/db/session.py` fixa `statement_cache_size=0` para evitar o erro
  `prepared statement … does not exist`. Session pooler também funciona.
- **RLS**: a migração do console admin habilita RLS (**sem policies**) em
  todas as tabelas do schema `public`. A aplicação conecta como **dona** das
  tabelas, o que contorna o RLS — a API funciona normalmente e o Data API
  (PostgREST) das roles anon/authenticated **não enxerga linhas**. Isso é
  intencional: o app é o único caminho de dados. Se algum dia expuser tabelas
  via Data API, crie policies explícitas antes.
- Não habilite `FORCE ROW LEVEL SECURITY` sem policies para a dona — isso
  trancaria a própria aplicação para fora das tabelas.

---

## 4. Mapa do painel

| Rota (`/admin/…`) | O que faz |
| --- | --- |
| `GET /login`, `POST /login`, `POST /logout` | Sessão do operador (cookie httpOnly + CSRF + TOTP opcional). |
| `GET /` | Dashboard com contadores agregados (usuários, prompts, relatos abertos, mensagens hoje, assinaturas por plano). |
| `GET /users`, `GET /users/{id}` | Lista/detalhe de usuários (busca por e-mail, nome ou id). |
| `POST /users/{id}/promote\|demote\|delete` | Papel e exclusão de conta. |
| `POST /users/{id}/suspend\|unsuspend` | Suspensão (a API responde `403 account_suspended` em tudo; o último admin nunca pode ser suspenso). |
| `POST /users/{id}/force-logout` | Invalida os refresh tokens da conta. |
| `POST /users/{id}/ban\|unban` | Banimento. |
| `POST /users/{id}/grant` | Concessão manual de plano (ledger `promo_redemptions`, `source = manual`). |
| `GET /system-prompts`, `POST /system-prompts`, `GET/POST /system-prompts/{id}`, `POST …/delete` | CRUD dos prompts de sistema usados pelo app. Cada save cria uma linha imutável em `prompt_versions` (versão, diff, rollback). |
| `GET /agents`, `POST /agents/test`, `GET/POST /agents/{entity}`, `…/edit`, `…/toggle`, `…/duplicate`, `…/versions`, `…/rollback` | Console de agentes: grupos, sub-agentes e agentes de prompt customizado, com sandbox "Testar agente" (quota-isento, rate-limitado). |
| `GET /promotions`, `POST /promotions`, `POST /promotions/{id}`, `…/toggle`, `…/delete` | Cards informativos de promoção (audience, janela, prioridade, CTA). |
| `GET /promotions/new`, `GET /promotions/{id}/edit` | Formulários de criação/edição. |
| `POST /promotions/codes`, `POST /promotions/codes/generate`, `GET /promotions/codes/export` | Códigos promocionais (geração em lote, exportação CSV). |
| `GET /promotions/codes/{id}`, `POST …/toggle`, `POST /promotions/codes/{id}` | Detalhe, ativar/desativar e editar um código. |
| `GET /promotions/grants`, `POST /promotions/grants` | Ledger de concessões (promo e manual) e concessão manual. |
| `GET /reports`, `POST /reports/{id}/status` | Moderação da comunidade (aprovar, rejeitar, sinalizar). |
| `GET /audit` | Trilha de auditoria append-only (`admin_audit_log`). |
| `GET /security/totp`, `POST /security/totp`, `POST /security/totp/disable` | Enrolar/desativar o segundo fator da própria conta. |
| `GET /settings`, `POST /settings/app` | Config remota do app: manutenção, versões, feature flags, anúncios. |
| `POST /settings/limits/{plan}`, `…/clear` | Overrides de limites por plano. |
| `POST /settings/ai/{name}` | Provedores de IA. |
| `POST /settings/contacts`, `POST /settings/contacts/{id}`, `…/delete` | Contatos oficiais de suporte. |

Páginas públicas fora do painel: `/privacy`, `/terms`, `/account-deletion`
(`app/api/public_pages.py`).

---

## 5. Modelo de segurança

- **Sessões próprias e opacas**: cookie `httpOnly` com path `/admin`, guardado
  em `admin_sessions` (não é JWT). Cada sessão carrega um token CSRF.
- **CSRF**: todo `POST` exige o token da sessão (`_validate_csrf`); sem ele a
  requisição é rejeitada.
- **TOTP (2FA) opcional**: com `ADMIN_REQUIRE_TOTP=true`, admin sem segredo
  não passa do login até enrolar; com segredo, o código é exigido no login.
- **Allowlist de IP**: `ADMIN_ALLOWED_IPS` restringe de onde o painel pode ser
  acessado (`403` fora da lista).
- **Expiração**: relógio de inatividade (`ADMIN_SESSION_IDLE_MINUTES`) +
  teto absoluto (`ADMIN_SESSION_ABSOLUTE_HOURS`); logout, force-logout de
  usuários e ban/suspensão também invalidam tokens.
- **Lockout de login**: o mesmo limitador de tentativas da API móvel, por
  (e-mail, IP) — `429` durante o bloqueio.
- **Auditoria**: toda ação administrativa registrada em `admin_audit_log`
  (append-only), visível em `/admin/audit`.
- **Menor exposição**: o painel serve apenas contadores agregados — nunca
  conversas, notas, saúde, finanças, estudos ou logbook.
- **Provisionamento**: em produção nenhum admin nasce de env var; o caminho é
  o SQL de §1.1 (ou o seed local em §1.2 fora de produção).
