-- Extensions required by the platform, created at first initialisation.
--
-- pgvector backs knowledge base retrieval (spec 33). Creating it here rather
-- than in a migration means Alembic revisions never need superuser rights.

CREATE EXTENSION IF NOT EXISTS "vector";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "citext";
