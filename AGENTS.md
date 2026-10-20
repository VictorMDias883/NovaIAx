# Rules for every task in this repo (token-saving)
- Do not read the whole repo. Read only files you will change or must match. Extend the existing admin panel (app/api/admin.py, templates, static); do not rebuild it.
- Keep the layering Router→Command→Service→Repository, Alembic migrations for every schema change, pt-BR UI strings, English code.
- Tests only for logic and critical flows. Run ruff, mypy and only the pytest files you touched; full pytest only when the task says so.
- Do not regenerate openapi.json or docs until the final task.
- Final report: max 5 lines (done, skipped/blocked, test result).