import base64
import json
import os
import urllib.request
import functions_framework
from google.cloud import billing_v1

PROJECT_ID     = os.environ.get('GCP_PROJECT_ID')
GCHAT_WEBHOOK  = os.environ.get('GCHAT_WEBHOOK_URL', '')

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
        # Structured log — triggers Cloud Monitoring alert (Opsi 3)
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
        'text': (
            f'🚨 *GCP Billing Kill Switch Triggered*\n'
            f'```{text}```'
        )
    })
    req = urllib.request.Request(
        GCHAT_WEBHOOK,
        data=payload.encode(),
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    urllib.request.urlopen(req, timeout=5)
