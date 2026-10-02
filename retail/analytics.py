"""Analytics are projections of confirmed orders and checkout failure events."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal


def report(store, start, end, currency='USD', simulation=False):
    if start.tzinfo is None or end.tzinfo is None or end<=start:
        raise ValueError('Choose a valid timezone-aware start and end')
    if (end-start).days>3660: raise ValueError('Choose a range of at most ten years')
    orders=[]
    for order in store.all('checkouts'):
        if bool(order.get('simulation')) != simulation or order.get('currency','USD')!=currency: continue
        at=datetime.fromisoformat(order['at'])
        if start<=at<end: orders.append(order)
    successful=[o for o in orders if o['status'] in ('confirmation_detected','simulated')]
    failures=[e for e in store.all('task_events') if e.get('event')=='CHECKOUT_FAILED' and bool(e.get('simulation'))==simulation and start<=datetime.fromisoformat(e['at'])<end]
    spent=sum((Decimal(str(o['total'])) for o in successful if o.get('total') is not None),Decimal(0))
    saved=sum((max(Decimal(0),(Decimal(str(o['reference_price']))-Decimal(str(o['unit_price'])))*o['quantity']) for o in successful if o.get('reference_price') is not None and o.get('unit_price') is not None),Decimal(0))
    step=timedelta(hours=1) if end-start<=timedelta(days=2) else timedelta(days=1) if end-start<=timedelta(days=93) else timedelta(days=7)
    points=[]; cursor=start
    while cursor<end:
        next_time=min(cursor+step,end)
        bought=[o for o in successful if cursor<=datetime.fromisoformat(o['at'])<next_time]
        failed=[e for e in failures if cursor<=datetime.fromisoformat(e['at'])<next_time]
        points.append({'at':cursor.isoformat(),'success':len(bought),'failures':len(failed),'spent':round(sum(o.get('total') or 0 for o in bought),2),'saved':round(sum(max(0,(o.get('reference_price') or 0)-(o.get('unit_price') or 0))*o['quantity'] for o in bought if o.get('reference_price') is not None and o.get('unit_price') is not None),2)})
        cursor=next_time
    return {'spent':float(spent),'saved':float(saved),'checkouts':len(successful),'failures':len(failures),'unknown_totals':sum(o.get('total') is None for o in successful),'currency':currency,'points':points,'orders':sorted(orders,key=lambda x:x['at'],reverse=True)}
