# Kubernetes Deployment Guide

## Prerequisites

| Tool | Version |
|---|---|
| kubectl | ≥ 1.28 |
| helm | ≥ 3.14 |
| A Kubernetes cluster | ≥ 1.28 (EKS, GKE, AKS, or k3s) |
| Ingress controller | nginx-ingress recommended |
| cert-manager | For automatic TLS (optional but recommended) |

---

## 1. Add Helm repositories

```bash
helm repo add bitnami https://charts.bitnami.com/bitnami
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add livekit https://helm.livekit.io
helm repo update
```

---

## 2. Pull sub-chart dependencies

```bash
cd deploy/helm/voice-agent-platform
helm dependency update
```

This writes `charts/postgresql-*.tgz`, `charts/redis-*.tgz`, and `charts/minio-*.tgz`.

---

## 3. Build and push images

```bash
# Build all three images from the repository root.
docker build -t your-registry/voice-agent/configuration-api:1.0.0 \
  -f services/configuration-api/Dockerfile .

docker build -t your-registry/voice-agent/ai-agent-worker:1.0.0 \
  -f services/ai-agent-worker/Dockerfile .

docker build -t your-registry/voice-agent/frontend:1.0.0 \
  --build-arg NEXT_PUBLIC_API_BASE_URL=https://api.example.com \
  -f services/frontend/Dockerfile .

docker push your-registry/voice-agent/configuration-api:1.0.0
docker push your-registry/voice-agent/ai-agent-worker:1.0.0
docker push your-registry/voice-agent/frontend:1.0.0
```

Set `global.imageRegistry` in your values file to point at `your-registry`.

---

## 4. Deploy LiveKit separately

The chart does not manage LiveKit.  Use the official chart:

```bash
helm upgrade --install livekit livekit/livekit-server \
  --namespace voice-agent \
  --set livekit.keys.devkey="$LIVEKIT_API_SECRET" \
  --set config.redis.address="$(kubectl get svc -n voice-agent -o jsonpath='{...}')"
```

Note the LiveKit WebSocket URL and SIP URI for step 5.

---

## 5. Install the platform

```bash
export JWT_SECRET=$(openssl rand -base64 32)
export CREDENTIAL_ENCRYPTION_KEY=$(openssl rand -base64 32)
export POSTGRES_PASSWORD=$(openssl rand -base64 24)
export MINIO_ROOT_PASSWORD=$(openssl rand -base64 24)
export LIVEKIT_API_KEY=your-livekit-key
export LIVEKIT_API_SECRET=your-livekit-secret

helm upgrade --install voice-agent-platform \
  ./deploy/helm/voice-agent-platform \
  -f deploy/helm/voice-agent-platform/values.production.yaml \
  --set global.imageRegistry="your-registry" \
  --set secrets.jwtSecret="$JWT_SECRET" \
  --set secrets.credentialEncryptionKey="$CREDENTIAL_ENCRYPTION_KEY" \
  --set secrets.postgresPassword="$POSTGRES_PASSWORD" \
  --set secrets.minioRootPassword="$MINIO_ROOT_PASSWORD" \
  --set secrets.livekitApiKey="$LIVEKIT_API_KEY" \
  --set secrets.livekitApiSecret="$LIVEKIT_API_SECRET" \
  --set configurationApi.livekitPublicUrl="wss://livekit.example.com" \
  --set configurationApi.livekitSipUri="sip:sip.example.com:5060" \
  --set frontend.apiBaseUrl="https://api.example.com" \
  --set "frontend.ingress.hosts[0].host=console.example.com" \
  --set "frontend.ingress.tls[0].hosts[0]=console.example.com" \
  --namespace voice-agent \
  --create-namespace \
  --wait
```

---

## 6. Run database migrations and seed

```bash
# Migrations run automatically via the initContainer in the configuration-api
# deployment.  To run them manually or to re-run the platform seed:
kubectl exec -n voice-agent deploy/voice-agent-platform-configuration-api -- \
  python -m app.cli seed-platform
```

---

## 7. Install the Prometheus Adapter (custom HPA metrics)

```bash
helm upgrade --install prometheus-adapter \
  prometheus-community/prometheus-adapter \
  --namespace monitoring \
  --set prometheus.url=http://prometheus-operated.monitoring.svc.cluster.local \
  --set prometheus.port=9090 \
  -f deploy/prometheus/adapter-config.yaml
```

Verify:

```bash
kubectl get --raw "/apis/custom.metrics.k8s.io/v1beta1" \
  | python3 -m json.tool | grep voice_worker_load_ratio
```

---

## 8. Verify the deployment

```bash
# All pods running
kubectl get pods -n voice-agent

# HPA status
kubectl get hpa -n voice-agent

# PDB status
kubectl get pdb -n voice-agent

# Configuration API health
kubectl port-forward svc/voice-agent-platform-configuration-api 8000:8000 -n voice-agent &
curl http://localhost:8000/ready

# AI worker health
kubectl port-forward svc/voice-agent-platform-ai-agent-worker 8090:8090 -n voice-agent &
curl http://localhost:8090/ready
```

---

## Graceful shutdown verification

To verify that a worker drains without dropping calls:

```bash
# While a test call is in progress, evict a worker pod.
kubectl delete pod -n voice-agent \
  $(kubectl get pod -n voice-agent -l app.kubernetes.io/component=ai-agent-worker \
    -o jsonpath='{.items[0].metadata.name}')

# Watch the replacement come up while the evicted pod finishes its call.
kubectl get pods -n voice-agent -w

# The call in the evicted pod should complete before it exits.
# Check the logs:
kubectl logs -n voice-agent <evicted-pod-name> | grep -E "drain|SIGTERM|shutdown"
```

Expected log sequence:
```
SIGTERM received — starting drain
drain: stopped accepting new jobs
drain: 1 active call(s) remaining
drain: all calls finished — exiting
```

---

## Rolling upgrade verification

```bash
# Update the worker image tag in values.production.yaml, then:
helm upgrade voice-agent-platform ./deploy/helm/voice-agent-platform \
  -f deploy/helm/voice-agent-platform/values.production.yaml \
  --reuse-values \
  --set aiAgentWorker.image.tag=1.0.1 \
  --namespace voice-agent

# Watch rollout while calls are in progress — no call should drop.
kubectl rollout status deploy/voice-agent-platform-ai-agent-worker -n voice-agent
```

---

## Scaling the worker fleet manually

```bash
# Override the HPA temporarily (e.g. before a scheduled event).
kubectl scale deploy voice-agent-platform-ai-agent-worker --replicas=8 -n voice-agent

# Resume HPA control.
kubectl annotate hpa voice-agent-platform-ai-agent-worker \
  autoscaling.alpha.kubernetes.io/conditions- -n voice-agent
```
