"""Normalized schema dump (JSON) for parity comparison. Usage: python scripts/dev/dump_schema.py <sync dsn>"""
import json, sys
from sqlalchemy import create_engine, text
eng = create_engine(sys.argv[1])
out = {"tables": {}, "enums": {}}
with eng.connect() as c:
    tables = [r[0] for r in c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1"))]
    for t in tables:
        cols = c.execute(text("""SELECT column_name, data_type, udt_name, is_nullable, column_default, character_maximum_length
                                 FROM information_schema.columns WHERE table_schema='public' AND table_name=:t ORDER BY column_name"""), {"t": t}).all()
        cons = c.execute(text("""SELECT contype, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = CAST(:t AS regclass) ORDER BY 2"""), {"t": t}).all()
        idx = c.execute(text("""SELECT indexdef FROM pg_indexes WHERE schemaname='public' AND tablename=:t ORDER BY 1"""), {"t": t}).all()
        out["tables"][t] = {
            "columns": {r[0]: [r[1], r[2], r[3], (r[4] or "").replace("::text","").replace("'",""), r[5]] for r in cols},
            "constraints": sorted(f"{r[0]}:{r[1]}" for r in cons),
            "indexes": sorted(r[0].split(" USING ",1)[1] if " USING " in r[0] else r[0] for r in idx),
        }
    enums = c.execute(text("""SELECT t.typname, string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder) FROM pg_type t JOIN pg_enum e ON e.enumtypid=t.oid GROUP BY 1 ORDER BY 1""")).all()
    out["enums"] = {r[0]: r[1] for r in enums}
print(json.dumps(out, indent=1, sort_keys=True))
