# Music Glossary

## Shoegaze
Shoegaze is a rock subgenre built on walls of distorted guitar, heavy reverb, and vocals buried deep in the mix. The name comes from performers who stared at their effects pedals on stage.

## B-side
A B-side is the secondary track released alongside a single's main song. Historically it filled the flip side of a vinyl single, but the term now means any companion track.

## Focus music
Focus music is instrumental or low-lyric music chosen to support concentration. It tends to have steady tempo, minimal dynamic shifts, and no vocals that compete for attention.

## Scrobbling
Scrobbling is automatically logging the tracks you play to a listening-history service. A scrobbler app or browser extension sends each play to a service like ListenBrainz or Last.fm.

## MBID
An MBID (MusicBrainz Identifier) is a permanent UUID assigned to an entity in MusicBrainz, such as an artist, release, or recording. It is stable even if names or metadata change.

## JSPF
JSPF (JSON Shareable Playlist Format) is a JSON representation of a playlist. ListenBrainz uses a JSPF-style structure for creating and editing playlists, including track identifiers and extensions.

## ListenBrainz radio
ListenBrainz radio generates a playlist from a free-text prompt such as an artist or mood. The mode parameter (easy, medium, hard) controls how adventurous the selection is.

## Fresh releases
Fresh releases are new albums or singles from artists a user listens to, surfaced by ListenBrainz within a chosen time window. The window is capped, and an empty list is a valid result.

## Popularity
In ListenBrainz, popularity ranks recordings by how many users have listened. A top-recordings-for-artist query returns recordings ordered by total listen count.

## Feedback score
A feedback score records a user's opinion of a recording: 1 means loved, -1 means hated, and 0 means no feedback. Setting the same score twice has the same effect as once.

## Rate limiting
Rate limiting is a server policy that refuses requests above a threshold. ListenBrainz reports remaining quota in headers and asks clients to wait when the limit is hit.

## User-Agent
A User-Agent header identifies the client making an HTTP request. MusicBrainz requires a descriptive User-Agent with contact information so operators can reach abusive clients.

## Exponential backoff
Exponential backoff retries a failed request after growing delays. Adding random jitter spreads retries so many clients do not retry in lockstep.

## Hybrid search
Hybrid search combines keyword scoring (such as BM25) with vector similarity. It can retrieve exact-term matches that embeddings miss and semantic matches that keywords miss.

## Contextual prefix
A contextual prefix is a short sentence added before a chunk before embedding it, describing where the chunk fits. It can improve retrieval by anchoring a chunk to its source.