/* OCV 云端免费栈 · 前端入口脚本
 *
 * 由 cloud_stack_ctl.py 往 frontend/index.html 注入一行 <script src=".../panel.js">，
 * 其余全部在这里完成，OCV 的 Vue 代码一行都不参与。
 *
 * 只做三件事：
 *   1. 挂一个悬浮按钮（可拖动、双击打开），点开是配置面板（iframe，样式与 OCV 完全隔离）
 *   2. 把 OCV 配音引擎下拉里的 "Qwen-TTS" 标注成 MiMo，避免用户以为要填 DashScope Key
 *   3. 记住按钮位置，免得它长期压在 OCV 自己的按钮上
 *
 * 刻意不往 Vue 管理的 DOM 区域插入任何节点：Vue 重渲染会把插入的节点删掉，
 * 我们的观察器又补回来，会形成抖动。改 option 文案是无害的，且用节流+差分避免循环。
 */
(function () {
  "use strict";

  if (window.__ocvCloudStackLoaded) return;
  window.__ocvCloudStackLoaded = true;

  var PREFIX = "ocvcs";

  function currentScript() {
    if (document.currentScript) return document.currentScript;
    var list = document.getElementsByTagName("script");
    for (var i = list.length - 1; i >= 0; i -= 1) {
      if (/\/panel\.js(\?|$)/.test(list[i].src || "")) return list[i];
    }
    return null;
  }

  var script = currentScript();
  var API_BASE = (script && script.getAttribute("data-api")) ||
    (script && script.src ? script.src.replace(/\/panel\.js(\?.*)?$/, "") : "http://127.0.0.1:8799");
  var OCV_URL = "http://127.0.0.1:5173";

  // 探测面板可用性 / 配置完整度
  function checkState() {
    return fetch(API_BASE + "/api/panel/state", { cache: "no-store" })
      .then(function (response) { return response.json(); })
      .then(function (payload) {
        if (!payload || !payload.ok) return null;
        return payload.data;
      })
      .catch(function () { return null; });
  }

  function injectStyles() {
    if (document.getElementById(PREFIX + "-styles")) return;
    var style = document.createElement("style");
    style.id = PREFIX + "-styles";
    style.textContent = [
      "." + PREFIX + "-btn{position:fixed;right:18px;bottom:18px;z-index:2147483000;",
      "display:flex;align-items:center;gap:7px;height:38px;padding:0 15px;border:none;",
      "border-radius:19px;background:#1f2329;color:#fff;font:600 12.5px/1 'Microsoft YaHei','PingFang SC',sans-serif;",
      "cursor:grab;touch-action:none;user-select:none;-webkit-user-select:none;",
      "box-shadow:0 6px 20px rgba(0,0,0,.28);transition:transform .12s ease,opacity .12s ease;}",
      "." + PREFIX + "-btn:hover{transform:translateY(-1px);}",
      "." + PREFIX + "-btn." + PREFIX + "-dragging{cursor:grabbing;transition:none;",
      "transform:none;box-shadow:0 10px 26px rgba(0,0,0,.34);}",
      "." + PREFIX + "-dot{width:7px;height:7px;border-radius:50%;background:#4ade80;box-shadow:0 0 0 3px rgba(74,222,128,.22);}",
      "." + PREFIX + "-btn[data-state='bad'] ." + PREFIX + "-dot{background:#f87171;box-shadow:0 0 0 3px rgba(248,113,113,.22);}",
      "." + PREFIX + "-btn[data-state='unknown'] ." + PREFIX + "-dot{background:#fbbf24;box-shadow:0 0 0 3px rgba(251,191,36,.22);}",
      "." + PREFIX + "-mask{position:fixed;inset:0;z-index:2147483001;background:rgba(15,17,21,.45);",
      "display:flex;align-items:center;justify-content:center;padding:24px;}",
      "." + PREFIX + "-box{width:min(920px,100%);height:min(760px,92vh);background:#fff;border-radius:14px;",
      "overflow:hidden;box-shadow:0 24px 70px rgba(0,0,0,.35);display:flex;flex-direction:column;}",
      "." + PREFIX + "-bar{display:flex;align-items:center;gap:10px;padding:10px 14px;border-bottom:1px solid #e3e6ea;",
      "font:600 13px/1.4 'Microsoft YaHei','PingFang SC',sans-serif;color:#1f2329;background:#fbfbfc;}",
      "." + PREFIX + "-close{margin-left:auto;border:1px solid #e3e6ea;background:#fff;border-radius:8px;",
      "padding:5px 12px;cursor:pointer;font-size:12px;color:#1f2329;}",
      "." + PREFIX + "-close:hover{border-color:#2f6fed;color:#2f6fed;}",
      "." + PREFIX + "-frame{flex:1 1 auto;width:100%;border:none;background:#f6f7f9;}"
    ].join("");
    document.head.appendChild(style);
  }

  var mask = null;

  function closePanel() {
    if (!mask) return;
    var node = mask;
    mask = null;
    node.remove();
    document.removeEventListener("keydown", onKey);
  }

  function onKey(event) {
    if (event.key === "Escape") closePanel();
  }

  function openPanel() {
    if (mask) return;
    injectStyles();
    mask = document.createElement("div");
    mask.className = PREFIX + "-mask";
    mask.addEventListener("click", function (event) {
      if (event.target === mask) closePanel();
    });

    var box = document.createElement("div");
    box.className = PREFIX + "-box";

    var bar = document.createElement("div");
    bar.className = PREFIX + "-bar";
    var title = document.createElement("span");
    title.textContent = "云端免费栈 · 配置（MiMo 配音 / 商汤模型）";
    bar.appendChild(title);

    var openExternal = document.createElement("button");
    openExternal.className = PREFIX + "-close";
    openExternal.type = "button";
    openExternal.textContent = "新窗口打开";
    openExternal.addEventListener("click", function () {
      window.open(API_BASE + "/panel", "_blank");
    });
    bar.appendChild(openExternal);

    var close = document.createElement("button");
    close.className = PREFIX + "-close";
    close.type = "button";
    close.textContent = "关闭";
    close.addEventListener("click", closePanel);
    bar.appendChild(close);
    box.appendChild(bar);

    var frame = document.createElement("iframe");
    frame.className = PREFIX + "-frame";
    frame.src = API_BASE + "/panel";
    frame.title = "云端免费栈配置面板";
    box.appendChild(frame);

    mask.appendChild(box);
    document.body.appendChild(mask);
    document.addEventListener("keydown", onKey);
  }

  // ---- 悬浮按钮的位置：单击（按住）拖动，双击才打开 ----
  //
  // 按钮固定在右下角会盖住 OCV 自己的按钮，所以改成可拖动并把位置记下来。
  // 交互口径：按下即可拖，不位移的两次点击 = 双击 = 打开面板，单击不作任何事
  // （单击只用于"按住别动"，不是操作手势）。键盘 Enter / Space 仍然能打开，
  // 那种合成 click 的 event.detail 为 0，可以据此区分。
  var POS_KEY = PREFIX + ":btn-pos";
  var DRAG_THRESHOLD = 4;
  var EDGE_MARGIN = 4;

  function readSavedPosition() {
    try {
      var raw = window.localStorage.getItem(POS_KEY);
      if (!raw) return null;
      var parsed = JSON.parse(raw);
      var left = Number(parsed && parsed.left);
      var top = Number(parsed && parsed.top);
      if (!isFinite(left) || !isFinite(top)) return null;
      return { left: left, top: top };
    } catch (error) {
      return null;
    }
  }

  function savePosition(left, top) {
    try {
      window.localStorage.setItem(POS_KEY, JSON.stringify({ left: left, top: top }));
    } catch (error) {
      /* 隐私模式等写不了就算了，位置只是便利，不影响功能 */
    }
  }

  // 坐标一律钳在视口里：换了小屏或改过分辨率之后，别把按钮丢到看不见的地方
  function clampPosition(left, top, width, height) {
    var maxLeft = Math.max(EDGE_MARGIN, window.innerWidth - width - EDGE_MARGIN);
    var maxTop = Math.max(EDGE_MARGIN, window.innerHeight - height - EDGE_MARGIN);
    return {
      left: Math.min(Math.max(EDGE_MARGIN, left), maxLeft),
      top: Math.min(Math.max(EDGE_MARGIN, top), maxTop)
    };
  }

  function placeButton(button, left, top, width, height) {
    var spot = clampPosition(left, top, width, height);
    button.style.left = spot.left + "px";
    button.style.top = spot.top + "px";
    // 一旦拖动过就改用 left/top 定位，必须清掉 right/bottom，否则两套定位会打架
    button.style.right = "auto";
    button.style.bottom = "auto";
    return spot;
  }

  function restorePosition(button) {
    var saved = readSavedPosition();
    if (!saved) return;
    var rect = button.getBoundingClientRect();
    placeButton(button, saved.left, saved.top, rect.width, rect.height);
  }

  function makeDraggable(button) {
    var dragging = false;
    var moved = false;
    var startX = 0;
    var startY = 0;
    var originLeft = 0;
    var originTop = 0;
    var width = 0;
    var height = 0;

    button.addEventListener("pointerdown", function (event) {
      if (event.button !== 0) return;
      var rect = button.getBoundingClientRect();
      dragging = true;
      moved = false;
      startX = event.clientX;
      startY = event.clientY;
      originLeft = rect.left;
      originTop = rect.top;
      width = rect.width;
      height = rect.height;
      button.classList.add(PREFIX + "-dragging");
      try { button.setPointerCapture(event.pointerId); } catch (error) { /* 捕获失败不影响拖动 */ }
    });

    button.addEventListener("pointermove", function (event) {
      if (!dragging) return;
      var dx = event.clientX - startX;
      var dy = event.clientY - startY;
      // 阈值内不算拖动：手抖 1-2 像素不该被当成拖拽
      if (!moved && Math.abs(dx) < DRAG_THRESHOLD && Math.abs(dy) < DRAG_THRESHOLD) return;
      moved = true;
      placeButton(button, originLeft + dx, originTop + dy, width, height);
    });

    function finish(event) {
      if (!dragging) return;
      dragging = false;
      button.classList.remove(PREFIX + "-dragging");
      try { button.releasePointerCapture(event.pointerId); } catch (error) { /* 已释放 */ }
      if (moved) {
        var rect = button.getBoundingClientRect();
        savePosition(rect.left, rect.top);
      }
    }

    button.addEventListener("pointerup", finish);
    button.addEventListener("pointercancel", finish);
  }

  function mountButton() {
    if (document.getElementById(PREFIX + "-btn")) return;
    injectStyles();
    var button = document.createElement("button");
    button.id = PREFIX + "-btn";
    button.className = PREFIX + "-btn";
    button.type = "button";
    button.dataset.state = "unknown";
    button.title = "拖动可移动位置，双击打开配置面板";

    var dot = document.createElement("span");
    dot.className = PREFIX + "-dot";
    button.appendChild(dot);
    var label = document.createElement("span");
    label.textContent = "云端免费栈";
    button.appendChild(label);

    makeDraggable(button);
    button.addEventListener("dblclick", function (event) {
      event.preventDefault();
      openPanel();
    });
    // 只有键盘触发的合成 click（detail === 0）才当"打开"，
    // 鼠标单击留给拖动，不弹面板
    button.addEventListener("click", function (event) {
      if (event.detail === 0) openPanel();
    });

    document.body.appendChild(button);
    restorePosition(button);
    refreshBadge();
  }

  function refreshBadge() {
    checkState().then(function (data) {
      var button = document.getElementById(PREFIX + "-btn");
      if (!button) return;
      if (!data) {
        button.dataset.state = "unknown";
        button.title = "面板服务未响应，检查 shim 是否在运行";
        return;
      }
      var ready = data.status.mimo_configured && data.status.llm_configured;
      button.dataset.state = ready ? "good" : "bad";
      button.title = ready
        ? "云端免费栈已就绪"
        : "尚未配置完成：缺 " +
          [!data.status.mimo_configured ? "MiMo Key" : null, !data.status.llm_configured ? "商汤 Key" : null]
            .filter(Boolean).join(" / ") + "，点击填写";
    });
  }

  // ---- 在 OCV 的「执行方式」下拉里**新增**一个 MiMo 选项 ----
  //
  // 1.10.0 起 MiMo 是**独立的第 4 个引擎**，与原生 Qwen-TTS 并列。
  // 因此这里做两件事：
  //   (a) 给每个 <select> 插入 <option value="mimo">（若还没有）；
  //   (b) **还原**曾被旧版本改写成 "MiMo TTS" 的 qwen 选项文案。
  // 只做 (a) 不做 (b) 的话，两个选项都会叫 MiMo（铁律 60）。
  var MIMO_VALUE = "mimo";
  var MIMO_LABEL = "MiMo TTS（云端免费栈）";
  var QWEN_LABEL = "Qwen-TTS";
  var relabelTimer = null;

  // OCV 的执行方式下拉里一定有 qwen / indextts25 / cluster 三个原生选项，
  // 用它来识别"这个 select 是配音引擎选择器"，避免误伤其它下拉。
  function isEngineSelect(select) {
    return !!select.querySelector('option[value="indextts25"]');
  }

  function syncOption(select) {
    var mimo = select.querySelector('option[value="' + MIMO_VALUE + '"]');
    if (!mimo) {
      mimo = document.createElement("option");
      mimo.value = MIMO_VALUE;
      mimo.textContent = MIMO_LABEL;
      // 插在 Qwen 之后，保持"两个云端选项挨着"的直觉顺序
      var qwen = select.querySelector('option[value="qwen"]');
      if (qwen && qwen.nextSibling) select.insertBefore(mimo, qwen.nextSibling);
      else select.appendChild(mimo);
    } else if (mimo.textContent !== MIMO_LABEL) {
      mimo.textContent = MIMO_LABEL;
    }
    // (b) 还原被旧版本改写的 qwen 文案
    var qwenOption = select.querySelector('option[value="qwen"]');
    if (qwenOption && /MiMo/i.test(qwenOption.textContent || "")) {
      qwenOption.textContent = QWEN_LABEL;
    }
  }

  function relabel() {
    var selects = document.querySelectorAll("select");
    for (var i = 0; i < selects.length; i += 1) {
      if (isEngineSelect(selects[i])) syncOption(selects[i]);
    }
  }

  function scheduleRelabel() {
    if (relabelTimer) return;
    relabelTimer = setTimeout(function () {
      relabelTimer = null;
      try { relabel(); } catch (error) { /* 纯装饰，失败无所谓 */ }
    }, 250);
  }

  function startRelabel() {
    scheduleRelabel();
    var observer = new MutationObserver(scheduleRelabel);
    observer.observe(document.body, { childList: true, subtree: true });
  }

  function start() {
    mountButton();
    startRelabel();
    // 配置可能随时被改（比如另一个窗口打开了面板），定期刷新状态点
    setInterval(refreshBadge, 15000);
    // 窗口尺寸变化后把按钮拉回视口内：换到小屏或改过分辨率时，
    // 记下来的位置可能已经落在视野之外
    window.addEventListener("resize", function () {
      var button = document.getElementById(PREFIX + "-btn");
      if (!button || !button.style.left) return;
      var rect = button.getBoundingClientRect();
      placeButton(button, rect.left, rect.top, rect.width, rect.height);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
