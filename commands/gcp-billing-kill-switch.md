# GCP Billing Kill Switch

Deploy a serverless kill switch that automatically disables GCP project billing when a 100% budget alert fires. Uses Pub/Sub + Eventarc + Cloud Run (Gen2). Fully automated — no manual steps except the final Budget Alert connection (Console-only limitation by GCP).

## Steps

### 1. Collect Input

Ask the user for:
- `PROJECT_ID` — GCP project to protect
- `BILLING_ACCOUNT_ID` — billing account linked to the project (format: `XXXXXX-XXXXXX-XXXXXX`)
- `REGION` — Cloud Run region (default: `asia-southeast2`)
- `SLACK_WEBHOOK_URL` — Slack webhook for notifications (optional, press Enter to skip)

Confirm inputs with the user before proceeding.

### 2. Enable Required APIs

```bash
gcloud services enable \
  cloudbilling.googleapis.com \
  pubsub.googleapis.com \
  run.googleapis.com \
  eventarc.googleapis.com \
  cloudbuild.googleapis.com \
  secretmanager.googleapis.com \
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
```

Grant `roles/billing.admin` at **Billing Account** level (critical):
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

Create a temp working directory and write the function files:

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
SLACK_WEBHOOK = os.environ.get('SLACK_WEBHOOK_URL', '')

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
        print(alert_msg)
        _notify_slack(alert_msg)
    except Exception as e:
        print(f'ERROR disabling billing: {e}')
        raise

def _notify_slack(text: str):
    if not SLACK_WEBHOOK:
        return
    payload = json.dumps({'text': f':rotating_light: *GCP Kill Switch*\n```{text}```'})
    req = urllib.request.Request(
        SLACK_WEBHOOK,
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

### 6. Deploy Cloud Run Function

If user provided a Slack webhook, store it in Secret Manager first:
```bash
echo -n 'SLACK_WEBHOOK_URL' | \
  gcloud secrets create slack-killswitch-webhook \
  --data-file=- \
  --project=PROJECT_ID

gcloud secrets add-iam-policy-binding slack-killswitch-webhook \
  --member="serviceAccount:billing-killswitch-sa@PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor" \
  --project=PROJECT_ID
```

Deploy (with Slack):
```bash
gcloud run deploy billing-kill-switch \
  --source=/tmp/kill-switch-deploy \
  --region=REGION \
  --service-account=billing-killswitch-sa@PROJECT_ID.iam.gserviceaccount.com \
  --set-env-vars=GCP_PROJECT_ID=PROJECT_ID \
  --set-secrets=SLACK_WEBHOOK_URL=slack-killswitch-webhook:latest \
  --no-allow-unauthenticated \
  --max-instances=3 \
  --timeout=60 \
  --project=PROJECT_ID
```

Deploy (without Slack):
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

### 7. Create Eventarc Trigger

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

### 8. Verify Deployment

Check Cloud Run service is running:
```bash
gcloud run services describe billing-kill-switch \
  --region=REGION \
  --project=PROJECT_ID \
  --format="value(status.conditions[0].status, status.url)"
```

Check Eventarc trigger is active:
```bash
gcloud eventarc triggers describe budget-kill-trigger \
  --location=REGION \
  --project=PROJECT_ID \
  --format="value(name, transport.pubsub.topic, destination.cloudRun.service)"
```

### 9. Test Kill Switch

Run a simulated budget exceeded event:
```bash
gcloud pubsub topics publish budget-alerts \
  --project=PROJECT_ID \
  --message='{"budgetDisplayName":"TEST-KILL-SWITCH","costAmount":1000,"budgetAmount":100,"currencyCode":"USD"}'
```

Wait 5 seconds, then check logs:
```bash
gcloud run services logs read billing-kill-switch \
  --region=REGION \
  --project=PROJECT_ID \
  --limit=20
```

Confirm log contains: `KILL SWITCH TRIGGERED`

Then re-link billing to restore:
```bash
gcloud billing projects link PROJECT_ID --billing-account=BILLING_ACCOUNT_ID
```

### 10. Connect Budget Alert to Pub/Sub (Manual — GCP Console only)

Inform the user that this final step must be done via Console (GCP does not support this via gcloud):

1. Go to: **Billing → Budgets & alerts**
2. Edit the existing budget (or create new)
3. Scroll to **Manage notifications**
4. Check **Connect a Pub/Sub topic to this budget**
5. Select: `projects/PROJECT_ID/topics/budget-alerts`
6. Ensure a **100% threshold** rule exists
7. Click **Save**

### 11. Final Summary

Print deployment summary:

| Component | Status |
|---|---|
| APIs enabled | ✅ |
| Service Account | ✅ billing-killswitch-sa@PROJECT_ID |
| Pub/Sub Topic | ✅ budget-alerts |
| Cloud Run Function | ✅ billing-kill-switch (REGION) |
| Eventarc Trigger | ✅ budget-kill-trigger |
| Slack Notification | ✅ / ⏭️ Skipped |
| Budget Alert Connection | ⚠️ Manual step required (Console) |

## Recovery

If kill switch fires and billing is disabled, re-enable with:
```bash
gcloud billing projects link PROJECT_ID --billing-account=BILLING_ACCOUNT_ID
```

## Troubleshooting

| Error | Fix |
|---|---|
| 403 on `updateBillingInfo` | `roles/billing.admin` must be at Billing Account level, not Project |
| Function not triggered | Verify Eventarc trigger and Pub/Sub topic name match exactly |
| Cloud Run deploy fails | Ensure `cloudbuild.googleapis.com` is enabled |
| Billing not disabled after test | Check `GCP_PROJECT_ID` env var is Project ID string, not Project Number |
