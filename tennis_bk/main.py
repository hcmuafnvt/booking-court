from playwright.sync_api import sync_playwright
import time

LOGIN_URL = "https://nvrc.perfectmind.com/23734/MemberRegistration/MemberSignIn?returnUrl=https%3a%2f%2fnvrc.perfectmind.com%2f23734%2fClients%2fBookMe4BookingPages%2fClasses%3fwidgetId%3da28b2c65-61af-407f-80d1-eaa58f30a94a%26calendarId%3dd0a5979d-2f83-4696-997e-ea18f86cbf30%26singleCalendarWidget%3dFalse"

USERNAME = "Gpham88@hotmail.com"
PASSWORD = "Yvrsgn88!"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)  # headed để debug
    context = browser.new_context()
    page = context.new_page()

    print("👉 Opening login page")
    page.goto(LOGIN_URL, wait_until="domcontentloaded")

    # Điền form
    page.fill('input[name="username"]', USERNAME)
    page.fill('input[name="password"]', PASSWORD)

    print("👉 Submitting login form")
    page.click('button#buttonLogin')

    # Chờ redirect sau login
    page.wait_for_load_state("networkidle")

    print("👉 Current URL after login:")
    print(page.url)

    # Lưu session
    context.storage_state(path="storage_state.json")
    print("✅ Session saved to storage_state.json")

    time.sleep(5)
    browser.close()
