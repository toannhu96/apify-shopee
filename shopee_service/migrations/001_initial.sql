CREATE TABLE IF NOT EXISTS accounts (
    alias text PRIMARY KEY,
    version bigint NOT NULL DEFAULT 1,
    status text NOT NULL CHECK (status IN ('pending','healthy','authentication','verification','rate_limit','disabled')),
    credentials text NOT NULL,
    next_request_at timestamptz NOT NULL DEFAULT now(),
    lease_token text,
    lease_until timestamptz,
    incident bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS runs (
    run_id text PRIMARY KEY,
    product_ids jsonb NOT NULL,
    deadline_at timestamptz NOT NULL,
    status text NOT NULL DEFAULT 'active',
    next_sequence bigint NOT NULL DEFAULT 0,
    last_dispatched_at timestamptz NOT NULL DEFAULT '1970-01-01T00:00:00Z',
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);
CREATE TABLE IF NOT EXISTS run_items (
    run_id text NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    item_id text NOT NULL,
    position integer NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    sequence bigint,
    result jsonb,
    error_code text,
    PRIMARY KEY (run_id,item_id),
    UNIQUE (run_id,sequence)
);
CREATE INDEX IF NOT EXISTS pending_items ON run_items(item_id) WHERE status='pending';
CREATE TABLE IF NOT EXISTS jobs (
    item_id text PRIMARY KEY,
    status text NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    account_alias text,
    account_version bigint,
    lease_token text,
    lease_until timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS product_cache (
    item_id text PRIMARY KEY,
    account_alias text NOT NULL REFERENCES accounts(alias),
    account_version bigint NOT NULL,
    result jsonb NOT NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS deliveries (
    run_id text NOT NULL,
    item_id text NOT NULL,
    result_id text NOT NULL,
    phase text NOT NULL CHECK (phase IN ('delivering','delivered','charging','charged','uncertain')),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id,item_id),
    FOREIGN KEY (run_id,item_id) REFERENCES run_items(run_id,item_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS alert_outbox (
    id bigserial PRIMARY KEY,
    dedupe_key text NOT NULL UNIQUE,
    payload jsonb NOT NULL,
    attempts integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_token text,
    lease_until timestamptz,
    delivered_at timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now()
);
