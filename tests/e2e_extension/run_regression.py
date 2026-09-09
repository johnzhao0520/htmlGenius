#!/usr/bin/env python3
"""htmlGenius v0.9.17 扩展核心功能回归 E2E v2(Playwright + 自带 Chromium 加载未打包扩展)。

v2 修正:激活页管理(facet 操作前 bring_to_front)、SW 主动唤醒后再断言、
localhost 夹具=本地 artifact(send_hint 应隐藏)、失败时自动 dump 诊断。

用法: uv run python tests/e2e_extension/run_regression.py
"""
import hashlib
import http.server
import json
import pathlib
import shutil
import socket
import sys
import threading
import time

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
EXT = ROOT / "extension"
PROFILE = pathlib.Path("/tmp/hg-e2e-profile")
# 使用仓库长期保留的样例，避免历史演示文件被清理后整套 E2E 无法运行。
FILE_TARGET = (ROOT / "samples" / "01_token.html").resolve()
RESULTS = []


def report(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""), flush=True)


def ext_id_from_manifest_key() -> str:
    import base64
    key = json.load(open(EXT / "manifest.json"))["key"]
    digest = hashlib.sha256(base64.b64decode(key)).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


def free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class HttpFileServer(threading.Thread):
    def __init__(self, root):
        super().__init__(daemon=True)
        self.port = free_port(); self._srv = None; self.root = root

    def run(self):
        h = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(*a, directory=str(self.root), **kw)
        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), h)
        self._srv.serve_forever()

    def stop(self):
        if self._srv: self._srv.shutdown()


def select_text(page, selector, start=0, length=12):
    page.evaluate(
        """([sel, s, n]) => {
            const el = document.querySelector(sel); const node = el.firstChild;
            const range = document.createRange();
            range.setStart(node, s); range.setEnd(node, Math.min(n, node.length));
            const s2 = window.getSelection(); s2.removeAllRanges(); s2.addRange(range);
        }""", [selector, start, length])


def drag_select_text(page, selector, reverse=False):
    """用真实鼠标跨行拖选，覆盖正向/反向手势及浏览器事件时序。"""
    points = page.evaluate(
        """(selector) => {
            const el = document.querySelector(selector);
            const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
            const nodes = [];
            while (walker.nextNode()) {
                if (walker.currentNode.data.trim()) nodes.push(walker.currentNode);
            }
            if (!nodes.length) throw new Error('no text node: ' + selector);
            const first = nodes[0];
            const last = nodes[nodes.length - 1];
            const firstOffset = Math.max(0, first.data.search(/\S/));
            const lastOffset = Math.max(0, last.data.search(/\s*$/) - 1);
            const a = document.createRange();
            a.setStart(first, firstOffset); a.setEnd(first, firstOffset + 1);
            const b = document.createRange();
            b.setStart(last, lastOffset); b.setEnd(last, lastOffset + 1);
            const ar = a.getBoundingClientRect(); const br = b.getBoundingClientRect();
            return {
                start: {x: ar.left + 2, y: ar.top + ar.height / 2},
                end: {x: br.right - 2, y: br.top + br.height / 2},
            };
        }""", selector)
    page.evaluate("getSelection().removeAllRanges()")
    start, end = points["start"], points["end"]
    if reverse:
        start, end = end, start
    page.mouse.move(start["x"], start["y"])
    page.mouse.down()
    page.mouse.move(end["x"], end["y"], steps=3)
    page.mouse.up()
    page.wait_for_timeout(350)
    return page.evaluate("getSelection().toString()")


def select_then_finish_pointer_gesture(page, selector):
    """确定性建立选区，再模拟用户松手；用于隔离测试 selectionchange 被拦截的兜底。"""
    page.evaluate(
        """(selector) => {
            const el = document.querySelector(selector);
            const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
            const nodes = [];
            while (walker.nextNode()) if (walker.currentNode.data.trim()) nodes.push(walker.currentNode);
            const first = nodes[0], last = nodes[nodes.length - 1];
            const start = Math.max(0, first.data.search(/\S/));
            const end = last.data.search(/\s*$/);
            const range = document.createRange();
            range.setStart(first, start); range.setEnd(last, end);
            const sel = getSelection(); sel.removeAllRanges(); sel.addRange(range);
            el.dispatchEvent(new PointerEvent('pointerup', {bubbles: true, cancelable: true}));
            el.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true}));
        }""", selector)
    page.wait_for_timeout(350)
    return page.evaluate("getSelection().toString()")


def cs_marker_seen(page):
    return any("[hg] cs loaded" in m.text for m in page.console_messages())


def via_active_tab(sp, msg):
    """从扩展页向当前活动 tab 发消息(与产品路径一致)。"""
    return sp.evaluate(
        """async (msg) => {
            const tabs = await chrome.tabs.query({active:true, currentWindow:true});
            if (!tabs.length) return {err: 'no tab'};
            try { return await chrome.tabs.sendMessage(tabs[0].id, msg); }
            catch (e) { return {err: String(e && e.message || e)}; }
        }""", msg)


def via_tab_url(sp, url, msg):
    """向指定 URL 的 tab 发消息，避免 headed E2E 的窗口焦点抖动误选其他页。"""
    return sp.evaluate(
        """async ([url, msg]) => {
            const tabs = await chrome.tabs.query({});
            const tab = tabs.find((t) => t.url === url);
            if (!tab) return {err: 'tab not found'};
            try { return await chrome.tabs.sendMessage(tab.id, msg); }
            catch (e) { return {err: String(e && e.message || e)}; }
        }""", [url, msg])


def enable_file_access_pref(ext_id):
    pref = PROFILE / "Default" / "Preferences"
    prefs = json.loads(pref.read_text())
    st = prefs.setdefault("extensions", {}).setdefault("settings", {}).setdefault(ext_id, {})
    st["newAllowFileAccess"] = True; st["file_access"] = True
    pref.write_text(json.dumps(prefs))
    print(f"   [prefs] file access 开关已写入({ext_id})")


def dump_state(tag, sp, page, errors):
    print(f"   ── dump[{tag}] ──")
    if page:
        print("   page errors:", [m.text for m in page.console_messages() if m.type == "error"][:5])
    if sp:
        print("   sp errors:", [m.text for m in sp.console_messages() if m.type == "error"][:5])
        print("   sp draft-host:", (sp.locator("#draft-host").inner_text() or "")[:80].replace("\n", "|"))
    print("   collected pageerrors:", errors[:5])


def run_phase(tag, http_url, do_edit_and_contract=True):
    ctx_errors = []
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE), headless=False,
            ignore_default_args=["--disable-extensions"],
            args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}",
                  "--no-first-run", "--no-default-browser-check"],
            viewport={"width": 1440, "height": 950},
        )
        ctx.set_default_timeout(8000)
        # 侧栏恢复既有应用 session 的回归夹具：不依赖真实生产账号，也不向线上发送测试 token。
        ctx.route(
            "https://pagetack-api.zeabur.app/auth/me",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "id": "stored-user",
                    "name": "Stored User",
                    "team_id": "stored-team",
                    "teams": [{"team_id": "stored-team", "name": "Stored Team", "role": "owner"}],
                }),
            ),
        )
        def wire(pg):
            pg.on("pageerror", lambda e: ctx_errors.append(f"pageerror: {e}"))
            pg.on("dialog", lambda d: d.dismiss())
        ctx.on("page", wire)
        import subprocess as _sp
        def _watchdog():
            print(f"   [watchdog] {tag} 超时,强杀浏览器", flush=True)
            _sp.run(["pkill", "-9", "-f", "hg-e2e-profile"])
        wd = threading.Timer(180, _watchdog); wd.daemon = True; wd.start()
        sp = page = fpage = blocked_page = None
        try:
            # 扩展页(sidepanel 作为 tab)
            sp = ctx.new_page()
            ext_id = ext_id_from_manifest_key()
            sp.goto(f"chrome-extension://{ext_id}/sidepanel.html")
            sp.wait_for_timeout(1500)
            report(f"[{tag}] sidepanel 页加载", sp.locator("#tabbar").count() == 1)

            # SW:主动唤醒(未知消息类型即可;绝不能 await bridge-query-providers——会触发 provider 探测,本机装 bridge 时长达数十秒)
            sp.evaluate("(chrome.runtime.sendMessage({type:'hg-e2e-wake'},()=>void chrome.runtime.lastError), 'sent')")
            time.sleep(2)
            sws = ctx.service_workers
            report(f"[{tag}] SW 唤醒启动", any(s.url.startswith(f"chrome-extension://{ext_id}/") for s in sws),
                   f"count={len(sws)}")
            sw = next((s for s in sws if s.url.startswith(f"chrome-extension://{ext_id}/")), None)
            analytics_ready = bool(sw and sw.evaluate("typeof globalThis.HGAnalyticsCore === 'object'"))
            report(f"[{tag}] SW 加载 analytics core", analytics_ready, f"ready={analytics_ready}")

            regs = sp.evaluate("async()=>{if(!chrome.scripting?.getRegisteredContentScripts)return [];try{return await chrome.scripting.getRegisteredContentScripts()}catch(e){return ['ERR:'+e.message]}}")
            report(f"[{tag}] 未使用动态注册(revert 干净)", isinstance(regs, list) and len(regs) == 0, json.dumps(regs)[:100])

            # --- http 注入 ---
            page = ctx.new_page()
            page.goto(http_url); page.wait_for_timeout(1800)
            page.bring_to_front(); time.sleep(0.3)
            ok_http = cs_marker_seen(page)
            r = via_active_tab(sp, {"type": "get-annotations"})
            report(f"[{tag}] http 注入+响应", ok_http and r.get("type") == "annotations-list",
                   f"cs={ok_http} resp={r.get('type') or r.get('err')}")

            # 顶部选区上方空间不足时，工具栏应自动放到下方，不能跑出视口。
            if ok_http:
                via_active_tab(sp, {"type": "activate", "showDialog": False})
                select_text(page, "#p-top", 0, 10)
                time.sleep(0.4)
                top_box = page.locator('#hg-toolbar.show').bounding_box()
                top_in_view = bool(top_box and top_box['y'] >= 0
                                   and top_box['y'] + top_box['height'] <= page.viewport_size['height'])
                report(f"[{tag}] 顶部选区 Comment 工具栏不越界", top_in_view, f"box={top_box}")

                # 用户真实问题发生在跨行大标题：快速正向拖选偶尔只有原生蓝色选区，
                # 反向拖选或先双击一个词后才出现 Comment。两种方向都必须独立稳定。
                for direction, reverse in (("正向", False), ("反向", True)):
                    attempts = []
                    for _ in range(6):
                        selected = drag_select_text(page, "#hero-title", reverse=reverse)
                        toolbar_visible = page.locator('#hg-toolbar.show button[data-act="comment"]').count() == 1
                        attempts.append(len(selected.strip()) >= 20 and toolbar_visible)
                    report(
                        f"[{tag}] 跨行标题{direction}拖选稳定显示 Comment",
                        all(attempts),
                        f"success={sum(attempts)}/{len(attempts)} selection={selected.strip()!r}",
                    )

            # --- D 评论流 ---
            # 以扩展页模拟 Side Panel 时，重点验证用户可见链路：选区浮窗 → 点击评论 → 草稿框出现。
            # 提交落库另由服务器/UI 测试覆盖；这里不要再依赖“活动 tab”这一与真实 Side Panel 不同的测试环境细节。
            if ok_http:
                select_text(page, "#p1", 0, 10)
                time.sleep(0.6)
                bb = page.locator('#hg-toolbar button[data-act="comment"]').bounding_box()
                shown = bb is not None
                report(f"[{tag}] 选区浮出工具栏", shown)
                broadcast_ok = False
                if shown:
                    page.mouse.click(bb['x'] + bb['width'] / 2, bb['y'] + bb['height'] / 2)
                    time.sleep(1.0)
                    sel_cleared = page.evaluate("getSelection().toString()") == ""
                    draft_visible = sp.locator("#draft-host .draft-card").count() == 1
                    broadcast_ok = sel_cleared and draft_visible
                    if not broadcast_ok:
                        dump_state("comment", sp, page, ctx_errors)
                report(f"[{tag}] 评论:工具栏点击→侧栏草稿框", broadcast_ok,
                       f"draft_visible={broadcast_ok}")
                if broadcast_ok:
                    sp.locator(".draft-input").fill("E2E 回归评论:这段要改")
                    sp.locator(".draft-save").click()
                    time.sleep(1.0)
                    # 假 session 无法在线落库时草稿会按产品设计保留；先清掉它，避免下一条
                    # 断言把旧草稿误认成刚创建的新草稿。
                    if sp.locator("#draft-host .draft-card").count() == 1:
                        sp.locator("#draft-host .draft-cancel").click(force=True)
                # 回到编辑页，避免草稿卡让后续编辑流的按钮处于隐藏 tab。
                sp.locator("#tab-edit").click()

                # 一些网站会在扩展注入前注册 selectionchange 监听并 stopImmediatePropagation。
                # 用户仍看得到蓝色原生选区，但 PageTack 的选区缓存可能还没有建立。
                # 侧栏 Comment 必须优先使用当前实时选区，不得因缓存缺失误报 NO_SELECTION。
                blocked_page = ctx.new_page()
                blocked_page.goto(http_url + "?block-selectionchange=1")
                blocked_page.wait_for_timeout(1600)
                blocked_page.bring_to_front(); time.sleep(0.3)
                via_active_tab(sp, {"type": "activate", "showDialog": False})
                live_selection = select_then_finish_pointer_gesture(blocked_page, "#hero-title")
                blocked_toolbar = blocked_page.locator('#hg-toolbar.show button[data-act="comment"]').count() == 1
                report(
                    f"[{tag}] 网页拦截 selectionchange 后松开鼠标仍显示 Comment",
                    len(live_selection.strip()) >= 20 and blocked_toolbar,
                    f"selection={live_selection.strip()!r} toolbar={blocked_toolbar}",
                )
                live_selection = blocked_page.evaluate("getSelection().toString()")
                direct = via_tab_url(sp, blocked_page.url, {"type": "create-comment"})
                time.sleep(0.8)
                direct_draft = sp.locator("#draft-host .draft-card").count() == 1
                report(f"[{tag}] 选区事件被网页拦截时侧栏评论仍可用",
                       bool(live_selection) and direct.get("ok") is True and direct_draft,
                       f"selection={live_selection!r} response={direct} draft={direct_draft}")
                if direct.get("ok") is True and direct_draft:
                    sp.locator("#draft-host .draft-cancel").click(force=True)
                blocked_page.close(); blocked_page = None

            # Chrome “重新加载扩展”不会自动替换已打开页面中的旧 content script。
            # 模拟页面仍留着旧版本标记；侧栏重开后应主动发现并注入当前版本，无需刷新网页。
            if ok_http:
                page.bring_to_front(); time.sleep(0.2)
                stale_set = sp.evaluate("""async () => {
                    const tab = (await chrome.tabs.query({active:true, currentWindow:true}))[0];
                    const out = await chrome.scripting.executeScript({
                        target: {tabId: tab.id}, func: () => { window.__hgContentVersion = '0.0.0'; return true; }
                    });
                    return !!(out && out[0] && out[0].result);
                }""")
                sp.reload(); sp.wait_for_timeout(900)
                handshake = sp.evaluate("""async () => {
                    const expected = chrome.runtime.getManifest().version;
                    const tab = (await chrome.tabs.query({active:true, currentWindow:true}))[0];
                    const out = await chrome.scripting.executeScript({
                        target: {tabId: tab.id}, func: () => String(window.__hgContentVersion || '')
                    });
                    return {expected, actual: out && out[0] && out[0].result};
                }""")
                report(
                    f"[{tag}] 扩展升级后旧页面脚本自动接管",
                    stale_set and handshake.get("actual") == handshake.get("expected"),
                    json.dumps(handshake, ensure_ascii=False),
                )

            # 用户没有开启 Google 自动登录时，只要现有 PageTack session 仍有效，侧栏也应恢复
            # 团队身份并显示“复制全站评论给 AI”。此前 early return 会把按钮永久隐藏。
            sp.evaluate("""async () => new Promise(resolve => {
                chrome.storage.local.remove(['auto_login_enabled', 'google_auto_login'], () => {
                    chrome.storage.local.set({
                        mode: 'synced', session_token: 'stored-session',
                        user: {id: 'stored-user', name: 'Stored User'},
                        team_id: 'stored-team', team_name: 'Stored Team'
                    }, resolve);
                });
            })""")
            page.bring_to_front(); time.sleep(0.2)
            sp.reload(); sp.wait_for_timeout(1000)
            restore_state = sp.evaluate("""async () => ({
                hidden: document.querySelector('#site-export-btn').hidden,
                activeUrls: (await chrome.tabs.query({active: true, currentWindow: true})).map(t => t.url),
                account: document.querySelector('#account-flow-host').innerText.slice(0, 60),
                stored: await chrome.storage.local.get([
                    'mode', 'session_token', 'user', 'team_id', 'auto_login_enabled'
                ])
            })""")
            report(
                f"[{tag}] 未开自动登录仍恢复团队并显示整站导出",
                sp.locator("#site-export-btn").get_attribute("hidden") is None,
                json.dumps(restore_state, ensure_ascii=False)[:500],
            )

            # --- B/C file:// 注入(用户症状页) ---
            # 模拟已登录团队账号：file:// 仍必须强制走本地评论，不发 RemoteStore/SSE。
            sp.evaluate("""async () => new Promise(resolve => chrome.storage.local.set({
                mode: 'synced', backend: 'https://example.invalid', session_token: 'test-token',
                user: {id: 'test-user', name: 'Test'}
            }, resolve))""")
            fpage = ctx.new_page()
            fpage.goto(FILE_TARGET.as_uri()); fpage.wait_for_timeout(1800)
            fpage.bring_to_front(); time.sleep(0.3)
            ok_file = cs_marker_seen(fpage)
            fr = via_active_tab(sp, {"type": "get-annotations"})
            report(f"[{tag}] file:// 注入(脚本文档)", ok_file and fr.get("type") == "annotations-list",
                   f"cs={ok_file} resp={fr.get('type') or fr.get('err')}")
            local_only = any("本地文件不参与团队同步" in m.text for m in fpage.console_messages())
            report(f"[{tag}] file:// 强制本地评论模式", local_only)
            # 本地 HTML 是最容易出现激活竞态的路径：必须验证“选区 → 评论工具栏”真实可用，
            # 不能只验证脚本已注入、消息能响应。
            local_toolbar = False
            if ok_file:
                target = fpage.locator("p").first
                if target.count():
                    fpage.evaluate("""() => {
                        const el = document.querySelector('p'); const node = el && el.firstChild;
                        if (!node) return;
                        const range = document.createRange(); range.setStart(node, 0); range.setEnd(node, Math.min(8, node.length));
                        const sel = getSelection(); sel.removeAllRanges(); sel.addRange(range);
                    }""")
                    fpage.wait_for_timeout(500)
                    local_toolbar = fpage.locator('#hg-toolbar.show button[data-act="comment"]').count() == 1
            report(f"[{tag}] file:// 选区显示评论工具栏", local_toolbar)
            # 保持下一个浏览器 phase 的 HTTP fixture 为离线本地模式；当前 file:// 页已经完成了本轮断言。
            sp.evaluate("""async () => new Promise(resolve => chrome.storage.local.set({
                mode: 'local', session_token: '', user: null
            }, resolve))""")

            # 独立扩展页在 Playwright 中会成为 active tab，而真实 Side Panel 不会；
            # 这会让依赖 active tab 的编辑/契约按钮错误地指向扩展页。此环境仅执行
            # 可忠实模拟的页面评论链路，编辑/契约由各自的单元测试覆盖。
            standalone_extension_tab = sp.evaluate("""async () => {
                const me = await chrome.tabs.getCurrent();
                const active = (await chrome.tabs.query({active:true, currentWindow:true}))[0];
                return !!(me && active && me.id === active.id);
            }""")
            # --- E 编辑流(先切回 fixture 活动页!) ---
            if do_edit_and_contract and ok_http and not standalone_extension_tab:
                page.bring_to_front(); time.sleep(0.3)
                sp.locator("#edit-btn").click(); time.sleep(0.8)
                er = via_active_tab(sp, {"type": "get-annotations"})
                editing = bool(er.get("editing"))
                report(f"[{tag}] 进入编辑态(content-script 确认)", editing, f"editing={er.get('editing')}")
                if editing:
                    select_text(page, "#p2", 0, 6); time.sleep(0.3)
                    sp.locator("#act-bold").click(); time.sleep(0.5)
                    w = page.evaluate("()=>{const b=document.querySelector('#p2 b')||document.querySelector('#p2 font[weight]');return b?getComputedStyle(b).fontWeight:'none'}")
                    report(f"[{tag}] 加粗生效", w in ("700", "bold"), f"fontWeight={w}")
                    sp.locator("#act-undo").click(); time.sleep(0.5)
                    back = page.evaluate("()=>!document.querySelector('#p2 b')")
                    report(f"[{tag}] 撤销还原", back)
                    sp.locator("#act-reset").click(); time.sleep(0.5)

                # --- F 任务流 ---
                sp.locator("#tab-comment").click(); time.sleep(0.3)
                sp.locator("#export-btn").click(); time.sleep(1.0)
                sheet = sp.locator("#contract-sheet")
                step = sheet.get_attribute("data-step")
                radios = sp.locator('input[name="contract-scope"]').count()
                visible_sheet = sheet.is_visible()
                # localhost 夹具=本地 artifact → send_hint 应隐藏(此为正确行为)
                hint = sp.locator("#contract-send-hint").is_visible()
                report(f"[{tag}] 契约页(三档范围/本地文档无发送提示)", visible_sheet and step == "compose" and radios == 3 and not hint,
                       f"step={step} radios={radios} send_hint(应False)={hint}")
                btn_txt = sp.locator("#contract-copy-prompt").inner_text()
                sp.locator("#contract-copy-prompt").click(force=True)
                time.sleep(0.8)
                after = sp.locator("#contract-copy-prompt").inner_text()
                fallback = sp.locator("#contract-output-fallback").is_visible()
                if not (after != btn_txt or fallback):
                    dump_state("contract", sp, page, ctx_errors)
                report(f"[{tag}] 复制 Prompt 反馈", after != btn_txt or fallback, f"'{btn_txt}'→'{after}' fallback={fallback}")

            fatal = [e for e in ctx_errors if "Receiving end does not exist" not in e and "Could not establish connection" not in e]
            report(f"[{tag}] pageerror 零致命", len(fatal) == 0, "; ".join(fatal[:2]))
        finally:
            wd.cancel()
            try: ctx.close()
            except Exception: pass
            time.sleep(1)
            import subprocess as _sp
            _sp.run(["pkill", "-9", "-f", "hg-e2e-profile"], capture_output=True)


def main():
    if PROFILE.exists(): shutil.rmtree(PROFILE)
    PROFILE.mkdir(parents=True)
    srv = HttpFileServer(ROOT / "tests" / "e2e_extension"); srv.start(); time.sleep(0.5)
    http_url = f"http://127.0.0.1:{srv.port}/fixture.html"
    try:
        print("══ Phase A:默认 profile ══")
        run_phase("A", http_url, do_edit_and_contract=False)
        print("\n══ Phase C:显式打开「允许访问文件网址」 ══")
        enable_file_access_pref(ext_id_from_manifest_key())
        run_phase("C", http_url, do_edit_and_contract=False)
    finally:
        srv.stop()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n════ 总计 {passed}/{len(RESULTS)} 通过 ════")
    for n, ok, d in RESULTS:
        if not ok: print(f"  ❌ {n} {('— ' + d) if d else ''}")
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
