"""One-time schema upgrades; run before the bot connects to Discord."""


def allow_nhie(store):
    if store.postgres:
        with store.transaction():
            # Serialize rollout startups before checking the migration marker.
            store.execute("LOCK TABLE prompts IN ACCESS EXCLUSIVE MODE")
            if store.execute("SELECT 1 FROM schema_migrations WHERE version=1").fetchone():
                return
            store.execute("ALTER TABLE prompts DROP CONSTRAINT prompts_kind_check")
            store.execute("""ALTER TABLE prompts ADD CONSTRAINT prompts_kind_check
                CHECK(kind IN ('truth','dare','nhie'))""")
            store.execute("INSERT INTO schema_migrations(version) VALUES(1)")
        return

    # SQLite cannot replace a CHECK constraint in place. Keep IDs and rebuild
    # inside one transaction; history continues to reference the final name.
    if store.conn.in_transaction:
        raise RuntimeError("Schema migration requires its own transaction.")
    store.execute("PRAGMA foreign_keys=OFF")
    try:
        with store.transaction():
            store.execute("BEGIN IMMEDIATE")
            if store.execute("SELECT 1 FROM schema_migrations WHERE version=1").fetchone():
                return
            columns = [row["name"] for row in store.execute("PRAGMA table_info(prompts)").fetchall()]
            if columns != ["id", "kind", "intensity", "theme", "text"]:
                raise RuntimeError("Unexpected prompt schema; migration left data unchanged.")
            dependencies = store.execute("""SELECT sql FROM sqlite_schema
                WHERE tbl_name='prompts' AND type IN ('index','trigger')
                AND sql IS NOT NULL""").fetchall()
            store.execute("""CREATE TABLE prompts_nhie_upgrade (
                id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('truth','dare','nhie')),
                intensity INTEGER NOT NULL CHECK(intensity BETWEEN 1 AND 5),
                theme TEXT, text TEXT NOT NULL UNIQUE)""")
            store.execute("""INSERT INTO prompts_nhie_upgrade(id,kind,intensity,theme,text)
                SELECT id,kind,intensity,theme,text FROM prompts""")
            store.execute("DROP TABLE prompts")
            store.execute("ALTER TABLE prompts_nhie_upgrade RENAME TO prompts")
            for row in dependencies:
                store.execute(row["sql"])
            if store.execute("PRAGMA foreign_key_check").fetchone():
                raise RuntimeError("Foreign-key check failed; migration left data unchanged.")
            store.execute("INSERT INTO schema_migrations(version) VALUES(1)")
    finally:
        store.execute("PRAGMA foreign_keys=ON")
