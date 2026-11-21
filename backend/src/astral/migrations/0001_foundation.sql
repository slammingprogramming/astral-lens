-- 0001 foundation: layers 0 and 1, tenancy seam, audit, tombstones.
-- Layers 2 and 3 (projections) live in 0002 and are disposable.
--
-- Invariants enforced here rather than by convention:
--   * assertion rows are append-only (no UPDATE ever; DELETE only inside the
--     case-scoped hard-delete path, which sets astral.hard_delete)
--   * assertion INSERT is allowed only from the assertion writer, which sets
--     astral.writer for its transaction
--   * every assertion carries evidence that is acquired in the same case, and
--     a derived_by record naming plugin, version and backend
--   * retraction is a separate append-only table, never a column update

CREATE TABLE schema_migration (
  version     text PRIMARY KEY,
  applied_at  timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- tenancy

CREATE TABLE workspace (
  id          uuid PRIMARY KEY,
  name        text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE principal (
  id            uuid PRIMARY KEY,
  workspace_id  uuid NOT NULL REFERENCES workspace(id),
  kind          text NOT NULL CHECK (kind IN ('user', 'service', 'agent')),
  name          text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (id, workspace_id)
);

CREATE TABLE case_ (
  id                  uuid PRIMARY KEY,
  workspace_id        uuid NOT NULL REFERENCES workspace(id),
  name                text NOT NULL,
  state               text NOT NULL DEFAULT 'open'
                      CHECK (state IN ('open', 'closed', 'erased')),
  retention_policy    jsonb NOT NULL DEFAULT '{}',
  collection_identity jsonb NOT NULL DEFAULT '{}',   -- egress persona (10 §2)
  created_by          uuid NOT NULL,
  created_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (id, workspace_id)
);

CREATE TABLE role_grant (
  id            uuid PRIMARY KEY,
  workspace_id  uuid NOT NULL REFERENCES workspace(id),
  principal_id  uuid NOT NULL REFERENCES principal(id),
  case_id       uuid REFERENCES case_(id),            -- NULL = workspace-wide
  role          text NOT NULL CHECK (role IN ('owner', 'investigator', 'reviewer', 'viewer')),
  granted_at    timestamptz NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------- sources

CREATE TABLE source (
  id            uuid PRIMARY KEY,
  workspace_id  uuid NOT NULL REFERENCES workspace(id),
  kind          text NOT NULL,                        -- 'upload', 'plugin', 'analyst', ...
  name          text NOT NULL,
  locator       text,
  -- Common upstream origin. Sources sharing an origin_key count once when
  -- corroboration is tallied (03 "Confidence propagation", rule 3).
  origin_key    text,
  -- A new, untested source is F: "no basis to judge", not "unreliable".
  default_source_grade char(1) NOT NULL DEFAULT 'F' CHECK (default_source_grade IN ('A','B','C','D','E','F')),
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, kind, name)
);

-- ------------------------------------------------------ layer 0: evidence

-- One row per distinct byte string. Visibility is NOT granted by this table:
-- an artifact is visible in a case only through artifact_acquisition, so
-- content-addressed dedup can never leak a blob across cases.
CREATE TABLE artifact (
  sha256        bytea PRIMARY KEY CHECK (octet_length(sha256) = 32),
  media_type    text NOT NULL,
  byte_size     bigint NOT NULL CHECK (byte_size >= 0),
  storage_uri   text NOT NULL,
  first_seen_at timestamptz NOT NULL,
  parent_sha256 bytea REFERENCES artifact(sha256),
  derivation    text                                   -- 'transcode', 'frame@12.3s', ...
);

-- 03 §2 shows acquisition fields on the artifact row but also says the same
-- image arriving from three sources is "one artifact with three acquisition
-- records". The second statement is the one that needs a table.
CREATE TABLE artifact_acquisition (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  workspace_id     uuid NOT NULL,
  case_id          uuid NOT NULL,
  sha256           bytea NOT NULL REFERENCES artifact(sha256),
  acquired_by      text NOT NULL,                      -- 'plugin-id@version' or 'upload'
  acquired_from    text,
  acquisition_ctx  jsonb NOT NULL DEFAULT '{}',
  acquired_at      timestamptz NOT NULL,
  acquired_principal uuid NOT NULL,
  FOREIGN KEY (case_id, workspace_id) REFERENCES case_(id, workspace_id)
);
CREATE INDEX artifact_acquisition_case ON artifact_acquisition (case_id, sha256);
CREATE INDEX artifact_acquisition_sha ON artifact_acquisition (sha256);

-- ------------------------------------------------------ layer 1: assertions

CREATE TABLE assertion (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  workspace_id   uuid NOT NULL,
  case_id        uuid NOT NULL,

  subject        uuid NOT NULL,
  predicate      text NOT NULL,
  object_kind    text NOT NULL CHECK (object_kind IN ('entity', 'literal')),
  object_entity  uuid,
  object_value   jsonb,

  source_id      uuid NOT NULL REFERENCES source(id),
  evidence       bytea[] NOT NULL,
  derived_from   bigint[] NOT NULL DEFAULT '{}',
  derived_by     jsonb NOT NULL,

  recorded_at    timestamptz NOT NULL,
  valid_from     timestamptz,
  valid_to       timestamptz,

  -- Admiralty: two independent axes, never collapsed. Pessimistic defaults.
  source_grade   char(1) NOT NULL DEFAULT 'F' CHECK (source_grade IN ('A','B','C','D','E','F')),
  info_grade     smallint NOT NULL DEFAULT 6 CHECK (info_grade BETWEEN 1 AND 6),

  created_by     uuid NOT NULL,

  FOREIGN KEY (case_id, workspace_id) REFERENCES case_(id, workspace_id),
  CHECK (cardinality(evidence) > 0),
  CHECK (
    (object_kind = 'entity'  AND object_entity IS NOT NULL AND object_value IS NULL) OR
    (object_kind = 'literal' AND object_value  IS NOT NULL AND object_entity IS NULL)
  ),
  CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from),
  CHECK (derived_by ?& ARRAY['plugin', 'version', 'backend'])
);
CREATE INDEX assertion_subject ON assertion (case_id, subject, predicate);
CREATE INDEX assertion_object  ON assertion (case_id, object_entity, predicate)
  WHERE object_entity IS NOT NULL;
CREATE INDEX assertion_derived_by_plugin ON assertion ((derived_by ->> 'plugin'), (derived_by ->> 'version'));
CREATE INDEX assertion_source ON assertion (source_id);

-- Retraction is its own append-only fact: "we stopped believing assertion N".
-- (03 declares assertion rows are never updated yet lists retracted_* columns
-- on them; the two statements conflict, and this table resolves it.)
CREATE TABLE assertion_retraction (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  workspace_id  uuid NOT NULL,
  case_id       uuid NOT NULL,
  assertion_id  bigint NOT NULL REFERENCES assertion(id),
  retracted_at  timestamptz NOT NULL,
  retracted_by  uuid NOT NULL,
  reason        text NOT NULL,
  batch_id      uuid,                                  -- one source/plugin sweep
  UNIQUE (assertion_id),
  FOREIGN KEY (case_id, workspace_id) REFERENCES case_(id, workspace_id)
);
CREATE INDEX assertion_retraction_batch ON assertion_retraction (batch_id);

-- Entity-resolution judgments: append-only edges (03 §5). Records are never
-- physically merged; undoing a merge is retracting the judgment.
CREATE TABLE judgment (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  workspace_id  uuid NOT NULL,
  case_id       uuid NOT NULL,
  entity_a      uuid NOT NULL,
  entity_b      uuid NOT NULL,
  verdict       text NOT NULL CHECK (verdict IN ('same', 'not_same', 'undecided')),
  -- field-level comparison vector that produced the proposal (03 §5)
  comparison    jsonb NOT NULL DEFAULT '{}',
  reasoning     text,
  decided_by    uuid NOT NULL,
  decided_at    timestamptz NOT NULL,
  FOREIGN KEY (case_id, workspace_id) REFERENCES case_(id, workspace_id),
  CHECK (entity_a <> entity_b)
);
CREATE INDEX judgment_case ON judgment (case_id, id);

CREATE TABLE judgment_retraction (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  workspace_id  uuid NOT NULL,
  case_id       uuid NOT NULL,
  judgment_id   bigint NOT NULL REFERENCES judgment(id),
  retracted_at  timestamptz NOT NULL,
  retracted_by  uuid NOT NULL,
  reason        text NOT NULL,
  UNIQUE (judgment_id),
  FOREIGN KEY (case_id, workspace_id) REFERENCES case_(id, workspace_id)
);

-- ------------------------------------------------------------------ audit

-- Append-only and hash-chained per workspace so tampering is detectable.
-- No case FK: the audit trail must outlive a hard delete of the case.
CREATE TABLE audit_log (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  workspace_id  uuid NOT NULL,
  case_id       uuid,
  at            timestamptz NOT NULL,
  principal_id  uuid NOT NULL,
  action        text NOT NULL,
  target_kind   text,
  target_id     text,
  detail        jsonb NOT NULL DEFAULT '{}',
  prev_hash     bytea NOT NULL,
  hash          bytea NOT NULL
);
CREATE INDEX audit_log_ws ON audit_log (workspace_id, id);
CREATE INDEX audit_log_case ON audit_log (case_id, id);

-- The one operation that genuinely destroys (10 §4). Always leaves a tombstone.
CREATE TABLE tombstone (
  id            uuid PRIMARY KEY,
  workspace_id  uuid NOT NULL,
  case_id       uuid NOT NULL,
  erased_at     timestamptz NOT NULL,
  erased_by     uuid NOT NULL,
  legal_basis   text NOT NULL,
  counts        jsonb NOT NULL
);

-- ------------------------------------------------------------ plugin registry

CREATE TABLE plugin (
  id            text NOT NULL,
  version       text NOT NULL,
  api_version   text NOT NULL,
  manifest      jsonb NOT NULL,
  runtime       text NOT NULL CHECK (runtime IN ('in_process', 'container')),
  state         text NOT NULL DEFAULT 'disabled'
                CHECK (state IN ('disabled', 'enabled', 'quarantined')),
  quarantine_reason text,
  signature     text,
  installed_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (id, version)
);

CREATE TABLE plugin_health (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  plugin_id     text NOT NULL,
  plugin_version text NOT NULL,
  checked_at    timestamptz NOT NULL,
  ok            boolean NOT NULL,
  error_class   text CHECK (error_class IN
                  ('site_changed', 'rate_limited', 'credentials_expired', 'blocked', 'other')),
  latency_ms    integer,
  detail        text
);
CREATE INDEX plugin_health_plugin ON plugin_health (plugin_id, plugin_version, id);

-- ------------------------------------------------- jobs (resource-aware queues)

CREATE TABLE job (
  id            uuid PRIMARY KEY,
  workspace_id  uuid NOT NULL,
  case_id       uuid,
  queue         text NOT NULL CHECK (queue IN ('cpu', 'gpu-small', 'gpu-large', 'remote')),
  stage         text NOT NULL,
  payload       jsonb NOT NULL DEFAULT '{}',
  depends_on    uuid[] NOT NULL DEFAULT '{}',
  state         text NOT NULL DEFAULT 'queued'
                CHECK (state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
  attempts      integer NOT NULL DEFAULT 0,
  max_attempts  integer NOT NULL DEFAULT 3,
  run_after     timestamptz NOT NULL DEFAULT now(),
  locked_by     text,
  locked_at     timestamptz,
  heartbeat_at  timestamptz,
  result        jsonb,
  error         text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz
);
CREATE INDEX job_claim ON job (queue, run_after) WHERE state = 'queued';

-- ------------------------------------------------------- model accounting

-- Declared VRAM figures are always optimistic; the residency manager records
-- what a load actually cost and prefers that on the next admission (05 §3).
CREATE TABLE model_footprint (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  model_id      text NOT NULL,
  backend       text NOT NULL,
  device        text NOT NULL,
  vram_mb       integer NOT NULL,
  ram_mb        integer NOT NULL,
  measured_at   timestamptz NOT NULL,
  host          text NOT NULL,
  notes         text
);
CREATE INDEX model_footprint_lookup ON model_footprint (model_id, backend, device, id);

-- --------------------------------------------------------------- guards

CREATE FUNCTION astral_forbid_mutation() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' AND current_setting('astral.hard_delete', true) = 'on' THEN
    RETURN OLD;
  END IF;
  RAISE EXCEPTION '% on % is forbidden: table is append-only', TG_OP, TG_TABLE_NAME
    USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION astral_forbid_audit_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION '% on % is forbidden: the audit log is append-only', TG_OP, TG_TABLE_NAME
    USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION astral_forbid_truncate() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'TRUNCATE on % is forbidden', TG_TABLE_NAME
    USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER assertion_append_only BEFORE UPDATE OR DELETE ON assertion
  FOR EACH ROW EXECUTE FUNCTION astral_forbid_mutation();
CREATE TRIGGER assertion_retraction_append_only BEFORE UPDATE OR DELETE ON assertion_retraction
  FOR EACH ROW EXECUTE FUNCTION astral_forbid_mutation();
CREATE TRIGGER judgment_append_only BEFORE UPDATE OR DELETE ON judgment
  FOR EACH ROW EXECUTE FUNCTION astral_forbid_mutation();
CREATE TRIGGER judgment_retraction_append_only BEFORE UPDATE OR DELETE ON judgment_retraction
  FOR EACH ROW EXECUTE FUNCTION astral_forbid_mutation();
CREATE TRIGGER artifact_acquisition_append_only BEFORE UPDATE OR DELETE ON artifact_acquisition
  FOR EACH ROW EXECUTE FUNCTION astral_forbid_mutation();
CREATE TRIGGER artifact_append_only BEFORE UPDATE OR DELETE ON artifact
  FOR EACH ROW EXECUTE FUNCTION astral_forbid_mutation();
CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE ON audit_log
  FOR EACH ROW EXECUTE FUNCTION astral_forbid_audit_mutation();
CREATE TRIGGER tombstone_append_only BEFORE UPDATE OR DELETE ON tombstone
  FOR EACH ROW EXECUTE FUNCTION astral_forbid_audit_mutation();
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
  FOR EACH STATEMENT EXECUTE FUNCTION astral_forbid_truncate();
CREATE TRIGGER assertion_no_truncate BEFORE TRUNCATE ON assertion
  FOR EACH STATEMENT EXECUTE FUNCTION astral_forbid_truncate();

-- Only the assertion writer may insert into layer 1 (invariant 1).
CREATE FUNCTION astral_assertion_guard() RETURNS trigger AS $$
DECLARE
  digest bytea;
  up bigint;
BEGIN
  IF current_setting('astral.writer', true) IS DISTINCT FROM '1' THEN
    RAISE EXCEPTION 'assertions may only be written through the assertion writer'
      USING ERRCODE = 'insufficient_privilege';
  END IF;

  -- evidence must be acquired in this very case
  FOREACH digest IN ARRAY NEW.evidence LOOP
    PERFORM 1 FROM artifact_acquisition
      WHERE case_id = NEW.case_id AND sha256 = digest;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'evidence % has no acquisition in case %', encode(digest, 'hex'), NEW.case_id
        USING ERRCODE = 'foreign_key_violation';
    END IF;
  END LOOP;

  -- upstream assertions must exist in this very case
  FOREACH up IN ARRAY NEW.derived_from LOOP
    PERFORM 1 FROM assertion WHERE id = up AND case_id = NEW.case_id;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'derived_from assertion % not found in case %', up, NEW.case_id
        USING ERRCODE = 'foreign_key_violation';
    END IF;
  END LOOP;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER assertion_guard BEFORE INSERT ON assertion
  FOR EACH ROW EXECUTE FUNCTION astral_assertion_guard();

INSERT INTO schema_migration (version) VALUES ('0001_foundation');
