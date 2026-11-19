"""Render a configured demo scene through the real Nexus UI."""

import argparse
import asyncio
import json
import socket
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn
from playwright.async_api import async_playwright

from nexus.app import create_app


async def capture(url: str, network, config: dict, output: Path) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport=config.get("viewport", {"width": 1320, "height": 840}),
            device_scale_factor=config.get("deviceScaleFactor", 2),
            locale=config.get("language", "en"),
            color_scheme=config.get("theme", "light"),
            reduced_motion="reduce",
        )
        storage = {
            "nexus-language": config.get("language", "en"),
            "nexus-theme": config.get("theme", "light"),
            "nexus-pane-widths": json.dumps(config.get("panes", {})),
        }
        await context.add_init_script(
            f"for (const [key, value] of Object.entries({json.dumps(storage)})) "
            "localStorage.setItem(key, value);"
        )
        page = await context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        frame = config.get("frame")
        if frame is not None:
            template = Path(__file__).with_name("demo-frame.html").read_text()
            styles = []
            for key, value in frame.items():
                unit = "px" if key in {"padding", "radius"} else ""
                styles.append(f"--frame-{key}: {value}{unit};")
            template = template.replace("/* frame-config */", "\n".join(styles))
            await page.route(
                "**/__capture__",
                lambda route: route.fulfill(content_type="text/html", body=template),
            )
        await page.goto(f"{url}/__capture__" if frame is not None else url)
        view = page.frame_locator("iframe") if frame is not None else page
        thread = network.seeded_threads[config["thread"]]
        await view.locator(f'.session-row[data-thread-id="{thread["id"]}"]').click()
        await view.locator("#thread-view").wait_for()
        if thread["turns"]:
            await view.locator("#conversation > *").first.wait_for()
        await view.locator("#history-loading").wait_for(state="hidden")
        has_goal = any(thread_id == thread["id"] for _, thread_id in network.goals)
        await view.locator("#goal-panel").wait_for(state="visible" if has_goal else "hidden")
        if "searchMode" in config:
            await view.locator("#search-mode").select_option(config["searchMode"], force=True)
        if config.get("hideDemoLabel"):
            await view.locator("body").evaluate(
                """() => {
                    const style = document.createElement('style');
                    style.textContent = '#demo-label { display: none !important; }';
                    document.head.append(style);
                }"""
            )
        await view.locator("#conversation-scroll").evaluate("element => element.scrollTop = 0")
        await page.mouse.move(0, 0)
        await view.locator("body").evaluate("() => document.fonts.ready")
        if errors:
            raise RuntimeError("\n".join(errors))
        if config.get("hideDemoLabel"):
            assert await view.locator("#demo-label").is_hidden()
        assert (
            await view.locator("#composer-goal").evaluate(
                "button => button.classList.contains('enabled')"
            )
            == has_goal
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(output), animations="disabled")
        await browser.close()


async def main(config_path: Path) -> None:
    config = json.loads(config_path.read_text())
    scene = config_path.parent / config["scene"] if "scene" in config else None
    output = config_path.parent / config["output"]
    with TemporaryDirectory(prefix="nexus-demo-") as directory, socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        app = create_app(Path(directory), demo=True, demo_config=scene, nexus_url=url)
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("Demo server did not start")
                await asyncio.sleep(0.01)
            await capture(url, app.state.service.demo_network, config, output)
        finally:
            server.should_exit = True
            await task
    print(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="JSON capture configuration")
    asyncio.run(main(parser.parse_args().config.resolve()))
