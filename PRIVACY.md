# Privacidade e conformidade

Documentação da suíte de privacidade do NovaIAx: gate de idade, registro de
consentimentos, exportação de dados, exclusão de conta (API e página pública),
retenção de dados e bloqueio por tentativas de login.

Implementação:

- Configuração: [`app/core/config.py`](app/core/config.py)
- Serviços: [`app/services/privacy_service.py`](app/services/privacy_service.py)
- Endpoints autenticados: [`app/api/v1/privacy_router.py`](app/api/v1/privacy_router.py)
- Páginas públicas: [`app/api/public_pages.py`](app/api/public_pages.py)
- Modelos: [`app/models/consent.py`](app/models/consent.py), [`app/models/deletion_request.py`](app/models/deletion_request.py)
- Migração: [`migrations/versions/a1b2c3d4e5f6_create_privacy_tables.py`](migrations/versions/a1b2c3d4e5f6_create_privacy_tables.py)
- Testes: [`tests/test_privacy_compliance.py`](tests/test_privacy_compliance.py)

## Gate de idade

`RegisterUserRequest` exige `birth_date` (ISO `YYYY-MM-DD`) e recusa com **422**
quem tiver menos de `MINIMUM_ACCOUNT_AGE_YEARS` (padrão **18**). A data é
armazenada em `users.birth_date` (nullable, para contas legadas).

```bash
curl -X POST "$BASE/api/v1/auth/register" -H 'Content-Type: application/json' -d '{
  "full_name": "Ana",
  "email": "ana@example.com",
  "password": "Str0ngPassw0rd!",
  "birth_date": "1990-01-01"
}'
```

## Consentimentos

O log de consentimentos fica em `consent_records` (`user_id`, `type`, `version`,
`accepted_at`; unico por `user_id`+`type`). Tipos válidos (`CONSENT_TYPES`):
`terms`, `privacy`, `microphone`, `contacts`, `location`, `learning`.

No registro os consentimentos `terms` e `privacy` são gravados automaticamente
com a versão vigente em `CONSENT_VERSIONS_JSON`. Os demais são opt-in e só
aparecem via `PUT`.

| Método | Rota | Descrição |
| --- | --- | --- |
| `GET` | `/api/v1/me/consents` | Lista o log do usuário autenticado. |
| `PUT` | `/api/v1/me/consents` | Concede/reconfirma `{"type": ..., "version": ...}`. Tipo desconhecido → **422**. |
| `DELETE` | `/api/v1/me/consents/{type}` | Retira um consentimento. Tipo desconhecido ou nunca concedido → **404**. |

## Exportação de dados

`GET /api/v1/me/export` devolve um bundle JSON portátil (usuário, consentimentos,
objetivos + roadmap, agente pessoal, memórias, ações de agente, feedback de IA,
assinatura, eventos de compra, produtividade, **saúde (medicamentos e tomadas),
finanças (despesas/receitas/contas/orçamentos), estudo (planos/baralhos/cartões/
revisões), diário (viagens/turnos/cuidados), comunidade (relatos, confirmações,
denúncias, bloqueios e rascunhos)** e a lista de terceiros que processam dados).
O bundle **nunca** inclui `password_hash` nem `purchase_token_hash`.

O endpoint é limitado por usuário a `DATA_EXPORT_MAX_PER_HOUR` por
`DATA_EXPORT_WINDOW_SECONDS`; ao exceder, responde **429**. Identidades via API
key são recusadas (**403**).

## Exclusão de conta

`DELETE /api/v1/me` exige a senha atual no corpo (`{"password": "..."}`), pois um
access token vazado não deve apagar dados. Senha incorreta → **400**. A remoção
ocorre em uma transação única (linhas dependentes e o usuário) e depois limpa as
chaves Redis do usuário (quota de IA, histórico de conversa, contadores) e
**apaga os arquivos de foto** de relatos de comunidade do disco. Quando
há assinatura Play, a resposta traz `subscription_management_url` apontando para
a tela de cancelamento do Google.

```json
{
  "message": "Account deleted.",
  "subscription_management_url": "https://play.google.com/store/account/subscriptions?sku=novaiax_pro_monthly&package=br.com.novaiax.agente"
}
```

Após a exclusão o usuário não consegue mais autenticar (**401**).

## Páginas públicas

Rotas públicas não recebem a dependência de autenticação do usuário; as rotas
protegidas recebem `get_current_user` na configuração do respectivo router:

| Rota | Descrição |
| --- | --- |
| `GET /privacy` | Política de privacidade, com os provedores de IA configurados como terceiros. |
| `GET /terms` | Termos de uso. |
| `POST /account-deletion/request` | Processa o formulário de exclusão. |
| `GET /account-deletion` | Formulário de exclusão de conta. |

O formulário aceita dois fluxos:

- **e-mail + senha** — prova de identidade; a conta é excluída imediatamente pelo
  mesmo `AccountDeletionService` da API.
- **apenas e-mail** — grava um `DeletionRequest` (`status = "pending"`) para
  tratamento manual pela operação.

O lado de escrita é protegido por CSRF: um token é criado em um cookie
(`novaiax_public_csrf`, path `/`) e comparado com o campo oculto do formulário.
Requisições sem o cookie são aceitas (mesmo comportamento do painel admin).

## Retenção e minimização

- **Histórico de conversa**: cada `save` grava um TTL rolante de
  `CONVERSATION_HISTORY_TTL_SECONDS` (padrão 30 dias; `<= 0` desativa o TTL).
- **Logs de IA**: `AI_LOGS_RETENTION_DAYS` (padrão 90) documenta a janela exibida
  na página de privacidade.
- **Bloqueio de login**: após `LOGIN_FAILED_ATTEMPTS_THRESHOLD` senhas erradas
  (padrão 5) em `LOGIN_FAILED_ATTEMPTS_WINDOW_SECONDS` (padrão 900 s), novas
  tentativas recebem **429**. Um login bem-sucedido zera o contador.

## Variáveis de ambiente

| Variável | Padrão | Descrição |
| --- | --- | --- |
| `MINIMUM_ACCOUNT_AGE_YEARS` | `18` | Idade mínima para criar conta. |
| `CONSENT_VERSIONS_JSON` | `{"terms":"1","privacy":"1",...}` | Versão vigente de cada tipo de consentimento. |
| `CONVERSATION_HISTORY_TTL_SECONDS` | `2592000` (30 dias) | TTL do histórico de conversa por usuário. |
| `AI_LOGS_RETENTION_DAYS` | `90` | Janela de retenção de logs de IA (informativa). |
| `LOGIN_FAILED_ATTEMPTS_THRESHOLD` | `5` | Tentativas de login falhas antes do bloqueio. |
| `LOGIN_FAILED_ATTEMPTS_WINDOW_SECONDS` | `900` | Janela do contador de falhas de login. |
| `DATA_EXPORT_MAX_PER_HOUR` | `10` | Exportações permitidas por usuário na janela. |
| `DATA_EXPORT_WINDOW_SECONDS` | `3600` | Janela do limite de exportação. |

## Testes

```bash
pytest tests/test_privacy_compliance.py -q
```

Cobrem: gate de idade, consentimentos automáticos e CRUD, exportação (formato,
ausência de segredos e rate limit), exclusão (senha, revogação de acesso, limpeza
de cache e URL do Play), páginas públicas (os dois fluxos + CSRF), bloqueio por
tentativas de login e o TTL de retenção do histórico.
