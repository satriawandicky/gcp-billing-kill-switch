# gcp-billing-kill-switch

Claude Code skill to deploy a serverless GCP billing kill switch. Automatically disables project billing when a 100% budget alert fires. Uses Pub/Sub + Eventarc + Cloud Run Gen2.

Two deploy modes — both warn at 90–99% and disable billing at 100%. The only difference is one extra pre-kill notification:

| Ratio | `sandbox` (default) | `customer` |
|---|---|---|
| `< 90%` | no-op | no-op |
| `90–99%` | 🟡 Warning webhook + log (billing **untouched**) | 🟡 Warning webhook + log (billing **untouched**) |
| `≥ 100%` | 🔴 Disable billing + critical webhook | 🔴 Pre-kill "AUTO-KILL IMMINENT" webhook → disable billing + critical webhook |

Both modes **never cut billing below 100%**. Pick `customer` for live customer-facing projects where you want a heads-up alert moments before the kill API call fires; pick `sandbox` (default) elsewhere.

Alerts fan out to **Google Chat and Slack in parallel** — both webhook URLs are optional; setting one, the other, or both is supported. Each channel POST has its own error isolation so one broken channel never blocks the other.

Includes a dead-letter topic on the Eventarc subscription and an age alert so silent failures of the kill switch itself page someone within ~5 min.

> ⚠️ **DESTRUCTIVE**: Disabling billing stops all services in the project. Use only when going offline is safer than unlimited spend.

## Install Skill

```bash
git clone https://github.com/satriawandicky/gcp-billing-kill-switch.git
cp gcp-billing-kill-switch/commands/gcp-billing-kill-switch.md ~/.claude/commands/
```

Invoke in Claude Code:
```
/gcp-billing-kill-switch
```

Claude will prompt for the required variables below before proceeding.

---

## Required Variables

| Variable | Example | Description |
|---|---|---|
| `PROJECT_ID` | `my-project-prod` | GCP project to protect (billing will be disabled on breach) |
| `BILLING_ACCOUNT_ID` | `01E24F-97C25D-DB772B` | Billing account linked to the project (format: `XXXXXX-XXXXXX-XXXXXX`) |
| `REGION` | `asia-southeast2` | Cloud Run deployment region (default: `asia-southeast2`) |
| `BUDGET_AMOUNT` | `100` | Monthly budget cap — number only, no currency symbol |
| `CURRENCY_CODE` | `IDR` / `USD` / `GBP` | Must match the billing account currency |

### Optional Variables

| Variable | Example | Description |
|---|---|---|
| `KILL_SWITCH_MODE` | `sandbox` / `customer` | Both modes warn at 90–99% (no billing change) and kill at 100%. `customer` adds a pre-kill imminent webhook moments before the 100% kill API call fires. Webhook required to receive warnings. |
| `GCHAT_WEBHOOK_URL` | `https://chat.googleapis.com/v1/spaces/...` | Google Chat incoming webhook for warnings + kill alerts. Optional. |
| `SLACK_WEBHOOK_URL` | `https://hooks.slack.com/services/...` | Slack incoming webhook for warnings + kill alerts. Optional. Set alongside `GCHAT_WEBHOOK_URL` to fan out to both. |
| `ALERT_EMAIL` | `admin@example.com` | Email for Cloud Monitoring alert when kill switch fires |

> At least one webhook is strongly recommended — without either, warnings + kill alerts only land in Cloud Logging.

---

## Architecture

```mermaid
flowchart TD
    A([💸 GCP Spending]) -->|50% / 90% / 100% thresholds| B

    subgraph BUDGET ["Budget Alert"]
        B[📊 Cloud Billing Budget]
    end

    subgraph MESSAGING ["Event Pipeline (with DLQ)"]
        C[📨 Pub/Sub Topic\nbudget-alerts]
        D[⚡ Eventarc Trigger\n5 retries]
        DLQ[💀 Dead-Letter Topic\nbudget-alerts-dlq]
        DLQSUB[📥 Pull Sub\nbudget-alerts-dlq-sub]
    end

    subgraph FUNCTION ["Cloud Run Function Gen2 — billing-kill-switch"]
        E[🔧 kill_switch handler\nKILL_SWITCH_MODE = sandbox or customer]
        E0{ratio vs<br/>WARN_THRESHOLD<br/>0.90 / 1.00}
        EOK([✅ Budget OK\nearly return])
        EW[🟡 Warning notify\nBUDGET WARNING NN%\nNO billing change]
        EPRE[🔴 Pre-kill notify\nAUTO-KILL IMMINENT\ncustomer mode only]
        E2[🔴 Cloud Billing API\nupdateProjectBillingInfo\nbilling_account_name=]
        E3[🔴 Kill confirmed notify\nKILL SWITCH TRIGGERED]
    end

    subgraph NOTIFY_FN ["_notify_all fanout — per-channel try/except"]
        NF[📨 Build payload\nprefix + text]
        NG[POST GCHAT_WEBHOOK_URL]
        NS[POST SLACK_WEBHOOK_URL]
    end

    subgraph NOTIFICATIONS ["External Notification Endpoints"]
        FG[💬 Google Chat Space]
        FS[💬 Slack Channel]
        G[📧 Cloud Monitoring\nEmail Alert]
        DLQA[🚨 DLQ Age Alert\n>5 min unacked]
        H[📋 Cloud Logging\nstructured JSON\nnotify_channel ok or failed]
    end

    subgraph TARGET ["Protected Project"]
        I[🔒 GCP Project\nBilling DISABLED]
    end

    B -->|publishes event| C
    C --> D
    D -->|push| E
    D -->|after 5 fails| DLQ
    DLQ --> DLQSUB
    DLQSUB -->|oldest_unacked_message_age| DLQA
    DLQA --> NF

    E --> E0
    E0 -->|ratio < 0.90| EOK
    E0 -->|0.90 ≤ ratio < 1.00| EW
    E0 -->|ratio ≥ 1.00 customer| EPRE
    E0 -->|ratio ≥ 1.00 sandbox| E2
    EPRE --> NF
    EPRE --> E2
    EW --> NF
    E2 --> E3
    E2 -->|unlink billing account| I
    E3 --> NF

    NF --> NG
    NF --> NS
    NG -.->|status=200<br/>or 4xx/timeout<br/>logged not raised| FG
    NS -.->|status=200<br/>or 4xx/timeout<br/>logged not raised| FS
    NG --> H
    NS --> H
    E3 -->|log ingestion| H
    H -->|log-based alert| G

    style BUDGET fill:#fff3cd,stroke:#ffc107
    style MESSAGING fill:#d1ecf1,stroke:#17a2b8
    style FUNCTION fill:#d4edda,stroke:#28a745
    style NOTIFY_FN fill:#e2f0d9,stroke:#5cb85c
    style NOTIFICATIONS fill:#e2d9f3,stroke:#6f42c1
    style TARGET fill:#f8d7da,stroke:#dc3545
    style I fill:#f8d7da,stroke:#dc3545,color:#721c24
    style DLQ fill:#f8d7da,stroke:#dc3545
    style DLQA fill:#f8d7da,stroke:#dc3545
    style EW fill:#fff3cd,stroke:#ffc107
    style EPRE fill:#ffe1e0,stroke:#dc3545
    style E2 fill:#ffe1e0,stroke:#dc3545
    style E3 fill:#ffe1e0,stroke:#dc3545
```

**Reading the flow:**
- `ratio < 0.90` → early return, no notify, no kill (silent)
- `0.90 ≤ ratio < 1.00` → 🟡 warning fans out to both webhooks via `_notify_all`; **billing untouched** in both modes
- `ratio ≥ 1.00, customer mode` → 🔴 pre-kill "AUTO-KILL IMMINENT" notify fires **before** the Cloud Billing API call, so the team gets a heads-up even if the disable call hangs
- `ratio ≥ 1.00, sandbox mode` → 🔴 straight to billing disable, then kill-confirmed notify
- Each `_notify_all` POST is wrapped in its own `try/except`; per-channel outcomes (`notify_gchat ok status=200` / `notify_slack failed: ...`) land in Cloud Logging — one broken webhook never blocks the other

### Component Table

| # | Component | Technology | Role |
|---|---|---|---|
| 1 | Budget Alert | Cloud Billing | Publishes at 50% / 90% / 100% thresholds |
| 2 | Pub/Sub Topic | `budget-alerts` | Event broker (main) |
| 3 | Pub/Sub DLQ | `budget-alerts-dlq` | Captures messages after 5 failed deliveries |
| 4 | Pub/Sub DLQ Sub | `budget-alerts-dlq-sub` | Pull sub — exposes age metric for alerting |
| 5 | Eventarc Trigger | `budget-kill-trigger` | Routes Pub/Sub → Cloud Run (5 retries, DLQ on fail) |
| 6 | Cloud Run Function | Python 3.12, Gen2 | Kill switch logic with `sandbox`/`customer` modes |
| 7 | Cloud Billing API | `updateProjectBillingInfo` | Unlinks billing from project |
| 8 | Google Chat Webhook | Incoming Webhook | Warnings + kill alerts to a Chat space |
| 9 | Slack Webhook | Incoming Webhook | Warnings + kill alerts to a Slack channel — fires in parallel with Chat |
| 10 | Cloud Monitoring | Log-based + DLQ age alerts | Email on trigger, page on DLQ stuck |
| 11 | Secret Manager | `gchat-killswitch-webhook` + `slack-killswitch-webhook` | Stores webhook URLs securely (one secret per channel) |
| 12 | Cloud Logging | Structured JSON logs | Full audit trail (incl. per-channel `notify_{gchat,slack} ok/failed` status) |

## What the Skill Does (Fully Automated)

1. Enables all required GCP APIs
2. Creates dedicated Service Account with least-privilege IAM (per-project, never shared)
3. Creates Pub/Sub topic `budget-alerts` + dead-letter topic `budget-alerts-dlq` + pull sub
4. Writes and deploys Cloud Run Function (Python 3.12, Gen2) with chosen `KILL_SWITCH_MODE`
5. Creates Eventarc trigger and attaches DLQ + retry policy to the managed subscription
6. Creates DLQ age alert (pages on stuck messages = silent kill switch failure)
7. Stores webhooks in Secret Manager (`gchat-killswitch-webhook` for Google Chat, `slack-killswitch-webhook` for Slack — either or both)
8. Sets up Cloud Monitoring email alert
9. Runs end-to-end test and restores billing after test

**One manual step**: Connect budget alert to Pub/Sub via GCP Console (GCP API limitation for reseller sub-accounts).

---

## Source Code

See [`source/`](./source/) for the Cloud Run function files:
- `main.py` — kill switch logic with `sandbox` / `customer` modes, 90% warning branch, dual-channel (Google Chat + Slack) webhook fanout with per-channel error isolation, structured logging
- `requirements.txt` — Python dependencies
- `Procfile` — Cloud Run entry point (required for functions-framework)

---

## IAM Requirements

| Role | Level | Purpose |
|---|---|---|
| `roles/run.invoker` | Project | Allow Eventarc to trigger Cloud Run |
| `roles/viewer` | Project | Read project info |
| `roles/billing.projectManager` | Project | Unlink billing from project |
| `roles/billing.admin` | **Billing Account** ⚠️ | Manage billing account associations |
| `roles/pubsub.publisher` (on DLQ) | DLQ topic | Pub/Sub service agent — required for DLQ delivery |
| `roles/pubsub.subscriber` (on source topic) | `budget-alerts` | Pub/Sub service agent — required to ack DLQ-routed msgs |

> ⚠️ `roles/billing.admin` must be at **Billing Account** level. `roles/billing.projectManager` must be at **Project** level. Both are required — missing either causes 403.

> ⚠️ **NEVER share the `billing-killswitch-sa` across projects.** Each project gets its own SA (`billing-killswitch-sa@${PROJECT_ID}.iam.gserviceaccount.com`). Cross-project SA reuse = one compromised project can disable billing on all others.

---

## Notifications

### Chat/Slack Webhooks (Opsi 1)
Two secrets, two env vars — each channel is independent:

| Secret | Env Var | Payload |
|---|---|---|
| `gchat-killswitch-webhook` | `GCHAT_WEBHOOK_URL` | `{"text": "..."}` to a Google Chat space |
| `slack-killswitch-webhook` | `SLACK_WEBHOOK_URL` | `{"text": "..."}` to a Slack channel |

Both accept the same payload shape. Set one, the other, or both — the function fans out to whichever URLs are configured. Per-channel `try/except` means a 4xx or timeout on one channel is logged but never blocks the other; each POST emits `notify_{gchat,slack} ok status=...` or `failed: ...` for observability.

Behavior across modes:
- Both `sandbox` and `customer`: 🟡 warning fires at 90–99% (no billing change), 🔴 kill confirmation fires at 100% after billing is disabled.
- `customer` only: an extra 🔴 "AUTO-KILL IMMINENT" pre-kill webhook fires at 100% **before** the Cloud Billing API call — so the team has a heads-up even if the disable call hangs.

Swap a secret value without redeploying:
```bash
echo -n 'NEW_URL' | gcloud secrets versions add gchat-killswitch-webhook --data-file=- --project=PROJECT_ID
# or the Slack secret:
echo -n 'NEW_URL' | gcloud secrets versions add slack-killswitch-webhook --data-file=- --project=PROJECT_ID
gcloud run services update billing-kill-switch --region=REGION --project=PROJECT_ID
```

### DLQ Age Alert (Opsi 2 — silent failure detection)
Pull subscription `budget-alerts-dlq-sub` exposes `oldest_unacked_message_age` metric. Alert fires if a message stays in DLQ > 5 min — means the kill switch Cloud Run service failed 5 retries.

### Cloud Monitoring Email (Opsi 3)
Log-based alert detects `KILL SWITCH TRIGGERED` in Cloud Run logs → sends email.

> ℹ️ Email notification channel requires verification. When creating the channel, Google sends a code to the email address (e.g. `G-XXXXXX`). Use this to verify:
> ```bash
> curl -X POST \
>   "https://monitoring.googleapis.com/v3/projects/PROJECT_ID/notificationChannels/CHANNEL_ID:verify" \
>   -H "Authorization: Bearer $(gcloud auth print-access-token)" \
>   -H "Content-Type: application/json" \
>   -H "x-goog-user-project: PROJECT_ID" \
>   -d '{"code": "G-XXXXXX"}'
> ```

---

## Lessons Learned (from sandbox testing)

| Issue | Root Cause | Fix Applied |
|---|---|---|
| Cloud Run 503 on startup | `functions-framework` not found as entry point | Add `Procfile`: `web: functions-framework --target=kill_switch --signature-type=cloudevent` |
| 403 Forbidden on billing disable | SA missing `roles/billing.projectManager` at project level | Grant `roles/billing.projectManager` on project |
| Budget create via CLI fails `INVALID_ARGUMENT` | Reseller sub-account (IDR) doesn't support budget creation via API | Create budget manually via GCP Console |
| Email notification not received | Email notification channel not verified | Verify channel using verification code sent to email |
| Alert fires but no email | Cloud Monitoring email channel requires explicit verification even for email type | Call `:verify` endpoint with code from inbox |

---

## Manual Test

### Option A — Non-destructive 90% warning test (safe for production)

Publishes a synthetic 90% budget message. Expected: 🟡 warning fans out to both webhooks, billing remains `enabled=True`.

```bash
gcloud pubsub topics publish budget-alerts \
  --project=YOUR_PROJECT_ID \
  --message='{"budgetDisplayName":"TEST-90-WARN","alertThresholdExceeded":0.9,"costAmount":9.0,"budgetAmount":10.0,"currencyCode":"USD"}'
```

Check logs (wait ~15s for cold-start):
```bash
gcloud logging read \
  'resource.type="cloud_run_revision" AND resource.labels.service_name="billing-kill-switch"' \
  --project=YOUR_PROJECT_ID --limit=15 \
  --format='value(timestamp,severity,textPayload,jsonPayload.message,jsonPayload.channel)' \
  --order=desc
```

Expected lines:
```
Budget check: 9.0/10.0 USD (90%) mode=sandbox
BUDGET WARNING (90%)
  Budget 'TEST-90-WARN': 9.0/10.0 USD
  Project: YOUR_PROJECT_ID (mode=sandbox)
notify_gchat ok status=200    gchat
notify_slack ok status=200    slack
```

Verify billing stayed enabled:
```bash
gcloud beta billing projects describe YOUR_PROJECT_ID --format='value(billingEnabled)'
# Expected: True
```

### Option B — Full 100% kill test (sandbox only — causes real outage)

> ⚠️ This actually disables billing. All running services on the project will stop within minutes.

```bash
gcloud pubsub topics publish budget-alerts \
  --project=YOUR_PROJECT_ID \
  --message='{"budgetDisplayName":"TEST-KILL-SWITCH","costAmount":1000,"budgetAmount":100,"currencyCode":"USD"}'
```

Check logs:
```bash
gcloud run services logs read billing-kill-switch \
  --region=YOUR_REGION \
  --project=YOUR_PROJECT_ID \
  --limit=10
```

Expected:
```
KILL SWITCH TRIGGERED
Budget 'TEST-KILL-SWITCH': 1000 >= 100 USD
Billing DISABLED for: YOUR_PROJECT_ID
notify_gchat ok status=200
notify_slack ok status=200
```

Restore billing after test:
```bash
gcloud billing projects link YOUR_PROJECT_ID --billing-account=YOUR_BILLING_ACCOUNT_ID
```

---

## Notes for Reseller Sub-accounts (e.g. Elitery IDR)

- Budget creation via CLI/API is **not supported** — use GCP Console
- Use `currencyCode: "IDR"` in test messages
- `roles/billing.costsManager` must be granted to the user at billing account level to create budgets via Console

---

## Cost

~$0/month — Cloud Run, Pub/Sub, Eventarc, Secret Manager, and Cloud Logging all within free tier for this workload.
