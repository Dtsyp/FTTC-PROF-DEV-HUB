#!/usr/bin/env bash
# Проверяет выбранный стек в одноразовом окружении на Linux/WSL.
set -euo pipefail
umask 077

case "$#:${1-}" in
    0:) target_database=fttc23_test ;;
    1:--missing-db) target_database=fttc23_missing ;;
    *) echo "Использование: bash $0 [--missing-db]" >&2; exit 2 ;;
esac

py=${PYTHON:-python3.13}
pg=${PG_BIN:-/usr/lib/postgresql/17/bin}
port=${FTTC23_PORT:-55423}
if ! [[ $port =~ ^[1-9][0-9]{0,4}$ ]] || (( port > 65535 )); then
    echo "FTTC23_PORT должен быть числом от 1 до 65535" >&2
    exit 2
fi

work=$(mktemp -d /tmp/fttc23.XXXXXXXX)
cleanup() {
    if [[ -s "$work/data/postmaster.pid" ]]; then
        "$pg/pg_ctl" -D "$work/data" -m fast -w stop >/dev/null || exit 1
    fi
    rm -rf -- "$work" || exit 1
}
trap cleanup EXIT
cd "$work"

"$py" -m venv venv
# Снимок зависимостей, разрешённых вместе на CPython 3.13 / Linux.
venv/bin/python -m pip --disable-pip-version-check install --quiet \
    --only-binary=:all: --no-cache-dir -r /dev/stdin <<'REQUIREMENTS'
alembic==1.20.1
annotated-doc==0.0.7
annotated-types==0.8.0
anyio==4.16.0
click==8.5.1
fastapi==0.165.0
h11==0.16.0
idna==3.20
Mako==1.4.3
MarkupSafe==3.0.4
opentelemetry-api==1.45.2
psycopg[binary]==3.3.9
psycopg-binary==3.3.9
pydantic==2.14.4
pydantic_core==2.50.5
SQLAlchemy==2.1.9
starlette==1.10.0
typing-inspection==0.4.5
typing_extensions==4.16.0
uvicorn==0.54.0
REQUIREMENTS
venv/bin/python -m pip check
venv/bin/alembic --version

# Параметры подключения разработчика действуют только вне этого процесса.
for setting in "${!PG@}"; do
    unset "$setting"
done
export PGHOST=127.0.0.1 PGPORT="$port" PGUSER=fttc23_local PGSSLMODE=disable PGPASSFILE="$work/pgpass"
venv/bin/python -c 'import secrets; print(secrets.token_hex(32))' > init.pw
{ printf '%s:%s:*:%s:' "$PGHOST" "$PGPORT" "$PGUSER"; cat init.pw; } > "$PGPASSFILE"
"$pg/initdb" -D data -U "$PGUSER" --pwfile=init.pw --no-locale --encoding=UTF8 \
    --auth-local=reject --auth-host=scram-sha-256 >/dev/null
"$pg/pg_ctl" -D data -l "$work/postgres.log" \
    -o "-h 127.0.0.1 -p $PGPORT -c unix_socket_directories=''" -w start >/dev/null || {
    cat "$work/postgres.log" >&2
    exit 1
}
"$pg/createdb" fttc23_test
"$pg/postgres" --version

venv/bin/python - "$target_database" <<'PY'
import json
import platform
import socket
import sys
from contextlib import closing
from http.client import HTTPConnection
from importlib.metadata import version
from threading import Thread
from typing import Literal

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import URL, create_engine, text
from sqlalchemy.exc import OperationalError

if sys.version_info[:2] != (3, 13):
    raise SystemExit("Требуется Python 3.13")
print(
    f"Python {platform.python_version()}, FastAPI {version('fastapi')}, Uvicorn {uvicorn.__version__}, "
    f"SQLAlchemy {version('SQLAlchemy')}, Psycopg {version('psycopg')}"
)
engine = create_engine(
    URL.create("postgresql+psycopg", database=sys.argv[1]),
    connect_args={"connect_timeout": 5},
)
app = FastAPI()


class ProbeResult(BaseModel):
    status: Literal["ok"]


@app.get("/probe")
def probe() -> ProbeResult:
    try:
        with engine.connect() as db:
            row = db.execute(text("SELECT current_database(), current_setting('server_version_num')::int, 1")).one()
    except OperationalError:
        raise HTTPException(status_code=503, detail="database unavailable") from None
    name, server_version, value = row
    print(f"SQL: {name} | {server_version} | {value}")
    if (name, server_version // 10000, value) != ("fttc23_test", 17, 1):
        raise HTTPException(status_code=503, detail="unexpected database")
    return ProbeResult(status="ok")


def get(port, path):
    with closing(HTTPConnection("127.0.0.1", port, timeout=10)) as client:
        client.request("GET", path)
        response = client.getresponse()
        return response.status, response.getheader("Content-Type"), response.read()


with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    worker = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    worker.start()
    try:
        code, _, body = get(port, "/openapi.json")
        if code != 200:
            raise SystemExit(f"GET /openapi.json -> {code}")
        spec = json.loads(body)
        content = spec["paths"]["/probe"]["get"]["responses"]["200"]["content"]
        model_ref = content["application/json"]["schema"].get("$ref")
        required = spec["components"]["schemas"]["ProbeResult"]["required"]
        if model_ref != "#/components/schemas/ProbeResult" or "status" not in required:
            raise SystemExit("Схема успешного ответа /probe не совпала")
        print("OpenAPI: GET /probe -> ProbeResult")

        code, media_type, body = get(port, "/probe")
        print(f"GET /probe -> {code} {media_type} {body.decode()}")
        if (code, media_type) != (200, "application/json") or json.loads(body) != {"status": "ok"}:
            raise SystemExit("Проверка HTTP и БД не прошла")
    finally:
        server.should_exit = True
        worker.join(timeout=10)
        engine.dispose()
        if worker.is_alive():
            raise SystemExit("Сервер Uvicorn не остановился")
PY
