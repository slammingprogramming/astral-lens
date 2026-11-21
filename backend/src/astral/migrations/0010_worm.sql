-- 0010: write-once (WORM) preservation. A worm_copy records one object preserved in an S3 bucket with
-- Object Lock: where it is, under which retention, and when its bytes were last read back and
-- matched. It holds hashes and locations, never content.
--
-- worm_copy is deliberately NOT removed by case erasure: a COMPLIANCE-mode object cannot be deleted
-- by anyone until its retention ends, and this table is the record that it still exists and until
-- when. worm_case ties a case to the digests it asked to preserve and IS removed with the case.

CREATE TABLE worm_copy (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  workspace_id    uuid NOT NULL,
  kind            text NOT NULL CHECK (kind IN ('artifact', 'exhibit')),
  sha256          bytea NOT NULL,
  bucket          text NOT NULL,
  object_key      text NOT NULL,
  version_id      text NOT NULL,
  lock_mode       text NOT NULL CHECK (lock_mode IN ('COMPLIANCE', 'GOVERNANCE')),
  retain_until    timestamptz NOT NULL,
  size_bytes      bigint NOT NULL,
  mirrored_by     uuid NOT NULL,
  mirrored_at     timestamptz NOT NULL DEFAULT now(),
  last_verified_at timestamptz,
  last_verified_ok boolean,
  UNIQUE (bucket, object_key, version_id)
);
CREATE INDEX worm_copy_sha ON worm_copy (sha256);

CREATE TABLE worm_case (
  case_id   uuid NOT NULL,
  sha256    bytea NOT NULL,
  PRIMARY KEY (case_id, sha256)
);

INSERT INTO schema_migration (version) VALUES ('0010_worm');
