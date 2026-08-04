# NovaIAx API Gateway

Este projeto fornece um gateway FastAPI para autenticação, rate limiting, cache Redis, logs estruturados e proxy para serviços downstream.

## Executar

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Ou com Docker:

```bash
docker compose up --build
```

## Variáveis de ambiente

Copie [.env.example](.env.example) para .env e ajuste os valores.

## Endpoints

- `POST /api/v1/auth/login`
- `POST /api/v1/auth/refresh`
- `GET /api/v1/proxy/ai/health`

## Adicionar um novo downstream

Inclua um novo item em `DOWNSTREAM_SERVICES_JSON` com `name`, `base_url` e `timeout_seconds`.
