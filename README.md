# gcp-billing-kill-switch

Claude Code skill to deploy a serverless GCP billing kill switch. Automatically disables project billing when a 100% budget alert fires. Uses Pub/Sub + Eventarc + Cloud Run Gen2.

## Install

```bash
git clone https://github.com/satriawandicky/gcp-billing-kill-switch.git
cp gcp-billing-kill-switch/commands/gcp-billing-kill-switch.md ~/.claude/commands/
```

## Usage

Type in Claude Code:
```
/gcp-billing-kill-switch
```

Provide:
- Project ID to protect
- Billing Account ID
- Region (default: `asia-southeast2`)
- Slack webhook URL (optional)

Claude will automatically:
1. Enable all required APIs
2. Create a dedicated service account with least-privilege IAM
3. Create Pub/Sub topic `budget-alerts`
4. Write and deploy Cloud Run Function (Python 3.12, Gen2)
5. Create Eventarc trigger
6. Run end-to-end test and verify logs
7. Re-link billing after test

Only one manual step remains: connecting the budget alert to Pub/Sub via GCP Console (GCP API limitation).

## Architecture

```
Budget Alert → Pub/Sub → Eventarc → Cloud Run → Cloud Billing API (disable)
```

## Cost

~$0/month — kill switch only runs when an alert fires.

> ⚠️ DESTRUCTIVE: Disabling billing stops all services in the project. Use only when going offline is safer than unlimited spend.
