"""Advance imported PostgreSQL identities without changing existing records."""
from sqlalchemy import text
from sqlalchemy.orm import Session


def repair_sequences(db: Session, models=None) -> list[str]:
    if db.get_bind().dialect.name != 'postgresql':
        return []
    if models is None:
        from database import Base
        tables = [t for t in Base.metadata.sorted_tables if t.name.startswith('navia_') and 'id' in t.c]
    else:
        tables = [model.__table__ for model in models]
    repaired = []
    quote = db.get_bind().dialect.identifier_preparer.quote
    for table in sorted(tables, key=lambda t: t.name):
        name = quote(table.name)
        seq = db.execute(text('SELECT pg_get_serial_sequence(:table, \'id\')'), {'table': table.name}).scalar()
        if not seq:
            continue
        # Block inserts while inspecting the maximum, including other importers.
        db.execute(text(f'LOCK TABLE {name} IN SHARE ROW EXCLUSIVE MODE'))
        maximum = db.execute(text(f'SELECT MAX(id) FROM {name}')).scalar()
        if maximum is None:
            continue
        seq_identifier = db.execute(text('''SELECT quote_ident(n.nspname) || '.' || quote_ident(c.relname)
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.oid=CAST(:seq AS regclass)'''), {'seq': seq}).scalar_one()
        value, called = db.execute(text(f'SELECT last_value, is_called FROM {seq_identifier}')).one()
        if maximum > value or (maximum == value and not called):
            db.execute(text('SELECT setval(CAST(:seq AS regclass), :value, true)'), {'seq': seq, 'value': maximum})
            repaired.append(table.name)
    return repaired
