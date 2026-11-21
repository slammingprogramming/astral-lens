-- 0004: face regions, their embeddings, and perceptual image hashes.
--
-- A face embedding is BIOMETRIC data (GDPR Art. 9, EU AI Act). Every row is
-- tagged at write time (`biometric`) and carries an `expires_at` taken from the
-- case's retention policy, so the retention clock starts when the data exists
-- and a sweep can delete it (`astral.faces.purge_expired`). The derived facts a
-- person acted on live in the assertion log; this table is a rebuildable index.
--
-- Embeddings are stored as real[] so the small tier needs no extension. When the
-- pgvector extension is present the same column is searched with `::vector`.
--
-- Not layer 1: rows here assert nothing about the world, and they ARE deletable
-- (retention, erasure), unlike assertions.

CREATE TABLE face (
  id             uuid PRIMARY KEY,
  workspace_id   uuid NOT NULL,
  case_id        uuid NOT NULL,
  artifact_sha   bytea NOT NULL,                  -- the image (or video frame) it came from
  frame_ms       integer,                         -- for video keyframes
  entity         uuid NOT NULL,                   -- the FaceRegion entity in the assertion log
  box            jsonb NOT NULL,                  -- {x, y, w, h} in source pixels
  landmarks      jsonb,
  det_score      real NOT NULL,
  model_id       text NOT NULL,                   -- key in data/models.yaml
  backend        text NOT NULL,
  embedding      real[] NOT NULL,
  dim            integer NOT NULL,
  -- operator-supplied watchlist enrolment (NULL = an ordinary face found in case media)
  watchlist      text,
  label          text,
  basis          text,                            -- why this person may be enrolled; recorded, not judged
  biometric      boolean NOT NULL DEFAULT true,
  expires_at     timestamptz,
  created_by     uuid NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (case_id, workspace_id) REFERENCES case_(id, workspace_id),
  CHECK (cardinality(embedding) = dim)
);
CREATE INDEX face_case ON face (case_id, model_id);
CREATE INDEX face_artifact ON face (case_id, artifact_sha);
CREATE INDEX face_expiry ON face (expires_at) WHERE expires_at IS NOT NULL;

CREATE TABLE image_hash (
  workspace_id   uuid NOT NULL,
  case_id        uuid NOT NULL,
  artifact_sha   bytea NOT NULL,
  algo           text NOT NULL,                   -- e.g. 'pdq-like-1'
  hash           text NOT NULL,                   -- hex
  quality        real,
  created_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (case_id, artifact_sha, algo),
  FOREIGN KEY (case_id, workspace_id) REFERENCES case_(id, workspace_id)
);

INSERT INTO schema_migration (version) VALUES ('0004_faces');
