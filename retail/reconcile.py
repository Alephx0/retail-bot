"""Reconcile an already-submitted order from saved, retailer-specific evidence."""
import argparse
import os
import re
from pathlib import Path

from .store import Store, now


def reconcile_saved_confirmation(store, diagnostic_id):
    evidence = store.get('diagnostics', diagnostic_id)
    if not evidence or evidence.get('action') != 'SUBMIT_ORDER' or evidence.get('url') != 'https://www.amazon.com/gp/buy/thankyou/handlers/display.html':
        raise ValueError('Not an Amazon order-confirmation diagnostic')
    task_id = evidence.get('task_id')
    journal = store.get('submissions', 'submission-' + task_id) if task_id else None
    task = store.get('tasks', task_id) if task_id else None
    reference = 'amazon-confirmed-' + diagnostic_id
    if not journal or journal.get('status') not in ('submitting', 'confirmed') or not task:
        raise ValueError('No matching unconfirmed order submission')
    if journal.get('account_id') != task.get('account_id') or journal.get('retailer') != 'amazon':
        raise ValueError('Submission account or retailer mismatch')
    snapshot = evidence.get('accessibility') or ''
    asin, quantity = journal.get('asin'), journal.get('quantity')
    if not asin or not isinstance(quantity, int) or quantity < 1:
        raise ValueError('Submitted item is incomplete')
    if not re.search(r'^\s*- heading "Order placed, thanks!"', snapshot, re.M):
        raise ValueError('Order success heading absent')
    if not re.search(rf'^\s*- link "[^"]+\b{quantity}":\s*\n\s*- /url: /dp/{re.escape(asin)}(?:\?|$)', snapshot, re.M):
        raise ValueError('Confirmed item or quantity does not match the submission')
    recorded = [c for c in store.all('checkouts') if c.get('task_id') == task_id and not c.get('simulation')]
    if recorded and (len(recorded) != 1 or recorded[0].get('order_id') != reference):
        raise ValueError('This task already has a different recorded purchase')
    if journal['status'] == 'confirmed' and journal.get('order_id') != reference:
        raise ValueError('Submission was confirmed from different evidence')
    if not recorded:
        store.put('checkouts', {'at': now(), 'task_id': task_id, 'account_id': task['account_id'],
                                'profile_id': task.get('profile_id', ''), 'retailer': 'amazon',
                                'asin': asin, 'quantity': quantity, 'total': journal.get('total'),
                                'currency': journal.get('currency', 'USD'), 'simulation': False,
                                'status': 'confirmation_detected', 'order_id': reference})
    if journal['status'] != 'confirmed':
        store.put('submissions', {**journal, 'status': 'confirmed', 'order_id': reference, 'at': now()}, journal['id'])
    if task.get('status') != 'completed':
        message = 'Amazon confirmation detected from saved evidence; verify details in Your Orders'
        store.put('tasks', {**task, 'status': 'completed', 'state': 'SUCCESS', 'message': message, 'updated_at': now()})
        store.put('task_events', {'task_id': task_id, 'group_id': task['group_id'], 'account_id': task['account_id'],
                                  'simulation': False, 'state': 'SUCCESS', 'previous_state': task.get('state', ''),
                                  'event': 'ORDER_CONFIRMED', 'message': message, 'at': now()})
        store.event(task_id, 'completed', message)
    return reference


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('diagnostic_id')
    parser.add_argument('--data-dir', type=Path, default=Path(os.environ.get('RETAIL_DATA', Path(__file__).resolve().parent.parent / 'data')))
    args = parser.parse_args()
    storage = Store(args.data_dir)
    try:
        print(reconcile_saved_confirmation(storage, args.diagnostic_id))
    finally:
        storage.db.close()
