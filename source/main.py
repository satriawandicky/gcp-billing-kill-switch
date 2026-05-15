import base64
import json
import os
import urllib.request
import functions_framework
from google.cloud import billing_v1

PROJECT_ID       = os.environ.get('GCP_PROJECT_ID')
GCHAT_WEBHOOK    = os.environ.get('GCHAT_WEBHOOK_URL', '')
KILL_SWITCH_MODE = os.environ.get('KILL_SWITCH_MODE', 'sandbox').lower()
WARN_THRESHOLD   = 0.85  # fire warning between 85% and 100%


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
            'severity': 'WARNING',
            'message': warn_msg,
            'project_id': PROJECT_ID,
            'budget_name': budget_name,
            'ratio': ratio,
            'cost_amount': cost_amount,
            'budget_amount': budget_amount,
            'currency': currency,
        }))
        _notify_webhook(warn_msg, level='warning')
        return

    # ratio >= 1.0 — kill switch fires
    if KILL_SWITCH_MODE == 'customer':
        _notify_webhook(
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
            'severity': 'CRITICAL',
            'message': alert_msg,
            'project_id': PROJECT_ID,
            'budget_name': budget_name,
            'cost_amount': cost_amount,
            'budget_amount': budget_amount,
            'currency': currency,
        }))
        _notify_webhook(alert_msg, level='critical')

    except Exception as e:
        print(json.dumps({'severity': 'ERROR', 'message': f'ERROR disabling billing: {e}'}))
        raise


def _notify_webhook(text: str, level: str = 'critical'):
    """POST {text} to GCHAT_WEBHOOK_URL. Payload is compatible with both
    Google Chat and Slack incoming webhooks.
    """
    if not GCHAT_WEBHOOK:
        return
    prefix = {'warning': '🟡 *Budget Warning*', 'critical': '🔴 *Kill Switch*'}.get(level, '🔴 *Kill Switch*')
    payload = json.dumps({
        'text': f'{prefix}\n```{text}```'
    })
    req = urllib.request.Request(
        GCHAT_WEBHOOK,
        data=payload.encode(),
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    urllib.request.urlopen(req, timeout=5)
