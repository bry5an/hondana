"""Pydantic request/response models for server.py's JSON API.

Kept separate so server.py stays about routing. Covers the endpoints whose
shape is small and fixed; left out are the book/audiobook/vocab/known-stats
responses, which are deliberately open-ended denormalized joins (see
CLAUDE.md) that a strict response_model would silently truncate
as fields are added over time.

Request models mirror the manual `payload.get(...)` defaults server.py used
before Pydantic — same optional fields, same fallback values — so swapping
`payload: dict = Body(...)` for one of these is additive for OpenAPI/Swagger,
not a behavior change. Handlers keep their existing `if not x: raise
HTTPException(400, ...)` checks rather than leaning on Pydantic's 422s, so
error responses are unchanged too.
"""
from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter


# ── requests ──────────────────────────────────────────────────────────────

class PairingKeysRequest(BaseModel):
    en_key: str = ""
    jp_key: str = ""


class PairingUnlinkRequest(PairingKeysRequest):
    block: bool = True  # False = undo a link just made, leaving no block behind


class ReadStatusRequest(BaseModel):
    read: bool = True


class ReadDateRequest(BaseModel):
    read_at: date  # YYYY-MM-DD: the day the book was finished


class AbIdsRequest(BaseModel):
    ab_ids: list[str] = []


class AcquireRequest(BaseModel):
    url: str = ""
    language: str = "auto"
    force: bool = False  # add even if it duplicates a book already in the library


class GenreRequest(BaseModel):
    genre: str = ""


class TitleRequest(BaseModel):
    title: str = ""


class SeriesRequest(BaseModel):
    series_name: str = ""
    series_order: str = ""


class BulkGenreRequest(BaseModel):
    book_ids: list[str]
    genre: str = ""   # empty = back to the derived genre


class SeriesAssignment(BaseModel):
    book_id: str
    series_name: str = ""   # empty = pinned standalone (unless `reset`)
    series_order: str = ""
    reset: bool = False     # drop the override: back to the automatic series


class BulkSeriesRequest(BaseModel):
    assignments: list[SeriesAssignment]


class SeriesRenameRequest(BaseModel):
    from_name: str
    to_name: str   # an existing series' name merges into it


class SeriesEdit(BaseModel):
    name: str = ""   # empty = pinned standalone
    order: str = ""


class BookDetailsRequest(BaseModel):
    """Every field is optional: omitted = leave alone, null (or "") = back to the
    automatic value, anything else = hand-set. For `series`, null resets while
    {"name": ""} pins the book as standalone."""
    title: str | None = None
    author: str | None = None
    language: str | None = None  # ja | en | other
    genre: str | None = None
    series: SeriesEdit | None = None


class BookDetailsState(BaseModel):
    values: dict         # what the book shows now: title/author/language/genre/series_name/series_order
    auto: dict           # what it would show with no override: title/author/language/genre
    overridden: list[str]  # fields with a hand-set value (series included when pinned/set)
    series_names: list[str]
    suggestion: dict | None  # {"series_name", "series_order"} for a book with no series
    cover: dict          # {"has": any cover at all, "overridden": hand-set, "v": cache-bust token}


class BookDetailsResponse(BookDetailsState):
    status: str
    job: dict | None  # the pipeline run queued by a language change


class AnalyzeBatchRequest(BaseModel):
    book_ids: list[str] = []


class AnalyzeBatchResponse(BaseModel):
    status: str
    jobs: dict  # {book_id: job state}


class BookJob(BaseModel):
    state: str  # queued | running | done | error
    step: str | None
    error: str | None
    queued_at: float
    finished_at: float | None


class AnalyzeQueuedResponse(BaseModel):
    status: str  # queued | skipped
    reason: str | None
    job: BookJob | None


class AlignmentState(BaseModel):
    state: str  # none | ready | stale | queued | running (a SubPlz run)
    coverage: float | None = None  # share of the book's sentences matched to a cue
    ok: bool | None = None  # coverage is high enough for the phone to highlight
    step: str | None = None  # while queued/running: "2/5 file.mp3", "matching"
    error: str | None = None  # the last SubPlz run failed; the stored alignment is untouched
    eligible: bool = False  # a Japanese book with its linked audiobook on this machine
    can_generate: bool = False  # [alignment] subplz_cmd can run on this server
    source: str | None = None  # the map is made from a `transcript` (SubPlz gen) or a `synced` SRT
    estimated: int | None = None  # sentences whose time is estimated, not heard
    accepts: list[str] = []  # the SRT kinds an upload may be: synced, transcript
    anchors: int = 0  # listeners' fixes in the map
    speech_gaps: int | None = None  # gaps of 5 s+ the narrator talks through; None until audited
    speech_gap_s: float | None = None  # ... and their total length


class AlignmentQueueBook(BaseModel):
    book_id: str
    title: str | None = None
    author: str | None = None
    runtime_min: float | None = None
    files: int = 1


class AlignmentQueueResponse(BaseModel):
    books: list[AlignmentQueueBook]
    total_min: float
    can_generate: bool


class AlignmentIngestResponse(BaseModel):
    status: str
    audiobook: str
    source: str  # transcript | synced: what the map is made from now
    coverage: float  # heard + estimated sentences over total
    ok: bool
    matched: int  # sentences heard
    estimated: int  # sentences timed between heard ones
    total: int


class AlignmentGenerateResponse(BaseModel):
    status: str  # queued | cancelled
    alignment: AlignmentState


class BookStatusResponse(BaseModel):
    job: BookJob | None  # None: nothing queued since the server started
    analyzable: bool
    vocab_status: str | None  # ok | no_text | error | None (not analyzed yet)
    known: dict | None = None  # this profile's reading-load entry; present once analyze has run
    alignment: AlignmentState | None = None  # audio↔text sync map


class AudiobookLinkRequest(BaseModel):
    # An Audible ISBN pins the link; null means "this book has no audiobook".
    isbn: str | None = None


class ProfileCreateRequest(BaseModel):
    name: str = ""
    language: str = "ja"


class ProfileActiveRequest(BaseModel):
    slug: str = ""


class SeedRequest(BaseModel):
    top_n: int | None = None


class WordsRequest(BaseModel):
    words: list[str] = []


class ReaderEventsRequest(BaseModel):
    """The iOS reader's outbox. Events are kept as raw dicts here and checked one
    by one against ReaderEvent, so a single bad event is rejected by id instead
    of failing the batch (which the phone would then retry forever)."""
    events: list[dict[str, Any]] = []


class AnkiNoteTypeField(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    # What the sample card puts in this field; empty for a field no value is mapped to.
    sample: str = Field(default="", max_length=2000)


class AnkiNoteTypeRequest(BaseModel):
    """PUT /api/reader/anki-note-type: the note type the phone's Settings › Anki describes."""
    name: str = Field(min_length=1, max_length=100)
    fields: list[AnkiNoteTypeField] = Field(min_length=1, max_length=40)
    front: str = Field(max_length=20000)
    back: str = Field(max_length=20000)
    css: str = Field(default="", max_length=20000)


class _ReaderEvent(BaseModel):
    id: str = Field(min_length=1)
    # Unix seconds, or an ISO 8601 string (reader.parse_ts).
    ts: float | str


class StatusEvent(_ReaderEvent):
    type: Literal["status"]
    # The token's orth key (orthBase), never the surface or the lemma.
    orth: str = Field(min_length=1)
    lemma: str = ""
    reading: str = ""
    status: Literal["known", "learning", "unknown", "ignored"]
    book_id: str = ""
    sentence: str = ""


class LookupEvent(_ReaderEvent):
    type: Literal["lookup"]
    orth: str = Field(min_length=1)
    lemma: str = ""
    book_id: str = ""
    sentence: str = ""


class ProgressEvent(_ReaderEvent):
    type: Literal["progress"]
    stable_key: str = Field(min_length=1)
    book_id: str = ""
    sha256: str = ""
    cfi: str = Field(min_length=1)
    fraction: float = Field(default=0.0, ge=0.0, le=1.0)


class AudioProgressEvent(_ReaderEvent):
    """Where the listener is in an audiobook: `track` is the file of a folder of mp3s (0 for an
    m4b) and `seconds` the time into that file."""
    type: Literal["audio_progress"]
    ab_id: str = Field(min_length=1)
    track: int = Field(default=0, ge=0)
    seconds: float = Field(ge=0.0, allow_inf_nan=False)


class SyncAnchorEvent(_ReaderEvent):
    """A listener's fix of the audio sync: sentence `sent` of the pack's
    section `doc` starts `ms` into `part` (the mp3 of a folder; null for an m4b). `op: clear` withdraws
    it. Fixes are kept per book and audiobook, not per profile: the audio is the same for everyone.
    `sent`, not `sentence`: the phone's outbox already sends a lookup's `sentence` as text."""
    type: Literal["sync_anchor"]
    book_id: str = Field(min_length=1)
    ab_id: str = Field(min_length=1)
    sha256: str = ""
    doc: int = Field(ge=0)
    sent: int = Field(ge=0)
    part: str | None = None
    ms: int = Field(default=0, ge=0)
    op: Literal["set", "clear"] = "set"


ReaderEvent = Annotated[
    StatusEvent | LookupEvent | ProgressEvent | AudioProgressEvent | SyncAnchorEvent, Field(discriminator="type")]
READER_EVENT = TypeAdapter(ReaderEvent)


class RestoreWordsRequest(BaseModel):
    id: str


# ── responses ─────────────────────────────────────────────────────────────

class StatusResponse(BaseModel):
    status: str


class PairingActionResponse(BaseModel):
    status: str
    en_key: str
    jp_key: str


class DeleteBookResponse(BaseModel):
    status: str
    book_id: str
    deleted_files: list[str]
    warnings: list[str] | None = None


class DeleteAudiobookResponse(BaseModel):
    status: str
    ab_id: str
    deleted_files: list[str]
    warnings: list[str] | None = None


class SearchResponse(BaseModel):
    source: str
    ids: list[str]


class HiddenAudiobookEntry(BaseModel):
    ab_id: str
    title: str | None
    author: str | None
    series: str | None
    in_catalog: bool


class HiddenAudiobooksResponse(BaseModel):
    count: int
    hidden: list[HiddenAudiobookEntry]


class HideResponse(BaseModel):
    status: str
    hidden: int
    total: int


class UnhideResponse(BaseModel):
    status: str
    restored: int
    total: int


class AddedBookResponse(BaseModel):
    status: str
    book_id: str
    title: str
    language: str


class DuplicateExisting(BaseModel):
    book_id: str
    title: str
    reason: str


class DuplicateBookResponse(BaseModel):
    status: str  # "duplicate"
    detail: str
    existing: DuplicateExisting


class GenreListResponse(BaseModel):
    genres: list[str]


class GenreSetResponse(BaseModel):
    status: str
    genre: str | None


class TitleSetResponse(BaseModel):
    status: str
    title: str | None


class SeriesSetResponse(BaseModel):
    status: str
    series_name: str | None
    series_order: str | None


class BulkGenreResponse(BaseModel):
    status: str
    genre: str | None
    changed: int


class BulkSeriesResponse(BaseModel):
    status: str
    changed: int


class SeriesRenameResponse(BaseModel):
    status: str
    series_name: str
    renamed: int   # volumes written; pair-inherited translations follow their partner


class CatalogEntry(BaseModel):
    isbn: str
    title: str | None
    author: str | None
    series: str | None
    order: str | None
    # An audiobook file for it is on this machine (the link picker says so).
    local: bool = False


class AudiobookDetailsRequest(BaseModel):
    """Every field is optional: omitted = leave alone, null (or "") = back to
    what the catalog, the tags or the file name say."""
    title: str | None = None
    author: str | None = None
    series: str | None = None
    order: str | None = None
    narrators: str | None = None
    genre: str | None = None
    description: str | None = None
    release_date: str | None = None  # YYYY, YYYY-MM or YYYY-MM-DD


class AudiobookDetailsState(BaseModel):
    ab_id: str
    values: dict           # what the row shows now (the request's fields)
    auto: dict             # what it would show with no hand edit
    overridden: list[str]  # fields with a hand-set value
    source: str            # catalog | tags | filename (a guess) | manual
    local: bool
    path: str | None
    series_names: list[str]
    cover: dict            # {"url", "overridden", "auto_url"}


class AudiobookDetailsResponse(AudiobookDetailsState):
    status: str


class AudioUploadRequest(BaseModel):
    filename: str
    size: int              # bytes, as the browser's File.size says


class AudioUploadState(BaseModel):
    """Where an upload stands. `offset` is the next byte to send (resume from it). `state` is
    receiving (offset < size) | received (all bytes in, `complete` not yet called) | queued |
    running (`step` says what) | done (`result`) | error (`error`, `reason`)."""
    id: str
    filename: str | None = None
    size: int | None = None
    offset: int = 0
    chunk_size: int | None = None
    state: Literal["receiving", "received", "queued", "running", "done", "error"]
    step: str | None = None
    error: str | None = None
    reason: str | None = None
    # done: {status: filed|duplicate, ab_id, path, title, author, series, needs_details}
    result: dict | None = None


class AudiobookMatchRequest(BaseModel):
    asin: str              # an ASIN, or a pasted Audible link
    rename: bool = False   # also add " [ASIN]" to the file (or folder) name


class AudiobookMatchState(BaseModel):
    ab_id: str
    local: bool
    mode: str              # name | assigned (matched by hand) | tag | none
    asin: str | None
    matchable: bool
    reason: str | None     # why it can't be matched
    can_rename: bool
    query: dict            # {"title", "author"}: what to search for first
    marketplace: dict      # {"code", "label"}


class AudiobookMatchResponse(AudiobookDetailsState):
    status: str
    old_ab_id: str
    renamed: bool = False
    rename_error: str | None = None


class AudibleCandidate(BaseModel):
    asin: str
    title: str | None
    author: str | None
    narrators: str | None
    series: str | None
    order: str | None
    runtime_min: int | None
    release_date: str | None
    cover: str | None
    url: str
    local: bool        # a file here is already this book
    in_catalog: bool   # the library already has its details


class AudibleSearchResponse(BaseModel):
    marketplace: dict
    results: list[AudibleCandidate]


class AudiobookLinkState(BaseModel):
    # "auto" (fuzzy match, or none found), "manual" (pinned to an ISBN) or
    # "none" (hand-unlinked).
    mode: str
    link: dict | None
    catalog: CatalogEntry | None


class AudiobookCandidatesResponse(BaseModel):
    results: list[CatalogEntry]


class ProfileSummary(BaseModel):
    slug: str
    name: str
    language: str
    created_at: str = ""


class ProfilesListResponse(BaseModel):
    active: str
    profiles: list[ProfileSummary]


class Profile(BaseModel):
    slug: str
    name: str
    language: str
    created_at: str


class ProfileCreateResponse(BaseModel):
    status: str
    profile: Profile


class ProfileActiveResponse(BaseModel):
    status: str
    active: str


class MarkWordsResponse(BaseModel):
    status: str
    added: int
    total: int
    marked: int
    books_updated: int
    books_moved: int = 0   # books whose Known % or strain this changed


class UnmarkWordsResponse(BaseModel):
    status: str
    unmarked: int
    skipped: list[str]
    total: int
    books_updated: int
    books_moved: int = 0   # books whose Known % or strain this changed


class SkipWordsResponse(BaseModel):
    status: str
    added: int
    not_words: int
    books_updated: int
    books_moved: int = 0   # books whose Known % or strain this changed


class UnskipWordsResponse(BaseModel):
    status: str
    removed: int
    not_words: int
    books_updated: int
    books_moved: int = 0   # books whose Known % or strain this changed


class SeedResponse(BaseModel):
    status: str
    total: int
    books_updated: int
    seeded_top_n: int


class ImportWordsResponse(BaseModel):
    status: str
    imported: int
    total: int
    books_updated: int
    # The copy saved before a replace (restore it to undo); null for a merge or
    # when the list being replaced was empty.
    snapshot: str | None = None
    # How many hand-marked words a replace carried over.
    marked: int = 0


class KnownSnapshot(BaseModel):
    id: str
    created_at: str | None = None
    reason: str | None = None  # replace | clear | restore: what it was taken before
    source: str | None = None
    count: int
    marked: int


class RestoreWordsResponse(BaseModel):
    status: str
    total: int
    books_updated: int


class AdminStageInfo(BaseModel):
    id: str
    label: str
    description: str
    group: str
    chips: list[str]
    in_chain: bool
    last_run: float | None = None


class AdminStatusResponse(BaseModel):
    running: bool
    stage: str | None
    label: str | None
    started_at: float | None
    finished_at: float | None
    ok: bool | None
    error: str | None
    output: str | None
    log_file: str | None
    tail: str | None = None
    now: float | None = None


class AdminRunResponse(BaseModel):
    status: str
    stage: str


class RejectedEvent(BaseModel):
    id: str | None = None
    reason: str


class ReaderEventsResponse(BaseModel):
    status: str
    # Orth keys the server could not set to the status asked for (unknown or
    # learning on a word that came from an import); the phone reverts them.
    skipped: list[str]
    # Events that failed validation; the phone drops them rather than retry.
    rejected: list[RejectedEvent]
    # The new GET /api/reader/vocab ETag.
    etag: str
    books_moved: int = 0   # books whose Known % or strain this changed


# ── admin password (auth.py) ──────────────────────────────────────────────────

class AuthState(BaseModel):
    password_set: bool
    admin: bool


class LoginRequest(BaseModel):
    password: str


class PasswordRequest(BaseModel):
    password: str


class TokenCreateRequest(BaseModel):
    name: str = ""


class TokenInfo(BaseModel):
    id: str
    name: str
    created_at: int
    last_used: int | None = None


class TokenCreated(TokenInfo):
    token: str


# ── settings (settings.py) ────────────────────────────────────────────────────

class SettingsUpdate(BaseModel):
    values: dict[str, Any]


class CheckPathRequest(BaseModel):
    path: str
