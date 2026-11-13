# Japanese Study API

FastAPI backend for the local Japanese study app.

## Run

```bash
docker compose up --build
```

API docs:

```text
http://127.0.0.1:8000/docs
```

The local Docker setup runs PostgreSQL and stores it in the `postgres_data`
Docker volume.

## Import Questions

Copy normalized `books.json` files into `import_data/`, then run:

```bash
docker compose run --rm api python scripts/import_questions.py
```

## Accounts

`david` is a learner account only. Administrators and learners are separate:
admins sign in to the admin console via `POST /api/admin/auth/login` and cannot
sign in to the learning app (`POST /api/auth/login`); tokens issued by one side
are rejected by the other. On startup, if no administrator exists yet and
`ADMIN_USERNAME` / `ADMIN_PASSWORD` are set, a seed admin is created.

## Health Check

```text
/api/health
```
