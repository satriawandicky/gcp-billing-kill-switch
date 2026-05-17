# GCP Billing Kill Switch

Deploy a serverless GCP billing kill switch. Automatically disables project billing when a 100% budget alert fires. Uses Pub/Sub + Eventarc + Cloud Run Gen2. Fully automated — no manual steps except connecting the budget alert to Pub/Sub (GCP Console limitation for standard accounts; reseller sub-accounts can use REST API).

---

## Required Inputs (fill before running)

| Variable             | Example                   | Description                                                  |
|----------------------|---------------------------|--------------------------------------------------------------|
| `PROJECT_ID`         | `my-project-prod`         | GCP project to protect (billing will be disabled on breach)  |
| `BILLING_ACCOUNT_ID` | `01E24F-97C25D-DB772B`    | Billing account linked to the project                        |
| `REGION`             | `us-central1` / `asia-southeast2` | Cloud Run deployment region — auto-selected based on billing account currency (see Step 1) |
| `BUDGET_AMOUNT`      | `100`                     | Monthly budget cap in numbers only (no currency symbol)      |
| `CURRENCY_CODE`      | `USD` / `GBP` / `IDR`    | Must match billing account currency                          |
| `KILL_SWITCH_MODE`   | `sandbox` / `customer`    | Both modes warn at 90–99% (no billing change) and kill at 100%. `customer` adds a pre-kill imminent webhook moments before the 100% kill call fires. |
| `GCHAT_WEBHOOK_URL`  | `https://chat.googleapis.com/v1/spaces/...` | Google Chat incoming webhook for warnings + kill alerts. Optional. |
| `SLACK_WEBHOOK_URL`  | `https://hooks.slack.com/services/...` | Slack incoming webhook for warnings + kill alerts. Optional. Set alongside `GCHAT_WEBHOOK_URL` to fan out to both channels in parallel. |
| `ALERT_EMAIL`        | `admin@example.com`       | Email for Cloud Monitoring alert when kill switch fires (optional) |

> Collect and confirm all required variables with the user before proceeding to Step 2.

---

## Steps

### 1. Collect Input

Ask the user for:
- `PROJECT_ID` — GCP project to protect
- `BILLING_ACCOUNT_ID` — billing account linked to the project (format: `XXXXXX-XXXXXX-XXXXXX`)
- `BUDGET_AMOUNT` — monthly budget cap (number only, e.g. `100`)
- `KILL_SWITCH_MODE` — `sandbox` (default) or `customer`. Both warn at 90–99% and kill at 100%; `customer` adds a pre-kill imminent webhook before the 100% kill call.
- `GCHAT_WEBHOOK_URL` — Google Chat incoming webhook URL (optional)
- `SLACK_WEBHOOK_URL` — Slack incoming webhook URL (optional). At least one webhook (Chat or Slack — or both) is strongly recommended; without either, warnings only go to Cloud Logging.
- `ALERT_EMAIL` — email address for Cloud Monitoring notification (optional)

**Auto-resolve `REGION` and `CURRENCY_CODE` from billing account:**

```bash
gcloud billing accounts describe BILLING_ACCOUNT_ID --format="value(currencyCode)"
```

Apply this mapping:
| `CURRENCY_CODE` | Default `REGION` | Typical account |
|---|---|---|
| `IDR` | `asia-southeast2` | Elitery / Indonesian reseller |
| `USD` | `us-central1` | WALT Labs LLC / standard |
| `GBP` / `EUR` | `europe-west1` | WALT Labs EMEA |
| other | ask user | — |

Always show the resolved `REGION` and `CURRENCY_CODE` to the user for confirmation before proceeding.

Confirm all inputs with the user before proceeding.

### 2. Enable Required APIs

```bash
gcloud services enable \
  cloudbilling.googleapis.com \
  billingbudgets.googleapis.com \
  pubsub.googleapis.com \
  run.googleapis.com \
  eventarc.googleapis.com \
  cloudbuild.googleapis.com \
  secretmanager.googleapis.com \
  monitoring.googleapis.com \
  logging.googleapis.com \
  aiplatform.googleapis.com \
  generativelanguage.googleapis.com \
  cloudaicompanion.googleapis.com \
  notebooks.googleapis.com \
  language.googleapis.com \
  documentai.googleapis.com \
  vision.googleapis.com \
  texttospeech.googleapis.com \
  discoveryengine.googleapis.com \
  speech.googleapis.com \
  automl.googleapis.com \
  dialogflow.googleapis.com \
  translate.googleapis.com \
  videointelligence.googleapis.com \
  visionai.googleapis.com \
  recommendationengine.googleapis.com \
  --project=PROJECT_ID
```

### 3. Create Dedicated Service Account

> ⚠️ **ONE SERVICE ACCOUNT PER PROJECT — DO NOT REUSE ACROSS PROJECTS.**
> The skill creates `billing-killswitch-sa@${PROJECT_ID}.iam.gserviceaccount.com` fresh for each project. Never override `--service-account` with an SA from a different project (e.g. don't reuse `stewart-sandbox` SA for `walt-manager-copilot`). Cross-project SA = blast radius leak: one compromised project can disable billing on every project the shared SA has access to.

```bash
gcloud iam service-accounts create billing-killswitch-sa \
  --display-name="Billing Kill Switch SA" \
  --project=PROJECT_ID
```

Grant roles at Project level:
```bash
gcloud projects add-iam-policy-binding PROJECT_ID \
  --member="serviceAccount:billing-killswitch-sa@PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/run.invoker"

gcloud projects add-iam-policy-binding PROJECT_ID \
  --member="serviceAccount:billing-killswitch-sa@PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/viewer"

# Required to unlink billing at project level
gcloud projects add-iam-policy-binding PROJECT_ID \
  --member="serviceAccount:billing-killswitch-sa@PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/billing.projectManager"
```

Grant `roles/billing.admin` at **Billing Account** level (critical — must be billing account level, not project):
```bash
gcloud billing accounts add-iam-policy-binding BILLING_ACCOUNT_ID \
  --member="serviceAccount:billing-killswitch-sa@PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/billing.admin"
```

### 4. Create Pub/Sub Topic

```bash
gcloud pubsub topics create budget-alerts --project=PROJECT_ID
```

### 4b. Create Dead-Letter Topic + IAM

Eventarc's auto-created subscription has no DLQ by default — if Cloud Run keeps returning non-2xx (cold-start timeout, IAM drift, quota exceeded), the budget alert message is **silently dropped after 5 retries** and the kill switch never fires.

Create DLQ topic:
```bash
gcloud pubsub topics create budget-alerts-dlq --project=PROJECT_ID
```

Grant the Pub/Sub service agent permission to publish to the DLQ and ack on the source subscription:
```bash
PROJECT_NUMBER=$(gcloud projects describe PROJECT_ID --format='value(projectNumber)')
PUBSUB_SA="service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com"

gcloud pubsub topics add-iam-policy-binding budget-alerts-dlq \
  --member="serviceAccount:${PUBSUB_SA}" \
  --role="roles/pubsub.publisher" \
  --project=PROJECT_ID

gcloud pubsub topics add-iam-policy-binding budget-alerts \
  --member="serviceAccount:${PUBSUB_SA}" \
  --role="roles/pubsub.subscriber" \
  --project=PROJECT_ID
```

Create a pull subscription on the DLQ — needed to expose `oldest_unacked_message_age` metric for the alert in Step 8b:
```bash
gcloud pubsub subscriptions create budget-alerts-dlq-sub \
  --topic=budget-alerts-dlq \
  --message-retention-duration=7d \
  --project=PROJECT_ID
```

> A message landing in DLQ = the kill switch failed to process a budget alert. Step 8b alerts on this within ~5 min.

### 5. Write Source Code

```bash
mkdir -p /tmp/kill-switch-deploy
```

Write `/tmp/kill-switch-deploy/main.py` (copy verbatim from [`source/main.py`](../source/main.py)):
```python
import base64
import json
import os
import urllib.request
import functions_framework
from google.cloud import billing_v1

PROJECT_ID       = os.environ.get('GCP_PROJECT_ID')
GCHAT_WEBHOOK    = os.environ.get('GCHAT_WEBHOOK_URL', '')
SLACK_WEBHOOK    = os.environ.get('SLACK_WEBHOOK_URL', '')
KILL_SWITCH_MODE = os.environ.get('KILL_SWITCH_MODE', 'sandbox').lower()
WARN_THRESHOLD   = 0.90  # fire warning between 90% and 100% — under 90% is silent, 100% auto-kills


@functions_framework.cloud_event
def kill_switch(cloud_event):
    data = cloud_event.data.get('message', {}).get('data', '')
    if not data:
        print('No data in message, skipping.')
        return

    msg           = json.loads(base64.b64decode(data).decode('utf-8'))
    cost_amount   = msg.get('costAmount', 0)
    budget_amount = msg.get('budgetAmount', 0)
    currency      = msg.get('currencyCode', 'USD')
    budget_name   = msg.get('budgetDisplayName', 'unknown')
    ratio         = (cost_amount / budget_amount) if budget_amount else 0

    print(f'Budget check: {cost_amount}/{budget_amount} {currency} ({ratio:.0%}) mode={KILL_SWITCH_MODE}')

    if ratio < WARN_THRESHOLD:
        print('Budget OK, no action needed.')
        return

    if ratio < 1.0:
        warn_msg = (
            f"BUDGET WARNING ({ratio:.0%})\n"
            f"Budget '{budget_name}': {cost_amount}/{budget_amount} {currency}\n"
            f"Project: {PROJECT_ID} (mode={KILL_SWITCH_MODE})"
        )
        print(json.dumps({
            'severity': 'WARNING', 'message': warn_msg,
            'project_id': PROJECT_ID, 'budget_name': budget_name,
            'ratio': ratio, 'cost_amount': cost_amount,
            'budget_amount': budget_amount, 'currency': currency,
        }))
        _notify_all(warn_msg, level='warning')
        return

    # ratio >= 1.0 — kill switch fires
    if KILL_SWITCH_MODE == 'customer':
        _notify_all(
            f"AUTO-KILL IMMINENT for {PROJECT_ID} "
            f"(cost {cost_amount} >= budget {budget_amount} {currency})",
            level='critical',
        )

    client = billing_v1.CloudBillingClient()
    try:
        client.update_project_billing_info(
            name=f'projects/{PROJECT_ID}',
            project_billing_info=billing_v1.ProjectBillingInfo(
                billing_account_name=''
            )
        )
        alert_msg = (
            f'KILL SWITCH TRIGGERED\n'
            f"Budget '{budget_name}': {cost_amount} >= {budget_amount} {currency}\n"
            f'Billing DISABLED for: {PROJECT_ID}'
        )
        print(json.dumps({
            'severity': 'CRITICAL', 'message': alert_msg,
            'project_id': PROJECT_ID, 'budget_name': budget_name,
            'cost_amount': cost_amount, 'budget_amount': budget_amount,
            'currency': currency,
        }))
        _notify_all(alert_msg, level='critical')

    except Exception as e:
        print(json.dumps({'severity': 'ERROR', 'message': f'ERROR disabling billing: {e}'}))
        raise


def _notify_all(text: str, level: str = 'critical'):
    """Fan-out POST {text} to both GCHAT_WEBHOOK_URL and SLACK_WEBHOOK_URL.
    Payload `{"text": "..."}` is compatible with both Google Chat and Slack
    incoming webhooks. Per-channel failures are logged but never raised, so
    one broken channel cannot block the other.
    """
    prefix = {'warning': '🟡 *Budget Warning*', 'critical': '🔴 *Kill Switch*'}.get(level, '🔴 *Kill Switch*')
    payload = json.dumps({'text': f'{prefix}\n```{text}```'}).encode()
    for channel, url in (('gchat', GCHAT_WEBHOOK), ('slack', SLACK_WEBHOOK)):
        if not url:
            continue
        req = urllib.request.Request(
            url, data=payload,
            headers={'Content-Type': 'application/json'}, method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                print(json.dumps({
                    'severity': 'INFO',
                    'message': f'notify_{channel} ok status={resp.status}',
                    'channel': channel,
                }))
        except Exception as e:
            print(json.dumps({
                'severity': 'ERROR',
                'message': f'notify_{channel} failed: {e}',
                'channel': channel,
            }))
```

Write `/tmp/kill-switch-deploy/requirements.txt`:
```
functions-framework>=3.0.0
google-cloud-billing>=1.11.0
```

Write `/tmp/kill-switch-deploy/Procfile`:
```
web: functions-framework --target=kill_switch --signature-type=cloudevent
```

> ⚠️ The `Procfile` is required. Without it, Cloud Run Buildpacks will try to find a `app` object in `main.py` and fail with 503.

### 6. Store Chat/Slack Webhooks

Two independent secrets — store whichever you have. Function fans out to both at runtime if both env vars are set; either one alone also works.

| Secret name | Env var | URL shape |
|---|---|---|
| `gchat-killswitch-webhook` | `GCHAT_WEBHOOK_URL` | `https://chat.googleapis.com/v1/spaces/...` |
| `slack-killswitch-webhook` | `SLACK_WEBHOOK_URL` | `https://hooks.slack.com/services/...` |

Both accept `{"text": "..."}` payload. Each channel POST is wrapped in its own `try/except`, so a 4xx or timeout on one channel never blocks the other.

For P1 rollout: use Slack webhook from `#sre-killswitch-p1` channel plus the team's Google Chat space.

Create the Google Chat secret (skip if not using GChat):
```bash
echo -n 'GCHAT_WEBHOOK_URL_VALUE' | \
  gcloud secrets create gchat-killswitch-webhook \
  --data-file=- \
  --project=PROJECT_ID

gcloud secrets add-iam-policy-binding gchat-killswitch-webhook \
  --member="serviceAccount:billing-killswitch-sa@PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor" \
  --project=PROJECT_ID
```

Create the Slack secret (skip if not using Slack):
```bash
echo -n 'SLACK_WEBHOOK_URL_VALUE' | \
  gcloud secrets create slack-killswitch-webhook \
  --data-file=- \
  --project=PROJECT_ID

gcloud secrets add-iam-policy-binding slack-killswitch-webhook \
  --member="serviceAccount:billing-killswitch-sa@PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor" \
  --project=PROJECT_ID
```

Rotate a secret value without redeploying (Cloud Run picks up `:latest` on next cold start; service-update forces immediate refresh):
```bash
echo -n 'NEW_WEBHOOK_URL' | \
  gcloud secrets versions add gchat-killswitch-webhook \
  --data-file=- --project=PROJECT_ID
# same for slack-killswitch-webhook
gcloud run services update billing-kill-switch --region=REGION --project=PROJECT_ID
```

### 7. Deploy Cloud Run Function

**Choose `KILL_SWITCH_MODE`:**

Both modes emit a warning at 90–99% (no billing change) and disable billing at 100%. The difference: `customer` adds one extra "AUTO-KILL IMMINENT" webhook moments before the 100% kill call.

- `sandbox` (default) — 90–99% warning, 100% kill (no pre-kill alert). Use for non-customer-facing projects.
- `customer` — 90–99% warning, 100% pre-kill imminent webhook → kill. Use for live customer projects.

With both webhooks (recommended):
```bash
gcloud run deploy billing-kill-switch \
  --source=/tmp/kill-switch-deploy \
  --region=REGION \
  --service-account=billing-killswitch-sa@PROJECT_ID.iam.gserviceaccount.com \
  --set-env-vars=GCP_PROJECT_ID=PROJECT_ID,KILL_SWITCH_MODE=KILL_SWITCH_MODE \
  --set-secrets=GCHAT_WEBHOOK_URL=gchat-killswitch-webhook:latest,SLACK_WEBHOOK_URL=slack-killswitch-webhook:latest \
  --no-allow-unauthenticated \
  --max-instances=3 \
  --timeout=60 \
  --project=PROJECT_ID
```

With only Google Chat webhook:
```bash
gcloud run deploy billing-kill-switch \
  --source=/tmp/kill-switch-deploy \
  --region=REGION \
  --service-account=billing-killswitch-sa@PROJECT_ID.iam.gserviceaccount.com \
  --set-env-vars=GCP_PROJECT_ID=PROJECT_ID,KILL_SWITCH_MODE=KILL_SWITCH_MODE \
  --set-secrets=GCHAT_WEBHOOK_URL=gchat-killswitch-webhook:latest \
  --no-allow-unauthenticated \
  --max-instances=3 \
  --timeout=60 \
  --project=PROJECT_ID
```

With only Slack webhook:
```bash
gcloud run deploy billing-kill-switch \
  --source=/tmp/kill-switch-deploy \
  --region=REGION \
  --service-account=billing-killswitch-sa@PROJECT_ID.iam.gserviceaccount.com \
  --set-env-vars=GCP_PROJECT_ID=PROJECT_ID,KILL_SWITCH_MODE=KILL_SWITCH_MODE \
  --set-secrets=SLACK_WEBHOOK_URL=slack-killswitch-webhook:latest \
  --no-allow-unauthenticated \
  --max-instances=3 \
  --timeout=60 \
  --project=PROJECT_ID
```

Without any webhook (warnings only in Cloud Logging):
```bash
gcloud run deploy billing-kill-switch \
  --source=/tmp/kill-switch-deploy \
  --region=REGION \
  --service-account=billing-killswitch-sa@PROJECT_ID.iam.gserviceaccount.com \
  --set-env-vars=GCP_PROJECT_ID=PROJECT_ID,KILL_SWITCH_MODE=KILL_SWITCH_MODE \
  --no-allow-unauthenticated \
  --max-instances=3 \
  --timeout=60 \
  --project=PROJECT_ID
```

> ⚠️ Without at least one webhook, warnings only land in Cloud Logging — nobody gets paged. Strongly recommended for both modes.

### 8. Create Eventarc Trigger

```bash
gcloud eventarc triggers create budget-kill-trigger \
  --location=REGION \
  --service-account=billing-killswitch-sa@PROJECT_ID.iam.gserviceaccount.com \
  --destination-run-service=billing-kill-switch \
  --destination-run-region=REGION \
  --event-filters="type=google.cloud.pubsub.topic.v1.messagePublished" \
  --transport-topic=budget-alerts \
  --project=PROJECT_ID
```

Wait 2 minutes for trigger to become active before testing.

**Attach DLQ + retry policy to the Eventarc-managed subscription.** Eventarc creates the push subscription itself (auto-named `eventarc-REGION-budget-kill-trigger-sub-NNN`), so DLQ flags can only be set post-create:

```bash
# Wait for Eventarc to create the subscription
sleep 60

# Discover the auto-created subscription name (it pushes to Cloud Run)
SUB_NAME=$(gcloud pubsub subscriptions list \
  --filter="topic:projects/PROJECT_ID/topics/budget-alerts" \
  --format="value(name)" \
  --project=PROJECT_ID | head -1)

echo "Eventarc subscription: ${SUB_NAME}"

# Attach DLQ + retry policy
gcloud pubsub subscriptions update "${SUB_NAME}" \
  --dead-letter-topic=budget-alerts-dlq \
  --max-delivery-attempts=5 \
  --min-retry-delay=10s \
  --max-retry-delay=600s \
  --project=PROJECT_ID
```

Verify:
```bash
gcloud pubsub subscriptions describe "${SUB_NAME}" \
  --project=PROJECT_ID \
  --format="value(deadLetterPolicy.deadLetterTopic,deadLetterPolicy.maxDeliveryAttempts,retryPolicy.minimumBackoff,retryPolicy.maximumBackoff)"
```

Expected: `projects/PROJECT_ID/topics/budget-alerts-dlq	5	10s	600s`

### 8b. Setup DLQ Age Alert (P1 rollout requirement)

If a budget message lands in the DLQ, the kill switch failed silently. Alert on `oldest_unacked_message_age` on the DLQ subscription so we know within 5 minutes.

Requires Slack/Chat notification channel from Step 9 (`$SLACK_CHANNEL` or `$CHANNEL`). If neither set, skip and configure manually later.

```bash
# Pick whichever channel exists from Step 9 (email or webhook)
ALERT_CHANNEL="${SLACK_CHANNEL:-$CHANNEL}"

cat > /tmp/dlq-age-alert.json <<EOF
{
  "displayName": "Kill Switch DLQ — message stuck",
  "documentation": {
    "content": "A budget alert message landed in budget-alerts-dlq. This means the kill switch Cloud Run service failed to process it after 5 retries. Investigate: Cloud Run logs for billing-kill-switch, IAM on billing-killswitch-sa, billing.googleapis.com quota.",
    "mimeType": "text/markdown"
  },
  "conditions": [{
    "displayName": "DLQ message age > 5 min",
    "conditionThreshold": {
      "filter": "resource.type=\"pubsub_subscription\" AND resource.labels.subscription_id=\"budget-alerts-dlq-sub\" AND metric.type=\"pubsub.googleapis.com/subscription/oldest_unacked_message_age\"",
      "comparison": "COMPARISON_GT",
      "thresholdValue": 300,
      "duration": "60s",
      "aggregations": [{"alignmentPeriod": "60s", "perSeriesAligner": "ALIGN_MAX"}]
    }
  }],
  "alertStrategy": {"autoClose": "604800s"},
  "notificationChannels": ["${ALERT_CHANNEL}"],
  "combiner": "OR",
  "enabled": true
}
EOF

gcloud alpha monitoring policies create \
  --policy-from-file=/tmp/dlq-age-alert.json \
  --project=PROJECT_ID
```

> Test the DLQ path post-deploy: publish a malformed message (e.g. `{"costAmount":"not-a-number"}`) to `budget-alerts`. After 5 failed deliveries (~5 min), the message lands in DLQ and the alert fires.

### 9. Setup Cloud Monitoring Email Alert (if email provided)

Create notification channel:
```bash
CHANNEL=$(gcloud beta monitoring channels create \
  --display-name="Kill Switch Email Alert" \
  --type=email \
  --channel-labels=email_address=ALERT_EMAIL \
  --project=PROJECT_ID \
  --format="value(name)")
```

Create log-based alert policy using JSON:
```bash
cat > /tmp/kill-switch-alert-policy.json << EOF
{
  "displayName": "Kill Switch Triggered Alert",
  "documentation": {
    "content": "GCP Billing Kill Switch was triggered. Billing has been disabled for the project.",
    "mimeType": "text/markdown"
  },
  "conditions": [
    {
      "displayName": "Kill switch log detected",
      "conditionMatchedLog": {
        "filter": "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"billing-kill-switch\" AND jsonPayload.message=~\"KILL SWITCH TRIGGERED\""
      }
    }
  ],
  "alertStrategy": {
    "notificationRateLimit": {
      "period": "300s"
    }
  },
  "combiner": "OR",
  "enabled": true,
  "notificationChannels": ["CHANNEL_ID"]
}
EOF
sed -i "s|CHANNEL_ID|$CHANNEL|g" /tmp/kill-switch-alert-policy.json
gcloud alpha monitoring policies create \
  --policy-from-file=/tmp/kill-switch-alert-policy.json \
  --project=PROJECT_ID
```

**Verify the email channel** — Google sends a verification code to the email address. Ask the user to check their inbox for a code like `G-XXXXXX`, then verify:
```bash
TOKEN=$(gcloud auth print-access-token)
curl -s -X POST \
  "https://monitoring.googleapis.com/v3/projects/PROJECT_ID/notificationChannels/CHANNEL_NUMERIC_ID:verify" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "x-goog-user-project: PROJECT_ID" \
  -d '{"code": "G-XXXXXX"}'
```

Confirm `verificationStatus: "VERIFIED"` in the response.

### 10. Test Kill Switch

> ⚠️ **PRODUCTION WARNING**: This test **actually disables billing** and causes a full outage on the project. All running services (Cloud Run, VMs, GKE, Cloud SQL) will stop within minutes.
>
> **Ask the user explicitly before running this step:**
> - Is this a sandbox/non-critical project?
> - Are there any live workloads on this project right now?
>
> If the project is production or has live workloads, **skip to the non-destructive alternative below**.

**Option A — Full test (sandbox only, causes real outage):**

```bash
gcloud pubsub topics publish budget-alerts \
  --project=PROJECT_ID \
  --message='{"budgetDisplayName":"TEST-KILL-SWITCH","costAmount":1000,"budgetAmount":100,"currencyCode":"CURRENCY_CODE"}'
```

Wait ~20 seconds, then check logs:
```bash
gcloud run services logs read billing-kill-switch \
  --region=REGION \
  --project=PROJECT_ID \
  --limit=10
```

Verify billing is disabled:
```bash
gcloud beta billing projects describe PROJECT_ID --format="value(billingEnabled)"
```

Expected: `False`

Restore billing after test:
```bash
gcloud billing projects link PROJECT_ID --billing-account=BILLING_ACCOUNT_ID
```

**Option B — Non-destructive verification (safe for production):**

Verify the Cloud Run service is deployed and reachable (no trigger fired):
```bash
gcloud run services describe billing-kill-switch \
  --region=REGION \
  --project=PROJECT_ID \
  --format="value(status.url,status.conditions[0].status)"
```

Verify the Eventarc trigger exists and is active:
```bash
gcloud eventarc triggers describe budget-kill-trigger \
  --location=REGION \
  --project=PROJECT_ID \
  --format="value(name,transport.pubsub.subscription)"
```

Verify the budget is connected to Pub/Sub:
```bash
gcloud billing budgets list \
  --billing-account=BILLING_ACCOUNT_ID \
  --format="table(displayName,notificationsRule.pubsubTopic,amount.specifiedAmount.units)"
```

Expected: budget shows `projects/PROJECT_ID/topics/budget-alerts` as the Pub/Sub topic. This confirms the full chain is wired without triggering the kill switch.

### 11. Connect Budget Alert to Pub/Sub

> If `ALERT_EMAIL` was provided in Step 9, the `$CHANNEL` variable (Cloud Monitoring notification channel) should be included in the budget so threshold alerts (50%, 90%, 100%) also go directly to email — not just when the kill switch fires.

**For standard billing accounts** — via CLI:
```bash
ALL_SVC="services/C7E2-9256-1C43,services/AEFD-7695-64FA,services/719A-983F-202D,services/D73B-5EEA-8215,services/04C4-B046-D8B2,services/D870-408D-92A6,services/C08E-37B9-80D3,services/02DA-B362-D983,services/74B1-77CF-C302,services/63DE-82AB-F564,services/EDA4-10BF-88A3,services/FBC0-AA4A-C89A,services/1DB1-3CD3-35A3,services/8CD0-2A17-0B05,services/E5FE-878F-FECE,services/AF9A-5F4C-31E5"
# 16 services: Vertex AI, Gemini API, Duet AI, Notebooks, Natural Language, Document AI,
# Vision API, Text-to-Speech, Vertex AI Search, Speech API (STT), AutoML, Dialogflow,
# Translation, Video Intelligence, Vertex AI Vision, Recommendations AI

# With email notification (ALERT_EMAIL was provided in Step 9):
gcloud billing budgets create \
  --billing-account=BILLING_ACCOUNT_ID \
  --display-name="PROJECT_ID-killswitch-budget" \
  --filter-projects="projects/PROJECT_ID" \
  --filter-services="$ALL_SVC" \
  --budget-amount=BUDGET_AMOUNTCURRENCY_CODE \
  --threshold-rule=percent=0.5 \
  --threshold-rule=percent=0.9 \
  --threshold-rule=percent=1.0 \
  --notifications-rule-pubsub-topic=projects/PROJECT_ID/topics/budget-alerts \
  --all-updates-rule-monitoring-notification-channels=$CHANNEL \
  --project=PROJECT_ID

# Without email notification (ALERT_EMAIL not provided, omit the last flag):
gcloud billing budgets create \
  --billing-account=BILLING_ACCOUNT_ID \
  --display-name="PROJECT_ID-killswitch-budget" \
  --filter-projects="projects/PROJECT_ID" \
  --filter-services="$ALL_SVC" \
  --budget-amount=BUDGET_AMOUNTCURRENCY_CODE \
  --threshold-rule=percent=0.5 \
  --threshold-rule=percent=0.9 \
  --threshold-rule=percent=1.0 \
  --notifications-rule-pubsub-topic=projects/PROJECT_ID/topics/budget-alerts \
  --project=PROJECT_ID
```

**For reseller sub-accounts (e.g. Elitery IDR)** — use REST API directly (gcloud CLI omits `schemaVersion` which causes `INVALID_ARGUMENT`):
```bash
TOKEN=$(gcloud auth print-access-token)
PROJECT_NUMBER=$(gcloud projects describe PROJECT_ID --format="value(projectNumber)")

# With email notification (ALERT_EMAIL provided — include monitoringNotificationChannels):
curl -s -X POST \
  "https://billingbudgets.googleapis.com/v1/billingAccounts/BILLING_ACCOUNT_ID/budgets" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "x-goog-user-project: PROJECT_ID" \
  -d '{
    "displayName": "PROJECT_ID-killswitch-budget",
    "budgetFilter": {
      "projects": ["projects/'"$PROJECT_NUMBER"'"],
      "services": [
        "services/C7E2-9256-1C43", "services/AEFD-7695-64FA", "services/719A-983F-202D",
        "services/D73B-5EEA-8215", "services/04C4-B046-D8B2", "services/D870-408D-92A6",
        "services/C08E-37B9-80D3", "services/02DA-B362-D983", "services/74B1-77CF-C302",
        "services/63DE-82AB-F564", "services/EDA4-10BF-88A3", "services/FBC0-AA4A-C89A",
        "services/1DB1-3CD3-35A3", "services/8CD0-2A17-0B05", "services/E5FE-878F-FECE",
        "services/AF9A-5F4C-31E5"
      ],
      "creditTypesTreatment": "INCLUDE_ALL_CREDITS",
      "calendarPeriod": "MONTH"
    },
    "amount": {"specifiedAmount": {"currencyCode": "CURRENCY_CODE", "units": "BUDGET_AMOUNT"}},
    "thresholdRules": [
      {"thresholdPercent": 0.5},
      {"thresholdPercent": 0.9},
      {"thresholdPercent": 1.0}
    ],
    "notificationsRule": {
      "pubsubTopic": "projects/PROJECT_ID/topics/budget-alerts",
      "schemaVersion": "1.0",
      "monitoringNotificationChannels": ["CHANNEL_FULL_NAME"]
    }
  }'
```

> `CHANNEL_FULL_NAME` = full resource name from Step 9, e.g. `projects/PROJECT_ID/notificationChannels/CHANNEL_NUMERIC_ID`

> Without email: omit the `"monitoringNotificationChannels"` field entirely.

> ⚠️ Two bugs in `gcloud billing budgets create` affect reseller accounts:
> 1. The CLI never sends `schemaVersion: "1.0"` — the API rejects requests with `Invalid schema version: ""`
> 2. Reseller sub-accounts require `projects/PROJECT_NUMBER` (numeric), not `projects/PROJECT_ID` (string)
> Both must be fixed simultaneously; either one alone still fails.

**If REST API also fails** — use GCP Console:
1. Billing → Budgets & alerts → Edit/create budget
2. Manage notifications → Connect Pub/Sub topic → `projects/PROJECT_ID/topics/budget-alerts`
3. Under "Manage email notifications" → add `ALERT_EMAIL`
4. Ensure 100% threshold exists → Save

### 12. Final Summary

Print deployment summary table with all components and their status.

---

## Troubleshooting

| Error | Root Cause | Fix |
|---|---|---|
| Cloud Run 503 / `Failed to find attribute 'app'` | Missing `Procfile` for functions-framework | Add `Procfile: web: functions-framework --target=kill_switch --signature-type=cloudevent` |
| 403 on `updateBillingInfo` | SA missing `roles/billing.projectManager` at project level | `gcloud projects add-iam-policy-binding ... --role="roles/billing.projectManager"` |
| 403 on `updateBillingInfo` (billing account) | SA missing `roles/billing.admin` at billing account level | `gcloud billing accounts add-iam-policy-binding ...` |
| Budget CLI `INVALID_ARGUMENT: Invalid schema version: ""` | `gcloud` omits `schemaVersion: "1.0"` from `notificationsRule` | Use REST API with `"schemaVersion": "1.0"` explicitly |
| Budget CLI `INVALID_ARGUMENT` on reseller account | Reseller sub-account requires project number, not project ID string | Use `projects/PROJECT_NUMBER` in `budgetFilter.projects` |
| Email notification not received | Email channel not verified | Send verification code and call `:verify` endpoint |
| Function not triggered | Eventarc trigger not yet active | Wait 2 minutes after trigger creation |
| Billing not disabled after test | Wrong `GCP_PROJECT_ID` env var | Ensure it's Project ID string, not Project Number |
