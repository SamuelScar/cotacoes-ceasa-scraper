CREATE TABLE IF NOT EXISTS cotacoes_sync_runs (
    id SMALLINT PRIMARY KEY CHECK (id = 1),
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    current_table TEXT,
    last_id BIGINT NOT NULL DEFAULT 0,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS cotacoes_sync_watermarks (
    table_name TEXT PRIMARY KEY,
    last_id BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE cotacoes_sync_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE cotacoes_sync_watermarks ENABLE ROW LEVEL SECURITY;

INSERT INTO cotacoes_sync_watermarks (table_name, last_id, updated_at)
SELECT 'estados', COALESCE(MAX(id), 0), NOW() FROM estados
ON CONFLICT (table_name) DO UPDATE
SET last_id = EXCLUDED.last_id, updated_at = EXCLUDED.updated_at;

INSERT INTO cotacoes_sync_watermarks (table_name, last_id, updated_at)
SELECT 'ceasas', COALESCE(MAX(id), 0), NOW() FROM ceasas
ON CONFLICT (table_name) DO UPDATE
SET last_id = EXCLUDED.last_id, updated_at = EXCLUDED.updated_at;

INSERT INTO cotacoes_sync_watermarks (table_name, last_id, updated_at)
SELECT 'entrepostos', COALESCE(MAX(id), 0), NOW() FROM entrepostos
ON CONFLICT (table_name) DO UPDATE
SET last_id = EXCLUDED.last_id, updated_at = EXCLUDED.updated_at;

INSERT INTO cotacoes_sync_watermarks (table_name, last_id, updated_at)
SELECT 'categorias', COALESCE(MAX(id), 0), NOW() FROM categorias
ON CONFLICT (table_name) DO UPDATE
SET last_id = EXCLUDED.last_id, updated_at = EXCLUDED.updated_at;

INSERT INTO cotacoes_sync_watermarks (table_name, last_id, updated_at)
SELECT 'produtos', COALESCE(MAX(id), 0), NOW() FROM produtos
ON CONFLICT (table_name) DO UPDATE
SET last_id = EXCLUDED.last_id, updated_at = EXCLUDED.updated_at;

INSERT INTO cotacoes_sync_watermarks (table_name, last_id, updated_at)
SELECT 'produto_aliases', COALESCE(MAX(id), 0), NOW() FROM produto_aliases
ON CONFLICT (table_name) DO UPDATE
SET last_id = EXCLUDED.last_id, updated_at = EXCLUDED.updated_at;

INSERT INTO cotacoes_sync_watermarks (table_name, last_id, updated_at)
SELECT 'unidades', COALESCE(MAX(id), 0), NOW() FROM unidades
ON CONFLICT (table_name) DO UPDATE
SET last_id = EXCLUDED.last_id, updated_at = EXCLUDED.updated_at;

INSERT INTO cotacoes_sync_watermarks (table_name, last_id, updated_at)
SELECT 'apresentacoes_unidade', COALESCE(MAX(id), 0), NOW()
FROM apresentacoes_unidade
ON CONFLICT (table_name) DO UPDATE
SET last_id = EXCLUDED.last_id, updated_at = EXCLUDED.updated_at;

INSERT INTO cotacoes_sync_watermarks (table_name, last_id, updated_at)
SELECT 'coletas', COALESCE(MAX(id), 0), NOW() FROM coletas
ON CONFLICT (table_name) DO UPDATE
SET last_id = EXCLUDED.last_id, updated_at = EXCLUDED.updated_at;

INSERT INTO cotacoes_sync_watermarks (table_name, last_id, updated_at)
SELECT 'cotacoes', COALESCE(MAX(id), 0), NOW() FROM cotacoes
ON CONFLICT (table_name) DO UPDATE
SET last_id = EXCLUDED.last_id, updated_at = EXCLUDED.updated_at;

INSERT INTO cotacoes_sync_runs (
    id,
    mode,
    status,
    current_table,
    last_id,
    started_at,
    updated_at
)
VALUES (1, 'full', 'completed', NULL, 0, NOW(), NOW())
ON CONFLICT (id) DO UPDATE
SET mode = EXCLUDED.mode,
    status = EXCLUDED.status,
    current_table = EXCLUDED.current_table,
    last_id = EXCLUDED.last_id,
    started_at = EXCLUDED.started_at,
    updated_at = EXCLUDED.updated_at;
