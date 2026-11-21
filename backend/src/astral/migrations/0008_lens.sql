-- 0008: lenses (02 §4). A lens is a named, versioned, parameterised pipeline stored as data. It
-- carries NO case content: parameters stand where a case's own names were. Workspace-level, so
-- it is not removed by erasing a case (and must not contain anything that would need to be).

CREATE TABLE lens (
  id            uuid PRIMARY KEY,
  workspace_id  uuid NOT NULL REFERENCES workspace(id),
  name          text NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9._-]{0,63}$'),
  version       integer NOT NULL CHECK (version >= 1),
  definition    jsonb NOT NULL,
  sha256        text NOT NULL,                       -- of the canonical definition
  origin        text NOT NULL CHECK (origin IN ('local', 'imported')),
  trust         jsonb NOT NULL DEFAULT '{}',         -- signature verdict at import
  created_by    uuid NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, name, version)
);
CREATE TRIGGER lens_append_only BEFORE UPDATE OR DELETE ON lens
  FOR EACH ROW EXECUTE FUNCTION astral_forbid_mutation();

INSERT INTO schema_migration (version) VALUES ('0008_lens');
