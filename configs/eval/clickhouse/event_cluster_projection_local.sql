-- Evaluation-only mirror of lucida-next/database/ddl/clickhouse/
-- 025_event_cluster_projection.sql. Historical capture replay must not inherit
-- the production retention TTL.
CREATE TABLE IF NOT EXISTS lucida.event_cluster_projection_local
(
    event_id        UUID,
    cluster_id      String,
    desired_version UInt64,
    assigned_at     DateTime64(9, 'UTC') DEFAULT now64(9)
)
ENGINE = ReplacingMergeTree(desired_version)
PARTITION BY modulo(cityHash64(event_id), 16)
ORDER BY event_id
SETTINGS index_granularity = 8192;
