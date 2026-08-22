#!/usr/bin/env python3
"""
Конвертер SQLite -> PostgreSQL (Supabase).

Читає будь-який файл SQLite, вивчає його схему і генерує SQL,
готовий до застосування в Supabase.

Використання:
    # 1. подивитись, що всередині, нічого не змінюючи
    python3 sqlite_to_supabase.py db.sqlite --inspect

    # 2. згенерувати SQL у теку out/
    python3 sqlite_to_supabase.py db.sqlite --out out/

Результат у --out:
    001_schema.sql        DDL: таблиці, PK, FK, індекси
    002_data_<table>.sql  дані пакетами INSERT
    003_finalize.sql      вирівнювання sequence для сурогатних ключів
    manifest.json         кількість рядків по таблицях для звірки
"""
import argparse
import json
import os
import re
import sqlite3
import sys

# --- мапінг типів -----------------------------------------------------------
# SQLite має динамічну типізацію: значення має тип, а не колонка.
# Орієнтуємось на оголошений тип (type affinity), як це робить сам SQLite.
TYPE_MAP = [
    (r"^bool", "boolean"),
    (r"^(tinyint|smallint|int2)", "smallint"),
    (r"^(bigint|int8|unsigned big int)", "bigint"),
    (r"int", "bigint"),                    # включно з INTEGER, MEDIUMINT
    (r"^(datetime|timestamp)", "timestamptz"),
    (r"^date$", "date"),
    (r"^time$", "time"),
    (r"(char|clob|text)", "text"),
    (r"blob", "bytea"),
    (r"^(real|float|double)", "double precision"),
    (r"^(numeric|decimal)", "numeric"),
    (r"^json", "jsonb"),
    (r"^uuid", "uuid"),
]


def pg_type(decl: str) -> str:
    d = (decl or "").strip().lower()
    if not d:
        return "text"                      # колонка без оголошеного типу
    for pattern, target in TYPE_MAP:
        if re.search(pattern, d):
            return target
    return "text"


def q(ident: str) -> str:
    """Ідентифікатор у подвійних лапках — безпечно для будь-яких назв."""
    return '"' + ident.replace('"', '""') + '"'


def lit(value, target_type: str) -> str:
    """Значення Python -> SQL-літерал Postgres."""
    if value is None:
        return "NULL"
    if target_type == "boolean":
        if isinstance(value, str):
            return "true" if value.strip().lower() in ("1", "true", "t", "yes") else "false"
        return "true" if value else "false"
    if target_type == "bytea":
        raw = value if isinstance(value, (bytes, bytearray)) else str(value).encode()
        return "'\\x" + raw.hex() + "'::bytea"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (bytes, bytearray)):
        # текстова колонка, але значення прийшло як BLOB
        value = value.decode("utf-8", errors="replace")
    s = str(value).replace("'", "''")
    if target_type == "jsonb":
        return "'" + s + "'::jsonb"
    return "'" + s + "'"


# --- читання схеми ----------------------------------------------------------
def read_schema(conn):
    cur = conn.cursor()
    cur.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    tables = []
    for (name,) in cur.fetchall():
        cols = []
        for cid, cname, decl, notnull, default, pk in cur.execute(
            f"PRAGMA table_info({q(name)})"
        ).fetchall():
            cols.append({
                "name": cname,
                "sqlite_type": decl,
                "pg_type": pg_type(decl),
                "notnull": bool(notnull),
                "default": default,
                "pk": pk,                      # 0 = не PK, інакше позиція в PK
            })
        fks = [
            {"column": f[3], "ref_table": f[2], "ref_column": f[4],
             "on_update": f[5], "on_delete": f[6]}
            for f in cur.execute(f"PRAGMA foreign_key_list({q(name)})").fetchall()
        ]
        indexes = []
        for _seq, idx_name, unique, origin, _partial in cur.execute(
            f"PRAGMA index_list({q(name)})"
        ).fetchall():
            if origin != "c":                  # 'c' = створений явно через CREATE INDEX
                continue                       # pk/unique-констрейнти вже в DDL таблиці
            idx_cols = [r[2] for r in cur.execute(
                f"PRAGMA index_info({q(idx_name)})").fetchall() if r[2]]
            if idx_cols:
                indexes.append({"name": idx_name, "unique": bool(unique),
                                "columns": idx_cols})
        (count,) = cur.execute(f"SELECT count(*) FROM {q(name)}").fetchone()
        tables.append({"name": name, "columns": cols, "foreign_keys": fks,
                       "indexes": indexes, "row_count": count})
    return tables


def order_tables(tables):
    """Топологічне сортування: батьківські таблиці раніше за дочірні."""
    by_name = {t["name"]: t for t in tables}
    ordered, seen, visiting = [], set(), set()

    def visit(name):
        if name in seen or name not in by_name:
            return
        if name in visiting:                   # циклічна FK — розірвемо тут
            return
        visiting.add(name)
        for fk in by_name[name]["foreign_keys"]:
            visit(fk["ref_table"])
        visiting.discard(name)
        seen.add(name)
        ordered.append(by_name[name])

    for t in tables:
        visit(t["name"])
    return ordered


# --- генерація SQL ----------------------------------------------------------
def build_ddl(tables):
    out = ["-- Згенеровано sqlite_to_supabase.py. Схема: public.", ""]
    for t in tables:
        pk_cols = [c for c in t["columns"] if c["pk"]]
        pk_cols.sort(key=lambda c: c["pk"])
        lines = []
        for c in t["columns"]:
            line = f"  {q(c['name'])} {c['pg_type']}"
            # NOT NULL для одноколонкового PK ставить сам PRIMARY KEY
            if c["notnull"] and not (len(pk_cols) == 1 and c["pk"]):
                line += " NOT NULL"
            d = c["default"]
            if d is not None and str(d).upper() not in ("NULL",):
                if str(d).upper() in ("CURRENT_TIMESTAMP", "CURRENT_DATE", "CURRENT_TIME"):
                    line += f" DEFAULT {d.lower()}"
                elif c["pg_type"] == "boolean":
                    line += f" DEFAULT {lit(d, 'boolean')}"
                else:
                    line += f" DEFAULT {d}"
            lines.append(line)
        if pk_cols:
            cols = ", ".join(q(c["name"]) for c in pk_cols)
            lines.append(f"  PRIMARY KEY ({cols})")
        out.append(f"CREATE TABLE IF NOT EXISTS public.{q(t['name'])} (")
        out.append(",\n".join(lines))
        out.append(");")
        out.append("")

    # FK окремим проходом — усі таблиці вже існують
    for t in tables:
        for i, fk in enumerate(t["foreign_keys"]):
            if not fk["column"] or not fk["ref_column"]:
                continue
            name = f"{t['name']}_{fk['column']}_fkey_{i}"
            clause = (
                f"ALTER TABLE public.{q(t['name'])} "
                f"ADD CONSTRAINT {q(name)} FOREIGN KEY ({q(fk['column'])}) "
                f"REFERENCES public.{q(fk['ref_table'])} ({q(fk['ref_column'])})"
            )
            if fk["on_delete"] and fk["on_delete"] != "NO ACTION":
                clause += f" ON DELETE {fk['on_delete']}"
            if fk["on_update"] and fk["on_update"] != "NO ACTION":
                clause += f" ON UPDATE {fk['on_update']}"
            # ADD CONSTRAINT не має IF NOT EXISTS — гасимо помилку,
            # щоб файл можна було застосувати повторно
            out.append(
                "DO $$ BEGIN\n"
                f"  {clause};\n"
                "EXCEPTION WHEN duplicate_object THEN NULL;\n"
                "END $$;"
            )
    out.append("")
    for t in tables:
        for idx in t["indexes"]:
            cols = ", ".join(q(c) for c in idx["columns"])
            uniq = "UNIQUE " if idx["unique"] else ""
            out.append(
                f"CREATE {uniq}INDEX IF NOT EXISTS {q(idx['name'])} "
                f"ON public.{q(t['name'])} ({cols});"
            )
    return "\n".join(out) + "\n"


def build_data(conn, table, batch_size):
    cols = table["columns"]
    names = ", ".join(q(c["name"]) for c in cols)
    types = [c["pg_type"] for c in cols]
    cur = conn.cursor()
    cur.execute(f"SELECT {', '.join(q(c['name']) for c in cols)} FROM {q(table['name'])}")
    chunks, batch = [], []

    def flush():
        if not batch:
            return
        chunks.append(
            f"INSERT INTO public.{q(table['name'])} ({names}) VALUES\n"
            + ",\n".join(batch) + "\nON CONFLICT DO NOTHING;"
        )
        batch.clear()

    for row in cur:
        batch.append("  (" + ", ".join(lit(v, t) for v, t in zip(row, types)) + ")")
        if len(batch) >= batch_size:
            flush()
    flush()
    header = f"-- {table['name']}: {table['row_count']} рядків\n"
    return header + "\n\n".join(chunks) + ("\n" if chunks else "")


def build_finalize(tables):
    """Вирівнює sequence там, де PK — одна ціла колонка (типовий autoincrement)."""
    out = ["-- Вирівнювання sequence після перенесення даних.", ""]
    found = False
    for t in tables:
        pk = [c for c in t["columns"] if c["pk"]]
        if len(pk) != 1 or pk[0]["pg_type"] not in ("bigint", "smallint"):
            continue
        col = pk[0]["name"]
        found = True
        # аргумент pg_get_serial_sequence парситься як кваліфіковане ім'я,
        # тому назва таблиці має бути в лапках усередині рядкового літерала
        ref = ("public." + q(t["name"])).replace("'", "''")
        colref = col.replace("'", "''")
        out.append(
            f"SELECT setval(pg_get_serial_sequence('{ref}', '{colref}'), "
            f"COALESCE((SELECT max({q(col)}) FROM public.{q(t['name'])}), 1), true) "
            f"WHERE pg_get_serial_sequence('{ref}', '{colref}') IS NOT NULL;"
        )
    if not found:
        out.append("-- Сурогатних цілочисельних ключів не знайдено, нічого робити.")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser(description="SQLite -> Supabase/PostgreSQL")
    ap.add_argument("sqlite_path")
    ap.add_argument("--out", help="тека для згенерованого SQL")
    ap.add_argument("--inspect", action="store_true",
                    help="лише показати схему, нічого не генерувати")
    ap.add_argument("--batch-size", type=int, default=500,
                    help="рядків на один INSERT (типово 500)")
    args = ap.parse_args()

    if not os.path.isfile(args.sqlite_path):
        sys.exit(f"Файл не знайдено: {args.sqlite_path}")
    with open(args.sqlite_path, "rb") as fh:
        if fh.read(15) != b"SQLite format 3":
            sys.exit(f"Це не база SQLite: {args.sqlite_path}")

    conn = sqlite3.connect(f"file:{args.sqlite_path}?mode=ro", uri=True)
    tables = order_tables(read_schema(conn))

    if not tables:
        sys.exit("У базі немає таблиць.")

    total = sum(t["row_count"] for t in tables)
    print(f"Таблиць: {len(tables)}, рядків усього: {total}\n")
    for t in tables:
        pk = [c["name"] for c in t["columns"] if c["pk"]]
        print(f"  {t['name']}  ({t['row_count']} рядків, {len(t['columns'])} колонок)"
              f"{'  PK: ' + ', '.join(pk) if pk else '  PK: немає'}")
        if args.inspect:
            for c in t["columns"]:
                print(f"      {c['name']}: {c['sqlite_type'] or '(без типу)'} -> {c['pg_type']}")
            for fk in t["foreign_keys"]:
                print(f"      FK {fk['column']} -> {fk['ref_table']}.{fk['ref_column']}")

    if args.inspect or not args.out:
        if not args.out:
            print("\nПідказка: додайте --out <тека>, щоб згенерувати SQL.")
        return

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "001_schema.sql"), "w", encoding="utf-8") as fh:
        fh.write(build_ddl(tables))
    for i, t in enumerate(tables, start=1):
        if not t["row_count"]:
            continue
        fname = f"002_{i:02d}_data_{re.sub(r'[^A-Za-z0-9_]', '_', t['name'])}.sql"
        with open(os.path.join(args.out, fname), "w", encoding="utf-8") as fh:
            fh.write(build_data(conn, t, args.batch_size))
    with open(os.path.join(args.out, "003_finalize.sql"), "w", encoding="utf-8") as fh:
        fh.write(build_finalize(tables))
    manifest = {"source": os.path.abspath(args.sqlite_path),
                "tables": {t["name"]: t["row_count"] for t in tables},
                "total_rows": total}
    with open(os.path.join(args.out, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    print(f"\nSQL записано в {args.out}/")


if __name__ == "__main__":
    main()
