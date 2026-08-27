-- Omero-storage-collector database schema
-- Fresh-install baseline for the collector-owned tables.
--
-- This file intentionally does NOT create:
--   * the existing legacy "imports" table
--   * PostgreSQL roles/users
--   * passwords
--   * OpenShift/Kubernetes Secrets
--
-- Apply as a database owner / PostgreSQL administrator, for example:
--   psql -U postgres -d omerofilestats -f schema.sql

BEGIN;

-- ============================================================================
-- 1. Current OMERO Fileset inventory
-- ============================================================================

CREATE TABLE public.omero_fileset (
    fileset_id bigint PRIMARY KEY,

    -- OMERO owner
    owner_id bigint NOT NULL,
    username text NOT NULL,
    firstname text,
    lastname text,

    -- OMERO group
    group_id bigint NOT NULL,
    group_name text NOT NULL,

    -- Fileset/import information
    imported_at timestamp without time zone NOT NULL,

    source_file_count integer NOT NULL DEFAULT 0,
    image_count integer NOT NULL DEFAULT 0,
    uncontained_image_count integer NOT NULL DEFAULT 0,
    total_bytes bigint NOT NULL DEFAULT 0,

    -- Current descriptive information only.
    -- These are refreshed from OMERO on each successful collector run.
    source_files jsonb NOT NULL DEFAULT '[]'::jsonb,
    locations jsonb NOT NULL DEFAULT '[]'::jsonb,

    -- Collector bookkeeping
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),

    -- Confirmed missing/deleted from OMERO
    deleted_at timestamptz,

    -- Safe deletion detection:
    -- a Fileset must be missing for multiple successful collector runs
    -- before deleted_at is set.
    missing_since_at timestamptz,
    missing_runs integer NOT NULL DEFAULT 0,

    CONSTRAINT omero_fileset_source_file_count_check
        CHECK (source_file_count >= 0),

    CONSTRAINT omero_fileset_image_count_check
        CHECK (image_count >= 0),

    CONSTRAINT omero_fileset_uncontained_image_count_check
        CHECK (
            uncontained_image_count >= 0
            AND uncontained_image_count <= image_count
        ),

    CONSTRAINT omero_fileset_total_bytes_check
        CHECK (total_bytes >= 0),

    CONSTRAINT omero_fileset_source_files_array_check
        CHECK (jsonb_typeof(source_files) = 'array'),

    CONSTRAINT omero_fileset_locations_array_check
        CHECK (jsonb_typeof(locations) = 'array'),

    CONSTRAINT omero_fileset_seen_dates_check
        CHECK (last_seen_at >= first_seen_at),

    CONSTRAINT omero_fileset_deleted_date_check
        CHECK (
            deleted_at IS NULL
            OR deleted_at >= first_seen_at
        ),

    CONSTRAINT omero_fileset_missing_runs_check
        CHECK (missing_runs >= 0)
);

CREATE INDEX idx_omero_fileset_active_group
    ON public.omero_fileset (group_id)
    WHERE deleted_at IS NULL;

CREATE INDEX idx_omero_fileset_active_owner
    ON public.omero_fileset (owner_id)
    WHERE deleted_at IS NULL;

CREATE INDEX idx_omero_fileset_active_imported
    ON public.omero_fileset (imported_at)
    WHERE deleted_at IS NULL;


-- ============================================================================
-- 2. Effective-dated storage / billing policy per OMERO group
-- ============================================================================

CREATE TABLE public.storage_policy (
    policy_id bigserial PRIMARY KEY,

    -- OMERO group
    group_id bigint NOT NULL,
    group_name text NOT NULL,

    -- TEMPORARY | AGREEMENT | CORE
    policy_type text NOT NULL,

    -- Retention period before TEMPORARY data becomes overdue.
    -- NULL is used for CORE.
    grace_days integer,

    -- Additional full calendar days between becoming overdue
    -- and becoming billable.
    billing_grace_days integer NOT NULL DEFAULT 7,

    -- Exact monetary rate in ore per decimal GB per day.
    -- Billing uses 1 GB = 1,000,000,000 bytes.
    rate_ore_per_gb_day numeric(12,4) NOT NULL DEFAULT 0,

    -- Inclusive policy validity period.
    valid_from date NOT NULL DEFAULT CURRENT_DATE,
    valid_until date,

    notes text,

    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT storage_policy_type_check
        CHECK (policy_type IN ('TEMPORARY', 'AGREEMENT', 'CORE')),

    CONSTRAINT storage_policy_grace_check
        CHECK (grace_days IS NULL OR grace_days >= 0),

    CONSTRAINT storage_policy_billing_grace_check
        CHECK (billing_grace_days >= 0),

    CONSTRAINT storage_policy_rate_check
        CHECK (rate_ore_per_gb_day >= 0),

    CONSTRAINT storage_policy_dates_check
        CHECK (
            valid_until IS NULL
            OR valid_until >= valid_from
        ),

    CONSTRAINT storage_policy_semantics_check
        CHECK (
            (
                policy_type = 'TEMPORARY'
                AND grace_days IS NOT NULL
            )
            OR
            (
                policy_type = 'AGREEMENT'
                AND grace_days = 0
            )
            OR
            (
                policy_type = 'CORE'
                AND grace_days IS NULL
                AND rate_ore_per_gb_day = 0
            )
        )
);

CREATE UNIQUE INDEX idx_storage_policy_group_start
    ON public.storage_policy (group_id, valid_from);

CREATE INDEX idx_storage_policy_group_dates
    ON public.storage_policy (group_id, valid_from, valid_until);


-- ============================================================================
-- 3. Daily historical group storage / billing snapshots
-- ============================================================================

CREATE TABLE public.group_storage_snapshot (
    snapshot_date date NOT NULL,
    group_id bigint NOT NULL,

    group_name text NOT NULL,

    -- Policy actually applied on this snapshot date.
    policy_id bigint NOT NULL,
    policy_type text NOT NULL,
    grace_days integer,
    billing_grace_days integer NOT NULL DEFAULT 0,
    rate_ore_per_gb_day numeric(12,4) NOT NULL,

    -- Total active storage
    fileset_count integer NOT NULL DEFAULT 0,
    total_bytes bigint NOT NULL DEFAULT 0,

    -- TEMPORARY Filesets past their retention period.
    overdue_fileset_count integer NOT NULL DEFAULT 0,
    overdue_bytes bigint NOT NULL DEFAULT 0,

    -- Storage actually chargeable on this calendar day.
    -- TEMPORARY: after retention + billing grace
    -- AGREEMENT: all active storage
    -- CORE: zero
    billable_fileset_count integer NOT NULL DEFAULT 0,
    billable_bytes bigint NOT NULL DEFAULT 0,

    -- Decimal GB billing:
    --   billable_bytes / 1,000,000,000 * rate_ore_per_gb_day
    -- Keep fractional ore here; round only when preparing an invoice.
    daily_charge_ore numeric(24,8)
        GENERATED ALWAYS AS (
            billable_bytes::numeric
            * rate_ore_per_gb_day
            / 1000000000::numeric
        ) STORED,

    collected_at timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (snapshot_date, group_id),

    CONSTRAINT group_storage_snapshot_policy_fk
        FOREIGN KEY (policy_id)
        REFERENCES public.storage_policy(policy_id),

    CONSTRAINT group_storage_snapshot_policy_type_check
        CHECK (policy_type IN ('TEMPORARY', 'AGREEMENT', 'CORE')),

    CONSTRAINT group_storage_snapshot_billing_grace_check
        CHECK (billing_grace_days >= 0),

    CONSTRAINT group_storage_snapshot_fileset_count_check
        CHECK (fileset_count >= 0),

    CONSTRAINT group_storage_snapshot_total_bytes_check
        CHECK (total_bytes >= 0),

    CONSTRAINT group_storage_snapshot_overdue_count_check
        CHECK (
            overdue_fileset_count >= 0
            AND overdue_fileset_count <= fileset_count
        ),

    CONSTRAINT group_storage_snapshot_overdue_bytes_check
        CHECK (
            overdue_bytes >= 0
            AND overdue_bytes <= total_bytes
        ),

    CONSTRAINT group_storage_snapshot_billable_count_check
        CHECK (
            billable_fileset_count >= 0
            AND billable_fileset_count <= fileset_count
        ),

    CONSTRAINT group_storage_snapshot_billable_bytes_check
        CHECK (
            billable_bytes >= 0
            AND billable_bytes <= total_bytes
        ),

    CONSTRAINT group_storage_snapshot_rate_check
        CHECK (rate_ore_per_gb_day >= 0)
);

CREATE INDEX idx_group_storage_snapshot_group_date
    ON public.group_storage_snapshot (group_id, snapshot_date DESC);

CREATE INDEX idx_group_storage_snapshot_date
    ON public.group_storage_snapshot (snapshot_date DESC);


-- ============================================================================
-- 4. Collector execution audit / health history
-- ============================================================================

CREATE TABLE public.collector_run (
    run_id bigserial PRIMARY KEY,

    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,

    status text NOT NULL DEFAULT 'RUNNING',

    -- What the collector saw in OMERO
    filesets_seen integer NOT NULL DEFAULT 0,
    total_bytes_seen bigint NOT NULL DEFAULT 0,

    -- Changes applied to omero_fileset
    filesets_inserted integer NOT NULL DEFAULT 0,
    filesets_updated integer NOT NULL DEFAULT 0,
    filesets_marked_deleted integer NOT NULL DEFAULT 0,

    -- Daily history
    snapshots_written integer NOT NULL DEFAULT 0,

    -- True when missing/deletion processing was intentionally skipped
    -- because the OMERO source inventory looked suspiciously incomplete.
    deletion_suppressed boolean NOT NULL DEFAULT false,

    error_message text,

    CONSTRAINT collector_run_status_check
        CHECK (status IN ('RUNNING', 'SUCCESS', 'FAILED', 'PARTIAL')),

    CONSTRAINT collector_run_filesets_seen_check
        CHECK (filesets_seen >= 0),

    CONSTRAINT collector_run_total_bytes_check
        CHECK (total_bytes_seen >= 0),

    CONSTRAINT collector_run_inserted_check
        CHECK (filesets_inserted >= 0),

    CONSTRAINT collector_run_updated_check
        CHECK (filesets_updated >= 0),

    CONSTRAINT collector_run_deleted_check
        CHECK (filesets_marked_deleted >= 0),

    CONSTRAINT collector_run_snapshots_check
        CHECK (snapshots_written >= 0),

    CONSTRAINT collector_run_dates_check
        CHECK (
            finished_at IS NULL
            OR finished_at >= started_at
        )
);

CREATE INDEX idx_collector_run_started
    ON public.collector_run (started_at DESC);

CREATE INDEX idx_collector_run_status
    ON public.collector_run (status, started_at DESC);

COMMIT;
