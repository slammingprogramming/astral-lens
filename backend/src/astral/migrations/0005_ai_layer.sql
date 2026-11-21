-- 0005: retrieval index and the plan ledger for the AI layer.
--
-- `text_chunk` is a rebuildable INDEX over text artifacts (documents, transcripts,
-- OCR). It holds no claims about the world; a chunk's `text_sha` + char range is
-- the citation anchor. `ai_plan` records what the AI layer was asked, what plan
-- was shown to the operator, whether it was approved, and what came back. The
-- layer writes NO assertions: model output is never evidence (invariant 10).

CREATE TABLE text_chunk (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  workspace_id  uuid NOT NULL,
  case_id       uuid NOT NULL,
  text_sha      bytea NOT NULL,
  start_char    integer NOT NULL,
  end_char      integer NOT NULL,
  body          text NOT NULL,
  tsv           tsvector GENERATED ALWAYS AS (to_tsvector('simple', body)) STORED,
  UNIQUE (case_id, text_sha, start_char),
  FOREIGN KEY (case_id, workspace_id) REFERENCES case_(id, workspace_id)
);
CREATE INDEX text_chunk_tsv ON text_chunk USING gin (tsv);
CREATE INDEX text_chunk_case ON text_chunk (case_id, text_sha);

CREATE TABLE ai_plan (
  id            uuid PRIMARY KEY,
  workspace_id  uuid NOT NULL,
  case_id       uuid NOT NULL,
  question      text NOT NULL,
  planner       text NOT NULL,                    -- 'rules' or the model that proposed it
  plan          jsonb NOT NULL,                   -- typed, bounded steps + cost/egress/footprint
  status        text NOT NULL DEFAULT 'proposed'
                CHECK (status IN ('proposed', 'approved', 'running', 'done', 'failed', 'rejected')),
  result        jsonb,
  created_by    uuid NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  approved_at   timestamptz,
  finished_at   timestamptz,
  FOREIGN KEY (case_id, workspace_id) REFERENCES case_(id, workspace_id)
);
CREATE INDEX ai_plan_case ON ai_plan (case_id, created_at);

INSERT INTO schema_migration (version) VALUES ('0005_ai_layer');
