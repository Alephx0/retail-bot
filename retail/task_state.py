"""Stable task states and event names independent of dashboard display labels."""
STATES = {
    'starting':('INITIALIZING','TASK_STARTED'),
    'authenticating':('AUTHENTICATING','AUTH_STARTED'),
    'ready':('READY','TASK_READY'),
    'in_queue':('IN_QUEUE','ACCOUNT_QUEUE_ENTERED'),
    'monitoring':('MONITORING','MONITOR_STARTED'),
    'product_found':('PRODUCT_FOUND','PRODUCT_AVAILABLE'),
    'waiting':('OUT_OF_STOCK','PRODUCT_UNAVAILABLE'),
    'carting':('CARTING','CART_ATTEMPT'),
    'carted':('CARTED','CART_SUCCESS'),
    'checkout':('CHECKOUT','CHECKOUT_STARTED'),
    'submitting':('PAYMENT_CONFIRMATION','ORDER_SUBMITTED'),
    'completed':('SUCCESS','ORDER_CONFIRMED'),
    'review':('MANUAL_ACTION_REQUIRED','MANUAL_ACTION_REQUIRED'),
    'attention':('MANUAL_ACTION_REQUIRED','MANUAL_ACTION_REQUIRED'),
    'retrying':('RETRY_WAIT','RETRY_SCHEDULED'),
    'error':('FAILED','TASK_FAILED'),
    'stopped':('STOPPED','TASK_STOPPED'),
}


def transition(previous, status):
    state,event=STATES.get(status,(status.upper(),status.upper()))
    # Terminal tasks can only restart through an explicit initialization. Loops
    # transition to READY after recording an order, before entering monitoring.
    if previous in ('FAILED','STOPPED') and state not in ('INITIALIZING','STOPPED','FAILED'):
        raise ValueError('A stopped task must be initialized before it can run')
    forward={
        'INITIALIZING':{'AUTHENTICATING','READY','IN_QUEUE'},
        'AUTHENTICATING':{'READY'},
        'IN_QUEUE':{'AUTHENTICATING','READY'},
        'READY':{'MONITORING','PRODUCT_FOUND','OUT_OF_STOCK'},
        'MONITORING':{'PRODUCT_FOUND','OUT_OF_STOCK'},
        'PRODUCT_FOUND':{'CARTING','OUT_OF_STOCK'},
        'CARTING':{'CARTED','SUCCESS'}, # simulations do not submit
        'CARTED':{'CHECKOUT','PAYMENT_CONFIRMATION','SUCCESS'},
        'CHECKOUT':{'PAYMENT_CONFIRMATION','SUCCESS'},
        'PAYMENT_CONFIRMATION':{'SUCCESS'},
        'SUCCESS':{'READY','INITIALIZING'},
        'OUT_OF_STOCK':{'MONITORING','READY'},
    }
    # A login challenge can interrupt any pre-submission browser step.
    branches={'FAILED','STOPPED','RETRY_WAIT','MANUAL_ACTION_REQUIRED','AUTHENTICATING'}
    if previous in forward and state!=previous and state not in forward[previous] and state not in branches:
        raise ValueError(f'Invalid task transition: {previous} → {state}')
    return state,event
