/* Browser regression fixture for a Playwright-compatible runner, while serving
   agentbridge/workspace/static on 127.0.0.1:18763. No logged-in session is used.
   The evaluate callback can also be loaded into a local preview and inspected
   through the Chrome extension (the preferred interactive validation route). */
async (page) => {
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.route("**/api/**", route => route.fulfill({ json: { authenticated: false } }));
  await page.route("**/assets/**", async route => {
    const response = await route.fetch({ url: route.request().url().replace("/assets/", "/") });
    await route.fulfill({ response });
  });
  await page.goto("http://127.0.0.1:18763/");
  await page.waitForFunction(() => typeof renderMarkdown === "function" && globalThis.DOMPurify);
  const checks = await page.evaluate(() => {
    const assert = (value, message) => { if (!value) throw new Error(message); };
    document.querySelector("#auth-view").hidden = true;
    document.querySelector("#app-view").hidden = false;
    const root = document.querySelector("#chat-messages");
    root.replaceChildren();
    const source = "## 日志分析结果\n\n共 **3 条记录**，合计 *3.5 小时*。\n\n- 已完成数据核对\n- 可下载明细\n  - 保留原始来源\n\n| 日期 | 工时 | 说明 |\n| --- | ---: | --- |\n| 2026-09-11 | 3.5 | 已核对 |\n\n> 统计范围：最近七天。\n\n```python\nprint('" + "long_code_".repeat(18) + "')\n```\n\n[来源](https://example.com) 与 `inline_code`\n\n- [x] 核对完成";
    const assistant = messageElement({ role: "assistant", text: source });
    root.append(assistant);
    assert(assistant.querySelector("h2")?.textContent === "日志分析结果", "history heading");
    for (const selector of ["strong", "em", "ul ul", "table th", "blockquote", "pre code", "li input:disabled"]) {
      assert(assistant.querySelector(selector), selector);
    }
    const link = assistant.querySelector("a");
    assert(link.target === "_blank" && link.rel.includes("noopener"), "safe link");
    const user = messageElement({ role: "user", text: "**保留输入原文**" });
    assert(!user.querySelector("strong") && user.textContent.includes("**"), "user text");
    const attack = document.createElement("div");
    renderMarkdown(attack, '<script>globalThis.mdPwned=1</script><img src=x onerror="globalThis.mdPwned=1"><svg onload="alert(1)"></svg><iframe src="https://example.com"></iframe><form id="chat-form"><input type="text"></form><a href="javascript:alert(1)">bad</a><a href="/api/logout">local</a><a href="data:text/html,evil">data</a><p style="position:fixed" onclick="alert(1)">text</p>');
    assert(!attack.querySelector("script,img,svg,iframe,form,input,[onclick],[onerror],[style],[id],a[href]"), "unsafe HTML removed");
    assert(!globalThis.mdPwned, "no script execution");
    handleChatProgress({ runId: "md-check", kind: "preamble", text: "**正在核对**" });
    const live = state.liveMessages.get("md-check");
    assert(live.progressDescription.querySelector("strong") && !live.progressDetails.open, "collapsed markdown progress");
    handleChatDelta({ runId: "md-check", state: "delta", text: "## 未完成\n\n```js\nconst x = 1" });
    assert(live.text.querySelector("pre code"), "incomplete streaming fence");
    handleChatDelta({ runId: "md-check", state: "final", text: "**流式结果已完成**" });
    assert(live.text.querySelector("strong") && !state.liveMessages.has("md-check"), "final markdown cleanup");
    assert(renderTaskPlanResult({ result: { draft: "## 草稿\n\n正文" } }).querySelector("h2"), "draft markdown");
    const parser = globalThis.marked;
    globalThis.marked = null;
    const fallback = document.createElement("div");
    renderMarkdown(fallback, "**保留内容**");
    assert(fallback.textContent === "**保留内容**", "missing dependency fallback");
    globalThis.marked = parser;
    return "history, streaming, drafts, progress, syntax, XSS and fallback passed";
  });
  const layouts = [];
  for (const width of [1100, 390]) {
    await page.setViewportSize({ width, height: 900 });
    const layout = await page.evaluate(() => ({
      width: innerWidth,
      overflow: document.documentElement.scrollWidth > innerWidth,
      codeScroll: [...document.querySelectorAll("pre")].some(el => el.scrollWidth > el.clientWidth),
    }));
    if (layout.overflow) throw new Error(`Page overflow at ${width}`);
    await page.screenshot({ path: `output/playwright/workspace-markdown-${width}.png`, fullPage: true });
    layouts.push(layout);
  }
  if (errors.length) throw new Error(errors.join("\n"));
  return { checks, layouts, errors };
}
