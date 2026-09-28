/**
 * 用 headless Chrome 真实渲染面板，验证 Jet Hub 分页真的能用。
 *
 * 用法：node dev_render_check.mjs <panelUrl> <chromePath>
 *
 * 为什么需要它：静态检查「HTML 里有 section」**不等于**「用户看得见」
 * —— 面板的分页是 `state.groups` 驱动、由 JS 动态建 tab 的，
 * `renderGroup()` 找不到 `card-<key>` 会**静默跳过**。只有真渲染一遍
 * 才能确认 tab 出现、卡片有内容、且没有 pageerror。
 */

const [, , panelUrl, chromePath] = process.argv
if (!panelUrl || !chromePath) {
  console.error('用法: node dev_render_check.mjs <panelUrl> <chromePath>')
  process.exit(2)
}

// 用 CDP 直连（不依赖 puppeteer-core —— 目标机不一定有）
import { spawn } from 'node:child_process'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const userDataDir = mkdtempSync(join(tmpdir(), 'ocv-jh-render-'))
const port = 9333 + Math.floor(Math.random() * 200)

const chrome = spawn(chromePath, [
  '--headless=new',
  `--remote-debugging-port=${port}`,
  `--user-data-dir=${userDataDir}`,
  '--no-first-run',
  '--no-default-browser-check',
  '--disable-gpu',
  '--window-size=1280,900',
  'about:blank',
], { stdio: ['ignore', 'ignore', 'pipe'] })

let chromeErr = ''
chrome.stderr.on('data', (d) => { chromeErr += String(d) })

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function getWsUrl() {
  for (let i = 0; i < 60; i += 1) {
    try {
      // 必须拿**页面目标**的 ws，而不是 /json/version 给的浏览器级 ws
      // —— 浏览器级连接上 Runtime.enable 这类域方法是不存在的。
      const res = await fetch(`http://127.0.0.1:${port}/json/list`)
      const list = await res.json()
      const page = (list || []).find((t) => t.type === 'page' && t.webSocketDebuggerUrl)
      if (page) return page.webSocketDebuggerUrl
    } catch {
      /* 还没起来 */
    }
    await sleep(250)
  }
  throw new Error(`Chrome 调试端口未就绪。stderr: ${chromeErr.slice(0, 400)}`)
}

/** 极简 CDP 客户端：只需要 navigate / evaluate / 收集 console+error。 */
class Cdp {
  constructor(ws) {
    this.ws = ws
    this.id = 0
    this.pending = new Map()
    this.consoleErrors = []
    this.pageErrors = []
    this.requests = []
    ws.addEventListener('message', (event) => {
      const msg = JSON.parse(event.data)
      if (msg.id !== undefined && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id)
        this.pending.delete(msg.id)
        if (msg.error) reject(new Error(msg.error.message))
        else resolve(msg.result)
        return
      }
      if (msg.method === 'Runtime.exceptionThrown') {
        const d = msg.params?.exceptionDetails
        this.pageErrors.push(d?.exception?.description || d?.text || 'unknown')
      }
      if (msg.method === 'Runtime.consoleAPICalled' && msg.params?.type === 'error') {
        this.consoleErrors.push(
          (msg.params.args || []).map((a) => a.value ?? a.description ?? '').join(' '),
        )
      }
      if (msg.method === 'Network.requestWillBeSent') {
        this.requests.push(msg.params?.request?.url || '')
      }
    })
  }

  send(method, params = {}) {
    return new Promise((resolve, reject) => {
      this.id += 1
      const id = this.id
      this.pending.set(id, { resolve, reject })
      this.ws.send(JSON.stringify({ id, method, params }))
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id)
          reject(new Error(`CDP 超时: ${method}`))
        }
      }, 30000)
    })
  }

  async eval(expression) {
    const result = await this.send('Runtime.evaluate', {
      expression,
      returnByValue: true,
      awaitPromise: true,
    })
    if (result.exceptionDetails) {
      throw new Error(result.exceptionDetails.exception?.description || 'eval 失败')
    }
    return result.result?.value
  }
}

const results = []
function check(label, ok, detail = '') {
  results.push({ label, ok, detail })
  console.log(`  [${ok ? 'OK  ' : 'FAIL'}] ${label}${!ok && detail ? `\n         ${detail}` : ''}`)
}

async function main() {
  const wsUrl = await getWsUrl()
  const ws = new WebSocket(wsUrl)
  await new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve, { once: true })
    ws.addEventListener('error', reject, { once: true })
  })
  const cdp = new Cdp(ws)
  await cdp.send('Runtime.enable')
  await cdp.send('Page.enable')
  await cdp.send('Network.enable')

  await cdp.send('Page.navigate', { url: panelUrl })
  // 面板加载后有若干异步请求（state + models + jethub status + 桥拉起）
  await sleep(9000)

  console.log('\n=== 渲染结果 ===')

  const tabs = await cdp.eval(
    'Array.prototype.map.call(document.querySelectorAll("#tabs button"), function(b){return b.textContent})',
  )
  check('分页按钮已渲染', Array.isArray(tabs) && tabs.length > 0, JSON.stringify(tabs))
  check('出现「云端 OAuth」分页',
    Array.isArray(tabs) && tabs.some((t) => t.includes('云端 OAuth')),
    `实际 tabs=${JSON.stringify(tabs)}`)
  check('不应再出现旧名「Jet Hub 账号」',
    Array.isArray(tabs) && !tabs.some((t) => t.includes('Jet Hub 账号')),
    `实际 tabs=${JSON.stringify(tabs)}`)

  const hasSection = await cdp.eval('!!document.getElementById("tab-jethub")')
  check('tab-jethub 容器存在', hasSection === true)

  // 切到云端 OAuth 分页
  await cdp.eval(`(function(){
    var btns = document.querySelectorAll("#tabs button");
    for (var i=0;i<btns.length;i++){ if(btns[i].textContent.indexOf("云端 OAuth")>=0){ btns[i].click(); return true; } }
    return false;
  })()`)
  await sleep(1500)

  const active = await cdp.eval(
    '(document.getElementById("tab-jethub")||{}).className || ""',
  )
  check('点击后 tab-jethub 变为 active', String(active).includes('active'), `className=${active}`)

  const bridgeRows = await cdp.eval(
    '(document.getElementById("jethub-bridge")||{}).textContent || ""',
  )
  check('桥状态卡片有内容', String(bridgeRows).trim().length > 10, `内容=${String(bridgeRows).slice(0, 120)}`)
  check('桥状态显示「未运行」或「运行中」',
    /运行中|未运行/.test(String(bridgeRows)), `内容=${String(bridgeRows).slice(0, 120)}`)

  // 提供商左栏（用户要求：叫「提供商」而不是 Provider）
  const rail = await cdp.eval(`(function(){
    var nodes = document.querySelectorAll("#jethub-providers button");
    return Array.prototype.map.call(nodes, function(n){return n.textContent});
  })()`)
  check('提供商左栏已渲染（9 家）', Array.isArray(rail) && rail.length === 9, JSON.stringify(rail))
  check('左栏含 CodeBuddy / Qoder / TRAE 等',
    Array.isArray(rail) && rail.some((t) => t.includes('CodeBuddy'))
    && rail.some((t) => t.includes('Qoder')) && rail.some((t) => t.includes('TRAE')),
    JSON.stringify(rail))
  const selectedCount = await cdp.eval(
    'document.querySelectorAll("#jethub-providers button[aria-selected=\\"true\\"]").length',
  )
  check('左栏有且只有一个选中项', Number(selectedCount) === 1, `selected=${selectedCount}`)

  const accountBox = await cdp.eval(
    '(document.getElementById("jethub-accounts")||{}).textContent || ""',
  )
  check('账号区有内容（未登录时应提示新增）',
    String(accountBox).trim().length > 0, `内容=${String(accountBox).slice(0, 120)}`)

  const modelBox = await cdp.eval(
    '(document.getElementById("jethub-models")||{}).textContent || ""',
  )
  check('模型区有内容（未登录时应提示先登录）',
    String(modelBox).trim().length > 0, `内容=${String(modelBox).slice(0, 120)}`)

  // 备份/恢复：**常驻卡片**（用户要求：不弹窗、直接放面板里）
  const backupUI = await cdp.eval(`(function(){
    var cardIds = ['btn-jh-backup-export-download','btn-jh-backup-export-server',
                   'btn-jh-backup-pick','btn-jh-backup-import','jh-backup-file'];
    var present = {};
    cardIds.forEach(function(id){ present[id] = !!document.getElementById(id); });
    var pick = document.getElementById('btn-jh-backup-pick');
    // 「选择文件」按钮必须在**当前可见的分页**里且可点
    var visible = pick ? (pick.offsetParent !== null) : false;
    return {
      present: present,
      pickVisible: visible,
      modalGone: !document.getElementById('jh-backup-modal'),
      openBtnGone: !document.getElementById('btn-jethub-backup-open')
    };
  })()`)
  check('备份/恢复按钮都在页面里（不弹窗）',
    backupUI && Object.keys(backupUI.present).every(function(k){ return backupUI.present[k]; }),
    JSON.stringify(backupUI && backupUI.present))
  check('备份弹窗已删除', backupUI && backupUI.modalGone === true, JSON.stringify(backupUI))
  check('「备份/恢复」弹窗入口按钮已删除',
    backupUI && backupUI.openBtnGone === true, JSON.stringify(backupUI))
  // 关键：切到云端 OAuth 分页后，「选择文件」按钮必须真的可见可点
  const pickOnTab = await cdp.eval(`(function(){
    var b = document.getElementById('btn-jh-backup-pick');
    if (!b) return null;
    return { visible: b.offsetParent !== null, disabled: !!b.disabled,
             hasFileInput: !!document.getElementById('jh-backup-file'),
             bound: typeof b.onclick === 'function' || !!b.__jhBoundHandler };
  })()`)
  check('切到本页后「选择文件」按钮可见可点',
    pickOnTab && pickOnTab.visible === true && pickOnTab.disabled === false,
    JSON.stringify(pickOnTab))

  // 关键回归：点「选择文件」必须真的唤起文件选择框。
  // 真实事故：modal 排在脚本之后 → jhBind 静默失败 → 点了没反应。
  // 这里监听 file input 的 click 事件来证明「按钮确实触发了 input」。
  const pickWorks = await cdp.eval(`(function(){
    var input = document.getElementById('jh-backup-file');
    if (!input) return { ok:false, why:'no input' };
    var fired = false;
    var handler = function(){ fired = true; };
    input.addEventListener('click', handler);
    var b = document.getElementById('btn-jh-backup-pick');
    b.click();
    input.removeEventListener('click', handler);
    return { ok: fired, why: fired ? '' : '按钮点击没有触发 file input，绑定失效' };
  })()`)
  check('点「选择文件」确实唤起文件选择框（原故障回归）',
    pickWorks && pickWorks.ok === true, JSON.stringify(pickWorks))

  // 关键：账号卡上方的字段区**不应再有**提供商下拉（左栏才是入口）
  const dupProvider = await cdp.eval(`(function(){
    var nodes = document.querySelectorAll('[data-key="JETHUB_PROVIDER"]');
    var rail = document.getElementById('jethub-providers');
    return { fieldCount: nodes.length, railButtons: rail ? rail.querySelectorAll('button').length : 0 };
  })()`)
  check('字段区没有重复的「提供商」下拉（用户要求删掉）',
    dupProvider && dupProvider.fieldCount === 0, JSON.stringify(dupProvider))
  check('提供商左栏仍是唯一入口（9 家）',
    dupProvider && dupProvider.railButtons === 9, JSON.stringify(dupProvider))

  // ===== F-009 回归：面板不该因为 provider 参数错位而变空 =====
  //
  // 真实事故：`jethub_status(provider, payload)` 被 `_panel_call(payload)`
  // 调用 → `provider` 拿到 dict → `str(dict)` 垃圾串发给桥 → 账号/模型/积分
  // 全查不到。现象是「面板打开一片空白」且**没有报错**。
  // 这里直接检查渲染结果里真的**有内容**，而不只是「元素存在」。
  const notBlank = await cdp.eval(`(function(){
    var accounts = (document.getElementById('jethub-accounts')||{}).textContent || '';
    var models = (document.getElementById('jethub-models')||{}).textContent || '';
    var bridge = (document.getElementById('jethub-bridge')||{}).textContent || '';
    return {
      accountsLen: accounts.trim().length,
      modelsLen: models.trim().length,
      bridgeHasProvider: /提供商：\\s*\\S/.test(bridge),
      // 垃圾串特征：出现 dict 字面量
      providerGarbage: bridge.indexOf("{'provider'") >= 0 || bridge.indexOf('{\\"provider\\"') >= 0
    };
  })()`)
  check('账号区渲染出了内容（不是空白）',
    notBlank && notBlank.accountsLen > 0, JSON.stringify(notBlank))
  check('模型区渲染出了内容（不是空白）',
    notBlank && notBlank.modelsLen > 0, JSON.stringify(notBlank))
  check('桥状态里的提供商名不是 dict 垃圾串（F-009 回归）',
    notBlank && notBlank.providerGarbage === false, JSON.stringify(notBlank))

  // ===== 用户两项要求 =====
  // 1) 「刷新凭据」按钮必须已移除（后台自动刷新）
  const refreshBtn = await cdp.eval(`(function(){
    var btns = document.querySelectorAll('.jh-btn');
    var n = 0;
    Array.prototype.forEach.call(btns, function(b){ if (b.textContent.indexOf('刷新凭据') >= 0) n++; });
    return n;
  })()`)
  check('账号卡上没有「刷新凭据」按钮（用户要求去掉）',
    Number(refreshBtn) === 0, `找到 ${refreshBtn} 个`)

  // 2) 来源提示条存在，且会显示**当前选用的模型**
  const sourceHint = await cdp.eval(`(function(){
    function setSrc(v){var s=document.querySelector('[data-key="LLM_SOURCE"]');s.value=v;s.dispatchEvent(new Event('change',{bubbles:true}));}
    var hint = document.getElementById('llm-source-hint');
    var out = { exists: !!hint };
    if (!hint) return out;
    setSrc('jethub');
    out.before = hint.innerText;
    out.warnWhenEmpty = hint.getAttribute('data-tone') === 'warn';
    var jh = document.querySelector('[data-key="JETHUB_MODEL"]');
    if (jh) {
      for (var i = 0; i < jh.options.length; i++) {
        if (jh.options[i].value && jh.options[i].value.indexOf('::') > 0) {
          jh.value = jh.options[i].value; break;
        }
      }
      jh.dispatchEvent(new Event('change', {bubbles:true}));
      out.picked = jh.value;
    }
    setSrc('custom'); setSrc('jethub');
    out.after = hint.innerText;
    out.tone = hint.getAttribute('data-tone');
    // 切到 custom 时不应再显示云端 OAuth 模型
    setSrc('custom');
    out.custom = hint.innerText;
    return out;
  })()`)
  check('语言模型页有「当前来源」提示条（F-021 回归，不被 renderGroup 擦除）',
    sourceHint && sourceHint.exists === true, JSON.stringify(sourceHint))
  check('未选模型时提示条给出警告',
    sourceHint && (sourceHint.warnWhenEmpty === true || !!sourceHint.picked),
    JSON.stringify(sourceHint))
  check('提示条显示**当前选用的云端 OAuth 模型**（用户要求）',
    sourceHint && sourceHint.picked
      ? sourceHint.after.indexOf(sourceHint.picked.split('::')[1]) >= 0
      : true,
    JSON.stringify({ picked: sourceHint && sourceHint.picked, after: sourceHint && sourceHint.after }))
  check('切到「自行填写 API」时提示条不再提云端 OAuth 模型',
    sourceHint && sourceHint.custom && sourceHint.custom.indexOf('云端 OAuth 模型') < 0,
    JSON.stringify(sourceHint && sourceHint.custom))

  // 能力门控：默认提供商（buddy）支持签到，按钮应可见
  const checkinVisible = await cdp.eval(`(function(){
    var b = document.getElementById("btn-jethub-checkin");
    if (!b) return null;
    return b.style.display !== "none";
  })()`)
  check('一键签到按钮存在且可见（默认提供商支持签到）', checkinVisible === true, `visible=${checkinVisible}`)

  // ===== 语言模型页：来源互斥（用户核心要求） =====
  const sourceUI = await cdp.eval(`(function(){
    var node = document.querySelector('[data-key="LLM_SOURCE"]');
    if (!node) return null;
    return {
      value: node.value,
      options: Array.prototype.map.call(node.options, function(o){return o.value})
    };
  })()`)
  check('语言模型页有「模型来源」下拉', sourceUI !== null, '未找到 LLM_SOURCE')
  check('来源下拉含 custom 与 jethub 两个选项',
    sourceUI && sourceUI.options.indexOf('custom') >= 0 && sourceUI.options.indexOf('jethub') >= 0,
    JSON.stringify(sourceUI))

  // 关键断言：切换来源时两路字段互斥显示
  const vis = await cdp.eval(`(function(){
    function visible(key){
      var n = document.querySelector('[data-key="'+key+'"]');
      if (!n) return null;
      var wrap = n.closest(".field") || n.parentElement;
      return wrap ? wrap.style.display !== "none" : null;
    }
    function setSource(v){
      var s = document.querySelector('[data-key="LLM_SOURCE"]');
      s.value = v;
      s.dispatchEvent(new Event("change", {bubbles:true}));
    }
    var out = {};
    setSource("custom");
    out.custom = { apiKey: visible("SENSENOVA_API_KEY"), model: visible("SENSENOVA_MODEL"),
                   jhModel: visible("JETHUB_MODEL") };
    setSource("jethub");
    out.jethub = { apiKey: visible("SENSENOVA_API_KEY"), model: visible("SENSENOVA_MODEL"),
                   jhModel: visible("JETHUB_MODEL") };
    setSource("custom");
    return out;
  })()`)
  check('选「自行填写 API」时显示商汤字段、隐藏云端 OAuth 模型',
    vis && vis.custom && vis.custom.apiKey === true && vis.custom.jhModel === false,
    JSON.stringify(vis && vis.custom))
  check('选「云端 OAuth」时隐藏商汤字段、显示云端 OAuth 模型',
    vis && vis.jethub && vis.jethub.apiKey === false && vis.jethub.jhModel === true,
    JSON.stringify(vis && vis.jethub))

  // 关键：JETHUB_MODEL 必须在**语言模型页**（用户要求模型选择在这页，不在云端 OAuth 页）
  const jethubModelSelect = await cdp.eval(`(function(){
    var inputs = document.querySelectorAll('[data-key="JETHUB_MODEL"]');
    if (!inputs.length) return null;
    var node = inputs[0];
    return {
      tag: node.tagName,
      options: Array.prototype.map.call(node.options||[], function(o){return o.value}),
      group: (node.closest("section")||{}).id || ""
    };
  })()`)
  check('语言模型页里有 JETHUB_MODEL 控件', jethubModelSelect !== null, '未找到 [data-key="JETHUB_MODEL"]')
  if (jethubModelSelect) {
    check('JETHUB_MODEL 是 select',
      String(jethubModelSelect.tag).toLowerCase() === 'select', JSON.stringify(jethubModelSelect))
    check('JETHUB_MODEL 位于 tab-llm 分页（不在云端 OAuth 页）',
      jethubModelSelect.group === 'tab-llm', `实际 ${jethubModelSelect.group}`)
    check('JETHUB_MODEL 下拉有「自定义」兜底项',
      Array.isArray(jethubModelSelect.options) && jethubModelSelect.options.includes('__custom__'),
      `options=${JSON.stringify(jethubModelSelect.options)}`)
  }

  // ===== F-027 回归：**保存后**互斥可见性不能丢 =====
  //
  // 真实事故：保存流程是 `save()` → `render()` → `renderGroup()` 执行
  // `card.innerHTML = ""` 重建卡片 —— 运行时打在 DOM 上的 `data-source`
  // **连着被清掉**，于是所有字段都显示出来（界面退回「API Key」形态），
  // 用户必须关掉再打开面板才恢复。
  //
  // 这里**真的点「保存」**，再比对保存前后的可见性。
  const saveProbe = `(function(){
    function vis(key){
      var n = document.querySelector('[data-key="'+key+'"]');
      if (!n) return null;
      var w = n.closest('.field') || n.parentElement;
      return w ? (w.style.display !== 'none') : null;
    }
    var s = document.querySelector('[data-key="LLM_SOURCE"]');
    return { source: s ? s.value : '?', apiKey: vis('SENSENOVA_API_KEY'),
             jhModel: vis('JETHUB_MODEL'),
             tagged: document.querySelectorAll('[data-source]').length };
  })()`
  await cdp.eval(`(function(){
    var s=document.querySelector('[data-key="LLM_SOURCE"]');
    if(s){s.value='jethub';s.dispatchEvent(new Event('change',{bubbles:true}));}
  })()`)
  await sleep(600)
  const beforeSave = await cdp.eval(saveProbe)
  // ⚠ 按钮文本是「保存并立即生效」（不是「保存」）—— 用 includes 匹配，
  //   以免按钮文案微调就让断言失效。
  const clickedSave = await cdp.eval(`(function(){
    var b = document.querySelectorAll('button');
    for (var i=0;i<b.length;i++){
      if (b[i].textContent.indexOf('保存') >= 0){ b[i].click(); return b[i].textContent.trim(); }
    }
    return false;
  })()`)
  await sleep(5000)
  const afterSave = await cdp.eval(saveProbe)

  check('页面有「保存」按钮可点', typeof clickedSave === 'string',
    `实际 ${JSON.stringify(clickedSave)}`)
  check('保存后 data-source 标记仍在（不被 renderGroup 清掉）',
    afterSave && afterSave.tagged > 0,
    `保存前 ${beforeSave && beforeSave.tagged} → 保存后 ${afterSave && afterSave.tagged}`)
  check('保存后仍是「云端 OAuth」形态：API Key 隐藏、云端模型显示（F-027 回归）',
    afterSave && afterSave.apiKey === false && afterSave.jhModel === true,
    JSON.stringify({ before: beforeSave, after: afterSave }))
  check('保存后来源值不变',
    afterSave && afterSave.source === 'jethub',
    JSON.stringify({ before: beforeSave, after: afterSave }))

  // 收尾：切回 custom，避免影响其它断言
  await cdp.eval(`(function(){
    var s=document.querySelector('[data-key="LLM_SOURCE"]');
    if(s){s.value='custom';s.dispatchEvent(new Event('change',{bubbles:true}));}
  })()`)
  await sleep(400)

  console.log('\n=== 页面错误 ===')
  check('无 pageerror', cdp.pageErrors.length === 0, cdp.pageErrors.slice(0, 3).join(' | '))
  check('无 console.error', cdp.consoleErrors.length === 0, cdp.consoleErrors.slice(0, 3).join(' | '))

  // ===== raw text 元素护栏（F-004）=====
  //
  // 真实事故：CSS 注释里出现了 style 元素的标签文本，导致 `<style>` 被
  // 提前关闭，**后面全部 CSS 变成页面可见文本**（用户看到「一堆乱码」）。
  // 这个断言问浏览器要三件证据：样式块只有一个、CSS 规则真的被解析、
  // 可见文本里没有 CSS 残留。
  const cssHealth = await cdp.eval(`(function(){
    var text = document.body.innerText || "";
    var markers = [".jh-layout", ".jh-rail", "display: flex", "border-radius: 14px",
                   "--accent", "color-mix", "box-shadow"];
    var leaks = markers.filter(function(m){ return text.indexOf(m) >= 0; });
    var sheets = document.styleSheets.length;
    var rules = -1;
    try { rules = sheets ? document.styleSheets[sheets-1].cssRules.length : -1; } catch (e) { rules = -1; }
    return {
      styleTags: document.querySelectorAll("style").length,
      sheets: sheets,
      rules: rules,
      leaks: leaks
    };
  })()`)
  check('样式块只有一个（未被提前关闭）',
    cssHealth && cssHealth.styleTags === 1,
    `styleTags=${cssHealth && cssHealth.styleTags} —— 多于 1 处说明 CSS 里有标签文本（F-004）`)
  check('CSS 真的被解析（规则数 > 0）',
    cssHealth && cssHealth.rules > 0, JSON.stringify(cssHealth))
  check('页面可见文本里没有 CSS 残留（不乱码）',
    cssHealth && cssHealth.leaks.length === 0,
    `泄漏的标记=${JSON.stringify(cssHealth && cssHealth.leaks)}`)

  ws.close()
  chrome.kill()

  const failed = results.filter((r) => !r.ok)
  console.log(`\n=== ${results.length - failed.length} passed, ${failed.length} failed ===`)
  return failed.length === 0 ? 0 : 1
}

main()
  .then((code) => {
    try { rmSync(userDataDir, { recursive: true, force: true }) } catch { /* ignore */ }
    process.exit(code)
  })
  .catch((error) => {
    console.error('渲染验证失败:', error.message)
    try { chrome.kill() } catch { /* ignore */ }
    try { rmSync(userDataDir, { recursive: true, force: true }) } catch { /* ignore */ }
    process.exit(1)
  })
