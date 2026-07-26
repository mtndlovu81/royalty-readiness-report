-- Royalty Readiness Report — schema.
--
-- Deliberately not idempotent: a second run fails loudly rather than silently
-- leaving the database on an older shape. Use `python scripts/init_db.py
-- --reset` to drop and rebuild.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE artists (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug            text UNIQUE NOT NULL,
  name            text NOT NULL,
  mbid            text UNIQUE,
  disambiguation  text,
  country         text,
  ipis            text[] NOT NULL DEFAULT '{}',
  status          text NOT NULL DEFAULT 'building',
    -- building | published | pending | failed
  source          text NOT NULL DEFAULT 'musicbrainz',
  verified_at     timestamptz,
  last_checked_at timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE songs (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  artist_id   uuid NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
  slug        text NOT NULL,
  title       text NOT NULL,
  iswc        text,
  work_mbid   text UNIQUE,
  source      text NOT NULL DEFAULT 'musicbrainz',
  verified_at timestamptz,
  UNIQUE (artist_id, slug)
);

CREATE TABLE versions (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  song_id        uuid NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
  recording_mbid text UNIQUE,
  title          text NOT NULL,
  isrc           text,
  length_ms      integer,
  first_released date,
  is_primary     boolean NOT NULL DEFAULT false,
  source         text NOT NULL DEFAULT 'musicbrainz',
  verified_at    timestamptz
);

CREATE TABLE contributors (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  mbid            text UNIQUE,
  name            text NOT NULL,
  ipis            text[] NOT NULL DEFAULT '{}',
  source          text NOT NULL DEFAULT 'musicbrainz',
  verified_at     timestamptz,
  last_checked_at timestamptz
);

CREATE TABLE song_contributors (
  song_id        uuid NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
  contributor_id uuid NOT NULL REFERENCES contributors(id),
  role           text NOT NULL,       -- writer | composer | lyricist | producer
  credited_as    text,                -- NEVER the Legal name alias
  PRIMARY KEY (song_id, contributor_id, role)
);

CREATE TABLE albums (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  artist_id          uuid NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
  slug               text NOT NULL,
  title              text NOT NULL,
  release_group_mbid text UNIQUE,
  first_released     date,
  UNIQUE (artist_id, slug)
);

CREATE TABLE album_versions (
  album_id   uuid NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
  version_id uuid NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  position   integer,
  PRIMARY KEY (album_id, version_id)
);

CREATE TABLE build_queue (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  artist_mbid  text UNIQUE NOT NULL,
  artist_id    uuid REFERENCES artists(id),
  status       text NOT NULL DEFAULT 'queued',
    -- queued | running | done | failed
  attempts     integer NOT NULL DEFAULT 0,
  error        text,
  requested_at timestamptz NOT NULL DEFAULT now(),
  started_at   timestamptz,
  finished_at  timestamptz
);

CREATE TABLE issues (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  type              text NOT NULL,   -- artist_request | data_report
  -- artist_request payload
  requested_name    text,
  spotify_artist_id text,
  -- data_report payload
  entity_type       text,            -- song | version | contributor | artist
  entity_id         uuid,
  field             text,
  user_says         text,
  suggested_value   text,
  -- shared
  request_count     integer NOT NULL DEFAULT 1,
  status            text NOT NULL DEFAULT 'open',
    -- open | in_progress | resolved | unconfirmed
  resolution_note   text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX issues_spotify_unique
  ON issues (spotify_artist_id)
  WHERE type = 'artist_request' AND spotify_artist_id IS NOT NULL;

CREATE UNIQUE INDEX issues_report_unique
  ON issues (entity_type, entity_id, field)
  WHERE type = 'data_report';

CREATE INDEX songs_artist_idx      ON songs (artist_id);
CREATE INDEX versions_song_idx     ON versions (song_id);
CREATE INDEX artists_status_idx    ON artists (status);
CREATE INDEX build_queue_status_idx ON build_queue (status, requested_at);
