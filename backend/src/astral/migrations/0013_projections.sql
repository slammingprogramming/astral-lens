-- 0013: more kinds of rebuildable projection. Dense text vectors can be served from Qdrant ('chunks'), and a
-- graph store can hold a projection of a case's relationships ('graph'). Both are derived from Postgres and
-- removed with the case; the bookkeeping tables only learn the new kind names.

ALTER TABLE service_sync DROP CONSTRAINT service_sync_kind_check;
ALTER TABLE service_sync ADD CONSTRAINT service_sync_kind_check CHECK (kind IN ('text', 'faces', 'chunks', 'graph'));

ALTER TABLE service_pending_delete DROP CONSTRAINT service_pending_delete_kind_check;
ALTER TABLE service_pending_delete ADD CONSTRAINT service_pending_delete_kind_check CHECK (kind IN ('text', 'faces', 'chunks', 'graph', 'case'));

INSERT INTO schema_migration (version) VALUES ('0013_projections');
