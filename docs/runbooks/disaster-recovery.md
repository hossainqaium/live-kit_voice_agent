# Disaster Recovery Runbook

**Platform:** Multi-Tenant AI Voice Agent Platform  
**Spec ref:** §66, Plan 7.11  
**Last rehearsed:** _(record date here after each rehearsal)_

---

## Scope

This runbook covers full-platform recovery from the following loss scenarios:

| Scenario | RTO target | RPO target |
|---|---|---|
| PostgreSQL data loss (single-node failure) | 15 min | ≤ last nightly backup |
| Complete cluster loss (all nodes gone) | 45 min | ≤ last nightly backup |
| MinIO / recordings loss | 30 min | ≤ last backup or erasure |
| Redis loss | 5 min | 0 (Redis is disposable) |
| LiveKit configuration drift | 5 min | 0 (PostgreSQL is authoritative) |

---

## 1. Prerequisites

You need access to:

1. The S3/MinIO bucket where `pg_dump` archives are stored (`backups/postgresql/`).
2. `kubectl` configured for the production cluster.
3. The Helm values file (`values.production.yaml`) and secrets (via ESO or Vault).
4. The repository at the commit that was deployed.

Verify you can list backup files:

```bash
aws s3 ls s3://backups/postgresql/ --endpoint-url https://s3.example.com
# or for MinIO:
mc ls minio-prod/backups/postgresql/
```

---

## 2. PostgreSQL restore

### 2a. Identify the latest good dump

```bash
aws s3 ls s3://backups/postgresql/ --endpoint-url $S3_ENDPOINT | sort -r | head -5
```

Download it:

```bash
DUMP=voice_agent-20261001T020000Z.sql.gz
aws s3 cp s3://backups/postgresql/${DUMP} ./ --endpoint-url $S3_ENDPOINT
```

### 2b. Stand up a clean PostgreSQL instance

If restoring in-cluster (Bitnami chart):

```bash
# Scale down the platform first so nothing writes to the old DB.
kubectl scale deployment voice-agent-platform-configuration-api --replicas=0 -n voice-agent
kubectl scale deployment voice-agent-platform-ai-agent-worker  --replicas=0 -n voice-agent

# Delete the old PVC (DESTRUCTIVE — confirm the backup is valid first).
kubectl delete pvc data-voice-agent-platform-postgresql-0 -n voice-agent

# Helm upgrade triggers PVC recreation.
helm upgrade voice-agent-platform deploy/helm/voice-agent-platform \
  -f deploy/helm/voice-agent-platform/values.production.yaml \
  --set secrets.postgresPassword="$POSTGRES_PASSWORD" \
  [other --set flags] \
  -n voice-agent --wait
```

For a managed database (RDS, Cloud SQL): create a new instance from the latest automated snapshot, or restore from the dump file.

### 2c. Restore the dump

```bash
# Get a shell into the new PostgreSQL pod.
PG_POD=$(kubectl get pod -n voice-agent -l app.kubernetes.io/component=primary \
         -o jsonpath='{.items[0].metadata.name}')

kubectl exec -i -n voice-agent ${PG_POD} -- bash -c \
  "gunzip -c | psql -U voice_agent -d voice_agent" \
  < ${DUMP}
```

### 2d. Run migrations on the restored data

```bash
kubectl exec -n voice-agent \
  deploy/voice-agent-platform-configuration-api \
  -- alembic upgrade head
```

### 2e. Scale the platform back up

```bash
kubectl scale deployment voice-agent-platform-configuration-api --replicas=2 -n voice-agent
kubectl scale deployment voice-agent-platform-ai-agent-worker  --replicas=2 -n voice-agent
```

### 2f. Verify

```bash
# API health
kubectl exec -n voice-agent deploy/voice-agent-platform-configuration-api \
  -- curl -sf http://localhost:8000/ready

# Basic data sanity
kubectl exec -n voice-agent deploy/voice-agent-platform-configuration-api \
  -- python -m app.cli list-tenants
```

---

## 3. Full cluster loss

If the entire cluster is gone (e.g. cloud region failure):

1. Provision a new cluster in another region.
2. Re-install the platform from scratch using the steps in `deploy/helm/DEPLOYMENT.md`.
3. Follow §2 above to restore PostgreSQL from the S3 dump.
4. MinIO recordings may also need restoration from a cross-region replica or backup (see §4).
5. Re-apply `deploy/otel/otel-collector.yaml` and `deploy/secrets/external-secrets.yaml`.
6. Update DNS to point at the new cluster's load balancer.

---

## 4. MinIO / recordings loss

Recordings are not directly recoverable if the MinIO PVC is lost and no cross-region replica exists.  The metadata (start time, duration, transcript, agent ID) is in PostgreSQL and is recoverable per §2.

If you use AWS S3 or GCS as the backing store (`minio.enabled=false`), enable versioning and cross-region replication on the bucket.

To restore a MinIO PVC from a prior snapshot (if your cloud supports volume snapshots):

```bash
# Find the VolumeSnapshot.
kubectl get volumesnapshot -n voice-agent

# Restore it as a new PVC (PVC spec depends on your CSI driver).
kubectl apply -f - <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: voice-agent-platform-minio-restored
  namespace: voice-agent
spec:
  dataSource:
    name: minio-snapshot-20261001
    kind: VolumeSnapshot
    apiGroup: snapshot.storage.k8s.io
  accessModes: [ReadWriteOnce]
  storageClassName: gp3
  resources:
    requests:
      storage: 500Gi
EOF
```

---

## 5. Redis loss

Redis holds only disposable state:

- LiveKit room participant lists (LiveKit reconciles these itself)
- API rate-limit counters (reset to zero on restart — brief over-allowance)
- Session cache (sessions will need re-login)

**Recovery**: Let the pods restart. Redis is empty on restart and the platform self-heals within minutes. No data recovery is needed or expected.

---

## 6. LiveKit configuration drift

If the LiveKit cluster is lost but PostgreSQL is intact:

1. Provision a new LiveKit cluster.
2. Update `livekit.url` and secrets in Helm values.
3. Helm upgrade the platform.
4. Run the Synchronize action in the Platform → LiveKit console, or:

```bash
kubectl exec -n voice-agent deploy/voice-agent-platform-configuration-api \
  -- python -m app.cli sync-livekit
```

This recreates all SIP trunks and dispatch rules from PostgreSQL. RTO: ~5 minutes.

---

## 7. Rehearsal checklist

Run through this checklist quarterly. Record date and outcome.

| # | Step | Expected result | Pass/Fail |
|---|---|---|---|
| 1 | List last 5 PostgreSQL dumps | ≥ 1 dump from yesterday | |
| 2 | Download the latest dump | File arrives in < 60 s | |
| 3 | Spin up a fresh PostgreSQL pod in a test namespace | Pod reaches Ready | |
| 4 | Restore the dump | No errors in psql output | |
| 5 | Run `alembic upgrade head` against the restored DB | "No new migrations" | |
| 6 | Verify tenant/agent count matches production via CLI | Counts match | |
| 7 | Tear down the test namespace | No leftover resources | |

**Rehearsal record:**

| Date | Engineer | Steps passed | Notes |
|---|---|---|---|
| _(fill in)_ | _(fill in)_ | _(fill in)_ | _(fill in)_ |
