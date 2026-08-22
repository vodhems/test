#!/usr/bin/env python3
"""
Звірка після перенесення: порівнює кількість рядків у SQLite і в цільовій базі.

    python3 verify_migration.py db.sqlite --expected out/manifest.json
    python3 verify_migration.py db.sqlite --actual '{"clients": 3}'

--actual приймає JSON з фактичними лічильниками з Postgres. Отримати його можна
запитом, який віддає Supabase:

    SELECT jsonb_object_agg(table_name, n) FROM (
      SELECT c.relname AS table_name, c.reltuples::bigint AS n
      FROM pg_class c JOIN pg_namespace ns ON ns.oid = c.relnamespace
      WHERE ns.nspname = 'public' AND c.relkind = 'r') s;

  Увага: reltuples — оцінка планувальника. Для точної звірки використовуйте
  count(*) по кожній таблиці (повільніше, але точно).
"""
import argparse
import json
import os
import sqlite3
import sys


def sqlite_counts(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
    names = [r[0] for r in cur.fetchall()]
    out = {}
    for n in names:
        ident = '"' + n.replace('"', '""') + '"'
        out[n] = cur.execute(f"SELECT count(*) FROM {ident}").fetchone()[0]
    conn.close()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sqlite_path")
    ap.add_argument("--expected", help="шлях до manifest.json")
    ap.add_argument("--actual", help="JSON {\"таблиця\": кількість} з цільової бази")
    args = ap.parse_args()

    src = sqlite_counts(args.sqlite_path)
    if args.actual:
        target = json.loads(args.actual)
    elif args.expected and os.path.isfile(args.expected):
        target = json.load(open(args.expected, encoding="utf-8")).get("tables", {})
    else:
        sys.exit("Вкажіть --actual або --expected.")

    width = max([len(t) for t in set(src) | set(target)] + [8])
    ok = True
    print(f"{'таблиця'.ljust(width)}  {'SQLite':>8}  {'ціль':>8}  статус")
    print("-" * (width + 30))
    for name in sorted(set(src) | set(target)):
        a, b = src.get(name), target.get(name)
        if a is None:
            status, ok = "⚠️  лише в цілі", False
        elif b is None:
            status, ok = "❌ не перенесено", False
        elif a == b:
            status = "✅"
        else:
            status, ok = f"❌ розбіжність {b - a:+d}", False
        print(f"{name.ljust(width)}  {str(a if a is not None else '—'):>8}  "
              f"{str(b if b is not None else '—'):>8}  {status}")
    print()
    print("Звірка пройдена." if ok else "Є розбіжності — переносити далі не можна.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
