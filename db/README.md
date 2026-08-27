# OMERO Storage Collector

Daily storage accounting and retention/billing collector for OMERO.

Repository:

`https://github.com/CCI-GU-Sweden/Omero-storage-collector`

The collector reads OMERO's PostgreSQL database using a dedicated **read-only** role and writes derived Fileset-level statistics to a separate PostgreSQL database.

## Architecture

```text
OMERO PostgreSQL
    omerodb
      |
      | SELECT only
      v
omero_stats_reader
      |
      v
OMERO Storage Collector
      |
      | SELECT / INSERT / UPDATE
      v
omerofilestats PostgreSQL
    ├── omero_fileset
    ├── storage_policy
    ├── group_storage_snapshot
    └── collector_run
```

The collector does **not** modify OMERO.

OMERO is the source of truth for the current Fileset inventory. All retention, billing, snapshot and collector state is stored in `omerofilestats`.

**NOTE**  
The existing legacy `imports` table belongs to the older filestatistics application and is not created or managed by this project.

---

## Current policy rules

### TEMPORARY

Default policy for groups without an explicitly configured policy.

- Retention period: **28 full days**
- Warning / billing grace: **7 full calendar days**
- Billing starts after both periods have elapsed
- Rate: **5 öre / GB / day**
- Users may be warned while overdue but still inside the 7-day billing grace period

Example for data imported on August 1:

```text
Aug 1–29      retention period
Aug 30–Sep 5  overdue / warning period, not billed
Sep 6 onward  billable
```

### AGREEMENT

- Storage is billable according to the configured daily rate
- Default rate currently used: **5 öre / GB / day**
- No retention warning grace is required
- Billing is based on the amount of data present on each calendar day

### CORE

- Included in storage statistics
- Excluded from retention-warning emails
- Excluded from billing

### Billing unit

Billing uses decimal GB:

```text
1 GB = 1,000,000,000 bytes
```

Daily calculations use the `Europe/Stockholm` calendar date.

No fractional-day billing is used.

---

# Fresh deployment

## 1. Prerequisites

You need:

- access to the OpenShift project / namespace
- `oc`
- access to the OMERO PostgreSQL database as an administrator
- access to the filestatistics PostgreSQL database as an administrator
- this Git repository
- PostgreSQL 15 or compatible PostgreSQL version

The examples below use:

```text
OpenShift namespace: core-omero-test

OMERO:
  Service:  omero-postgres-server
  Port:     5432
  Database: omerodb

Statistics:
  Service:  filestatistics-pg15
  Port:     5432
  Database: omerofilestats
```

Adjust names for production as required.

---

## 2. Create the statistics schema

The collector does **not** create or migrate its own database tables.

This is intentional: the runtime collector account does not receive DDL privileges.

Create the database if required, then apply:

```text
db/schema.sql
```

Example:

```bash
psql -U postgres -d omerofilestats -f db/schema.sql
```

The schema creates the four collector-owned tables:

```text
omero_fileset
storage_policy
group_storage_snapshot
collector_run
```

It does not create:

- the legacy `imports` table
- PostgreSQL roles
- passwords
- OpenShift Secrets

`schema.sql` is a **fresh-install baseline** and is intentionally not written using `CREATE TABLE IF NOT EXISTS`.

Applying it to an already-initialized database should fail rather than silently hide a schema mismatch.

---

# Database roles

## 3. Create the OMERO read-only role

Connect to `omerodb` as a PostgreSQL administrator.

Create the role:

```sql
CREATE ROLE omero_stats_reader
WITH
    LOGIN
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOINHERIT
    NOREPLICATION
    NOBYPASSRLS;
```

Set its password interactively:

```text
\password omero_stats_reader
```

Enable an additional read-only guardrail:

```sql
ALTER ROLE omero_stats_reader
SET default_transaction_read_only = on;
```

Grant only read access:

```sql
GRANT CONNECT ON DATABASE omerodb
TO omero_stats_reader;

GRANT USAGE ON SCHEMA public
TO omero_stats_reader;

GRANT SELECT ON ALL TABLES IN SCHEMA public
TO omero_stats_reader;
```

Ensure future OMERO tables created by the OMERO role are also readable:

```sql
ALTER DEFAULT PRIVILEGES
FOR ROLE omero
IN SCHEMA public
GRANT SELECT ON TABLES TO omero_stats_reader;
```

### Verify OMERO protection

```sql
SET ROLE omero_stats_reader;

SELECT current_user, session_user;

SELECT count(*)
FROM public.fileset;
```

A write attempt must fail:

```sql
UPDATE public.image
SET id = id
WHERE false;
```

Expected:

```text
ERROR: permission denied for table image
```

Return to the administrator role:

```sql
RESET ROLE;
```

The absence of write privileges is the real security boundary. `default_transaction_read_only = on` is an additional guardrail.

---

## 4. Create the statistics collector role

Connect to `omerofilestats` as a PostgreSQL administrator.

Create:

```sql
CREATE ROLE filestats_collector
WITH
    LOGIN
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOINHERIT
    NOREPLICATION
    NOBYPASSRLS;
```

Set the password interactively:

```text
\password filestats_collector
```

Grant database/schema access:

```sql
GRANT CONNECT ON DATABASE omerofilestats
TO filestats_collector;

GRANT USAGE ON SCHEMA public
TO filestats_collector;
```

Grant collector permissions:

```sql
GRANT SELECT, INSERT, UPDATE
ON public.omero_fileset
TO filestats_collector;

GRANT SELECT, INSERT
ON public.storage_policy
TO filestats_collector;

GRANT SELECT, INSERT, UPDATE
ON public.group_storage_snapshot
TO filestats_collector;

GRANT SELECT, INSERT, UPDATE
ON public.collector_run
TO filestats_collector;
```

Grant required sequence permissions:

```sql
GRANT USAGE, SELECT
ON SEQUENCE public.storage_policy_policy_id_seq
TO filestats_collector;

GRANT USAGE, SELECT
ON SEQUENCE public.collector_run_run_id_seq
TO filestats_collector;
```

The collector deliberately receives:

```text
SELECT
INSERT
UPDATE
```

but no:

```text
DELETE
CREATE
ALTER
DROP
```

Rows representing removed OMERO data are retained and marked with `deleted_at`.

---

# OpenShift Secrets

## 5. Select the OpenShift project

PowerShell:

```powershell
oc project core-omero-test
```

Verify:

```powershell
oc project
```

---

## 6. Create the OMERO reader Secret

PowerShell:

```powershell
$omeroPassword = Read-Host "Password for omero_stats_reader"
```

```powershell
oc create secret generic omero-stats-reader-secret `
  --from-literal=username=omero_stats_reader `
  --from-literal=password=$omeroPassword
```

Then remove the PowerShell variable:

```powershell
Remove-Variable omeroPassword
```

Verify without exposing the secret values:

```powershell
oc describe secret omero-stats-reader-secret
```

---

## 7. Create the statistics writer Secret

```powershell
$filestatsPassword = Read-Host "Password for filestats_collector"
```

```powershell
oc create secret generic filestats-collector-secret `
  --from-literal=username=filestats_collector `
  --from-literal=password=$filestatsPassword
```

```powershell
Remove-Variable filestatsPassword
```

Verify:

```powershell
oc describe secret filestats-collector-secret
```

Never commit decoded passwords, `.env` files, or Kubernetes/OpenShift Secret values to Git.

---

# Build the collector

## 8. Create the OpenShift BuildConfig

From the OpenShift project:

```powershell
oc new-build https://github.com/CCI-GU-Sweden/Omero-storage-collector.git `
  --strategy=docker `
  --name=omero-storage-collector
```

This creates:

- a `BuildConfig`
- an `ImageStream`
- an image tagged `omero-storage-collector:latest`

Check builds:

```powershell
oc get builds
```

Check the image stream:

```powershell
oc get imagestream omero-storage-collector
```

---

## 9. Rebuild after Git changes

The current setup does not require an automatic GitHub webhook.

After:

```powershell
git add .
git commit -m "Description of change"
git push
```

start a new OpenShift build:

```powershell
oc start-build omero-storage-collector --follow
```

---

# Test deployment

## 10. Dry-run Job

The dry-run Job should use:

```text
DRY_RUN=true
```

or omit `DRY_RUN`, since the collector defaults to dry-run mode.

The collector connects to:

```text
OMERO_DB_HOST=omero-postgres-server
OMERO_DB_PORT=5432
OMERO_DB_NAME=omerodb

STATS_DB_HOST=filestatistics-pg15
STATS_DB_PORT=5432
STATS_DB_NAME=omerofilestats
```

Credentials come from:

```text
omero-stats-reader-secret
filestats-collector-secret
```

Create the Job using the manifest in `k8s/`.

Example:

```powershell
oc create -f .\k8s\dry-run-job.yaml
```

View logs:

```powershell
oc logs -f job/omero-storage-collector-dryrun
```

Expected final message:

```text
DRY RUN COMPLETE - no database rows were written.
```

---

## 11. Write-test Job

The write-test manifest explicitly sets:

```yaml
- name: DRY_RUN
  value: "false"
```

Create it:

```powershell
oc create -f .\k8s\write-test-job.yaml
```

View logs:

```powershell
oc logs -f job/omero-storage-collector-write-test
```

To rerun a completed Job:

```powershell
oc delete job omero-storage-collector-write-test --ignore-not-found
oc create -f .\k8s\write-test-job.yaml
```

---

# Verification

## 12. Verify the current Fileset inventory

Enter the PostgreSQL pod:

```powershell
oc rsh <filestatistics-pg15-pod-name>
```

Then connect:

```bash
psql -U postgres -d omerofilestats
```

Check current active inventory:

```sql
SELECT
    count(*) AS filesets,
    sum(total_bytes) AS total_bytes,
    round(
        sum(total_bytes)::numeric / 1000000000,
        3
    ) AS total_gb
FROM public.omero_fileset
WHERE deleted_at IS NULL;
```

Inspect recent Filesets:

```sql
SELECT
    fileset_id,
    username,
    group_name,
    imported_at,
    source_file_count,
    image_count,
    total_bytes,
    locations,
    first_seen_at,
    last_seen_at,
    missing_runs,
    deleted_at
FROM public.omero_fileset
ORDER BY fileset_id DESC
LIMIT 20;
```

---

## 13. Verify storage policies

```sql
SELECT
    policy_id,
    group_id,
    group_name,
    policy_type,
    grace_days,
    billing_grace_days,
    rate_ore_per_gb_day,
    valid_from,
    valid_until
FROM public.storage_policy
ORDER BY group_id, valid_from;
```

For a default TEMPORARY group, expect:

```text
policy_type            TEMPORARY
grace_days             28
billing_grace_days      7
rate_ore_per_gb_day     5
```

The first automatically-created default policy begins on the earliest Fileset import date discovered for that group.

Once a group has policy history, the collector does not silently invent a replacement policy.

---

## 14. Verify daily snapshots

```sql
SELECT
    snapshot_date,
    group_id,
    group_name,
    policy_type,
    grace_days,
    billing_grace_days,

    fileset_count,

    total_bytes,
    round(
        total_bytes::numeric / 1000000000,
        3
    ) AS total_gb,

    overdue_fileset_count,
    round(
        overdue_bytes::numeric / 1000000000,
        3
    ) AS overdue_gb,

    billable_fileset_count,
    round(
        billable_bytes::numeric / 1000000000,
        3
    ) AS billable_gb,

    daily_charge_ore,
    daily_charge_ore / 100 AS daily_charge_sek,

    collected_at

FROM public.group_storage_snapshot
ORDER BY snapshot_date DESC, group_id;
```

The primary key is:

```text
(snapshot_date, group_id)
```

Rerunning the collector on the same day refreshes the day's row instead of creating duplicate snapshots.

---

## 15. Verify collector history

```sql
SELECT
    run_id,
    started_at,
    finished_at,
    status,

    filesets_seen,
    filesets_inserted,
    filesets_updated,
    filesets_marked_deleted,

    snapshots_written,
    deletion_suppressed,

    error_message

FROM public.collector_run
ORDER BY run_id DESC
LIMIT 20;
```

Expected statuses:

```text
RUNNING
SUCCESS
PARTIAL
FAILED
```

A run becomes `PARTIAL` when deletion detection is intentionally suppressed because the OMERO inventory appears suspiciously incomplete.

---

# Safe missing / deletion detection

The collector does not immediately mark a Fileset deleted when it is missing from one OMERO extraction.

Current defaults:

```text
DELETION_CONFIRM_RUNS=2
DELETION_MIN_SOURCE_RATIO=0.80
FORCE_LARGE_DELETION=false
```

These defaults currently live in `collector.py`.

They can later be exposed as deployment environment variables if desired.

## Normal behavior

```text
Seen normally
    missing_runs = 0
    deleted_at = NULL

Missing on first successful run
    missing_runs = 1
    deleted_at = NULL

Missing on second successful run
    missing_runs = 2
    deleted_at = set

Seen again later
    missing_runs = 0
    missing_since_at = NULL
    deleted_at = NULL
```

This means a Fileset remains in storage accounting for one confirmation run before being considered removed.

## Large-drop protection

Before processing missing Filesets, the collector compares the current OMERO Fileset count with the previously active inventory.

With:

```text
DELETION_MIN_SOURCE_RATIO=0.80
```

a source result below 80% of the previous active Fileset count is considered suspicious.

Unless forced explicitly:

```text
FORCE_LARGE_DELETION=false
```

the collector suppresses deletion processing rather than interpreting a broken/incomplete OMERO query as mass data deletion.

The corresponding `collector_run` should record:

```text
status = PARTIAL
deletion_suppressed = true
```

---

# OMERO data model used by the collector

Storage is accounted at **Fileset level**.

The collector derives:

```text
Fileset
├── owner
├── group
├── import timestamp
├── FilesetEntry → OriginalFile
│   ├── source filename
│   ├── client path
│   └── size
├── Image count
└── current Project / Dataset locations
```

A single source file may create multiple OMERO Images.

For example:

```text
1 CZI source file
    ↓
1 Fileset
    ↓
4 OMERO Images
```

Therefore these values are tracked separately:

```text
source_file_count
image_count
total_bytes
```

Project and Dataset information is treated as **current navigation information**, not storage truth.

The Fileset remains the storage/accounting unit even if users rename, move, or link their Images into different Projects/Datasets.

---

# Important safety properties

## OMERO is read-only

The collector's OMERO role has no write privileges.

The collector must never use the normal OMERO application password or PostgreSQL administrator password.

## Statistics database uses least privilege

`filestats_collector` can update collector-owned data but cannot perform DDL or arbitrary deletes.

## No automatic OMERO deletion

This project is reporting/accounting only.

`deleted_at` means that a Fileset was no longer observed in OMERO after safe confirmation.

The collector does not delete user data from OMERO.

## Billing data is daily

Billing should be derived from `group_storage_snapshot`, not reconstructed later from the current Fileset inventory.

This preserves the amount of chargeable storage that actually existed on each day.

---

# Planned production flow

Once testing is complete:

```text
GitHub
  ↓
OpenShift BuildConfig
  ↓
omero-storage-collector image
  ↓
daily OpenShift CronJob
  ↓
OMERO read-only extraction
  ↓
omero_fileset
  ↓
policy evaluation
  ↓
group_storage_snapshot
  ↓
dashboard / retention emails / billing reports
```

Before production deployment, verify:

- dry-run succeeds
- repeat write runs are idempotent
- policy configuration is valid
- deletion confirmation works
- mass-deletion suppression works
- `collector_run` records SUCCESS/PARTIAL/FAILED correctly
- daily snapshots are correct
- billing boundary dates have been tested
