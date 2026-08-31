# OMERO Storage Collector

Daily OMERO storage accounting, retention-policy, and billing collector for OpenShift/Kubernetes.

Repository:  
https://github.com/CCI-GU-Sweden/Omero-storage-collector

The collector reads OMERO PostgreSQL using a dedicated **read-only** account, converts the current OMERO Fileset inventory into storage statistics, and writes those statistics to a separate PostgreSQL database.

It is designed to support:

- current storage usage by OMERO group and user
- retention tracking
- overdue-data warnings
- daily billing snapshots
- agreement/core-storage policies
- safe detection of deleted/missing Filesets
- future dashboard, email, and invoice workflows

The collector **never modifies OMERO**.

---

## Architecture

```text
                         READ ONLY
OMERO PostgreSQL  ------------------------>
omerodb                                  |
                                         |
                                omero_stats_reader
                                         |
                                         v
                              OMERO Storage Collector
                              OpenShift CronJob
                                         |
                                         | SELECT / INSERT / UPDATE
                                         v
                              Statistics PostgreSQL
                              omerofilestats
                                         |
                    +--------------------+--------------------+
                    |                    |                    |
                    v                    v                    v
              Current inventory     Daily snapshots     Collector health
              omero_fileset         group_storage_     collector_run
                                    snapshot
                    |
                    v
              storage_policy
```

OMERO remains the source of truth for Filesets and storage.

The statistics database stores derived state and historical daily snapshots.

---

# Storage model

Storage accounting is performed at **OMERO Fileset level**.

One Fileset can contain one or more source files and can generate multiple OMERO Images.

For example:

```text
1 CZI source file
        |
        v
    1 Fileset
        |
        +---- Image
        +---- Image
        +---- Image
        +---- Image
```

The collector therefore tracks separately:

```text
source_file_count
image_count
total_bytes
```

Project and Dataset names are stored as current navigation information only.

They are not treated as storage truth because Images may later be moved, renamed, or linked differently.

---

# Storage policies

Three policy types are supported.

## TEMPORARY

Default for groups without an explicit policy.

Current defaults:

```text
Retention period:      28 full days
Warning/billing grace: 7 full calendar days
Rate:                  5 öre / GB / day
```

Example for an import on August 1:

```text
Aug 1-29      within retention period
Aug 30-Sep 5  overdue / warning period, not billed
Sep 6 onward  billable
```

## AGREEMENT

Storage is billable every calendar day while the agreement policy is active.

Typical configuration:

```text
grace_days = 0
billing_grace_days = 0
rate = 5 öre / GB / day
```

## CORE

CORE storage is:

- included in total storage monitoring
- excluded from overdue warnings
- excluded from billing

Typical configuration:

```text
grace_days = NULL
billing_grace_days = 0
rate = 0
```

---

# Billing units

Billing uses **decimal GB**:

```text
1 GB = 1,000,000,000 bytes
```

The configured rate is stored in ore per GB per day.

```text
100 öre = 1 SEK
```

Daily charges retain fractional ore in the database.

Rounding should happen when producing a final invoice, not during each daily calculation.

All calendar-day calculations use `Europe/Stockholm`.

---

# Prerequisites

You need:

- access to the OpenShift/Kubernetes namespace
- `oc`
- Git
- access to the OMERO PostgreSQL database as an administrator for initial role setup
- access to the statistics PostgreSQL database as an administrator for initial role/schema setup
- PostgreSQL 15 or compatible
- access to this GitHub repository

Example test environment used during development:

```text
OpenShift project:
core-omero-test

OMERO PostgreSQL:
service:  omero-postgres-server
port:     5432
database: omerodb

Statistics PostgreSQL:
service:  filestatistics-pg15
port:     5432
database: omerofilestats
```

Adjust these values for another environment.

---

# 1. Clone the repository

```bash
git clone https://github.com/CCI-GU-Sweden/Omero-storage-collector.git
cd Omero-storage-collector
```

---

# 2. Create the statistics database schema

Follow `db/README.md`.

The schema itself is `db/schema.sql`.

For a fresh database, it creates:

```text
omero_fileset
storage_policy
group_storage_snapshot
collector_run
```

The collector does **not** create or alter tables itself.

This is intentional. The runtime collector account has no DDL privileges.

---

# 3. Create database roles

Two dedicated PostgreSQL identities are used.

## OMERO database

```text
omero_stats_reader
```

This role has:

```text
CONNECT
USAGE
SELECT
```

and no OMERO write privileges.

It also has:

```text
default_transaction_read_only = on
```

as an additional safety guardrail.

## Statistics database

```text
filestats_collector
```

This role has only the permissions needed to maintain collector-owned tables.

It has no:

```text
DELETE
CREATE
ALTER
DROP
```

See `db/README.md` for the SQL role and GRANT commands.

---

# 4. Select the OpenShift project

Example:

```powershell
oc project core-omero-test
```

Verify:

```powershell
oc project
```

---

# 5. Create OpenShift Secrets

Do not place database passwords in Git, YAML, Docker images, or committed `.env` files.

The collector expects:

```text
omero-stats-reader-secret
filestats-collector-secret
```

Example PowerShell workflow:

```powershell
$omeroPassword = Read-Host "Password for omero_stats_reader"

oc create secret generic omero-stats-reader-secret `
  --from-literal=username=omero_stats_reader `
  --from-literal=password=$omeroPassword

Remove-Variable omeroPassword
```

Then:

```powershell
$filestatsPassword = Read-Host "Password for filestats_collector"

oc create secret generic filestats-collector-secret `
  --from-literal=username=filestats_collector `
  --from-literal=password=$filestatsPassword

Remove-Variable filestatsPassword
```

Verify metadata without printing secret values:

```powershell
oc describe secret omero-stats-reader-secret
oc describe secret filestats-collector-secret
```

---

# 6. Create the collector image build

The repository contains a Dockerfile.

Create an OpenShift Docker-strategy build:

```powershell
oc new-build https://github.com/CCI-GU-Sweden/Omero-storage-collector.git `
  --strategy=docker `
  --name=omero-storage-collector
```

This creates an OpenShift `BuildConfig` and `ImageStream`, and builds:

```text
omero-storage-collector:latest
```

Check:

```powershell
oc get buildconfig omero-storage-collector
oc get imagestream omero-storage-collector
oc get builds
```

---

# 7. Rebuild after code changes

The current deployment workflow uses an explicit build rather than relying on a GitHub webhook.

After making changes:

```powershell
git add .
git commit -m "Describe the change"
git push
```

Then:

```powershell
oc start-build omero-storage-collector --follow
```

The collector Jobs use:

```yaml
imagePullPolicy: Always
```

so the latest image is pulled when a new Job starts.

---

# 8. Dry-run test

Before enabling writes, run the dry-run manifest:

```powershell
oc delete job omero-storage-collector-dryrun --ignore-not-found
oc create -f .\k8s\dry-run-job.yaml
oc logs -f job/omero-storage-collector-dryrun
```

Expected ending:

```text
Extraction successful
...
DRY RUN COMPLETE - no database rows were written.
```

---

# 9. Write-test

After the dry run is correct:

```powershell
oc delete job omero-storage-collector-write-test --ignore-not-found
oc create -f .\k8s\write-test-job.yaml
oc logs -f job/omero-storage-collector-write-test
```

A healthy run should end with `WRITE RUN COMPLETE.` and report no deletion suppression during normal operation.

---

# 10. Deploy the CronJob

The scheduled collector manifest is:

```text
k8s/omero-storage-collector.yaml
```

Validate it against the OpenShift API before creating it:

```powershell
oc apply --dry-run=server -f .\k8s\omero-storage-collector.yaml
```

Then deploy:

```powershell
oc apply -f .\k8s\omero-storage-collector.yaml
```

Inspect it:

```powershell
oc get cronjob omero-storage-collector
oc describe cronjob omero-storage-collector
```

---

# Current test schedule

The test environment is normally scaled down at night and during weekends.

The test collector is therefore scheduled for:

```text
07:00 Monday-Friday
Europe/Stockholm
```

Cron expression:

```text
0 7 * * 1-5
```

This runs one hour after the test environment's normal 06:00 weekday startup.

The CronJob also uses:

```text
concurrencyPolicy: Forbid
```

so overlapping collector runs are not allowed.

A different schedule should be chosen for production according to that environment's maintenance/startup windows.

---

# 11. Test the exact CronJob template manually

Before enabling the schedule, deploy the CronJob with:

```yaml
suspend: true
```

Then create a one-off Job from it:

```powershell
oc create job `
  --from=cronjob/omero-storage-collector `
  omero-storage-collector-manual
```

Follow logs:

```powershell
oc logs -f job/omero-storage-collector-manual
```

Check completion:

```powershell
oc get job omero-storage-collector-manual
```

Expected:

```text
Complete
1/1
```

---

# 12. Enable scheduled collection

When the manual CronJob-template test succeeds:

```powershell
oc patch cronjob omero-storage-collector `
  -p '{"spec":{"suspend":false}}'
```

Verify:

```powershell
oc get cronjob omero-storage-collector
```

Expected:

```text
SUSPEND = False
```

The CronJob is now active.

---

# 13. Verify collector runs

Inspect OpenShift Jobs:

```powershell
oc get jobs | Select-String omero-storage-collector
```

Inspect the latest collector execution in PostgreSQL:

```sql
SELECT
    run_id,
    started_at,
    finished_at,
    status,
    filesets_seen,
    total_bytes_seen,
    filesets_inserted,
    filesets_updated,
    filesets_marked_deleted,
    snapshots_written,
    deletion_suppressed,
    error_message
FROM public.collector_run
ORDER BY run_id DESC
LIMIT 10;
```

Typical healthy result:

```text
status                    SUCCESS
filesets_marked_deleted   0
snapshots_written         1
deletion_suppressed       false
error_message             NULL
```

---

# 14. Verify current storage

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

---

# 15. Verify daily group snapshots

```sql
SELECT
    snapshot_date,
    group_id,
    group_name,
    policy_type,
    fileset_count,
    total_bytes,
    overdue_fileset_count,
    overdue_bytes,
    billable_fileset_count,
    billable_bytes,
    daily_charge_ore,
    collected_at
FROM public.group_storage_snapshot
ORDER BY snapshot_date DESC, group_id;
```

Daily billing should be based on `group_storage_snapshot`, not reconstructed later from the current Fileset inventory.

---

# Safe Fileset deletion detection

The collector does not assume that one missing Fileset means it was deleted.

Current defaults:

```text
DELETION_CONFIRM_RUNS=2
DELETION_MIN_SOURCE_RATIO=0.80
FORCE_LARGE_DELETION=false
```

## Individual missing Fileset

```text
Run 1 missing:
missing_runs = 1
deleted_at = NULL

Run 2 missing:
missing_runs = 2
deleted_at = set
```

If the Fileset reappears:

```text
missing_since_at = NULL
missing_runs = 0
deleted_at = NULL
```

## Suspicious mass drop

If the source extraction falls below 80% of the previously active Fileset inventory, missing/deletion processing is suppressed.

The collector records:

```text
deletion_suppressed = true
collector_run.status = PARTIAL
```

Repeated incomplete inventories remain suppressed rather than eventually turning into automatic mass deletion.

A legitimate large removal should therefore be investigated and explicitly acknowledged.

---

# Policy history

Policies are effective-dated using:

```text
valid_from
valid_until
```

The collector requires exactly one active policy for every active OMERO group on the snapshot date.

A default TEMPORARY policy is created only if the group has **never had a policy**.

If an AGREEMENT expires without a successor, the collector should fail policy validation rather than silently creating a new TEMPORARY policy.

---

# Updating the database schema

`db/schema.sql` represents the current **fresh-install baseline**.

Do not use runtime collector permissions for schema changes.

Future deployed-schema changes should be added as explicit migrations, for example:

```text
db/
├── schema.sql
└── migrations/
    ├── 001_example_change.sql
    └── 002_next_change.sql
```

Database migrations should be run separately using an appropriately privileged database administrator account.

---

# Useful OpenShift commands

Show the collector CronJob:

```powershell
oc get cronjob omero-storage-collector
```

Describe configuration:

```powershell
oc describe cronjob omero-storage-collector
```

Show recent collector Jobs:

```powershell
oc get jobs | Select-String omero-storage-collector
```

Logs from a Job:

```powershell
oc logs job/<job-name>
```

Suspend scheduled collection:

```powershell
oc patch cronjob omero-storage-collector `
  -p '{"spec":{"suspend":true}}'
```

Resume:

```powershell
oc patch cronjob omero-storage-collector `
  -p '{"spec":{"suspend":false}}'
```

Trigger a manual run from the current CronJob template:

```powershell
oc create job `
  --from=cronjob/omero-storage-collector `
  omero-storage-collector-manual
```

---

# Security principles

## Never use the normal OMERO application database account

The collector must use `omero_stats_reader`.

## Never give the collector OMERO write privileges

OMERO is read-only from this project.

## Do not store passwords in Git

Use OpenShift Secrets.

## Do not give the collector database-owner permissions

Schema management and runtime data collection are separate responsibilities.

## No automatic deletion from OMERO

`deleted_at` is collector metadata only.

The collector never deletes Filesets, Images, OriginalFiles, Projects, Datasets, or any other OMERO objects.

---

# Deployment checklist

Before considering an environment operational:

- [ ] `db/schema.sql` applies successfully to a fresh database
- [ ] OMERO `omero_stats_reader` role exists
- [ ] Statistics `filestats_collector` role exists
- [ ] OpenShift Secrets exist
- [ ] Collector image builds successfully
- [ ] Dry run succeeds
- [ ] Write test succeeds
- [ ] Repeat writes are idempotent
- [ ] `collector_run` records SUCCESS correctly
- [ ] one-missing-run protection is tested
- [ ] two-missing-run deletion marking is tested
- [ ] reappearing Fileset resurrection is tested
- [ ] `<80%` inventory suppression is tested
- [ ] TEMPORARY billing boundaries are tested
- [ ] AGREEMENT billing is tested
- [ ] CORE exclusion from billing is tested
- [ ] CronJob template works as a manual Job
- [ ] CronJob schedule is appropriate for the environment
- [ ] CronJob is unsuspended

---

# Future work

The collector database is intended to feed:

```text
OMERO storage dashboard
retention-warning emails
billing reports / invoices
storage-capacity monitoring
```

These consumers should use dedicated read-only database roles rather than reuse `filestats_collector`.
