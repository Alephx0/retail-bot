import asyncio
from datetime import datetime, timezone
from .account_consistency import PurchaseCooldown
from .amazon import Amazon, Attention, AuthenticationRequired, ChallengeDetected, BackoffRequired, AccessDenied, CartRejected
from .models import Group, Task, eligible, inputs, rejection_reasons, stock_observation, DOMAINS
from .store import now
from .adapters import CartService, CheckoutService
from .monitors import MonitorUnavailable, monitor_items
from .timing import jittered_sleep, exponential_backoff_with_jitter
from .performance import stage, timed, elapsed


class TaskRunner:
    def __init__(self, engine): self.engine=engine
    def __getattr__(self, key): return getattr(self.engine,key)

    async def hide_if_background(self, adapter, page):
        settings = self.store.get('settings', 'settings') or {}
        if isinstance(adapter, Amazon) and not settings.get('show_browser_window', False):
            await adapter.hide(page)

    @timed('Close account browser')
    async def close_context(self, adapter, context):
        await context.close()
        # Persistent profile owners close asynchronously on the context close
        # event. Drain those owners before another task reuses the account path.
        owner = getattr(context, '_retail_close_task', None)
        if owner:
            await asyncio.gather(owner, return_exceptions=True)

    async def wait_for_backoff(self, id, exc, event_count):
        """Back off without shortening a retailer's Retry-After instruction."""
        floor = 30 * (2 ** min(max(event_count - 1, 0), 3))
        delay = max(exc.retry_after_seconds or 0, min(900, floor))
        self.status(
            id,
            'backing_off',
            f'Retailer requested a cooldown (HTTP {exc.status}); next read-only inspection in {delay}s',
            rate_limit_status=exc.status,
            retry_after_seconds=delay,
            rate_limit_events=event_count,
        )
        await asyncio.sleep(delay)

    async def verify_account_session(self, id, adapter, context, account, page):
        """Verify or sign in on this connection, retaining it for manual review."""
        backoffs = 0
        while True:
            self.status(id, 'authenticating', 'Verifying account session; signing in if needed')
            try:
                await adapter.ensure_session(context, account, page)
                await self.hide_if_background(adapter, page)
                return self.store.get('accounts', account['id']) or account
            except BackoffRequired as exc:
                backoffs += 1
                await self.wait_for_backoff(id, exc, backoffs)
                if backoffs >= 3:
                    await self.pause(id, 'attention', 'Repeated retailer cooldowns during sign-in. Review the account connection before resuming.')
                    backoffs = 0
            except AccessDenied:
                await self.pause(id, 'attention', 'Amazon denied access during sign-in. Check the visible browser and account connection; Resume rechecks the session.')
            except Attention as exc:
                await self.pause(id, 'attention', str(exc))

    async def recover_authentication(self, id, adapter, context, account, page):
        """Keep this exact context alive; resume only after login is verified."""
        account = await self.verify_account_session(id, adapter, context, account, page)
        page._retail_agent_attempts = set()  # Fresh DOM after login.
        return account

    async def run(self, id):
        with self.engine.performance.scope('task', id):
            await self._run(id)

    async def _run(self, id):
        account_lock = None
        lock_acquired = False
        slot_acquired = False
        context = None
        monitors = []
        retain_context = False
        cart_attempted = False
        try:
            saved_task = self.store.get("tasks", id)
            task = {**saved_task, **Task.model_validate(saved_task).model_dump(mode="json")}
            saved_group = self.store.get("groups", task["group_id"])
            group = {**saved_group, **Group.model_validate(saved_group).model_dump()}
            if task.get('max_total') is not None:
                group['max_total'] = task['max_total']
            adapter=self.engine.adapter_for(group["retailer"]) if not task["simulation"] else None
            cart=CartService(adapter)
            checkout_service=CheckoutService(adapter)
            if not task["simulation"]:
                account_lock = self.account_locks.setdefault(task["account_id"], asyncio.Lock())
                if task['checkout_mode'] in ('review', 'automatic'):
                    policy_account = self.store.get('accounts', task['account_id'])
                    current = datetime.now(timezone.utc)
                    eligible_at = PurchaseCooldown(self.store).eligible_at(
                        task['account_id'], policy_account.get('purchase_cooldown_days', 0), current)
                    if eligible_at > current:
                        self.status(id, 'stopped', 'Account purchase cooldown: start again after ' + eligible_at.isoformat())
                        return
            items = monitor_items(self.store, group, task)
            account = self.store.get('accounts', task['account_id']) or {'region': 'US'}
            # Stock reads are independent of sign-in. Start them concurrently;
            # account verification still gates every purchasing action.
            monitors = self.monitors.subscribe(task, group, items, account['region'])
            seen_observations = {}
            startup_products = None
            session_mode = (self.store.get('settings', 'settings') or {}).get('session_verification_mode', 'http')
            @timed('Account preparation')
            async def prepare_account(start_item=None, *, preliminary=False):
                nonlocal account_lock, lock_acquired, slot_acquired, account, context
                start_item = start_item or items[0]
                account_lock=self.account_locks.setdefault(task["account_id"],asyncio.Lock())
                if account_lock.locked():self.status(id,"in_queue","Waiting for this account's active task to finish")
                with stage('Account lease wait'):
                    await account_lock.acquire()
                lock_acquired=True
                self.engine.check_pending_order(task['account_id'])
                if task['checkout_mode'] in ('review', 'automatic'):
                    policy_account = self.store.get('accounts', task['account_id'])
                    current = datetime.now(timezone.utc)
                    eligible_at = PurchaseCooldown(self.store).eligible_at(
                        task['account_id'], policy_account.get('purchase_cooldown_days', 0), current)
                    if eligible_at > current:
                        self.status(id, 'stopped', 'Account purchase cooldown: start again after ' + eligible_at.isoformat())
                        return False
                while task['account_id'] in getattr(adapter, 'logins', {}):
                    self.status(id, 'in_queue', 'Waiting for the account login browser to close')
                    login_context = adapter.logins[task['account_id']]
                    closed = asyncio.Event()
                    def on_closed(_):
                        closed.set()
                    login_context.on('close', on_closed)
                    try:
                        with stage('Waiting for login browser close'):
                            if adapter.logins.get(task['account_id']) is login_context:
                                await closed.wait()
                    finally:
                        login_context.remove_listener('close', on_closed)
                account = self.store.get("accounts", task["account_id"])
                account_proxy = account.get("proxy") or self.proxy(account.get("proxy_list_id", ""), account["id"])
                connection = account_proxy if task["use_account_proxy"] else self.proxy(task["proxy_id"], id)
                if preliminary and session_mode == 'http' and hasattr(adapter, 'probe_session'):
                    backoffs = 0
                    while True:
                        self.status(id, 'authenticating', 'Checking saved session without opening a browser')
                        try:
                            result = await adapter.probe_session(account, connection)
                            if result == 'authenticated':
                                self.status(id, 'ready', 'Saved session accepted; browser verification follows when stock is available')
                                return True
                            break  # Missing, expired or inconclusive proof requires the normal browser flow.
                        except BackoffRequired as exc:
                            backoffs += 1
                            await self.wait_for_backoff(id, exc, backoffs)
                            if backoffs >= 3:
                                await self.pause(id, 'attention', 'Repeated retailer cooldowns. Review the account connection before resuming.')
                                backoffs = 0
                        except AccessDenied:
                            await self.pause(id, 'attention', 'Amazon denied the session check. Review the account connection before resuming.')
                if self.browser_slots.locked():
                    self.status(id, 'in_queue', 'Waiting for an available browser worker')
                with stage('Browser worker wait'):
                    await self.browser_slots.acquire()
                slot_acquired=True
                browser_args = {'headless': True} if preliminary and session_mode == 'headless' else {}
                if account_proxy and account_proxy != connection:
                    self.status(id, 'authenticating', 'Preparing account session on its sign-in connection')
                    with stage('Sign-in browser setup'):
                        login_context = await adapter.context(account, account_proxy, task["solver_id"], **browser_args)
                    try:
                        login_page = await login_context.new_page()
                        await self.hide_if_background(adapter, login_page)
                        login_page._retail_start_url = f"https://{DOMAINS[account['region']]}/dp/{start_item['asin']}" if group['retailer'] == 'amazon' else None
                        self.pages[id] = [login_page]
                        account = await self.verify_account_session(id, adapter, login_context, account, login_page)
                    finally:
                        await self.close_context(adapter, login_context)
                self.status(id, 'authenticating', 'Opening account browser; session verification follows')
                with stage('Account browser setup'):
                    context = await adapter.context(account, connection, task["solver_id"], **browser_args)
                context._retail_task_id = id
                with stage('Account page setup'):
                    login_page = await context.new_page()
                await self.hide_if_background(adapter, login_page)
                login_page._retail_start_url = f"https://{DOMAINS[account['region']]}/dp/{start_item['asin']}" if group['retailer'] == 'amazon' else None
                self.pages[id] = [login_page]
                context._retail_task_login_page = login_page
                if hasattr(adapter, 'recovery'):
                    await adapter.recovery.install(login_page)
                account = await self.verify_account_session(id, adapter, context, account, login_page)
                self.status(id, 'ready', 'Account session verified')
                if browser_args and (self.store.get('settings', 'settings') or {}).get('show_browser_window', True):
                    await self.close_context(adapter, context)
                    context = None
                    self.pages.pop(id, None)
                return True

            # Give the concurrent first scan a short grace period. Closing an
            # already verified browser after just 100ms can force another full
            # launch/sign-in when stock arrives a fraction of a second later.
            # Out-of-stock returns immediately; slow scans release the slot.
            if not task['simulation']:
                if not await prepare_account(preliminary=True):
                    return
                try:
                    with stage('Initial stock handoff'):
                        startup_products = await asyncio.wait_for(self.monitors.observations(monitors, seen_observations),
                                                                  .1 if task['checkout_mode'] == 'monitor' else 1.0)
                except asyncio.TimeoutError:
                    pass
                retain_context = context is not None and task['checkout_mode'] != 'monitor' and any(
                    stock_observation(product)[0] == 'available' for product in startup_products or [])
            pages = []
            self.status(id,"waiting","Standby: waiting for monitor stock observations")
            attempts, successes = 0, 0
            rate_limit_events = 0
            seen_offers = set()
            next_monitor = 0
            while True:
                latest = self.store.get("groups", group["id"])
                group["delay_ms"] = latest.get("delay_ms", 4500)
                if not cart_attempted and not retain_context:
                    if context:
                        await self.diagnostics.finish_trace(id, context)
                        await self.close_context(adapter, context)
                        context = None
                    self.pages.pop(id, None)
                    if lock_acquired:
                        account_lock.release()
                        lock_acquired = False
                    if slot_acquired:
                        self.browser_slots.release()
                        slot_acquired = False
                retain_context = False
                # Keep the last stock/restriction explanation visible while
                # waiting for a new observation instead of immediately erasing it.
                if (self.store.get('tasks', id) or {}).get('status') != 'waiting':
                    self.status(id, "waiting", "Standby: waiting for restock from assigned monitor")
                checkout_page = None
                try:
                    products = startup_products
                    startup_products = None
                    if products is None:
                        with stage('Waiting for stock observation'):
                            products = await self.monitors.observations(monitors, seen_observations)
                    chosen = None
                    for index, (product, item) in enumerate(zip(products, items)):
                        if group.get("notify_offer") and product.get("offer_id") and product["offer_id"] not in seen_offers:
                            seen_offers.add(product["offer_id"])
                            self.store.event(id, "offer_found", f"Offer ID found for {item['asin']}")
                            await self.notify(task, f"Offer ID found for {item['asin']}")
                    # Shared anonymous stock is a signal to inspect the offer in
                    # the signed-in account, not permission to mutate a cart.
                    # Rotate across available inputs so one rejected offer cannot
                    # starve another assigned product.
                    for offset in range(len(products)):
                        index = (next_monitor + offset) % len(products)
                        if (eligible(products[index], items[index], group) if task['simulation']
                                else stock_observation(products[index])[0] == 'available'):
                            chosen = index
                            next_monitor = (index + 1) % len(products)
                            break
                    if chosen is None or task["checkout_mode"] == "monitor":
                        reasons = [f"{p['asin']}: " + '; '.join(rejection_reasons(p, i, group)) for p, i in zip(products, items) if rejection_reasons(p, i, group)]
                        message = ' | '.join(reasons)[:1600] or 'Monitor-only task: matching stock found; checkout is disabled for this task'
                        self.status(id, "waiting", message)
                        continue
                    self.status(id,"product_found","Stock detected; checking the assigned account offer")
                    elapsed('Stock signal age at task selection', monitors[chosen].observed_at)
                    product = products[chosen]
                    if not task['simulation'] and context is None:
                        if not await prepare_account(items[chosen]):
                            return
                        self.status(id, 'product_found', 'Rechecking stock in the account session')

                    checkout_page = None
                    if not task["simulation"]:
                        checkout_page = getattr(context, '_retail_task_login_page', None) or await context.new_page()
                        context._retail_task_login_page = None
                        if hasattr(adapter, 'recovery'):
                            await adapter.recovery.install(checkout_page)
                        if checkout_page not in pages:
                            await self.hide_if_background(adapter, checkout_page)
                        self.pages[id] = [*pages, checkout_page] if checkout_page not in pages else pages
                        # Recheck in the checkout session immediately before carting.
                        with stage('Account offer verification'):
                            fresh = await adapter.inspect(checkout_page, items[chosen], account["region"])
                        if not eligible(fresh, items[chosen], group, defer_unknown_seller=True):
                            self.status(id, 'waiting', 'Product changed before carting: ' + '; '.join(rejection_reasons(fresh, items[chosen], group)))
                            if checkout_page not in pages:
                                await checkout_page.close()
                            continue
                        product = fresh
                        checkout_page._retail_product_condition = product.get('condition', '')
                    attempts = 0
                    rate_limit_events = 0
                    total = round(product["price"] * task["quantity"], 2)
                    if total > group["max_total"]:
                        await self.pause(id, 'attention', f"Item subtotal {total:.2f} exceeds the order budget {group['max_total']:.2f}. Stop the task to change quantity or budget.")
                        if checkout_page and checkout_page not in pages:
                            await checkout_page.close()
                        await asyncio.sleep(group["delay_ms"] / 1000)
                        continue
                    seller_note = ' Seller is unreadable on the product page; it must be verified before order submission.' if product.get('seller') in ('Unknown', '') else ''
                    self.status(id, "carting", (f"Preparing Buy Now for {product['asin']}" if task.get('use_buy_now') else f"Adding {product['asin']} to cart") + seller_note)
                    if task["simulation"]:
                        await asyncio.sleep(0.7)
                        self.store.put("checkouts", {"at": now(), "task_id": id, "account_id":task["account_id"], "profile_id":task["profile_id"], "retailer":group["retailer"], "reference_price":product.get("original_price"), "unit_price":product.get("price"), "image":product.get("image",""), "asin": product["asin"], "title": product["title"], "quantity": task["quantity"], "total": total, "simulation": True, "status": "simulated", "currency": "USD"})
                        successes += 1
                        self.status(id, "completed", "Simulated checkout completed; no order placed")
                        if group["loop"] and successes < group["max_checkouts"]:
                            self.status(id,"ready","Preparing the next configured checkout")
                            await asyncio.sleep(group["delay_ms"] / 1000)
                            continue
                        break
                    try:
                        cart_attempted = True
                        used_buy_now = False
                        if task.get('use_buy_now') and group['retailer'] == 'amazon':
                            used_buy_now = await adapter.buy_now(checkout_page, task['quantity'], items[chosen]['asin'])
                        if used_buy_now:
                            quantity = task['quantity']
                        else:
                            with stage('Cart action and verification'):
                                quantity = await cart.add(checkout_page, items[chosen], task["quantity"])
                    except AuthenticationRequired:
                        raise
                    except CartRejected as exc:
                        cart_attempted=False
                        self.status(id,"error",str(exc),stage="Cart preflight")
                        await self.diagnostics.capture(task,checkout_page,"Cart preflight",str(exc))
                        break
                    except Exception as exc:
                        reason = str(exc) if isinstance(exc, (Attention, ValueError)) else type(exc).__name__
                        await self.pause(id, "attention", "Cart action failed: " + reason + ". Inspect the Amazon cart. Resume stops this task without another cart attempt.")
                        self.status(id, "stopped", "Cart result unverified; check Amazon before restarting")
                        break
                    self.status(id, "carted", "Cart verified; preparing checkout")
                    snapshot = None
                    if task["checkout_mode"] in ("automatic", "quote"):
                        self.status(id, "checkout", "Validating cart, quantity and final order total")
                        try:
                            with stage('Checkout review and total verification'):
                                snapshot = await checkout_service.review(checkout_page,items[chosen],quantity,group,{**task, 'use_buy_now': used_buy_now})
                        except AuthenticationRequired:
                            raise
                        except Attention as exc:
                            if task["checkout_mode"] == "quote":
                                self.status(id, "error", "Could not verify final checkout total: " + str(exc))
                                await self.diagnostics.capture(task, checkout_page, "Checkout quote", str(exc))
                                break
                            await self.pause(id, "review", str(exc) + ". Complete checkout manually, then Resume to check confirmation.")
                        if task["checkout_mode"] == "quote" and snapshot:
                            self.store.put("quotes", {**snapshot, "at": now(), "task_id": id, "account_id": account["id"], "retailer": group["retailer"], "status": "final_review"})
                            self.status(id, "completed", f"Final checkout total ${snapshot['total']:.2f} for {quantity} item(s); no order placed")
                            break
                        if snapshot:
                            self.store.put("submissions", {**snapshot, "task_id": id, "account_id": account["id"], "retailer": group["retailer"], "status": "submitting", "at": now()}, "submission-" + id)
                            self.status(id, "submitting", "Submitting order once; automatic retries disabled")
                            with stage('Submit order once'):
                                await checkout_service.submit(checkout_page)
                    else:
                        ready = 'Checkout ready via Buy Now' if used_buy_now else 'Cart ready'
                        await self.pause(id, "review", f"{ready} ({quantity} requested). Review shipping, tax and the {group['max_total']:.2f} budget in Amazon. Complete checkout there, then Resume to inspect the result.")
                    if account.get("cvv") and await adapter.payment_verification(checkout_page):
                        await adapter.verify_cvv(checkout_page, account["cvv"])
                    with stage('Verify order confirmation'):
                        order = await checkout_service.verify(checkout_page)
                    if not order:
                        await self.pause(id, "attention", "Order confirmation could not be verified. Check Your Orders before doing anything else. Resume stops this task without recording a success.")
                        self.status(id, "stopped", "Checkout unverified; check Amazon order history")
                        break
                    payment_pending = await adapter.payment_verification(checkout_page)
                    if payment_pending and task["auto_open_3ds"]:
                        if isinstance(adapter, Amazon):
                            await adapter.expose(checkout_page)
                        else:
                            await checkout_page.bring_to_front()
                    checkout = self.store.put("checkouts", {"at": now(), "task_id": id, "account_id":task["account_id"], "profile_id":task["profile_id"], "retailer":group["retailer"], "reference_price":product.get("original_price"), "unit_price":product.get("price"), "image":product.get("image",""), "asin": product["asin"], "title": product["title"], "quantity": quantity, "total": snapshot["total"] if snapshot else None, "simulation": False, "status": "payment_verification" if payment_pending else "confirmation_detected", "order_id": order, "currency": {"US": "USD", "UK": "GBP", "CA": "CAD"}[account["region"]], "retailer": group["retailer"]})
                    if snapshot:
                        self.store.put("submissions", {**snapshot, "task_id": id, "account_id": account["id"], "retailer": group["retailer"], "status": "confirmed", "order_id": order, "at": now()}, "submission-" + id)
                    while payment_pending:
                        await self.pause(id, "attention", "Order confirmation received, but payment verification is required. Complete the CVV or bank approval in the browser, then Resume. No new order will be submitted.")
                        payment_pending = await adapter.payment_verification(checkout_page)
                    if checkout["status"] == "payment_verification":
                        self.store.put("checkouts", {**checkout, "status": "confirmation_detected"})
                    self.status(id, "completed", "Amazon order confirmation detected; verify final details in Your Orders")
                    await self.notify({**task, "status": "completed"}, "Amazon order confirmation detected. Check Your Orders for payment verification and final details.")
                    successes += 1
                    if (self.store.get('accounts', account['id']) or {}).get('purchase_cooldown_days', 0):
                        self.status(id, 'completed', 'Order confirmed; account purchase cooldown prevents further loop orders')
                        break
                    if group["loop"] and successes < group["max_checkouts"]:
                        # Preserve each completed intent before resetting transient state.
                        journal=self.store.get("submissions","submission-"+id)
                        if journal: self.store.put("submissions",journal,"submission-"+id+"-"+str(successes))
                        cart_attempted=False
                        checkout_page._retail_agent_attempts = set()
                        self.status(id,"ready","Order confirmed; waiting for next configured checkout")
                        if checkout_page not in pages: await checkout_page.close()
                        await asyncio.sleep(group["delay_ms"]/1000)
                        continue
                    break
                except MonitorUnavailable as exc:
                    self.status(id, 'error', str(exc))
                    break
                except BackoffRequired as exc:
                    if cart_attempted:
                        await self.pause(id, "attention", "Rate limit or service backoff occurred after carting. Check Amazon order history before continuing.")
                        self.status(id, "stopped", "Checkout state needs review; no automatic retry was attempted")
                        break
                    rate_limit_events += 1
                    if rate_limit_events >= 3:
                        await self.pause(
                            id,
                            'attention',
                            'Repeated retailer rate limits/service backoffs were detected. Automatic polling is paused. '
                            'Wait before resuming; Resume performs a fresh read-only inspection first.',
                        )
                        rate_limit_events = 0
                        continue
                    await self.wait_for_backoff(id, exc, rate_limit_events)
                    continue
                except ChallengeDetected as exc:
                    if cart_attempted or self.store.get('submissions', 'submission-' + id):
                        await self.pause(id, 'attention', 'A retailer verification challenge appeared after checkout activity. Check Your Orders before continuing.')
                        self.status(id, 'stopped', 'Verification challenge after checkout; no automatic retry or solve attempted')
                        break
                    await self.pause(
                        id,
                        'attention',
                        str(exc) + ' Automatic challenge solving is disabled; Resume only after verification is complete.',
                    )
                    continue
                except AccessDenied:
                    if cart_attempted:
                        await self.pause(id, "attention", "Access denied after carting; inspect orders before continuing")
                        self.status(id, "stopped", "Checkout needs review")
                        break
                    await self.pause(id, 'attention', 'Amazon denied access. Automatic retry is disabled. Check the visible browser and connection; Resume performs a fresh inspection.')
                except AuthenticationRequired:
                    if self.store.get('submissions', 'submission-' + id):
                        await self.pause(id, 'attention', 'Authentication changed after an order submission was attempted. Check Your Orders; this task will not submit again.')
                        self.status(id, 'stopped', 'Order submission was not retried after authentication changed')
                        break
                    interrupted_page = checkout_page or pages[0]
                    account = await self.recover_authentication(id, adapter, interrupted_page.context, account, interrupted_page)
                    if interrupted_page.context is not context and account.get('session', {}).get('cookies'):
                        await context.add_cookies(account['session']['cookies'])
                    cart_attempted = False
                    if checkout_page and checkout_page not in pages:
                        await checkout_page.close()
                    retain_context = True
                    seen_observations.clear()
                    self.status(id, 'retrying', 'Signed in; re-inspecting product and cart before the interrupted step')
                    continue
                except Attention as exc:
                    if cart_attempted:
                        await self.pause(id, "attention", "Checkout state uncertain. Check Amazon order history. Resume stops this task without retrying.")
                        self.status(id, "stopped", "Review Amazon cart and orders before restarting")
                        break
                    await self.pause(id, "attention", str(exc))
                except Exception as exc:
                    if cart_attempted:
                        await self.pause(id, "attention", "Browser failed after carting. Check Amazon order history. Resume stops this task without retrying.")
                        self.status(id, "stopped", "Review Amazon cart and orders before restarting")
                        break
                    attempts += 1
                    if attempts >= group["max_errors"]:
                        self.status(id, "error", f"Stopped after {attempts} errors ({type(exc).__name__}). Inspect the browser and account before restarting.")
                        break
                    self.status(id, "retrying", f"Browser / network error ({type(exc).__name__}); retry {attempts}/{group['max_errors']}", attempt=attempts,max_attempts=group["max_errors"],retry_delay_ms=task["retry_delay_ms"])
                    await asyncio.sleep(min(60, task["retry_delay_ms"] / 1000 * 2 ** (attempts - 1)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.status(id, "error", str(exc) if isinstance(exc,ValueError) else f"Could not start task ({type(exc).__name__}). Check browser installation and account settings.")
        finally:
            await self.monitors.unsubscribe(id, monitors)
            if context:
                try:
                    await self.diagnostics.finish_trace(id,context)
                    await self.close_context(adapter, context)
                except Exception:
                    pass
            if lock_acquired: account_lock.release()
            if slot_acquired: self.browser_slots.release()
            self.jobs.pop(id, None)
            self.wakes.pop(id, None)
            self.pages.pop(id, None)
            self.live_view_locks.pop(('tasks', id), None)
            self.store.delete("harvesters", "harvester-" + id)
