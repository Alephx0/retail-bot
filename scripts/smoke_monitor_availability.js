// Playwright MCP: intercepted read models only; no tasks or retailer purchases.
async page => {
  const check=(ok,message)=>{if(!ok)throw Error(message);};
  const explanation='In stock; Unavailable for the selected delivery location; Sign in required for this offer';
  await page.unrouteAll({behavior:'wait'});
  await page.route('**/api/state',async route=>{
    const response=await route.fetch(),state=await response.json();
    state.monitors=[{id:'stock-fixture',group_id:'fixture',asin:'B07ZLF9WQ5',region:'US',simulation:false,
      status:'in_stock',message:explanation,task_ids:[],connection:'Local fixture',
      log:[{at:new Date().toISOString(),status:'in_stock',message:explanation}]},
      {id:'unknown-fixture',group_id:'fixture',asin:'B012345678',region:'US',simulation:false,
      status:'stock_unknown',message:'Stock could not be verified',task_ids:[],connection:'Local fixture',log:[]}];
    state.feed=[{asin:'B07ZLF9WQ5',title:'Banana Bunch (4-5 Count)',price:.99,seller:'Amazon.com',
      available:false,availability_status:'available',availability_message:explanation,condition:'new',simulation:false,at:new Date().toISOString()}];
    await route.fulfill({response,json:state});
  });
  try {
    await page.goto('http://127.0.0.1:8785');
    await page.waitForFunction(()=>stateLoaded);
    await page.locator('[data-view=checkouts]').click();
    await page.locator('[data-context-view=monitors]').click();
    const stock=page.locator('[data-monitor-row=stock-fixture]');
    check((await stock.locator('.badge').innerText()).trim()==='In stock','Stock badge reflects inventory');
    check((await stock.innerText()).includes(explanation),'Purchase restriction remains visible');
    check((await page.locator('[data-monitor-row=unknown-fixture] .badge').innerText()).trim()==='Stock unverified','Unknown stock is not out of stock');
    await stock.locator('[data-monitor-log]').click();
    check((await page.locator('[data-monitor-log-body]').innerText()).includes(explanation),'Log preserves restriction');
    await page.keyboard.press('Escape');
    for(const width of [1440,390]){
      await page.setViewportSize({width,height:900});
      check(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Monitor layout fits '+width);
      await page.screenshot({path:`C:/Users/justi/Documents/Python/Retail Bot/artifacts/monitor-availability-${width}.png`,fullPage:true});
    }
    await page.locator('[data-context-view=feed]').click();
    check((await page.locator('#screen').innerText()).includes(explanation),'Product feed agrees with monitor');
    check(!(await page.locator('#screen').innerText()).includes('Out of stock'),'In-stock restriction is not mislabeled');
    return {passed:['Inventory badge','Delivery/sign-in explanation','Unknown inventory','Monitor log','Desktop and narrow layouts','Product feed consistency']};
  } finally {await page.unrouteAll({behavior:'wait'});}
}
