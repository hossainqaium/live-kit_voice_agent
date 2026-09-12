# Failure Testing Playbook

**Platform:** Multi-Tenant AI Voice Agent Platform  
**Spec ref:** §73, Plan 7.12  
**Purpose:** Verify each failure mode in §73 has a documented, observed recovery behaviour.

Run each scenario while at least one real or simulated call is in progress. Record the observed behaviour in the table at the end of each section. A scenario is **PASS** when recovery is automatic, within the stated time, and no call is dropped (unless the scenario explicitly expects drop).

---

## Prerequisites

```bash
# Ensure you are in the voice-agent namespace
kubectl config set-context --current --namespace=voice-agent

# Confirm at least 2 worker pods are running
kubectl get pods -l app.kubernetes.io/component=ai-agent-worker

# Confirm HPA is active
kubectl get hpa

# Open the Grafana Platform Overview dashboard in a browser to watch metrics.
```

---

## 1. AI Worker pod killed mid-call

**Spec §73 scenario:** AI worker process dies while handling a call.

**Expected behaviour:**
- The killed pod enters `Terminating` state and is removed from the Service endpoints.
- Active calls on the killed pod are interrupted (unavoidable — process is gone).
- The HPA or ReplicaSet starts a replacement pod within 30 s.
- New calls are routed to healthy pods within 60 s.
- `alert: WorkerPodCountTooLow` fires if < 2 pods remain.

```bash
# Start a test call, then:
WORKER_POD=$(kubectl get pod -l app.kubernetes.io/component=ai-agent-worker \
  -o jsonpath='{.items[0].metadata.name}')
kubectl delete pod ${WORKER_POD} --grace-period=0 --force

# Watch replacement
kubectl get pods -w -l app.kubernetes.io/component=ai-agent-worker
```

**Record:**
| Date | Replacement pod ready in | Calls on remaining pods affected? | Pass/Fail |
|---|---|---|---|

---

## 2. AI Worker graceful drain (SIGTERM)

**Spec §73 scenario:** Worker receives SIGTERM (rolling deploy, node drain).

**Expected behaviour:**
- `preStop` sleep (5 s) completes.
- Worker logs `"SIGTERM received — starting drain"`.
- Active calls run to completion (up to 600 s).
- Pod exits after all calls finish or after drain timeout, whichever comes first.
- `terminationGracePeriodSeconds` (620 s) is never hit.

```bash
# Initiate a graceful termination:
kubectl delete pod ${WORKER_POD}   # respects terminationGracePeriodSeconds

# Follow drain logs:
kubectl logs -f ${WORKER_POD}
```

**Record:**
| Date | Active calls drained successfully | Drain duration | Pod exited cleanly | Pass/Fail |
|---|---|---|---|---|

---

## 3. Configuration API restart

**Spec §73 scenario:** API pod crashes or is restarted.

**Expected behaviour:**
- In-flight HTTP requests complete or fail with 502/503 from the upstream LB (the `preStop` sleep gives kube-proxy time to drain).
- The replacement pod starts, runs `alembic upgrade head` (init container), and reaches `Ready` within 30 s.
- Active calls are unaffected (workers have already loaded their configuration and do not call the API mid-call).

```bash
kubectl rollout restart deployment/voice-agent-platform-configuration-api
kubectl rollout status deployment/voice-agent-platform-configuration-api
```

**Record:**
| Date | API downtime (s) | Active calls dropped | Pass/Fail |
|---|---|---|---|

---

## 4. Rolling deployment during active calls

**Spec §73 scenario:** Image update while calls are running.

**Expected behaviour:**
- `maxUnavailable: 0` keeps the old pod running until the new one is `Ready`.
- Zero calls are dropped during the rollout.
- The PDB prevents both pods from being evicted simultaneously.

```bash
# Simulate a deploy by changing the image tag to itself.
kubectl set image deployment/voice-agent-platform-ai-agent-worker \
  ai-agent-worker=voice-agent/ai-agent-worker:$(kubectl get deployment \
    voice-agent-platform-ai-agent-worker \
    -o jsonpath='{.spec.template.spec.containers[0].image}' | cut -d: -f2)

kubectl rollout status deployment/voice-agent-platform-ai-agent-worker
```

**Record:**
| Date | Calls dropped | Rollout duration | Pass/Fail |
|---|---|---|---|

---

## 5. Redis failure

**Spec §73 scenario:** Redis pod crashes.

**Expected behaviour:**
- Active calls continue (workers hold call state in memory).
- LiveKit room participant counts in Prometheus may go stale (LiveKit re-announces on reconnect).
- API rate limiting resets to zero (brief over-allowance, acceptable for ≤ 5 min).
- When Redis restarts, sessions must re-authenticate (console users are logged out).

```bash
REDIS_POD=$(kubectl get pod -l app.kubernetes.io/component=master -o jsonpath='{.items[0].metadata.name}')
kubectl delete pod ${REDIS_POD} --grace-period=0 --force
kubectl get pod -w -l app.kubernetes.io/component=master
```

**Record:**
| Date | Active calls affected | Redis recovery time | Pass/Fail |
|---|---|---|---|

---

## 6. PostgreSQL failure

**Spec §73 scenario:** PostgreSQL pod crashes.

**Expected behaviour:**
- Active calls are **not** immediately affected (workers loaded configuration at call start).
- New calls fail (worker cannot decrypt credentials, load routing rules, or validate new sessions).
- API returns 503 on any request that touches the database.
- On PostgreSQL restart the API reconnects automatically (SQLAlchemy connection pool retries).

```bash
PG_POD=$(kubectl get pod -l app.kubernetes.io/component=primary -o jsonpath='{.items[0].metadata.name}')
kubectl delete pod ${PG_POD} --grace-period=0 --force
kubectl get pod -w -l app.kubernetes.io/component=primary
```

**Record:**
| Date | Active calls affected | New calls failed for | API recovery time | Pass/Fail |
|---|---|---|---|---|

---

## 7. STT provider failure (circuit breaker)

**Spec §73 scenario:** Primary STT provider returns errors.

**Expected behaviour:**
- After the circuit-breaker threshold (5 failures in 60 s), `voice_provider_circuit_state{kind="stt"} = 2`.
- `alert: ProviderCircuitOpen` fires.
- The `FallbackAdapter` routes subsequent STT calls to the fallback provider.
- Affected turns experience a brief delay (first failure + fallback latency) then recover.
- `voice_provider_fallbacks_total` counter increments.

```bash
# Simulate by setting an invalid STT credential for the primary provider
# (in dev: point the credential at localhost:9999).
# Watch the circuit-breaker metric:
kubectl port-forward svc/voice-agent-platform-ai-agent-worker 8090:8090 &
curl -s http://localhost:8090/metrics | grep voice_provider_circuit_state
```

**Record:**
| Date | Fallback activated | Circuit opened after (s) | Call quality during failover | Pass/Fail |
|---|---|---|---|---|

---

## 8. LLM provider timeout mid-call

**Spec §73 scenario:** LLM API hangs for longer than the per-turn timeout.

**Expected behaviour:**
- `voice_llm_first_token_latency_seconds` shows high values.
- After the configured LLM timeout, the worker logs a timeout error and either re-prompts or uses the fallback LLM.
- The call does not hang indefinitely — the caller hears silence for at most (timeout + TTS) seconds, then a recovery response.

```bash
# Simulate: set LLM endpoint to a server that accepts but never responds.
# Verify the turn-timeout fires by checking:
kubectl logs deploy/voice-agent-platform-ai-agent-worker | grep -i "llm.*timeout"
```

**Record:**
| Date | Turn recovered in | Caller heard silence for | Pass/Fail |
|---|---|---|---|

---

## 9. Worker exhaustion (all slots occupied)

**Spec §73 scenario:** Every worker pod is at `maxConcurrentCalls`.

**Expected behaviour:**
- `alert: WorkerFleetSaturated` fires.
- New calls see `voice_calls_rejected_total` increment.
- The API returns a "capacity exceeded" response to the SIP layer.
- HPA adds pods if load ratio > 0.7, bringing capacity back above demand.

```bash
# Simulate by reducing max concurrent calls temporarily:
kubectl set env deployment/voice-agent-platform-ai-agent-worker \
  WORKER_MAX_CONCURRENT_CALLS=1

# Then place more calls than pods × 1.
# Restore afterwards:
kubectl set env deployment/voice-agent-platform-ai-agent-worker \
  WORKER_MAX_CONCURRENT_CALLS=10
```

**Record:**
| Date | Calls rejected | HPA response time | Pass/Fail |
|---|---|---|---|

---

## 10. LiveKit node failure (drift)

**Spec §73 scenario:** A LiveKit node goes offline; SIP trunks and dispatch rules disappear.

**Expected behaviour:**
- Within one drift-check interval (300 s + jitter) drifted resources are detected.
- `api_livekit_drift_detected` counter increments; `alert: LiveKitConfigurationDrift` fires.
- The Synchronize action recreates the resources from PostgreSQL.
- New calls resume after re-sync.

```bash
# Simulate drift by deleting a dispatch rule directly in LiveKit:
livekit-cli delete-sip-dispatch-rule --id <rule-id>

# Wait for next drift check or force it:
kubectl exec deploy/voice-agent-platform-configuration-api \
  -- python -m app.cli detect-drift

# Check the console at /platform/livekit for "Configuration Drift Detected".
# Use the Repair button or:
kubectl exec deploy/voice-agent-platform-configuration-api \
  -- python -m app.cli sync-livekit
```

**Record:**
| Date | Drift detected in | Resources repaired in | Pass/Fail |
|---|---|---|---|

---

## 11. Network partition / high packet loss

**Spec §73 scenario:** 20 % packet loss on the media path.

**Expected behaviour:**
- LiveKit's WebRTC layer handles minor packet loss via NACK/FEC.
- STT transcription quality degrades slightly but calls continue.
- `alert: HighTimeToFirstAudio` may fire if retransmissions add > 1.5 s latency.

```bash
# Inject packet loss on a worker node with tc (Linux traffic control):
kubectl debug node/<node-name> --image=nicolaka/netshoot -it -- \
  tc qdisc add dev eth0 root netem loss 20%

# Restore:
kubectl debug node/<node-name> --image=nicolaka/netshoot -it -- \
  tc qdisc del dev eth0 root netem
```

**Record:**
| Date | Call drop rate | STT quality impact | Pass/Fail |
|---|---|---|---|

---

## 12. High CPU (worker node)

**Spec §73 scenario:** Worker node CPU > 90 %.

**Expected behaviour:**
- `alert: NodeHighCPU` fires.
- Active calls experience higher latency (LLM inference, TTS decode slower on a CPU-starved node).
- Kubernetes prefers to schedule new worker pods on less-loaded nodes (due to `podAntiAffinity`).

```bash
# Stress test a worker node (requires stress tool):
kubectl run cpu-stress --image=polinux/stress --restart=Never \
  --overrides='{"spec":{"nodeName":"<target-node>"}}' \
  -- stress --cpu 8 --timeout 120s
```

**Record:**
| Date | Call latency increase | Scheduler moved new pods | Pass/Fail |
|---|---|---|---|

---

## Summary table

Fill this in after each full run of the playbook.

| Scenario | Status | Last run | Notes |
|---|---|---|---|
| 1. Worker killed | PENDING | — | |
| 2. Worker drain | PENDING | — | |
| 3. API restart | PENDING | — | |
| 4. Rolling deploy | PENDING | — | |
| 5. Redis failure | PENDING | — | |
| 6. PostgreSQL failure | PENDING | — | |
| 7. STT circuit breaker | PENDING | — | |
| 8. LLM timeout | PENDING | — | |
| 9. Worker exhaustion | PENDING | — | |
| 10. LiveKit drift | PENDING | — | |
| 11. Packet loss | PENDING | — | |
| 12. High CPU | PENDING | — | |
