-- 0011: dedicated search and vector services (OpenSearch, Qdrant) as REBUILDABLE PROJECTIONS.
-- service_sync records how far each service has been brought in step with Postgres for a case. It holds
-- counters and a fingerprint of face ids, never content, and is removed with the case.
--
-- service_pending_delete is the record that a deletion could not be carried out because a service was
-- unreachable (case erased, or biometrics purged). It holds only a case id and a service name: the
-- service still has data it should not, and this row is what makes that visible and retryable. It is
-- deliberately NOT removed by case erasure, for the same reason a tombstone is not.

CREATE TABLE service_sync (
  case_id     uuid NOT NULL,
  service     text NOT NULL,
  kind        text NOT NULL CHECK (kind IN ('text', 'faces')),
  cursor      bigint NOT NULL DEFAULT 0,
  fingerprint text,
  synced_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (case_id, service, kind)
);

CREATE TABLE service_pending_delete (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  case_id    uuid NOT NULL,
  service    text NOT NULL,
  kind       text NOT NULL CHECK (kind IN ('text', 'faces', 'case')),
  reason     text NOT NULL,
  noted_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (case_id, service, kind)
);

INSERT INTO schema_migration (version) VALUES ('0011_services');
