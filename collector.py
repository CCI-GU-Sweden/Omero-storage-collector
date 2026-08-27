import os
import sys

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


OMERO_HOST = os.getenv("OMERO_DB_HOST", "omero-postgres-server")
OMERO_PORT = int(os.getenv("OMERO_DB_PORT", "5432"))
OMERO_DB = os.getenv("OMERO_DB_NAME", "omerodb")

STATS_HOST = os.getenv("STATS_DB_HOST", "filestatistics-pg15")
STATS_PORT = int(os.getenv("STATS_DB_PORT", "5432"))
STATS_DB = os.getenv("STATS_DB_NAME", "omerofilestats")

DRY_RUN = os.getenv("DRY_RUN", "true").strip().lower() not in {
    "false",
    "0",
    "no",
}

DELETION_CONFIRM_RUNS = int(
    os.getenv("DELETION_CONFIRM_RUNS", "2")
)

DELETION_MIN_SOURCE_RATIO = float(
    os.getenv("DELETION_MIN_SOURCE_RATIO", "0.80")
)

FORCE_LARGE_DELETION = (
    os.getenv("FORCE_LARGE_DELETION", "false")
    .strip()
    .lower()
    in {"true", "1", "yes"}
)

def required_env(name: str) -> str:
    value = os.getenv(name)

    if not value:
        raise RuntimeError(f"Required environment variable is missing: {name}")

    return value


OMERO_USER = required_env("OMERO_DB_USER")
OMERO_PASSWORD = required_env("OMERO_DB_PASSWORD")

STATS_USER = required_env("STATS_DB_USER")
STATS_PASSWORD = required_env("STATS_DB_PASSWORD")


EXTRACT_SQL = """
WITH file_members AS (
    SELECT
        fse.fileset AS fileset_id,
        fse.originalfile AS originalfile_id,
        MIN(fse.clientpath) AS client_path,
        MAX(ofile.name) AS original_name,
        MAX(ofile.size) AS size_bytes

    FROM filesetentry fse

    JOIN originalfile ofile
        ON ofile.id = fse.originalfile

    WHERE fse.originalfile IS NOT NULL

    GROUP BY
        fse.fileset,
        fse.originalfile
),

file_agg AS (
    SELECT
        fileset_id,

        COUNT(*) AS source_file_count,

        COALESCE(
            SUM(size_bytes),
            0
        ) AS total_bytes,

        jsonb_agg(
            jsonb_build_object(
                'name', original_name,
                'client_path', client_path
            )
            ORDER BY originalfile_id
        ) AS source_files

    FROM file_members

    GROUP BY fileset_id
),

image_agg AS (
    SELECT
        i.fileset AS fileset_id,

        COUNT(*) AS image_count,

        COUNT(*) FILTER (
            WHERE NOT EXISTS (
                SELECT 1
                FROM datasetimagelink dil
                WHERE dil.child = i.id
            )
        ) AS uncontained_image_count

    FROM image i

    WHERE i.fileset IS NOT NULL

    GROUP BY i.fileset
),

fileset_dataset AS (
    SELECT DISTINCT
        i.fileset AS fileset_id,
        d.id AS dataset_id,
        d.name AS dataset_name

    FROM image i

    JOIN datasetimagelink dil
        ON dil.child = i.id

    JOIN dataset d
        ON d.id = dil.parent

    WHERE i.fileset IS NOT NULL
),

location_rows AS (
    SELECT DISTINCT
        fd.fileset_id,

        p.id AS project_id,
        p.name AS project_name,

        fd.dataset_id,
        fd.dataset_name

    FROM fileset_dataset fd

    LEFT JOIN projectdatasetlink pdl
        ON pdl.child = fd.dataset_id

    LEFT JOIN project p
        ON p.id = pdl.parent
),

location_agg AS (
    SELECT
        fileset_id,

        jsonb_agg(
            jsonb_build_object(
                'project_id', project_id,
                'project_name', project_name,
                'dataset_id', dataset_id,
                'dataset_name', dataset_name
            )
            ORDER BY
                project_id NULLS LAST,
                dataset_id
        ) AS locations

    FROM location_rows

    GROUP BY fileset_id
)

SELECT
    fs.id AS fileset_id,

    fs.owner_id,
    e.omename AS username,
    e.firstname,
    e.lastname,

    fs.group_id,
    eg.name AS group_name,

    ev.time AS imported_at,

    COALESCE(fa.source_file_count, 0) AS source_file_count,
    COALESCE(ia.image_count, 0) AS image_count,
    COALESCE(ia.uncontained_image_count, 0)
        AS uncontained_image_count,

    COALESCE(fa.total_bytes, 0) AS total_bytes,

    COALESCE(
        fa.source_files,
        '[]'::jsonb
    ) AS source_files,

    COALESCE(
        la.locations,
        '[]'::jsonb
    ) AS locations

FROM fileset fs

LEFT JOIN experimenter e
    ON e.id = fs.owner_id

LEFT JOIN experimentergroup eg
    ON eg.id = fs.group_id

LEFT JOIN event ev
    ON ev.id = fs.creation_id

LEFT JOIN file_agg fa
    ON fa.fileset_id = fs.id

LEFT JOIN image_agg ia
    ON ia.fileset_id = fs.id

LEFT JOIN location_agg la
    ON la.fileset_id = fs.id
   
ORDER BY fs.id;
"""

UPSERT_FILESET_SQL = """
INSERT INTO public.omero_fileset (
    fileset_id,

    owner_id,
    username,
    firstname,
    lastname,

    group_id,
    group_name,

    imported_at,

    source_file_count,
    image_count,
    uncontained_image_count,
    total_bytes,

    source_files,
    locations,

    first_seen_at,
    last_seen_at,
    deleted_at
)
VALUES (
    %(fileset_id)s,

    %(owner_id)s,
    %(username)s,
    %(firstname)s,
    %(lastname)s,

    %(group_id)s,
    %(group_name)s,

    %(imported_at)s,

    %(source_file_count)s,
    %(image_count)s,
    %(uncontained_image_count)s,
    %(total_bytes)s,

    %(source_files)s,
    %(locations)s,

    now(),
    now(),
    NULL
)
ON CONFLICT (fileset_id)
DO UPDATE SET
    owner_id = EXCLUDED.owner_id,
    username = EXCLUDED.username,
    firstname = EXCLUDED.firstname,
    lastname = EXCLUDED.lastname,

    group_id = EXCLUDED.group_id,
    group_name = EXCLUDED.group_name,

    imported_at = EXCLUDED.imported_at,

    source_file_count = EXCLUDED.source_file_count,
    image_count = EXCLUDED.image_count,
    uncontained_image_count = EXCLUDED.uncontained_image_count,
    total_bytes = EXCLUDED.total_bytes,

    source_files = EXCLUDED.source_files,
    locations = EXCLUDED.locations,

    last_seen_at = now(),
    missing_runs = 0,
    deleted_at = NULL;
"""

ENSURE_DEFAULT_POLICY_SQL = """
INSERT INTO public.storage_policy (
    group_id,
    group_name,
    policy_type,
    grace_days,
    rate_ore_per_gb_day,
    valid_from
)
SELECT
    %(group_id)s,
    %(group_name)s,
    'TEMPORARY',
    28,
    5,
    %(valid_from)s
WHERE NOT EXISTS (
    SELECT 1
    FROM public.storage_policy sp
    WHERE sp.group_id = %(group_id)s
)
ON CONFLICT (group_id, valid_from)
DO NOTHING
RETURNING policy_id;
"""

POLICY_VALIDATION_SQL = """
WITH params AS (
    SELECT
        (CURRENT_TIMESTAMP AT TIME ZONE 'Europe/Stockholm')::date
            AS snapshot_date
),

active_groups AS (
    SELECT DISTINCT
        group_id,
        group_name
    FROM public.omero_fileset
    WHERE deleted_at IS NULL
)

SELECT
    ag.group_id,
    ag.group_name,
    COUNT(sp.policy_id) AS active_policy_count

FROM active_groups ag

CROSS JOIN params p

LEFT JOIN public.storage_policy sp
    ON sp.group_id = ag.group_id
   AND sp.valid_from <= p.snapshot_date
   AND (
       sp.valid_until IS NULL
       OR sp.valid_until >= p.snapshot_date
   )

GROUP BY
    ag.group_id,
    ag.group_name

HAVING COUNT(sp.policy_id) <> 1

ORDER BY ag.group_id;
"""

SNAPSHOT_SQL = """
WITH params AS (
    SELECT
        (CURRENT_TIMESTAMP AT TIME ZONE 'Europe/Stockholm')::date
            AS snapshot_date
),

active_policy AS (
    SELECT
        sp.*,
        p.snapshot_date

    FROM public.storage_policy sp

    CROSS JOIN params p

    WHERE sp.valid_from <= p.snapshot_date
      AND (
          sp.valid_until IS NULL
          OR sp.valid_until >= p.snapshot_date
      )
),

calculated AS (
    SELECT
        ap.snapshot_date,

        fs.fileset_id,
        fs.group_id,
        fs.group_name,
        fs.total_bytes,

        ap.policy_id,
        ap.policy_type,
        ap.grace_days,
        ap.billing_grace_days,
        ap.rate_ore_per_gb_day,

        -- TEMPORARY becomes overdue on the calendar day
        -- after the retention period.
        CASE
            WHEN ap.policy_type = 'TEMPORARY'
             AND ap.snapshot_date >=
                 fs.imported_at::date
                 + ap.grace_days
                 + 1
            THEN true
            ELSE false
        END AS is_overdue,

        -- AGREEMENT is billable immediately.
        --
        -- TEMPORARY gets:
        --   retention grace
        --   + warning/billing grace
        -- before billing starts.
        CASE
            WHEN ap.policy_type = 'AGREEMENT'
                THEN true

            WHEN ap.policy_type = 'TEMPORARY'
             AND ap.snapshot_date >=
                 fs.imported_at::date
                 + ap.grace_days
                 + ap.billing_grace_days
                 + 1
            THEN true

            ELSE false
        END AS is_billable

    FROM public.omero_fileset fs

    JOIN active_policy ap
        ON ap.group_id = fs.group_id

    WHERE fs.deleted_at IS NULL
),

grouped AS (
    SELECT
        snapshot_date,
        group_id,

        MAX(group_name) AS group_name,

        policy_id,
        policy_type,
        grace_days,
        billing_grace_days,
        rate_ore_per_gb_day,

        COUNT(*) AS fileset_count,
        SUM(total_bytes) AS total_bytes,

        COUNT(*) FILTER (
            WHERE is_overdue
        ) AS overdue_fileset_count,

        COALESCE(
            SUM(total_bytes) FILTER (
                WHERE is_overdue
            ),
            0
        ) AS overdue_bytes,

        COUNT(*) FILTER (
            WHERE is_billable
        ) AS billable_fileset_count,

        COALESCE(
            SUM(total_bytes) FILTER (
                WHERE is_billable
            ),
            0
        ) AS billable_bytes

    FROM calculated

    GROUP BY
        snapshot_date,
        group_id,
        policy_id,
        policy_type,
        grace_days,
        billing_grace_days,
        rate_ore_per_gb_day
)

INSERT INTO public.group_storage_snapshot (
    snapshot_date,
    group_id,
    group_name,

    policy_id,
    policy_type,
    grace_days,
    billing_grace_days,
    rate_ore_per_gb_day,

    fileset_count,
    total_bytes,

    overdue_fileset_count,
    overdue_bytes,

    billable_fileset_count,
    billable_bytes,

    collected_at
)

SELECT
    snapshot_date,
    group_id,
    group_name,

    policy_id,
    policy_type,
    grace_days,
    billing_grace_days,
    rate_ore_per_gb_day,

    fileset_count,
    total_bytes,

    overdue_fileset_count,
    overdue_bytes,

    billable_fileset_count,
    billable_bytes,

    now()

FROM grouped

ON CONFLICT (snapshot_date, group_id)
DO UPDATE SET
    group_name = EXCLUDED.group_name,

    policy_id = EXCLUDED.policy_id,
    policy_type = EXCLUDED.policy_type,
    grace_days = EXCLUDED.grace_days,
    billing_grace_days =
        EXCLUDED.billing_grace_days,
    rate_ore_per_gb_day =
        EXCLUDED.rate_ore_per_gb_day,

    fileset_count = EXCLUDED.fileset_count,
    total_bytes = EXCLUDED.total_bytes,

    overdue_fileset_count =
        EXCLUDED.overdue_fileset_count,
    overdue_bytes =
        EXCLUDED.overdue_bytes,

    billable_fileset_count =
        EXCLUDED.billable_fileset_count,
    billable_bytes =
        EXCLUDED.billable_bytes,

    collected_at = now()

RETURNING
    snapshot_date,
    group_id;
"""

MARK_MISSING_SQL = """
WITH seen AS (
    SELECT unnest(%(seen_ids)s::bigint[]) AS fileset_id
)

UPDATE public.omero_fileset fs

SET
    missing_since_at = COALESCE(
        fs.missing_since_at,
        now()
    ),
    missing_runs = fs.missing_runs + 1

WHERE fs.deleted_at IS NULL
  AND NOT EXISTS (
      SELECT 1
      FROM seen s
      WHERE s.fileset_id = fs.fileset_id
  )

RETURNING
    fs.fileset_id,
    fs.missing_runs;
"""

CONFIRM_DELETED_SQL = """
UPDATE public.omero_fileset

SET deleted_at = now()

WHERE deleted_at IS NULL
  AND missing_runs >= %(confirm_runs)s

RETURNING fileset_id;
"""

CREATE_RUN_SQL = """
INSERT INTO public.collector_run (
    status
)
VALUES (
    'RUNNING'
)
RETURNING run_id;
"""

FINISH_RUN_SQL = """
UPDATE public.collector_run
SET
    finished_at = now(),
    status = %(status)s,

    filesets_seen = %(filesets_seen)s,
    total_bytes_seen = %(total_bytes_seen)s,

    filesets_inserted = %(filesets_inserted)s,
    filesets_updated = %(filesets_updated)s,
    filesets_marked_deleted = %(filesets_marked_deleted)s,

    snapshots_written = %(snapshots_written)s,
    deletion_suppressed = %(deletion_suppressed)s,

    error_message = %(error_message)s

WHERE run_id = %(run_id)s;
"""


FAIL_RUN_SQL = """
UPDATE public.collector_run
SET
    finished_at = now(),
    status = 'FAILED',

    filesets_seen = %(filesets_seen)s,
    total_bytes_seen = %(total_bytes_seen)s,

    error_message = %(error_message)s

WHERE run_id = %(run_id)s;
"""

def connect_omero():
    return psycopg.connect(
        host=OMERO_HOST,
        port=OMERO_PORT,
        dbname=OMERO_DB,
        user=OMERO_USER,
        password=OMERO_PASSWORD,
        connect_timeout=10,
        application_name="omero-storage-collector",
        row_factory=dict_row,
    )


def connect_stats():
    return psycopg.connect(
        host=STATS_HOST,
        port=STATS_PORT,
        dbname=STATS_DB,
        user=STATS_USER,
        password=STATS_PASSWORD,
        connect_timeout=10,
        application_name="omero-storage-collector",
        row_factory=dict_row,
    )


def validate_rows(rows):
    if not rows:
        raise RuntimeError(
            "OMERO returned zero Filesets. "
            "Refusing to continue."
        )

    fileset_ids = [row["fileset_id"] for row in rows]

    if len(fileset_ids) != len(set(fileset_ids)):
        raise RuntimeError(
            "Extraction returned duplicate Fileset IDs."
        )

    for row in rows:
        if row["total_bytes"] < 0:
            raise RuntimeError(
                f"Fileset {row['fileset_id']} has negative size."
            )

        if row["source_file_count"] < 0:
            raise RuntimeError(
                f"Fileset {row['fileset_id']} has invalid source file count."
            )

        if row["image_count"] < 0:
            raise RuntimeError(
                f"Fileset {row['fileset_id']} has invalid image count."
            )

        if (
            row["uncontained_image_count"]
            > row["image_count"]
        ):
            raise RuntimeError(
                f"Fileset {row['fileset_id']} has invalid "
                "uncontained image count."
            )

        if not isinstance(row["source_files"], list):
            raise RuntimeError(
                f"Fileset {row['fileset_id']} source_files "
                "is not a JSON array."
            )

        if not isinstance(row["locations"], list):
            raise RuntimeError(
                f"Fileset {row['fileset_id']} locations "
                "is not a JSON array."
            )


def upsert_filesets(conn, rows):
    records = []

    for row in rows:
        record = dict(row)

        record["source_files"] = Jsonb(
            row["source_files"]
        )

        record["locations"] = Jsonb(
            row["locations"]
        )

        records.append(record)

    with conn.cursor() as cur:
        cur.executemany(
            UPSERT_FILESET_SQL,
            records,
        )
        

def ensure_default_policies(conn, rows):
    groups = {}

    for row in rows:
        group_id = row["group_id"]
        imported_date = row["imported_at"].date()

        if group_id not in groups:
            groups[group_id] = {
                "group_name": row["group_name"],
                "valid_from": imported_date,
            }
        else:
            groups[group_id]["group_name"] = row["group_name"]

            if imported_date < groups[group_id]["valid_from"]:
                groups[group_id]["valid_from"] = imported_date

    created = 0

    with conn.cursor() as cur:
        for group_id, info in groups.items():
            cur.execute(
                ENSURE_DEFAULT_POLICY_SQL,
                {
                    "group_id": group_id,
                    "group_name": info["group_name"],
                    "valid_from": info["valid_from"],
                },
            )

            if cur.fetchone() is not None:
                created += 1

    return created


def validate_active_policies(conn):
    invalid = conn.execute(
        POLICY_VALIDATION_SQL
    ).fetchall()

    if invalid:
        details = ", ".join(
            f"group {row['group_id']} "
            f"({row['group_name']}): "
            f"{row['active_policy_count']} active policies"
            for row in invalid
        )

        raise RuntimeError(
            f"Invalid storage policy configuration: {details}"
        )


def write_snapshots(conn):
    rows = conn.execute(
        SNAPSHOT_SQL
    ).fetchall()

    return len(rows)

def process_missing_filesets(
    conn,
    rows,
    previous_active_count,
):
    seen_ids = [
        row["fileset_id"]
        for row in rows
    ]

    seen_count = len(seen_ids)


    # Initial population: nothing exists yet, therefore there can be nothing missing.

    if previous_active_count == 0:
        return {
            "missing": 0,
            "deleted": 0,
            "suppressed": False,
        }

    source_ratio = (
        seen_count / previous_active_count
    )

    large_drop = (
        source_ratio
        < DELETION_MIN_SOURCE_RATIO
    )

    if large_drop and not FORCE_LARGE_DELETION:
        print()
        print(
            "WARNING: deletion detection suppressed."
        )
        print(
            f"Previous active Filesets: "
            f"{previous_active_count}"
        )
        print(
            f"Current OMERO Filesets: "
            f"{seen_count}"
        )
        print(
            f"Source ratio: "
            f"{source_ratio:.3f}"
        )
        print(
            "This is below the configured "
            f"minimum of "
            f"{DELETION_MIN_SOURCE_RATIO:.3f}."
        )

        return {
            "missing": 0,
            "deleted": 0,
            "suppressed": True,
        }

    missing_rows = conn.execute(
        MARK_MISSING_SQL,
        {
            "seen_ids": seen_ids,
        },
    ).fetchall()

    deleted_rows = conn.execute(
        CONFIRM_DELETED_SQL,
        {
            "confirm_runs":
                DELETION_CONFIRM_RUNS,
        },
    ).fetchall()

    return {
        "missing": len(missing_rows),
        "deleted": len(deleted_rows),
        "suppressed": False,
    }

def create_collector_run():
    with connect_stats() as conn:
        row = conn.execute(
            CREATE_RUN_SQL
        ).fetchone()

        conn.commit()

        return row["run_id"]


def finish_collector_run(
    conn,
    run_id,
    *,
    status,
    filesets_seen,
    total_bytes_seen,
    filesets_inserted,
    filesets_updated,
    filesets_marked_deleted,
    snapshots_written,
    deletion_suppressed,
    error_message=None,
):
    conn.execute(
        FINISH_RUN_SQL,
        {
            "run_id": run_id,
            "status": status,
            "filesets_seen": filesets_seen,
            "total_bytes_seen": total_bytes_seen,
            "filesets_inserted": filesets_inserted,
            "filesets_updated": filesets_updated,
            "filesets_marked_deleted":
                filesets_marked_deleted,
            "snapshots_written": snapshots_written,
            "deletion_suppressed":
                deletion_suppressed,
            "error_message": error_message,
        },
    )


def fail_collector_run(
    run_id,
    *,
    filesets_seen,
    total_bytes_seen,
    error_message,
):
    if run_id is None:
        return

    try:
        with connect_stats() as conn:
            conn.execute(
                FAIL_RUN_SQL,
                {
                    "run_id": run_id,
                    "filesets_seen": filesets_seen,
                    "total_bytes_seen":
                        total_bytes_seen,
                    "error_message":
                        str(error_message),
                },
            )

            conn.commit()

    except Exception as log_error:
        print(
            "WARNING: Could not update "
            f"collector_run {run_id}: "
            f"{log_error}",
            file=sys.stderr,
        )

def count_inserted_updated(conn, rows):
    seen_ids = [
        row["fileset_id"]
        for row in rows
    ]

    existing = conn.execute(
        """
        SELECT fileset_id
        FROM public.omero_fileset
        WHERE fileset_id =
            ANY(%(ids)s::bigint[])
        """,
        {
            "ids": seen_ids,
        },
    ).fetchall()

    existing_ids = {
        row["fileset_id"]
        for row in existing
    }

    inserted = sum(
        1
        for fileset_id in seen_ids
        if fileset_id not in existing_ids
    )

    updated = len(seen_ids) - inserted

    return inserted, updated


def main():
    mode = "DRY RUN" if DRY_RUN else "WRITE MODE"
    print(f"OMERO storage collector - {mode}")
    print()

    run_id = None
    filesets_seen = 0
    total_bytes_seen = 0

    try:
        if not DRY_RUN:
            run_id = create_collector_run()

            print(
                f"Collector run ID: {run_id}"
            )

        # Source connection
        with connect_omero() as conn:
            source_info = conn.execute(
                """
                SELECT
                    current_database() AS database_name,
                    current_user AS user_name,
                    current_setting(
                        'default_transaction_read_only'
                    ) AS read_only
                """
            ).fetchone()

            print(
                "OMERO connection:",
                source_info["database_name"],
                source_info["user_name"],
                f"read_only={source_info['read_only']}",
            )

            if source_info["user_name"] != "omero_stats_reader":
                raise RuntimeError(
                    "Unexpected OMERO database user."
                )

            if source_info["read_only"] != "on":
                raise RuntimeError(
                    "OMERO connection is not read-only."
                )

            rows = conn.execute(EXTRACT_SQL).fetchall()

        validate_rows(rows)

        filesets_seen = len(rows)

        total_bytes_seen = sum(
            row["total_bytes"]
            for row in rows
        )

        # Destination connection
        with connect_stats() as conn:
            destination_info = conn.execute(
                """
                SELECT
                    current_database() AS database_name,
                    current_user AS user_name,
                    (
                        SELECT count(*)
                        FROM public.omero_fileset
                    ) AS existing_filesets
                """
            ).fetchone()

            print(
                "Statistics connection:",
                destination_info["database_name"],
                destination_info["user_name"],
                f"existing_filesets="
                f"{destination_info['existing_filesets']}",
            )

            if destination_info["user_name"] != "filestats_collector":
                raise RuntimeError(
                    "Unexpected statistics database user."
                )

            if not DRY_RUN:
                print()
                print("WRITE MODE ENABLED")

                previous_active_count = conn.execute(
                    """
                    SELECT count(*)
                    FROM public.omero_fileset
                    WHERE deleted_at IS NULL
                    """
                ).fetchone()["count"]

                print(
                    f"Upserting {len(rows)} Filesets "
                    "into omero_fileset..."
                )

                filesets_inserted, filesets_updated = (
                    count_inserted_updated(
                        conn,
                        rows,
                    )
                )

                upsert_filesets(conn, rows)

                policies_created = ensure_default_policies(
                    conn,
                    rows,
                )

                deletion_result = process_missing_filesets(
                    conn,
                    rows,
                    previous_active_count,
                )

                validate_active_policies(conn)

                snapshots_written = write_snapshots(conn)

                if deletion_result["suppressed"]:
                    run_status = "PARTIAL"

                    run_message = (
                        "Deletion detection suppressed because "
                        "the OMERO source inventory was below "
                        "the configured safety threshold."
                    )
                else:
                    run_status = "SUCCESS"
                    run_message = None


                stored_count = conn.execute(
                    """
                    SELECT count(*)
                    FROM public.omero_fileset
                    WHERE deleted_at IS NULL
                    """
                ).fetchone()["count"]

                finish_collector_run(
                    conn,
                    run_id,

                    status=run_status,

                    filesets_seen=filesets_seen,
                    total_bytes_seen=total_bytes_seen,

                    filesets_inserted=filesets_inserted,
                    filesets_updated=filesets_updated,

                    filesets_marked_deleted=
                        deletion_result["deleted"],

                    snapshots_written=snapshots_written,

                    deletion_suppressed=
                        deletion_result["suppressed"],

                    error_message=run_message,
                )

                conn.commit()

                print(
                    f"Default policies created: "
                    f"{policies_created}"
                )

                print(
                    f"Active Filesets after UPSERT: "
                    f"{stored_count}"
                )

                print(
                    f"Storage snapshots written: "
                    f"{snapshots_written}"
                )

                print(
                    f"Filesets currently missing: "
                    f"{deletion_result['missing']}"
                )

                print(
                    f"Filesets marked deleted: "
                    f"{deletion_result['deleted']}"
                )

                print(
                    f"Deletion suppressed: "
                    f"{deletion_result['suppressed']}"
                )

        # Report
        total_bytes = sum(
            row["total_bytes"]
            for row in rows
        )

        print()
        print("Extraction successful")
        print("---------------------")
        print(f"Filesets: {len(rows)}")
        print(f"Total bytes: {total_bytes:,}")
        print(f"Total decimal GB: {total_bytes / 1_000_000_000:.3f}")
        print()

        print()
        if DRY_RUN:
            print("DRY RUN COMPLETE - no database rows were written.")
        else:
            print("WRITE RUN COMPLETE.")

    except Exception as exc:
        fail_collector_run(
            run_id,

            filesets_seen=filesets_seen,
            total_bytes_seen=total_bytes_seen,

            error_message=exc,
        )

        raise

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)