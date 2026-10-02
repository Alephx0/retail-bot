"""Check contextual integrations without contacting external services."""
import asyncio
from uuid import uuid4
from patchright.async_api import async_playwright

async def main():
    async with async_playwright() as driver:
        browser=await driver.chromium.launch()
        page=await browser.new_page(viewport={"width":1440,"height":1000})
        errors=[]
        page.on("pageerror",lambda e:errors.append(str(e)))
        await page.goto("http://127.0.0.1:8766")
        await page.locator("[data-view=settings]").click()
        await page.locator("[data-settings-tab=integrations]").click()
        await page.locator("[data-context-view=solvers]").click()
        await page.locator("#primary").click()
        name="Manual "+uuid4().hex[:6]
        await page.get_by_label("Connection name",exact=True).fill(name)
        await page.locator("#editor button[type=submit]").click()
        row=page.locator("tr").filter(has_text=name)
        await row.get_by_role("button",name="Test",exact=True).click()
        await row.get_by_text("connected",exact=True).wait_for()
        await page.locator("[data-view=settings]").click()
        await page.locator("[data-settings-tab=integrations]").click()
        await page.locator("[data-context-view=mailboxes]").click()
        await page.locator("#primary").click()
        await page.get_by_label("Mailbox name",exact=True).fill("Fixture Inbox")
        await page.get_by_label("IMAP host",exact=True).fill("imap.example.com")
        await page.get_by_label("Mailbox username",exact=True).fill("fixture@example.com")
        await page.get_by_label("App password",exact=True).fill("fixture-only")
        await page.locator("#editor button[type=submit]").click()
        await page.locator("tr").filter(has_text="Fixture Inbox").first.wait_for()
        await page.locator("[data-view=settings]").click()
        await page.locator("[data-settings-tab=retailers]").click()
        assert await page.locator(".group-card").count()==11
        await page.locator("[data-settings-tab=data]").click()
        async with page.expect_download() as download:
            await page.get_by_role("link",name="Download encrypted workspace backup").click()
        assert (await download.value).suggested_filename.endswith(".zip")
        assert not errors,errors
        await browser.close()
        print("PASS: contextual solver/IMAP forms, manual provider check, retailers, encrypted backup")

if __name__=="__main__":asyncio.run(main())
