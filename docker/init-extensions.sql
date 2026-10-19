-- Initialize essential PostgreSQL extensions for OrbitR
-- PostGIS for geospatial queries (PointField, MultiPolygonField, distance calculation)
CREATE EXTENSION IF NOT EXISTS postgis;

-- pg_trgm for fuzzy text search and TrigramSimilarity matching
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Additional utility extensions for similarity and indexing
CREATE EXTENSION IF NOT EXISTS fuzzystrmatch;
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
