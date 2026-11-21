-- 0006: the team profile. An identity provider's subject maps to a principal; comments and
-- assignments are how people work on one case together. Nothing here is evidence: a comment
-- is an analyst's note about the record, never an assertion about the world.

CREATE TABLE principal_identity (
  issuer        text NOT NULL,
  subject       text NOT NULL,
  principal_id  uuid NOT NULL REFERENCES principal(id),
  workspace_id  uuid NOT NULL REFERENCES workspace(id),
  email         text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  last_seen_at  timestamptz,
  PRIMARY KEY (issuer, subject)
);

-- Append-only: a comment can be answered or superseded, not rewritten.
CREATE TABLE comment (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  workspace_id  uuid NOT NULL,
  case_id       uuid NOT NULL,
  target_kind   text NOT NULL CHECK (target_kind IN ('case', 'entity', 'artifact', 'assertion', 'plan')),
  target_id     text,
  body          text NOT NULL CHECK (length(body) BETWEEN 1 AND 10000),
  author        uuid NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (case_id, workspace_id) REFERENCES case_(id, workspace_id)
);
CREATE INDEX comment_case ON comment (case_id, id);
CREATE TRIGGER comment_append_only BEFORE UPDATE OR DELETE ON comment
  FOR EACH ROW EXECUTE FUNCTION astral_forbid_mutation();

CREATE TABLE assignment (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  workspace_id  uuid NOT NULL,
  case_id       uuid NOT NULL,
  target_kind   text NOT NULL CHECK (target_kind IN ('case', 'entity', 'artifact', 'assertion', 'plan')),
  target_id     text,
  assignee      uuid NOT NULL,
  assigned_by   uuid NOT NULL,
  note          text NOT NULL DEFAULT '',
  status        text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
  created_at    timestamptz NOT NULL DEFAULT now(),
  closed_at     timestamptz,
  FOREIGN KEY (case_id, workspace_id) REFERENCES case_(id, workspace_id)
);
CREATE INDEX assignment_case ON assignment (case_id, status);

INSERT INTO schema_migration (version) VALUES ('0006_team');
