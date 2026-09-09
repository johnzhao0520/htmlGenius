#!/usr/bin/env python3
"""Capture reproducible screenshots for the PageTack local-test installation guide."""
import base64
import hashlib
import http.server
import json
import pathlib
import shutil
import socket
import threading

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"
GUIDE = ROOT / "docs" / "pagetack-local-test-guide"
OUT = GUIDE / "images"
PROFILE = pathlib.Path("/tmp/pagetack-guide-profile")


def extension_id() -> str:
    key = json.loads((EXT / "manifest.json").read_text())["key"]
    digest = hashlib.sha256(base64.b64decode(key)).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class Server(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.port = free_port()
        self.server = None

    def run(self):
        handler = lambda *args, **kwargs: http.server.SimpleHTTPRequestHandler(
            *args, directory=str(GUIDE), **kwargs
        )
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        self.server.serve_forever()

    def stop(self):
        if self.server:
            self.server.shutdown()


def activate(sidepanel, page):
    page.bring_to_front()
    sidepanel.evaluate(
        """async () => {
            const [tab] = await chrome.tabs.query({active:true, currentWindow:true});
            return chrome.tabs.sendMessage(tab.id, {type:'activate', showDialog:false});
        }"""
    )


def drag_select(page, selector, start_chars=0, end_chars=38):
    points = page.evaluate(
        """([selector, startChars, endChars]) => {
            const node = document.querySelector(selector).firstChild;
            const rectFor = (start, end) => {
              const range = document.createRange(); range.setStart(node,start); range.setEnd(node,end);
              return range.getBoundingClientRect();
            };
            const a = rectFor(startChars, startChars + 1);
            const b = rectFor(endChars - 1, endChars);
            return {a:{x:a.left+2,y:a.top+a.height/2},b:{x:b.right-2,y:b.top+b.height/2}};
        }""",
        [selector, start_chars, end_chars],
    )
    page.mouse.move(points["a"]["x"], points["a"]["y"])
    page.mouse.down()
    page.mouse.move(points["b"]["x"], points["b"]["y"], steps=8)
    page.mouse.up()
    page.wait_for_timeout(500)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(PROFILE, ignore_errors=True)
    server = Server()
    server.start()
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(PROFILE),
            headless=False,
            ignore_default_args=["--disable-extensions"],
            args=[
                f"--disable-extensions-except={EXT}",
                f"--load-extension={EXT}",
                "--no-first-run",
                "--no-default-browser-check",
                "--lang=en-US",
            ],
            viewport={"width": 1440, "height": 900},
        )
        context.set_default_timeout(10000)

        extensions = context.new_page()
        extensions.goto("chrome://extensions/")
        extensions.wait_for_timeout(1800)
        extensions.evaluate(
            """() => {
                const manager = document.querySelector('extensions-manager');
                const toolbar = manager.shadowRoot.querySelector('extensions-toolbar');
                const toggle = toolbar.shadowRoot.querySelector('#devMode');
                if (!toggle.checked) toggle.click();
            }"""
        )
        extensions.wait_for_timeout(600)
        extensions.screenshot(path=OUT / "01-load-unpacked.png")

        sidepanel = context.new_page()
        sidepanel.goto(f"chrome-extension://{extension_id()}/sidepanel.html")
        sidepanel.evaluate(
            "() => new Promise(resolve => chrome.storage.local.set({hg_lang:'en'}, resolve))"
        )
        sidepanel.reload()
        sidepanel.wait_for_timeout(1200)

        page = context.new_page()
        page.goto(f"http://127.0.0.1:{server.port}/demo.html")
        page.wait_for_timeout(1400)
        activate(sidepanel, page)
        drag_select(page, "#hero-title")
        page.screenshot(path=OUT / "02-select-and-comment.png")

        button = page.locator('#hg-toolbar button[data-act="comment"]')
        button.click()
        sidepanel.wait_for_timeout(700)
        sidepanel.set_viewport_size({"width": 460, "height": 900})
        sidepanel.locator("#draft-host .draft-input").fill(
            "Make the headline more specific and outcome-focused."
        )
        sidepanel.screenshot(path=OUT / "03-write-comment.png")

        # Saving needs the annotated page to remain Chrome's active tab so the
        # side panel can resolve the correct target tab and page origin.
        page.bring_to_front()
        sidepanel.locator("#draft-host .draft-save").click()
        sidepanel.wait_for_timeout(1000)
        if sidepanel.locator("#tab-comments").count():
            sidepanel.locator("#tab-comments").click()
        sidepanel.wait_for_timeout(500)
        sidepanel.screenshot(path=OUT / "04-comment-inbox.png")

        page.bring_to_front()
        sidepanel.locator("#export-btn").click()
        sidepanel.wait_for_timeout(500)
        sidepanel.screenshot(path=OUT / "05-revise-with-ai.png")
        context.close()
    server.stop()
    print(f"Captured tutorial screenshots in {OUT}")


if __name__ == "__main__":
    main()
