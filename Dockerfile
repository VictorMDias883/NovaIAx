FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Instala as dependências a partir do pyproject.toml (fonte única).
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

# Copia o restante da aplicação (configurações, testes, etc.).
COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
