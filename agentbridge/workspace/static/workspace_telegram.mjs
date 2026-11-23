// Login-scoped binding. Challenge codes live only in the two active UI sessions.
export async function loadTelegramLinks({container, api, scope, document, onError}) {
  if (!container) return;
  const element = (tag, text) => {
    const node = document.createElement(tag);
    if (text) node.textContent = text;
    return node;
  };
  async function render() {
    const {items} = await api("/api/hosts/telegram");
    scope.assertCurrent();
    container.replaceChildren();
    for (const item of items) {
      const section = element("section");
      section.className = "telegram-host-binding";
      section.append(element("h3", item.display_name + " · Telegram"));
      const bot = element("a", "@" + item.username);
      bot.href = "https://t.me/" + encodeURIComponent(item.username);
      bot.target = "_blank";
      bot.rel = "noopener noreferrer";
      section.append(bot);
      const call = (action, fields = {}) => api("/api/hosts/telegram/" + action, {
        method:"POST", csrf:true, body:{instance_id:item.instance_id, ...fields},
      });
      const control = (label, action) => {
        const button = element("button", label);
        button.type = "button";
        button.className = "secondary";
        button.addEventListener("click", async () => {
          button.disabled = true;
          try { await action(); }
          catch (error) { if (scope.current()) onError(error); }
          finally { if (scope.current()) button.disabled = false; }
        });
        section.append(button);
      };
      if (item.state === "linked") {
        section.append(element("span", "私聊已关联"));
        if (item.source !== "existing") {
          control("解除关联", async () => { await call("revoke"); await render(); });
        }
      } else if (item.sender_hint && item.challenge_id) {
        section.append(element("p", "请核对私聊账号 " + item.sender_hint + "，并输入机器人发给你的确认码。"));
        const input = element("input");
        input.type = "text";
        input.inputMode = "numeric";
        input.maxLength = 8;
        input.autocomplete = "one-time-code";
        input.setAttribute("aria-label", "Telegram 私聊确认码");
        section.append(input);
        control("确认本人私聊", async () => {
          if (!/^[0-9]{8}$/.test(input.value.trim())) throw new Error("请输入机器人发来的八位确认码。");
          await call("confirm", {challenge_id:item.challenge_id, confirmation_code:input.value.trim()});
          await render();
        });
      } else {
        control(item.challenge_id ? "重新生成绑定入口" : "关联我的 Telegram", async () => {
          const result = await call("begin");
          scope.assertCurrent();
          if (result.state === "linked") return render();
          if (!/^[A-Za-z0-9_-]{1,64}$/.test(result.code)) throw new Error("绑定入口未能生成。");
          const link = element("a", "打开机器人私聊");
          link.href = bot.href + "?start=" + encodeURIComponent(result.code);
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          section.replaceChildren(element("h3", item.display_name + " · Telegram"), link,
            element("p", "在 Telegram 点击开始，收到确认码后回到这里。"));
          control("已收到，继续绑定", render);
        });
        if (item.challenge_id) control("查看绑定进度", render);
      }
      container.append(section);
    }
  }
  try { await render(); } catch (error) { if (scope.current()) onError(error); }
}
