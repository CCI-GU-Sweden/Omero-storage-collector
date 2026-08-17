import json
import os
import sys

import psycopg
from psycopg.rows import dict_row


OMERO_HOST = os.getenv("OMERO_DB_HOST", "omero-postgres-server")
OMERO_PORT = int(os.getenv("OMERO_DB_PORT", "5432"))
OMERO_DB = os.getenv("OMERO_DB_NAME", "omerodb")

STATS_HOST = os.getenv("STATS_DB_HOST", "filestatistics-pg15")
STATS_PORT = int(os.getenv("STATS_DB_PORT", "5432"))
STATS_DB = os.getenv("STATS_DB_NAME", "omerofilestats")


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


def main():
    print("OMERO storage collector - DRY RUN")
    print()

    #
    # Source connection
    #
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

    #
    # Destination connection
    #
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

    #
    # Dry-run report
    #
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

    for row in rows:
        print(
            f"Fileset {row['fileset_id']}: "
            f"{row['username']} / "
            f"{row['group_name']} / "
            f"{row['total_bytes'] / 1_000_000:.1f} MB / "
            f"{row['source_file_count']} source file(s) / "
            f"{row['image_count']} image(s) / "
            f"{row['uncontained_image_count']} uncontained"
        )

        print(
            "  source_files:",
            json.dumps(
                row["source_files"],
                ensure_ascii=False,
                default=str,
            ),
        )

        print(
            "  locations:",
            json.dumps(
                row["locations"],
                ensure_ascii=False,
                default=str,
            ),
        )

    print()
    print("DRY RUN COMPLETE - no database rows were written.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)