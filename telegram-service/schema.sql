CREATE TABLE IF NOT EXISTS chats (
    chat_id TEXT PRIMARY KEY,
    chat_type TEXT NOT NULL DEFAULT 'legacy',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    cards_glb INTEGER NOT NULL DEFAULT 1 CHECK (cards_glb IN (0, 1)),
    cards_jp INTEGER NOT NULL DEFAULT 1 CHECK (cards_jp IN (0, 1)),
    summons_glb INTEGER NOT NULL DEFAULT 1 CHECK (summons_glb IN (0, 1)),
    summons_jp INTEGER NOT NULL DEFAULT 1 CHECK (summons_jp IN (0, 1)),
    events_glb INTEGER NOT NULL DEFAULT 1 CHECK (events_glb IN (0, 1)),
    events_jp INTEGER NOT NULL DEFAULT 1 CHECK (events_jp IN (0, 1)),
    news_glb INTEGER NOT NULL DEFAULT 1 CHECK (news_glb IN (0, 1)),
    news_jp INTEGER NOT NULL DEFAULT 1 CHECK (news_jp IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_chats_active ON chats(active);
