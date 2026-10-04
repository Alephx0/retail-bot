import asyncio
from datetime import datetime, timezone
from .account_consistency import PurchaseCooldown
from .amazon import Amazon, Attention, AuthenticationRequired, ChallengeDetected, BackoffRequired, AccessDenied, CartRejected
from .models import Group, Task, eligible, inputs, rejection_reasons, DOMAINS
from .store import now
from .adapters import MonitorService, CartService, CheckoutService
from .timing import jittered_sleep, exponential_backoff_with_jitter


class TaskRunner:
    def __init__(self, engine): self.engine=engine
    def __getattr__(self, key): return getattr(self.engine,key)

    async def hide_if_background(self, adapter, page):
        settings = self.store.get('settings', 'settings') or {}
        if isinstance(adapter, Amazon) and not settings.get('show_browser_window', False):
            await adapter.hide(page)

    async def wait_for_backoff(self, id, exc, event_count):
        """Apply a bounded cooldown without consuming the generic error budget."""
        floor = 30 * (2 ** min(max(event_count - 1, 0), 3))
        delay = min(900, max(exc.retry_after_seconds or 0, floor))
        self.status(
            id,
            'backing_off',
            f'Retailer requested a cooldown (HTTP {exc.status}); next read-only inspection in {delay}s',
            rate_limit_status=exc.status,
            retry_after_seconds=delay,
            rate_limit_events=event_count,
        )
        await asyncio.sleep(delay)

    async def recover_authentication(self, id, adapter, context, account, page):
        """Keep this exact context alive; resume only after login is verified."""
        while True:
            self.status(id, 'authenticating', 'Rechecking the interrupted account session')
            try:
                await adapter.ensure_session(context, account, page)
                page._retail_agent_attempts = set()  # Fresh DOM after login.
                await self.hide_if_background(adapter, page)
                return self.store.get('accounts', account['id']) or account
            except Attention as exc:
                await self.pause(id, 'attention', str(exc) + ' Complete verification in View Browser, then Resume; the interrupted step will be rechecked.')

    async def run(self, id):
        account_lock = None
        lock_acquired = False
        slot_acquired = False
        context = None
        monitor_context = None
        cart_attempted = False
        try:
            saved_task = self.store.get("tasks", id)
            task = {**saved_task, **Task.model_validate(saved_task).model_dump(mode="json")}
            saved_group = self.store.get("groups", task["group_id"])
            group = {**saved_group, **Group.model_validate(saved_group).model_dump()}
            adapter=self.engine.adapter_for(group["retailer"]) if not task["simulation"] else None
            monitor=MonitorService(adapter)
            cart=CartService(adapter)
            checkout_service=CheckoutService(adapter)
            if not task["simulation"]:
                account_lock=self.account_locks.setdefault(task["account_id"],asyncio.Lock())
                if account_lock.locked():self.status(id,"in_queue","Waiting for this account's active task to finish")
                await account_lock.acquire()
                lock_acquired=True
                if task['checkout_mode'] in ('review', 'automatic'):
                    policy_account = self.store.get('accounts', task['account_id'])
                    current = datetime.now(timezone.utc)
                    eligible_at = PurchaseCooldown(self.store).eligible_at(
                        task['account_id'], policy_account.get('purchase_cooldown_days', 0), current)
                    if eligible_at > current:
                        self.status(id, 'stopped', 'Account purchase cooldown: start again after ' + eligible_at.isoformat())
                        return
                if self.browser_slots.locked():
                    self.status(id, 'in_queue', 'Waiting for an available browser worker')
                await self.browser_slots.acquire()
                slot_acquired=True
            products_text = self.store.get("input_lists", group["input_list_id"])["products"] if group["input_list_id"] else group["products"]
            items = inputs(products_text) if group["retailer"] == "amazon" else [{"asin": line.strip(), "max_price": group["max_price"], "offer_id": ""} for line in products_text.splitlines() if line.strip()]
            if group.get("offer_id"):
                for item in items:
                    item["offer_id"] = item["offer_id"] or group["offer_id"]
            if group.get("skip_monitoring") and any(not i.get("offer_id") for i in items):
                raise ValueError("Skip Monitoring requires an ASIN and Offer ID for every input")
            pages = []
            if not task["simulation"]:
                account = self.store.get("accounts", task["account_id"])
                account_proxy = account.get("proxy") or self.proxy(account.get("proxy_list_id", ""), account["id"])
                connection = account_proxy if task["use_account_proxy"] else self.proxy(task["proxy_id"], id)
                if account_proxy and account_proxy != connection:
                    login_context = await adapter.context(account, account_proxy, task["solver_id"])
                    try:
                        login_page = await login_context.new_page()
                        await self.hide_if_background(adapter, login_page)
                        login_page._retail_start_url = f"https://{DOMAINS[account['region']]}/dp/{items[0]['asin']}" if group['retailer'] == 'amazon' else None
                        self.pages[id] = [login_page]
                        while True:
                            try:
                                await adapter.ensure_session(login_context, account, login_page)
                                break
                            except AccessDenied:
                                await self.pause(id, 'attention', 'Amazon denied access during sign-in. Check the visible browser and account connection; Resume rechecks the session.')
                            except Attention as exc:
                                await self.pause(id, "attention", str(exc))
                        account = self.store.get("accounts", account["id"])
                    finally:
                        await login_context.close()
                context = await adapter.context(account, connection, task["solver_id"])
                context._retail_task_id = id
                login_page = await context.new_page()
                await self.hide_if_background(adapter, login_page)
                login_page._retail_start_url = f"https://{DOMAINS[account['region']]}/dp/{items[0]['asin']}" if group['retailer'] == 'amazon' else None
                self.pages[id] = [login_page]
                while True:
                    self.status(id, "authenticating", "Preparing account session")
                    try:
                        await adapter.ensure_session(context, account, login_page)
                        account = self.store.get("accounts", account["id"])
                        await self.hide_if_background(adapter, login_page)
                        break
                    except AccessDenied:
                        await self.pause(id, 'attention', 'Amazon denied access during sign-in. Check the visible browser and account connection; Resume rechecks the session.')
                    except Attention as exc:
                        await self.pause(id, "attention", str(exc))
                monitor_context = await adapter.context(account, self.proxy(group["monitor_proxy_id"], id), task["solver_id"]) if group["monitor_proxy_id"] else context
                monitor_context._retail_task_id = id
                if monitor_context is context:
                    pages = [login_page] + [await monitor_context.new_page() for _ in items[1:]]
                    for page in pages[1:]:
                        await self.hide_if_background(adapter, page)
                else:
                    await login_page.close()
                    pages = [await monitor_context.new_page() for _ in items]
                    for page in pages:
                        await self.hide_if_background(adapter, page)
                self.pages[id] = pages
            self.status(id,"ready","Account session and inputs are ready")
            attempts, successes = 0, 0
            rate_limit_events = 0
            seen_offers = set()
            while True:
                latest = self.store.get("groups", group["id"])
                group["delay_ms"] = latest.get("delay_ms", 4500)
                self.status(id, "ready" if group.get("skip_monitoring") else "monitoring", "Validating supplied offers" if group.get("skip_monitoring") else "Checking product availability")
                checkout_page = None
                try:
                    if task["simulation"]:
                        await asyncio.sleep(1)
                        products = [{"asin": item["asin"], "title": f"Simulation product Â· {item['asin']}", "price": 0 if group["mode"] == "deals" and group["only_freebies"] else min(29.99, item["max_price"] if item["max_price"] is not None else group["max_price"] if group["max_price"] is not None else 29.99), "original_price": 100, "offer_id": item["offer_id"], "amazon_seller": True, "seller": "Amazon (simulation)", "condition": "new", "available": True} for item in items]
                    else:
                        if group.get("skip_monitoring"):
                            # One cart-validation observation instead of a monitor fan-out.
                            direct = await adapter.inspect(pages[0],items[0],account['region'])
                            from .adapters import MonitorEvent
                            observations = [MonitorEvent(group['retailer'],items[0]['asin'],direct.get('offer_id',''),direct.get('seller',''),direct.get('price'),'available' if direct.get('available') else 'unavailable',now(),direct)]
                        else:
                            observations = await monitor.scan(pages,items,account['region'],group['retailer'],group['monitor_concurrency'])
                        failures = [x for x in observations if isinstance(x,BaseException)]
                        # Never hide a challenge/rate-limit/access-denied signal
                        # just because another concurrent product page succeeded.
                        operational = next(
                            (x for x in failures if isinstance(
                                x, (ChallengeDetected, BackoffRequired, AccessDenied, AuthenticationRequired)
                            )),
                            None,
                        )
                        if operational:
                            raise operational
                        if len(failures)==len(observations): raise failures[0]
                        products = [x.observation if not isinstance(x,BaseException) else {"asin":item['asin'],"title":"Observation unavailable","price":None,"available":False,"seller":"Unknown","condition":"unknown","offer_id":""} for x,item in zip(observations,items)]
                    attempts = 0
                    rate_limit_events = 0
                    chosen = None
                    for index, (product, item) in enumerate(zip(products, items)):
                        self.store.put("feed", dict(product, at=now(), simulation=task["simulation"], group_id=group["id"], retailer=group["retailer"]), f"{id}-{item['asin']}")
                        if group.get("notify_offer") and product.get("offer_id") and product["offer_id"] not in seen_offers:
                            seen_offers.add(product["offer_id"])
                            self.store.event(id, "offer_found", f"Offer ID found for {item['asin']}")
                            await self.notify(task, f"Offer ID found for {item['asin']}")
                        if chosen is None and eligible(product, item, group, defer_unknown_seller=task['checkout_mode'] != 'monitor'):
                            chosen = index
                    if chosen is None or task["checkout_mode"] == "monitor":
                        reasons = [f"{p['asin']}: " + '; '.join(rejection_reasons(p, i, group)) for p, i in zip(products, items) if rejection_reasons(p, i, group)]
                        message = ' | '.join(reasons)[:1600] or 'Monitor-only task: matching stock found; checkout is disabled for this task'
                        if task['checkout_mode'] != 'monitor' and any(p.get('available') or p.get('availability_status') == 'unknown' for p in products):
                            await self.pause(id, 'attention', message + '. Stop the task to adjust group filters, or inspect the browser and Resume.')
                            continue
                        self.status(id, "waiting", message)
                        await asyncio.sleep(group["delay_ms"] / 1000)
                        continue
                    self.status(id,"product_found","Eligible product found")
                    product = products[chosen]
                    checkout_page = None
                    if not task["simulation"]:
                        checkout_page = await context.new_page() if monitor_context is not context else pages[chosen]
                        if checkout_page not in pages:
                            await self.hide_if_background(adapter, checkout_page)
                        self.pages[id] = [*pages, checkout_page] if checkout_page not in pages else pages
                        # Recheck in the checkout session immediately before carting.
                        fresh = await adapter.inspect(checkout_page, items[chosen], account["region"])
                        if not eligible(fresh, items[chosen], group, defer_unknown_seller=True):
                            await self.pause(id, 'attention', 'Product changed before carting: ' + '; '.join(rejection_reasons(fresh, items[chosen], group)))
                            if checkout_page not in pages:
                                await checkout_page.close()
                            await asyncio.sleep(group["delay_ms"] / 1000)
                            continue
                        product = fresh
                        checkout_page._retail_product_condition = product.get('condition', '')
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
                            await checkout_service.submit(checkout_page)
                    else:
                        ready = 'Checkout ready via Buy Now' if used_buy_now else 'Cart ready'
                        await self.pause(id, "review", f"{ready} ({quantity} requested). Review shipping, tax and the {group['max_total']:.2f} budget in Amazon. Complete checkout there, then Resume to inspect the result.")
                    if account.get("cvv") and await adapter.payment_verification(checkout_page):
                        await adapter.verify_cvv(checkout_page, account["cvv"])
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
            if monitor_context and monitor_context is not context:
                try:
                    await self.diagnostics.finish_trace(id,monitor_context)
                    await monitor_context.close()
                except Exception:
                    pass
            if context:
                try:
                    await self.diagnostics.finish_trace(id,context)
                    await context.close()
                except Exception:
                    pass
            if lock_acquired: account_lock.release()
            if slot_acquired: self.browser_slots.release()
            self.jobs.pop(id, None)
            self.wakes.pop(id, None)
            self.pages.pop(id, None)
            self.live_view_locks.pop(('tasks', id), None)
            self.store.delete("harvesters", "harvester-" + id)