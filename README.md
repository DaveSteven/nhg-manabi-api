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

## Render Deployment

This project includes a `render.yaml` Blueprint for a Docker web service plus
a Render PostgreSQL database.

1. Push this repository to GitHub, GitLab, or Bitbucket.
2. In Render, create a new Blueprint and select this repository.
3. Review the `japanese-study-api` web service and `japanese-study-db`
   PostgreSQL database.
4. Set these prompted environment variables:

```text
CORS_ORIGINS=https://your-frontend-domain
DEFAULT_DAVID_PASSWORD=replace-with-a-real-password
```

The Blueprint wires `DATABASE_URL` to the Render PostgreSQL internal connection
string. The pre-deploy command imports the bundled `import_data` question data
before the service starts.

If you create the Render web service manually instead of using the Blueprint,
create a Render PostgreSQL database first and add this environment variable to
the web service:

```text
DATABASE_URL=<your Render PostgreSQL internal connection string>
```

Health check:

```text
/api/health
```
