-- 0014: contextual chunk headers. A model writes one short sentence that situates a passage in its document, so a
-- keyword search can find a passage that never repeats the document's subject (docs/08 section 5). It is an INDEX
-- aid like text_chunk itself: model output, never evidence, never shown as a quotation, never written as an assertion
-- (invariant 10). The passage's own text and its (text_sha, start_char, end_char) anchor are untouched, and a citation
-- is still checked against the stored bytes. Rebuildable: dropping the table loses nothing that is not regenerable.

CREATE TABLE text_chunk_context (
  chunk_id    bigint PRIMARY KEY REFERENCES text_chunk(id) ON DELETE CASCADE,
  case_id     uuid NOT NULL,
  header      text NOT NULL CHECK (char_length(header) BETWEEN 1 AND 400),
  model       text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  tsv         tsvector GENERATED ALWAYS AS (to_tsvector('simple', header)) STORED
);
CREATE INDEX text_chunk_context_tsv ON text_chunk_context USING gin (tsv);
CREATE INDEX text_chunk_context_case ON text_chunk_context (case_id);

INSERT INTO schema_migration (version) VALUES ('0014_chunk_context');
