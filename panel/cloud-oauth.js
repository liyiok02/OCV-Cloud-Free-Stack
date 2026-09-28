/* SPDX-License-Identifier: AGPL-3.0-only */
/* ===========================================================================
   云端 OAuth 分页的前端逻辑（纯 DOM，无框架、无构建）。

   由 `dev_patch_panel_html.py` 注入到 index.html 的面板 IIFE 内部
   —— 因此可以直接用该 IIFE 里的 `el / h / request / toast / drafts / state /
   loadAllModels`，不需要额外接线。

   设计要点（对齐原 Jet Hub 面板）：
     * **视觉 99% 在 CSS、交互状态 100% 通过 `data-*` 暴露** —— 所以本文件
       只负责把状态属性写对（`data-enabled / data-tone / data-disabled /
       aria-selected / checked`），样式表原样工作。
     * **能力门控是不渲染，不是渲染后禁用** —— 不支持的提供商连请求都不发
       （原版 `credits-capabilities.js:9-14` 记录了真实事故）。
     * **一键签到串行** —— 桥侧 `for...await`，前端不做任何并发优化。
   =========================================================================== */

var jethubState = null;
var jethubLoginTimer = null;
var jethubProvider = "";
var jethubBusy = false;
var jethubPendingFile = null;

/**
 * 统一解包。后端 `_panel_call` 的约定是：
 *   * handler 返回**含 `ok` 的 dict** → **原样下发**（不包 `data`）；
 *   * 否则 → 包成 `{ok:true, data:...}`。
 *
 * 云端 OAuth 的 handler 一律返回含 `ok` 的扁平 dict，所以拿到的通常就是
 * payload 本身。用 `payload.data || payload` 兼容两种形态 —— **这个坑真踩过**：
 * 首版只读 `payload.data`，于是整个分页空白且页面**没有任何报错**，
 * 只有真浏览器渲染才验得出来（见 docs/JETHUB.md F-003）。
 */
function jhUnwrap(payload) {
  if (!payload) return {};
  return payload.data || payload;
}

function jhCall(path, body) {
  return request(path, { method: "POST", body: body || {} });
}

function jhNotice(node, tone, message) {
  if (!node) return;
  if (!message) { node.style.display = "none"; node.textContent = ""; return; }
  node.style.display = "";
  node.setAttribute("data-tone", tone || "");
  node.textContent = message;
}

function jhFmtTime(ms) {
  if (!ms || !isFinite(ms)) return "未知";
  try { return new Date(ms).toLocaleString(); } catch (e) { return "未知"; }
}

/** 能力查询：**不支持的按钮直接不渲染**（不是渲染后禁用）。 */
function jhCapability(id) {
  var list = (jethubState && jethubState.capabilities) || [];
  for (var i = 0; i < list.length; i++) {
    if (list[i].id === id) return list[i];
  }
  return { balance: false, checkin: false, onboarding: false };
}

function renderJetHubProviders() {
  var rail = el("jethub-providers");
  if (!rail) return;
  rail.innerHTML = "";
  var providers = (jethubState && jethubState.providers) || [];
  if (!providers.length) {
    rail.appendChild(h("div", "jh-empty", "（桥未运行）"));
    return;
  }
  providers.forEach(function (provider) {
    var row = document.createElement("button");
    row.setAttribute("type", "button");
    row.setAttribute("aria-selected", provider.id === jethubProvider ? "true" : "false");
    row.appendChild(h("span", "jh-dot"));
    row.appendChild(h("span", null, provider.label || provider.id));
    row.addEventListener("click", function () {
      if (jethubProvider === provider.id) return;
      jethubProvider = provider.id;
      // 记住选择：写进面板草稿，点「保存」即持久化到 JETHUB_PROVIDER
      drafts.JETHUB_PROVIDER = provider.id;
      renderJetHubProviders();
      refreshJetHub();
    });
    rail.appendChild(row);
  });
}

function renderJetHubBridge() {
  var box = el("jethub-bridge");
  if (!box) return;
  box.innerHTML = "";
  var bridge = (jethubState && jethubState.bridge) || {};
  var rows = [
    ["开关", jethubState && jethubState.enabled ? "已启用" : "已关闭"],
    ["桥", bridge.listening
      ? ("运行中（端口 " + bridge.port + "，PID " + ((bridge.health || {}).pid || "?") + "）")
      : "未运行"],
    ["Node", bridge.node_found ? bridge.node : "未找到（既无 runtime/node，PATH 里也没有 node）"],
    ["提供商", jethubProvider || "（未选择）"],
    ["状态目录", (jethubState && jethubState.storage || {}).state_dir || ""]
  ];
  rows.forEach(function (row) {
    box.appendChild(h("dt", null, row[0]));
    box.appendChild(h("dd", null, String(row[1])));
  });
  if (jethubState && jethubState.message) {
    jhNotice(el("jethub-login-hint"), "warn", jethubState.message);
  }
  if (bridge.listening && (jethubState.storage || {}).poolInPlugin === false) {
    jhNotice(el("jethub-login-hint"), "err", "⚠ 账号池不在插件目录内，请检查配置");
  }
}

function renderJetHubAccounts() {
  var box = el("jethub-accounts");
  if (!box) return;
  box.innerHTML = "";
  if (!jethubState) return;
  if (jethubState.accounts_error) {
    box.appendChild(h("div", "jh-notice", "读取账号失败：" + jethubState.accounts_error));
    return;
  }
  var accounts = jethubState.accounts || [];
  if (!accounts.length) {
    box.appendChild(h("div", "jh-empty",
      "这个提供商下还没有账号。点「新增账号」，会在浏览器里打开登录窗口。"));
    return;
  }
  var cap = jhCapability(jethubProvider);
  // 余额按 accountId 索引（桥逐账号返回，三态：ok / error / 未返回=读取中）
  var balanceMap = {};
  var balances = (jethubState.balances || {}).accounts || [];
  balances.forEach(function (row) { balanceMap[row.accountId] = row; });

  accounts.forEach(function (account) {
    var card = h("div", "jh-card");
    card.setAttribute("data-enabled", account.enabled ? "true" : "false");

    var top = h("div", "jh-cardTop");
    top.appendChild(h("span", "jh-status"));
    top.appendChild(h("span", "jh-name", account.nickname || account.id));
    var tag = h("span", "jh-tag", account.enabled ? "启用" : "停用");
    tag.setAttribute("data-tone", account.enabled ? "on" : "off");
    top.appendChild(tag);
    if (account.refreshable === false) {
      var noRefresh = h("span", "jh-tag", "不可续期");
      noRefresh.setAttribute("data-tone", "warn");
      top.appendChild(noRefresh);
    }
    card.appendChild(top);

    var meta = document.createElement("dl");
    meta.className = "jh-meta";
    meta.appendChild(h("dt", null, "凭据"));
    meta.appendChild(h("dd", null, account.id));
    meta.appendChild(h("dt", null, "有效期"));
    var expired = account.expiresAt && account.expiresAt < Date.now();
    var expiresText = jhFmtTime(account.expiresAt);
    meta.appendChild(h("dd", null, expired ? (expiresText + "（已过期）") : expiresText));

    if (cap.balance) {
      meta.appendChild(h("dt", null, "积分"));
      var dd = document.createElement("dd");
      var row = balanceMap[account.id];
      if (!row) {
        // **查不到 ≠ 0** —— 明确显示「读取中」而不是 0
        var pending = h("span", "jh-credit", "读取中…");
        pending.setAttribute("data-tone", "muted");
        dd.appendChild(pending);
      } else if (row.status !== "ok") {
        var fail = h("span", "jh-credit", "查询失败");
        fail.setAttribute("data-tone", "warn");
        fail.title = row.message || "";
        dd.appendChild(fail);
      } else {
        var value = row.balance || {};
        dd.appendChild(h("span", "jh-credit", value.label || "—"));
        // Loomy 的「永久 / 每日」两池。
        // ⚠ 字段名是 `packages`（vendor `loomy-credits.js:136-144` 返回
        // `{total, packages:[makePackage('永久积分',…), makePackage('每日赠送',…)]}`），
        // 不是 `pools`；而且**元素是对象**、金额在 `remaining` 里。
        // 首版读 `raw.pools` 且当数字用 → 这段**静默永不显示**。
        var packs = value.raw && value.raw.packages;
        if (Array.isArray(packs) && packs.length === 2) {
          var first = packs[0] || {};
          var second = packs[1] || {};
          var a = first.remaining !== undefined ? first.remaining : first.total;
          var b = second.remaining !== undefined ? second.remaining : second.total;
          if (a !== undefined || b !== undefined) {
            dd.appendChild(h("span", "jh-creditExtra",
              (first.name || "永久") + " " + (a === undefined ? "—" : a)
              + " · " + (second.name || "每日") + " " + (b === undefined ? "—" : b)));
          }
        }
        // 另有已失效额度时一并提示（同样来自 vendor 的 expiredTotal）
        var expiredTotal = value.raw && value.raw.expiredTotal;
        if (typeof expiredTotal === "number" && expiredTotal > 0) {
          dd.appendChild(h("span", "jh-creditExtra", "另有 " + expiredTotal + " 已失效"));
        }
      }
      meta.appendChild(dd);
    }
    card.appendChild(meta);

    var actions = h("div", "jh-actions");

    var toggle = h("button", "jh-btn", account.enabled ? "停用" : "启用");
    toggle.setAttribute("type", "button");
    toggle.addEventListener("click", function () {
      jhAccountAction("toggle", account.id, { enabled: !account.enabled });
    });
    actions.appendChild(toggle);

    // ⚠ 「刷新凭据」按钮**已移除**（用户要求）。
    //
    // 凭据刷新是**后台自动**的：桥在每次调用前走
    // `resolveUsableCredential()` —— 发现凭据过期（或即将过期，留 60 秒余量）
    // 就按账号自己的 credentialRef 主动刷新，再发请求。
    // 这与参考项目的行为一致：**用户不需要、也不应该手动刷**。
    // 手动按钮只会让人误以为「不点就不会刷新」。
    //
    // 账号卡上仍保留 `refreshable` 这一行信息（只读展示），
    // 让人知道这个账号的凭据是可以自动续期的。

    if (cap.onboarding) {
      var onboarding = h("button", "jh-btn", "领新手任务");
      onboarding.setAttribute("type", "button");
      onboarding.addEventListener("click", function () {
        onboarding.disabled = true;
        jhCall("/api/panel/jethub/credits",
          { action: "claimOnboarding", provider: jethubProvider }).then(function (payload) {
          onboarding.disabled = false;
          jhNotice(el("jethub-checkin-result"), payload.ok ? "ok" : "err",
            jhUnwrap(payload).message || payload.message || "");
          refreshJetHub();
        });
      });
      actions.appendChild(onboarding);
    }

    var remove = h("button", "jh-btn", "删除");
    remove.setAttribute("type", "button");
    remove.setAttribute("data-kind", "danger");
    remove.addEventListener("click", function () {
      if (!window.confirm("删除账号 " + (account.nickname || account.id)
          + "？其凭据也会一并删除。")) return;
      jhAccountAction("remove", account.id);
    });
    actions.appendChild(remove);

    card.appendChild(actions);
    box.appendChild(card);
  });
}

function jhAccountAction(action, accountId, extra, done) {
  var body = { action: action, account_id: accountId, provider: jethubProvider };
  if (extra) {
    for (var key in extra) {
      if (Object.prototype.hasOwnProperty.call(extra, key)) body[key] = extra[key];
    }
  }
  jhCall("/api/panel/jethub/account", body).then(function (payload) {
    if (done) done();
    if (!payload.ok) { toast(payload.message || "操作失败", "err"); return; }
    jethubState.accounts = jhUnwrap(payload).accounts || [];
    renderJetHubAccounts();
    toast("已更新", "ok");
  });
}

function renderJetHubModels() {
  var box = el("jethub-models");
  if (!box) return;
  box.innerHTML = "";
  if (!jethubState) return;
  if (jethubState.models_error) {
    box.appendChild(h("div", "jh-notice", "读取模型失败：" + jethubState.models_error));
    return;
  }
  var models = jethubState.models || [];
  // 记进缓存，供「当前选用模型」的可用性校验使用
  jhCacheModels(jethubProvider, models);
  if (!models.length) {
    box.appendChild(h("div", "jh-empty", "没有可用模型。先登录账号，再点「刷新模型」。"));
    return;
  }
  var disabledCount = 0;
  models.forEach(function (model) {
    if (model.disabled) disabledCount += 1;
    var row = h("div", "jh-modelRow");
    row.setAttribute("data-disabled", model.disabled ? "true" : "false");
    var info = h("div", "jh-modelInfo");
    info.appendChild(h("span", "jh-modelName", model.name || model.id));
    info.appendChild(h("span", "jh-modelId", model.id));
    row.appendChild(info);

    var toggle = document.createElement("input");
    toggle.type = "checkbox";
    toggle.className = "jh-switch";
    toggle.setAttribute("role", "switch");
    toggle.checked = !model.disabled;
    toggle.disabled = jethubBusy;
    toggle.addEventListener("change", function () {
      toggle.disabled = true;
      jhCall("/api/panel/jethub/model/toggle",
        { provider: jethubProvider, model_id: model.id, disabled: !toggle.checked })
        .then(function (payload) {
          if (!payload.ok) {
            toast(payload.message || "失败", "err");
            toggle.checked = !toggle.checked;
            toggle.disabled = false;
            return;
          }
          jethubState.models = jhUnwrap(payload).models || [];
          renderJetHubModels();
          toast("已更新模型显示列表", "ok");
          loadAllModels();
        });
    });
    row.appendChild(toggle);
    box.appendChild(row);
  });
  // 计数：一眼看出关掉了几个（黑名单制的可见性）
  box.insertBefore(
    h("div", "jh-empty", "共 " + models.length + " 个模型，已关闭 " + disabledCount + " 个。"),
    box.firstChild);
}

function renderJetHub() {
  if (!jethubState) return;
  if (!jethubProvider) {
    jethubProvider = jethubState.provider
      || drafts.JETHUB_PROVIDER
      || ((jethubState.providers || [])[0] || {}).id
      || "buddy";
  }
  renderJetHubProviders();
  renderJetHubBridge();
  renderJetHubAccounts();
  renderJetHubModels();

  // 能力门控：不支持的按钮**直接隐藏**
  var cap = jhCapability(jethubProvider);
  var balanceBtn = el("btn-jethub-balances");
  if (balanceBtn) balanceBtn.style.display = cap.balance ? "" : "none";
  var checkinBtn = el("btn-jethub-checkin");
  if (checkinBtn) checkinBtn.style.display = cap.checkin ? "" : "none";
}

function refreshJetHub() {
  return jhCall("/api/panel/jethub/status", { provider: jethubProvider })
    .then(function (payload) {
      if (!payload.ok) {
        toast(payload.message || "读取云端 OAuth 状态失败", "err");
        return;
      }
      jethubState = jhUnwrap(payload);
      if (jethubState.provider) jethubProvider = jethubProvider || jethubState.provider;
      renderJetHub();
    });
}

function jhBind(id, handler) {
  var node = el(id);
  if (node) node.addEventListener("click", handler);
}

jhBind("btn-jethub-refresh", function () {
  refreshJetHub().then(function () { toast("已刷新", "ok"); });
});

jhBind("btn-jethub-ensure", function () {
  toast("正在启动桥…");
  jhCall("/api/panel/jethub/ensure", { restart: true }).then(function (payload) {
    if (!payload.ok) { toast(payload.message || "启动失败", "err"); return; }
    jethubState = jhUnwrap(payload);
    renderJetHub();
    toast("桥已就绪", "ok");
    loadAllModels();
  });
});

jhBind("btn-jethub-stop", function () {
  jhCall("/api/panel/jethub/stop", {}).then(function (payload) {
    toast(jhUnwrap(payload).message || payload.message || "已停止", payload.ok ? "ok" : "err");
    refreshJetHub();
  });
});

/* 两步式登录：先拿 URL **立刻** window.open（保住浏览器手势），再轮询结果。
   绝不能在 await 完整个登录流程后才开窗 —— 那时手势已过期，弹窗被拦。 */
jhBind("btn-jethub-login", function () {
  var nickname = (el("jethub-nickname") || {}).value || "";
  var hint = el("jethub-login-hint");
  jhNotice(hint, "warn", "正在获取登录地址…");
  jhCall("/api/panel/jethub/login/start",
    { provider: jethubProvider, nickname: nickname }).then(function (payload) {
    if (!payload.ok) {
      jhNotice(hint, "err", payload.message || "启动登录失败");
      toast(payload.message || "启动登录失败", "err");
      return;
    }
    var data = jhUnwrap(payload);
    if (data.login_url) {
      var opened = window.open(data.login_url, "_blank", "width=900,height=700");
      if (!opened) {
        // 弹窗被拦：显示地址让用户手工点，**绝不** location.href 跳转
        hint.style.display = "";
        hint.setAttribute("data-tone", "warn");
        hint.innerHTML = "弹窗被拦截，请手工打开：<a href=\"" + data.login_url
          + "\" target=\"_blank\" rel=\"noreferrer\">" + data.login_url + "</a>";
      } else {
        jhNotice(hint, "warn", data.note || "已打开登录窗口，请在浏览器里完成授权…");
      }
    } else {
      jhNotice(hint, "warn", data.note || "该提供商的登录在后台进行，请稍候…");
    }
    toast("请在浏览器里完成授权", "ok");
    pollJethubLogin(data.account_id, jethubProvider, nickname);
  });
});

function pollJethubLogin(accountId, provider, nickname) {
  if (jethubLoginTimer) clearInterval(jethubLoginTimer);
  var hint = el("jethub-login-hint");
  var tries = 0;
  jethubLoginTimer = setInterval(function () {
    tries += 1;
    if (tries > 300) {  // 5 分钟（1 秒一次）
      clearInterval(jethubLoginTimer);
      jethubLoginTimer = null;
      jhNotice(hint, "err", "登录超时，请重试。");
      return;
    }
    jhCall("/api/panel/jethub/login/poll",
      { account_id: accountId, provider: provider }).then(function (payload) {
      if (!payload.ok) return;  // 单次失败不中断轮询
      var data = jhUnwrap(payload);
      if (data.message) jhNotice(hint, "warn", data.message);
      if (!data.done) return;
      clearInterval(jethubLoginTimer);
      jethubLoginTimer = null;
      if (data.success) {
        toast("登录成功，账号已加入", "ok");
        if (el("jethub-nickname")) el("jethub-nickname").value = "";
        jhNotice(hint, "ok", "登录成功。");
        setTimeout(function () { jhNotice(hint, "", ""); }, 4000);
        refreshJetHub().then(function () { loadAllModels(); });
      } else {
        jhNotice(hint, "err", data.message || "登录失败");
      }
    });
  }, 1000);
}

jhBind("btn-jethub-balances", function () {
  var btn = el("btn-jethub-balances");
  btn.disabled = true;
  jhCall("/api/panel/jethub/credits", { action: "balances", provider: jethubProvider })
    .then(function (payload) {
      btn.disabled = false;
      if (!payload.ok) { toast(payload.message || "查询失败", "err"); return; }
      if (!jethubState) jethubState = {};
      jethubState.balances = jhUnwrap(payload);
      renderJetHubAccounts();
      toast("积分已刷新", "ok");
    });
});

/* 一键签到：桥侧**串行**执行（跨渠道并发会触发风控，见 docs/JETHUB.md） */
jhBind("btn-jethub-checkin", function () {
  var btn = el("btn-jethub-checkin");
  var box = el("jethub-checkin-result");
  btn.disabled = true;
  jhNotice(box, "warn", "正在逐家签到（串行执行，可能耗时较久）…");
  jhCall("/api/panel/jethub/credits", { action: "claimAll" }).then(function (payload) {
    btn.disabled = false;
    if (!payload.ok) { jhNotice(box, "err", payload.message || "签到失败"); return; }
    var data = jhUnwrap(payload);
    jhNotice(box, data.total_failed > 0 ? "warn" : "ok", data.message || "签到完成");
    // 失败明细：**每个非零计数都要出现**（不用 else-if 短路）
    var failed = (data.results || []).filter(function (r) { return r.failed > 0; });
    if (failed.length) {
      var ul = document.createElement("ul");
      failed.forEach(function (r) {
        (r.messages || []).slice(0, 6).forEach(function (msg) {
          ul.appendChild(h("li", null, (r.label || r.provider) + "：" + msg));
        });
      });
      box.appendChild(ul);
    }
    refreshJetHub();
  });
});

jhBind("btn-jethub-models", function () {
  refreshJetHub().then(function () {
    toast("已刷新模型清单", "ok");
    loadAllModels();
  });
});

function jhBulkModels(disabled) {
  var label = disabled ? "关闭全部" : "打开全部";
  // 关闭全部是**批量写**（逐项加黑名单），二次确认
  if (disabled && !window.confirm("关闭全部模型？它们会从 OCV 的模型下拉里消失。")) return;
  jethubBusy = true;
  renderJetHubModels();
  toast(label + "中…");
  request("/api/panel/jethub/models/all",
    { method: "POST", body: { provider: jethubProvider, disabled: disabled } })
    .then(function (payload) {
      jethubBusy = false;
      if (!payload.ok) { toast(payload.message || "失败", "err"); refreshJetHub(); return; }
      jethubState.models = jhUnwrap(payload).models || [];
      renderJetHubModels();
      toast(label + "完成", "ok");
      loadAllModels();
    });
}

jhBind("btn-jethub-models-all-on", function () { jhBulkModels(false); });
jhBind("btn-jethub-models-all-off", function () { jhBulkModels(true); });

jhBind("btn-jethub-test", function () {
  var box = el("jethub-test-result");
  jhNotice(box, "warn", "测试中（最长 2 分钟）…");
  var model = drafts.JETHUB_MODEL
    || (state && state.values && state.values.JETHUB_MODEL) || "";
  jhCall("/api/panel/jethub/test", { model: model, provider: jethubProvider })
    .then(function (payload) {
      var data = jhUnwrap(payload);
      if (!payload.ok) {
        jhNotice(box, "err", payload.message || data.message || "测试失败");
        return;
      }
      jhNotice(box, data.ok ? "ok" : "err", data.message || "");
    });
});

/* ==================== 备份 / 恢复（本地上传，直接展开在面板中） ====================
   用户要求两件事，这里都落实：

   1. **不要另外弹窗** —— 备份与恢复各自成为一张常驻卡片，直接在页面里操作，
      不再有 modal（原先那个 `jh-backup-modal` 已删除）。
   2. **点「选择文件」要真的跳出系统文件选择框** —— 这一点原先**是坏的**：
      modal 被插在**脚本结束标签之后**，而 `jhBind()` 在脚本执行时就跑完了，
      绑定那一刻元素还不存在 → 静默跳过 → 按钮点了没反应。
      现在元素就在本卡片内、且**先于脚本**出现，绑定必然成功。

   （注意：本文件是注入到 script 元素内部的，所以**任何地方都不能出现
   script 元素的标签文本** —— 连注释里也不行，那会提前关闭脚本块。
   本次开发在这个坑上连踩三次，见 docs/JETHUB.md F-004。）

   为稳妥起见，这里的绑定走 `jhBindWhenReady()`：元素已存在就立刻绑，
   否则等一小段（最多若干次）—— 这样无论将来卡片被移到哪里都不会再静默失效。 */

function jhBackupExport(saveServer) {
  jhCall("/api/panel/jethub/backup", { action: "export", save_server: !!saveServer })
    .then(function (payload) {
      if (!payload.ok) { toast(payload.message || "导出失败", "err"); return; }
      var data = jhUnwrap(payload);
      if (data.document) {
        // Blob + <a download> 触发「保存到本地」—— 用户自己挑存哪
        var blob = new Blob([JSON.stringify(data.document, null, 2)],
          { type: "application/json" });
        var url = URL.createObjectURL(blob);
        var a = document.createElement("a");
        a.href = url;
        a.download = data.name || "cloud-oauth-backup.json";
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setTimeout(function () { URL.revokeObjectURL(url); }, 5000);
        toast(data.message || "备份已下载到本地", "ok");
      } else {
        toast(data.message || "已留档", "ok");
        refreshJetHub();
      }
    });
}

function jhBackupImport(file) {
  if (!file) return;
  var mode = window.confirm(
    "恢复模式：\n\n【确定】= 合并（同名覆盖、其余保留）\n【取消】= 替换（先清空再写入）")
    ? "merge" : "replace";
  if (mode === "replace" && !window.confirm("替换会先清空现有账号与凭据，确定继续？")) return;
  var reader = new FileReader();
  reader.onload = function () {
    jhCall("/api/panel/jethub/backup", {
      action: "import",
      document: String(reader.result || ""),
      file_name: file.name,
      mode: mode
    }).then(function (payload) {
      if (!payload.ok) { toast(payload.message || "恢复失败", "err"); return; }
      toast(jhUnwrap(payload).message || "已恢复", "ok");
      refreshJetHub();
    });
  };
  reader.onerror = function () { toast("读取文件失败", "err"); };
  reader.readAsText(file);
}

/**
 * 元素就绪后再绑定（幂等，最多等 ~6 秒）。
 *
 * 存在的理由见上面注释：原先因为 modal 排在脚本之后，`jhBind` 静默失效，
 * 用户点「选择文件」毫无反应。这个包装让「绑定时机」不再依赖
 * 「元素恰好写在脚本前面」这种脆弱假设。
 */
function jhBindWhenReady(id, handler, triesLeft) {
  var node = el(id);
  if (node) {
    if (node.__jhBoundHandler === handler) return;
    node.__jhBoundHandler = handler;
    node.addEventListener("click", handler);
    return;
  }
  var left = triesLeft === undefined ? 30 : triesLeft;
  if (left <= 0) return;
  setTimeout(function () { jhBindWhenReady(id, handler, left - 1); }, 200);
}

jhBindWhenReady("btn-jh-backup-export-download", function () { jhBackupExport(false); });
jhBindWhenReady("btn-jh-backup-export-server", function () { jhBackupExport(true); });

/* 「选择备份文件」：唤起隐藏的 <input type=file>。
   注意必须先 `value = ""` 再 `click()` —— 否则连续选**同一个文件**时
   change 事件不会触发（浏览器认为值没变），用户会以为按钮坏了。 */
jhBindWhenReady("btn-jh-backup-pick", function () {
  var input = el("jh-backup-file");
  if (!input) { toast("找不到文件选择控件", "err"); return; }
  input.value = "";
  input.click();
});

jhBindWhenReady("btn-jh-backup-import", function () {
  if (!jethubPendingFile) { toast("请先点「选择备份文件…」挑一个备份", "err"); return; }
  jhBackupImport(jethubPendingFile);
});

/* file input 的 change：记住选中的文件并回显文件名 */
(function () {
  // ⚠ 重试必须**有上限**（F-014：首版是无上限的 setTimeout 递归）。
  // 若元素因某种原因始终不存在，无上限重试会让定时器永久驻留。
  function bindInput(triesLeft) {
    var input = el("jh-backup-file");
    if (!input) {
      if (triesLeft > 0) setTimeout(function () { bindInput(triesLeft - 1); }, 200);
      return;
    }
    if (input.__jhBoundChange) return;
    input.__jhBoundChange = true;
    input.addEventListener("change", function () {
      var file = input.files && input.files[0];
      jethubPendingFile = file || null;
      var label = el("jh-backup-filename");
      if (label) {
        label.textContent = file ? file.name : "（未选择文件）";
        label.setAttribute("data-picked", file ? "true" : "false");
      }
      if (file) toast("已选择：" + file.name, "ok");
    });
  }
  bindInput(30);
})();


/* ==================== 语言模型来源：互斥可见性 ====================
   用户要求：「语言模型和云端 OAuth 两项都是 LLM，不能同时使用，
   在语言模型中给用户下拉选择」；「云端 OAuth 页只用于配置账号，
   不用于选择具体用哪个模型」。

   所以：
     * 来源下拉（LLM_SOURCE）永远可见；
     * 标了 `source: "custom"` 的字段只在选「自行填写 API」时显示；
     * 标了 `source: "jethub"` 的字段只在选「云端 OAuth」时显示；
     * 没标 `source` 的字段（深度思考、重试、Agent 1B…）两路通用，始终显示。

   实现靠给字段外层 `div.field` 打 **`data-source` 属性** —— 这样
   **不必逐个记住字段名**：后端加字段时只要写上 `source` 就自动生效。
   属性由下面这段在渲染后统一补（`buildField` 是既有代码，不动它）。 */

function jhCurrentSource() {
  var node = document.querySelector('[data-key="LLM_SOURCE"]');
  if (node && node.value) return node.value;
  if (drafts.LLM_SOURCE) return drafts.LLM_SOURCE;
  if (state && state.values && state.values.LLM_SOURCE) return state.values.LLM_SOURCE;
  if (state && state.llm_source) return state.llm_source;
  return "custom";
}

/** 给每个字段外层打上 data-source（幂等）。 */
function jhTagFieldSources() {
  if (!state || !state.fields) return;
  state.fields.forEach(function (field) {
    if (!field.source) return;
    var node = document.querySelector('[data-key="' + field.key + '"]');
    if (!node) return;
    var wrap = node.closest(".field") || node.parentElement;
    // 已在同一个 wrap 上打过同样的值就跳过 —— 避免无谓的 DOM 写入
    if (wrap && wrap.getAttribute("data-source") !== field.source) {
      wrap.setAttribute("data-source", field.source);
    }
  });
}

/**
 * 按 provider 缓存的模型清单（`{buddy: [{id, name, disabled}, …]}`）。
 *
 * 用于「当前选用模型」的**可用性校验** —— 光有 `provider::model` 字符串
 * 看不出这个模型还在不在、有没有被「显示列表」关掉。
 * 由 `renderJetHubModels()` 与 `jhBulkModels()` 在拿到新清单时刷新。
 */
var jethubModelsByProvider = {};

/** 把一次 `models.list` 的结果记进缓存（同时兼容 `{models:[…]}` 与裸数组）。 */
function jhCacheModels(providerId, models) {
  if (!providerId) return;
  if (Array.isArray(models)) {
    jethubModelsByProvider[String(providerId)] = models;
  } else if (models && Array.isArray(models.models)) {
    jethubModelsByProvider[String(providerId)] = models.models;
  }
}

/**
 * 把 `<provider>::<model>` 渲染成人看得懂的一行。
 *
 * 值本身是给桥用的（按 `::` 拆分路由），但**用户需要看到「哪家 · 哪个模型」**
 * —— 用户明确要求「要显示当前选用哪个云端 OAuth 的模型」。
 *
 * 顺带校验它**是否还在当前可用清单里**：账号被删了、或该模型被
 * 「显示列表」关掉了，这里就要明确提示 —— 否则用户只会在调用时遇到
 * 「还没有任何账号」或「模型不可用」这种指不到根因的报错。
 */
var JH_PROVIDER_NAMES = {
  buddy: "CodeBuddy（腾讯）", workbuddy: "WorkBuddy（腾讯国际版）",
  lobsterai: "LobsterAI（有道）", qoder: "Qoder（阿里）", trae: "TRAE（字节）",
  cline: "Cline", loomy: "Loomy（讯飞）", codearts: "CodeArts（华为云）",
  atomcode: "AtomCode（AtomGit）",
};

function jhDescribeModel(value) {
  if (!value) return null;
  var text = String(value);
  var idx = text.indexOf("::");
  if (idx <= 0) {
    // 手填的裸 model id：没有提供商前缀，桥会用默认提供商兜底
    return { title: text, note: "未带提供商前缀，将用默认提供商", tone: "warn" };
  }
  var providerId = text.slice(0, idx).trim();
  var modelId = text.slice(idx + 2).trim();
  var providerLabel = JH_PROVIDER_NAMES[providerId] || providerId;
  var result = {
    title: providerLabel + " · " + modelId,
    note: "",
    tone: "",
  };
  // 在已拉取的清单里核对可用性（清单按 provider 缓存在 jethubModelsByProvider）
  var rows = (jethubModelsByProvider || {})[providerId];
  if (Array.isArray(rows) && rows.length > 0) {
    var hit = null;
    for (var i = 0; i < rows.length; i++) {
      if (rows[i] && String(rows[i].id) === modelId) { hit = rows[i]; break; }
    }
    if (hit === null) {
      result.note = "不在该提供商的模型清单里（可能已下线）";
      result.tone = "warn";
    } else if (hit.disabled) {
      result.note = "该模型已被「显示列表」关闭，OCV 里选不到它";
      result.tone = "warn";
    }
  }
  return result;
}

function jhApplySourceVisibility() {
  var source = jhCurrentSource();
  Array.prototype.forEach.call(document.querySelectorAll("[data-source]"), function (row) {
    var want = row.getAttribute("data-source");
    // `show` = 当前来源要显示的；`hide` = 不显示
    // `both` 两路都显示；其余按来源匹配
    var show = (want === "both" || want === source);
    row.style.display = show ? "" : "none";
    // ⚠ **只隐藏，不卸载**：字段的 DOM 保留着，草稿值（drafts）也保留。
    //   这样「切到云端 OAuth 再切回来」时，之前填的 API Key / 地址 / 模型
    //   都还在 —— 否则用户会以为配置丢了（那是个更糟的体验）。
    row.setAttribute("data-hidden-by-source", show ? "false" : "true");
  });

  var hint = el("llm-source-hint");
  if (!hint) return;
  if (source === "jethub") {
    // 用户要求：「要显示当前选用哪个云端 OAuth 的模型」
    var current = drafts.JETHUB_MODEL !== undefined
      ? drafts.JETHUB_MODEL
      : ((state && state.values && state.values.JETHUB_MODEL) || "");
    var desc = jhDescribeModel(current);
    var lines = ['<div><b>当前使用：云端 OAuth</b>（模型来自你已登录的账号）</div>'];
    if (desc === null) {
      lines.push('<div class="jh-sourceWarn">⚠ 还没选模型 —— 请在下面「云端 OAuth 模型」里选一个。</div>');
      hint.setAttribute("data-tone", "warn");
    } else {
      lines.push('<div>当前选用模型：<b>' + jhEscape(desc.title) + '</b></div>');
      if (desc.note) {
        lines.push('<div class="jh-sourceWarn">⚠ ' + jhEscape(desc.note) + '</div>');
        hint.setAttribute("data-tone", "warn");
      } else {
        hint.setAttribute("data-tone", "ok");
      }
    }
    hint.innerHTML = lines.join("");
  } else {
    var m = drafts.SENSENOVA_MODEL !== undefined
      ? drafts.SENSENOVA_MODEL
      : ((state && state.values && state.values.SENSENOVA_MODEL) || "");
    hint.innerHTML = '<div><b>当前使用：自行填写 API</b>'
      + (m ? '（模型 ' + jhEscape(String(m)) + '）' : '')
      + ' —— 模型与服务商由下面的字段决定。</div>';
    hint.setAttribute("data-tone", "");
  }
}

/** 极简 HTML 转义（只用于我们把值插入 innerHTML 的场景）。 */
function jhEscape(text) {
  return String(text === undefined || text === null ? "" : text)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function jhRefreshSourceUI() {
  jhTagFieldSources();
  jhApplySourceVisibility();
}

/* 来源下拉一变就立刻切换可见性（不必等保存） */
(function () {
  function bind() {
    var node = document.querySelector('[data-key="LLM_SOURCE"]');
    if (!node || node.__jhBound) return false;
    node.__jhBound = true;
    node.addEventListener("change", function () {
      drafts.LLM_SOURCE = node.value;
      jhApplySourceVisibility();
      // 切到云端 OAuth 时顺手拉一次模型清单（用户要求在这一页选模型）
      if (node.value === "jethub" && typeof loadAllModels === "function") loadAllModels();
    });
    return true;
  }

  // ⚠ 首次绑定可以在有限次轮询内完成，但**重新打标不能停**（F-027）。
  //
  // 保存流程是：`save()` → `render()` → `renderGroup()` 执行
  // `card.innerHTML = ""` 重建整个卡片 —— 我们在 DOM 上打的 `data-source`
  // **连着被清掉**，于是所有字段都显示出来（界面退回「API Key」形态），
  // 用户必须关掉再打开面板才恢复。
  //
  // 首版靠一个有上限的轮询（`tries > 30` 后 clearInterval）来打标，
  // 而保存发生在轮询结束之后 → 必然复现。
  //
  // 所以：轮询只负责**首次绑定**；打标交给 MutationObserver **长期守着**。
  //
  // ⚠ 清理条件必须**无条件**带上限（F-014：写成 `ready && tries > 30`
  //   的话，元素始终找不到时 interval 会永久驻留）。
  var tries = 0;
  var timer = setInterval(function () {
    var ready = bind();
    jhRefreshSourceUI();
    tries += 1;
    if (tries > 30) clearInterval(timer);
  }, 400);

  var watchTries = 0;
  var watcher = setInterval(function () {
    var card = el("card-llm");
    if (!card) {
      if (++watchTries > 30) clearInterval(watcher);
      return;
    }
    if (!card.__jhSourceObserved) {
      card.__jhSourceObserved = true;
      // 观察 childList（renderGroup 清空重画）与子树变化；
      // 回调里同步重打标 + 重算可见性，避免闪烁。
      var observer = new MutationObserver(function () {
        jhRefreshSourceUI();
      });
      observer.observe(card, { childList: true, subtree: false });
      card.__jhSourceObserver = observer;
    }
    jhRefreshSourceUI();
    clearInterval(watcher);
  }, 400);
})();

/* 把「当前来源」提示条插到语言模型卡片顶部（幂等）。
   它是纯前端元素，不进 FIELDS —— 因为它不是配置项，只是状态说明。

   ⚠ **必须在每次 renderGroup 之后重建**（F-021 的真实缺陷）：
   面板的 `renderGroup()` 会执行 `card.innerHTML = ""` 把卡片清空重画，
   于是插进 `card-llm` 的提示条**每次渲染都被擦掉** —— 实测首次加载后
   它就消失了（`hintExists: false`）。
   所以这里的 `ensureHint()` 会被 `MutationObserver` 盯着卡片，
   被清掉就立刻补回来。 */
function jhEnsureSourceHint() {
  var card = el("card-llm");
  if (!card) return false;
  if (el("llm-source-hint")) return true;
  var note = document.createElement("div");
  note.className = "jh-notice";
  note.id = "llm-source-hint";
  card.insertBefore(note, card.firstChild);
  // 补回来后立刻填内容（否则会短暂空白）
  jhApplySourceVisibility();
  return true;
}

(function () {
  // 首次：轮询等 card-llm 出现（renderGroup 是异步的）
  var tries = 0;
  var timer = setInterval(function () {
    if (jhEnsureSourceHint()) { clearInterval(timer); jhRefreshSourceUI(); }
    if (++tries > 30) clearInterval(timer);
  }, 400);

  // 之后：盯着 card-llm 的子节点变化，被 renderGroup 清空就补回来
  var watchTries = 0;
  var watcher = setInterval(function () {
    var card = el("card-llm");
    if (!card) { if (++watchTries > 30) clearInterval(watcher); return; }
    if (!card.__jhObserved) {
      card.__jhObserved = true;
      var observer = new MutationObserver(function () {
        // 同步补回，避免闪烁
        jhEnsureSourceHint();
      });
      observer.observe(card, { childList: true });
      card.__jhObserver = observer;
    }
    jhEnsureSourceHint();
    clearInterval(watcher);
  }, 400);
})();



