"""SQL მიგრაციები: `python -m app.db.migrate`  (DSN: APP_MIGRATION_DATABASE_URL, ან APP_DATABASE_URL).

* ფაილები: app/db/migrations/NNNN_name.sql, სრულდება ნომრის მიხედვით, თითო - ცალკე ტრანზაქციაში.
* გამოყენებული მიგრაციის შეცვლა აკრძალულია: checksum მოწმდება და შეუსაბამობისას გაშვება ჩერდება.
* advisory lock: ორი ერთდროული deploy (რამდენიმე replica) მიგრაციას ორჯერ ვერ გაუშვებს.
* აღრიცხვა `meta.schema_migrations`-შია - public სქემის ცხრილებზე გაცემული GRANT მას არ ეხება.

მიგრაციას უშვებს სქემის მფლობელი როლი (არა ai_accountant_app). ახალ ცხრილს სჭირდება: tenant_id,
RLS (ENABLE + FORCE + policy) და GRANT ai_accountant_app-ზე - ამას tests/test_migrations.py ამოწმებს.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import sys
from dataclasses import dataclass
from importlib import resources

import psycopg

log = logging.getLogger(__name__)
_NAME_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_LOCK_KEY = "ai_accountant:migrations"


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()


def discover() -> list[Migration]:
    found: dict[int, Migration] = {}
    for entry in resources.files("app.db").joinpath("migrations").iterdir():
        match = _NAME_RE.match(entry.name)
        if not match:
            continue
        version = int(match.group(1))
        if version in found:
            raise MigrationError(f"duplicate migration version {version:04d}")
        found[version] = Migration(version, entry.name, entry.read_text(encoding="utf-8"))
    return [found[v] for v in sorted(found)]


def migrate(dsn: str, migrations: list[Migration] | None = None) -> list[str]:
    """აბრუნებს ახლა გამოყენებული მიგრაციების სახელებს (უკვე გამოყენებულს ხელახლა არ უშვებს)."""
    migrations = discover() if migrations is None else migrations
    applied_now: list[str] = []
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (_LOCK_KEY,))
        try:
            conn.execute("CREATE SCHEMA IF NOT EXISTS meta")
            conn.execute("""CREATE TABLE IF NOT EXISTS meta.schema_migrations (
                                version    integer PRIMARY KEY,
                                name       text NOT NULL,
                                checksum   text NOT NULL,
                                applied_at timestamptz NOT NULL DEFAULT now())""")
            applied = {row[0]: (row[1], row[2]) for row in
                       conn.execute("SELECT version, name, checksum FROM meta.schema_migrations")}
            for m in migrations:
                if m.version in applied:
                    name, checksum = applied[m.version]
                    if checksum != m.checksum:
                        raise MigrationError(f"{m.name} was modified after it was applied (as {name}); "
                                             "add a new migration instead")
                    continue
                with conn.transaction():
                    conn.execute(m.sql)
                    conn.execute("INSERT INTO meta.schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                                 (m.version, m.name, m.checksum))
                log.info("applied migration %s", m.name)
                applied_now.append(m.name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (_LOCK_KEY,))
    return applied_now


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="apply database migrations")
    parser.add_argument("--dsn", default=os.environ.get("APP_MIGRATION_DATABASE_URL")
                        or os.environ.get("APP_DATABASE_URL"))
    parser.add_argument("--list", action="store_true", help="only list known migrations")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.list:
        for m in discover():
            print(f"{m.name}  {m.checksum[:12]}")
        return 0
    if not args.dsn:
        parser.error("set APP_MIGRATION_DATABASE_URL (schema owner) or pass --dsn")
    try:
        applied = migrate(args.dsn)
    except MigrationError as exc:
        log.error("%s", exc)
        return 1
    log.info("database is up to date (%d applied now)", len(applied))
    return 0


if __name__ == "__main__":
    sys.exit(main())
