# GCP Billing Kill Switch

Deploy a serverless GCP billing kill switch. Automatically disables project billing when a 100% budget alert fires. Uses Pub/Sub + Eventarc + Cloud Run Gen2. Fully automated — no manual steps except connecting the budget alert to Pub/Sub (GCP Console limitation for standard accounts; reseller sub-accounts can use REST API).

---

## Required Inputs (fill before running)

| Variable             | Example                   | Description                                                  |
|----------------------|---------------------------|--------------------------------------------------------------|
| `PROJECT_ID`         | `my-project-prod`         | GCP project to protect (billing will be disabled on breach)  |
| `BILLING_ACCOUNT_ID` | `01E24F-97C25D-DB772B`    | Billing account linked to the project                        |
| `REGION`             | `asia-southeast2`         | Cloud Run deployment region (default: `us-central1`)         |
| `BUDGET_AMOUNT`      | `100`                     | Monthly budget cap in numbers only (no currency symbol)      |
| `CURRENCY_CODE`      | `USD` / `GBP` / `IDR`    | Must match billing account currency                          |
| `GCHAT_WEBHOOK_URL`  | `https://chat.googleapis.com/v1/spaces/...` | Google Chat webhook for kill switch alerts (optional) |
| `ALERT_EMAIL`        | `admin@example.com`       | Email for Cloud Monitoring alert when kill switch fires (optional) |

> Collect and confirm all required variables with the user before proceeding to Step 2.

---

## Steps

### 1. Collect Input

Ask the user for:
- `PROJECT_ID` — GCP project to protect
- `BILLING_ACCOUNT_ID` — billing account linked to the project (format: `XXXXXX-XXXXXX-XXXXXX`)
- `REGION` — Cloud Run region (default: `asia-southeast2`)
- `BUDGET_AMOUNT` — monthly budget cap (number only, e.g. `100`)
- `CURRENCY_CODE` — billing account currency (default: `USD`; use `IDR` for Elitery/Indonesian accounts)
- `GCHAT_WEBHOOK_URL` — Google Chat Space webhook URL (optional, press Enter to skip)
- `ALERT_EMAIL` — email address for Cloud Monitoring notification (optional)

Confirm inputs with the user before proceeding.

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
  --project=PROJECT_ID
```

### 3. Create Dedicated Service Account

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

### 5. Write Source Code

```bash
mkdir -p /tmp/kill-switch-deploy
```

Write `/tmp/kill-switch-deploy/main.py`:
```python
import base64
import json
import os
import urllib.request
import functions_framework
from google.cloud import billing_v1

PROJECT_ID    = os.environ.get('GCP_PROJECT_ID')
GCHAT_WEBHOOK = os.environ.get('GCHAT_WEBHOOK_URL', '')

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

    print(f'Budget check: {cost_amount}/{budget_amount} {currency}')

    if cost_amount < budget_amount:
        print('Budget OK, no action needed.')
        return

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
        # Structured log — triggers Cloud Monitoring alert
        print(json.dumps({
            'severity': 'CRITICAL',
            'message': alert_msg,
            'project_id': PROJECT_ID,
            'budget_name': budget_name,
            'cost_amount': cost_amount,
            'budget_amount': budget_amount,
            'currency': currency,
        }))
        _notify_gchat(alert_msg)

    except Exception as e:
        print(json.dumps({'severity': 'ERROR', 'message': f'ERROR disabling billing: {e}'}))
        raise


def _notify_gchat(text: str):
    if not GCHAT_WEBHOOK:
        return
    payload = json.dumps({
        'text': f'🚨 *GCP Billing Kill Switch Triggered*\n```{text}```'
    })
    req = urllib.request.Request(
        GCHAT_WEBHOOK,
        data=payload.encode(),
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    urllib.request.urlopen(req, timeout=5)
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

### 6. Store Google Chat Webhook (if provided)

```bash
echo -n 'GCHAT_WEBHOOK_URL' | \
  gcloud secrets create gchat-killswitch-webhook \
  --data-file=- \
  --project=PROJECT_ID

gcloud secrets add-iam-policy-binding gchat-killswitch-webhook \
  --member="serviceAccount:billing-killswitch-sa@PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor" \
  --project=PROJECT_ID
```

### 7. Deploy Cloud Run Function

With Google Chat:
```bash
gcloud run deploy billing-kill-switch \
  --source=/tmp/kill-switch-deploy \
  --region=REGION \
  --service-account=billing-killswitch-sa@PROJECT_ID.iam.gserviceaccount.com \
  --set-env-vars=GCP_PROJECT_ID=PROJECT_ID \
  --set-secrets=GCHAT_WEBHOOK_URL=gchat-killswitch-webhook:latest \
  --no-allow-unauthenticated \
  --max-instances=3 \
  --timeout=60 \
  --project=PROJECT_ID
```

Without Google Chat:
```bash
gcloud run deploy billing-kill-switch \
  --source=/tmp/kill-switch-deploy \
  --region=REGION \
  --service-account=billing-killswitch-sa@PROJECT_ID.iam.gserviceaccount.com \
  --set-env-vars=GCP_PROJECT_ID=PROJECT_ID \
  --no-allow-unauthenticated \
  --max-instances=3 \
  --timeout=60 \
  --project=PROJECT_ID
```

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

### 11. Connect Budget Alert to Pub/Sub

**For standard billing accounts** — via CLI:
```bash
ALL_SVC="services/C7E2-9256-1C43,services/AEFD-7695-64FA,services/719A-983F-202D,services/D73B-5EEA-8215,services/04C4-B046-D8B2,services/D870-408D-92A6,services/C08E-37B9-80D3,services/02DA-B362-D983,services/74B1-77CF-C302,services/63DE-82AB-F564,services/EDA4-10BF-88A3,services/FBC0-AA4A-C89A,services/1DB1-3CD3-35A3,services/8CD0-2A17-0B05,services/E5FE-878F-FECE,services/AF9A-5F4C-31E5"
# 16 services: Vertex AI, Gemini API, Duet AI, Notebooks, Natural Language, Document AI,
# Vision API, Text-to-Speech, Vertex AI Search, Speech API (STT), AutoML, Dialogflow,
# Translation, Video Intelligence, Vertex AI Vision, Recommendations AI

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
      "schemaVersion": "1.0"
    }
  }'
```

> ⚠️ Two bugs in `gcloud billing budgets create` affect reseller accounts:
> 1. The CLI never sends `schemaVersion: "1.0"` — the API rejects requests with `Invalid schema version: ""`
> 2. Reseller sub-accounts require `projects/PROJECT_NUMBER` (numeric), not `projects/PROJECT_ID` (string)
> Both must be fixed simultaneously; either one alone still fails.

**If REST API also fails** — use GCP Console:
1. Billing → Budgets & alerts → Edit/create budget
2. Manage notifications → Connect Pub/Sub topic → `projects/PROJECT_ID/topics/budget-alerts`
3. Ensure 100% threshold exists → Save

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
