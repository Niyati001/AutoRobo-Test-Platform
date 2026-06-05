"""
Automated screenshot capture for the ARVP README.

Requirements:
    pip install playwright
    playwright install chromium

Usage:
    # Make sure docker compose is up, then:
    python docs/capture_screenshots.py
"""

import asyncio
import sys
from pathlib import Path

try:
    from playwright.async_api import async_playwright
except ImportError:
    print("Install playwright first:  pip install playwright && playwright install chromium")
    sys.exit(1)

OUT = Path(__file__).parent / "screenshots"
OUT.mkdir(exist_ok=True)

BASE = "http://localhost:8000"
GRAFANA = "http://localhost:3000"      # direct port (Nginx proxy bypassed for screenshots)
GRAFANA_NGINX = "http://localhost/grafana"
PROMETHEUS = "http://localhost:9090"

SIM_SWAGGER = "http://localhost:8002/docs"
VAL_SWAGGER = "http://localhost:8003/docs"
FI_SWAGGER = "http://localhost:8004/docs"

SHOTS = [
    # (url, filename, wait_for_selector, wait_until, viewport)
    (
        f"{BASE}/docs",
        "swagger-gateway.png",
        ".swagger-ui .info",
        "domcontentloaded",
        {"width": 1400, "height": 900},
    ),
    (
        SIM_SWAGGER,
        "swagger-simulation.png",
        ".swagger-ui .opblock",
        "domcontentloaded",
        {"width": 1400, "height": 960},
    ),
    (
        VAL_SWAGGER,
        "swagger-validation.png",
        ".swagger-ui .opblock",
        "domcontentloaded",
        {"width": 1400, "height": 960},
    ),
    (
        FI_SWAGGER,
        "swagger-fault-injection.png",
        ".swagger-ui .opblock",
        "domcontentloaded",
        {"width": 1400, "height": 960},
    ),
    (
        f"{PROMETHEUS}/targets",
        "prometheus-targets.png",
        "table",
        "domcontentloaded",
        {"width": 1400, "height": 900},
    ),
]

GRAFANA_USER = "admin"
GRAFANA_PASS = "admin123"


async def login_grafana(page):
    # Login directly on port 3000 (bypass nginx subpath routing)
    await page.goto(f"{GRAFANA}/login", wait_until="domcontentloaded", timeout=20_000)
    await page.wait_for_selector("input[name='user']", timeout=10_000)
    await page.fill("input[name='user']", GRAFANA_USER)
    await page.fill("input[name='password']", GRAFANA_PASS)
    await page.click("button[type='submit']")
    await asyncio.sleep(3)


async def capture_dashboard(page):
    """Navigate directly to the fleet overview dashboard on port 3000."""
    try:
        await page.set_viewport_size({"width": 1600, "height": 960})
        url = f"{GRAFANA}/d/arvp-fleet-overview/arvp-fleet-operations-overview?theme=dark"
        await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        await asyncio.sleep(6)  # let Prometheus panels render
        out_path = OUT / "grafana-dashboard.png"
        await page.screenshot(path=str(out_path), full_page=False)
        print("  Saved: docs/screenshots/grafana-dashboard.png")
    except Exception as e:
        print(f"  SKIP grafana-dashboard.png: {e}")


async def capture():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        # Prometheus metrics graph — memory usage per service (9 colored lines)
        try:
            await page.set_viewport_size({"width": 1440, "height": 860})
            await page.goto(
                f"{PROMETHEUS}/graph?g0.expr=process_resident_memory_bytes"
                "&g0.tab=0&g0.stacked=0&g0.range_input=2h",
                wait_until="domcontentloaded",
                timeout=20_000,
            )
            await asyncio.sleep(4)
            await page.screenshot(path=str(OUT / "prometheus-metrics.png"), full_page=False)
            print("  Saved: docs/screenshots/prometheus-metrics.png")
        except Exception as e:
            print(f"  SKIP prometheus-metrics.png: {e}")

        for url, filename, selector, wait_until, viewport in SHOTS:
            out_path = OUT / filename
            try:
                await page.set_viewport_size(viewport)
                await page.goto(url, wait_until=wait_until, timeout=20_000)
                if selector:
                    await page.wait_for_selector(selector, timeout=12_000)
                await asyncio.sleep(1.5)
                await page.screenshot(path=str(out_path), full_page=False)
                print(f"  Saved: docs/screenshots/{filename}")
            except Exception as e:
                print(f"  SKIP {filename}: {e}")

        await browser.close()


if __name__ == "__main__":
    print("Capturing ARVP screenshots...")
    asyncio.run(capture())
    print(f"\nDone. Screenshots saved to:  docs/screenshots/")
    print("Add them to the README with:")
    print("  ![Swagger UI](docs/screenshots/swagger-ui.png)")
    print("  ![Prometheus Targets](docs/screenshots/prometheus-targets.png)")
    print("  ![Grafana Dashboard](docs/screenshots/grafana-dashboard.png)")
