# -*- coding: utf-8 -*-
"""DouyinWebviewPage —— 把豆包管理器 webview 目标包成"够用版 Playwright page"，
使 main.py 的函数能直接驱动它。驱动走原始 CDP(CdpSocket)。
"""
import base64
import json
import time
from pathlib import Path


def _qs(sx):
    return json.dumps(str(sx))


def _norm(s):
    return "".join(str(s or "").split())


# 输入区被 visibility:hidden / pointer-events:none 禁用时的自愈等待（秒）。
# 2026-09-17 实测：点「新对话」/进视频模式/上传参考图后，豆包会把 tiptap 编辑器
# 短暂置为 `visibility:hidden` + 继承 `pointer-events:none` —— 此时 `focus()` 不生效、
# `Input.insertText` 无处可落，读回必然为空。这是「提示词写不进去」的真正状态成因。
# 提成模块常量便于单测压到 ~0。
BLOCKED_WAIT_S = 25.0

# 硬错误标记：写入失败的根因在「页面/输入区状态」，不是运气 —— 外层重试无意义，
# 必须把原因原样透传给用户（main._fill_prompt / manager_run 都按这个标记判定）。
FILL_HARD_ERR = "输入区不可用"


class _Locator:
    def __init__(self, page, selector=None, filter_text=None, nth=None):
        self._p = page
        self._sel = selector
        self._ft = filter_text
        self._nth = nth

    @property
    def first(self):
        return _Locator(self._p, self._sel, self._ft, 0)

    @property
    def last(self):
        """对应 Playwright 的 `.last`。旧 shim 没有这个属性 → 调用处 AttributeError
        会被上层 except 吞掉、静默降级。"""
        n = self.count()
        return _Locator(self._p, self._sel, self._ft, max(0, n - 1))

    def nth(self, i):
        return _Locator(self._p, self._sel, self._ft, i)

    def filter(self, *, has_text=None):
        return _Locator(self._p, self._sel, has_text, self._nth)

    def count(self):
        return len(self._p._list(self._sel, self._ft))

    def all(self):
        return [_Locator(self._p, self._sel, self._ft, i) for i in range(self.count())]

    def is_visible(self):
        el = self._el()
        return bool(el is not None)

    def inner_text(self):
        el = self._el(); return (el or {}).get("text") or ""

    def _el(self):
        els = self._p._list(self._sel, self._ft)
        if not els:
            return None
        i = self._nth if self._nth is not None else 0
        return els[min(i, len(els) - 1)]

    def wait_for(self, state="visible", timeout=30000):
        dl = time.time() + timeout / 1000
        while time.time() < dl:
            if self._el() is not None:
                return
            time.sleep(0.15)
        raise RuntimeError("wait_for timeout: %r" % (self._sel,))

    def click(self, timeout=3000):
        self.wait_for(timeout=timeout)
        idx = self._nth if self._nth is not None else 0
        ok = self._p._click(self._sel, idx, filter_text=self._ft)
        if not ok:
            raise RuntimeError("click: 未找到元素 %r" % (self._sel or self._ft))
        return ok

    def hover(self, timeout=4000):
        self.wait_for(timeout=timeout)
        idx = self._nth if self._nth is not None else 0
        return self._p._hover(self._sel, idx, filter_text=self._ft)

    def fill(self, text, timeout=None):
        """⚠️ 必须接受 timeout 形参。

        main._fill_prompt 调的是 `editor.fill(prompt, timeout=ms)`（Playwright 的签名）。
        旧 shim 只写了 `fill(self, text)` → 这里抛 TypeError → 被 `_fill_prompt` 的
        `except Exception` 吞掉 → 退化成 `page.keyboard.insert_text`（**完全没有校验和重试的裸写**）。
        结果就是「写回校验 + 重试」这条防线在管理器通道从来没生效过 —— 2026-09-17 排查时
        靠 `page.last_fill_mode` 是空的才发现。timeout 由 _fill 自己的重试逻辑承接，这里忽略。
        """
        return self._p._fill(text)

    def press(self, key):
        return self._p._press_key(key)

    def set_input_files(self, path):
        return self._p._set_files([str(path)])


class _Mouse:
    """Playwright `page.mouse` 的最小替身（只支持 move/click）。"""

    def __init__(self, page):
        self._p = page

    def move(self, x, y):
        self._p._ws.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": int(x), "y": int(y), "button": "none"})
        return True

    def click(self, x, y):
        return self._p.click_xy(x, y)


class _Keyboard:
    def __init__(self, page):
        self._p = page

    def insert_text(self, text):
        self._p._insert_text(text)

    def press(self, key):
        self._p._press_key(key)


_JS_PICK_EDITOR = r"""(() => {
  // 挑「真正可交互」的编辑器：豆包页面上可能同时存在多个 contenteditable
  // （聊天输入框、上传区的装饰性输入位、弹窗里的搜索框）。旧实现一律取
  // document.querySelector 的第一个 —— 拿到 pointer-events:none 的那个就必然写不进去。
  const cands = [];
  const push = (e) => {
    if (!e) return;
    const r = e.getBoundingClientRect();
    const cs = getComputedStyle(e);
    let blocked = null;
    for (let p = e; p; p = p.parentElement) {
      if (getComputedStyle(p).pointerEvents === 'none') {
        blocked = p.tagName + (p.className ? '.' + String(p.className).split(' ').slice(0, 2).join('.') : '');
        break;
      }
    }
    const vis = (r.width > 0 && r.height > 0) && cs.visibility !== 'hidden' && cs.display !== 'none';
    const pe = cs.pointerEvents;
    const score = (vis ? 2 : 0) + (pe !== 'none' ? 2 : 0) + (blocked ? 0 : 3);
    cands.push({ e: e, r: r, cs: cs, vis: vis, pe: pe, blocked: blocked, score: score });
  };
  for (const e of document.querySelectorAll('[contenteditable="true"]')) push(e);
  if (!cands.length) {
    for (const t of document.querySelectorAll('textarea')) push(t);
  }
  if (!cands.length) return null;
  cands.sort((a, b) => b.score - a.score);
  const b = cands[0];
  b.all = cands;
  return b;
})()"""

_JS_EDITOR_HEALTH = r"""(() => {
  const out = {ceCount: document.querySelectorAll('[contenteditable="true"]').length,
               taCount: document.querySelectorAll('textarea').length};
  const desc = (e) => e.tagName + (e.className ? '.' + String(e.className).split(' ').filter(Boolean).slice(0, 2).join('.') : '');
  const cands = [];
  const push = (e, tag) => {
    const r = e.getBoundingClientRect();
    const cs = getComputedStyle(e);
    // 阻塞原因要从「祖先」上找：pointer-events / visibility 都是可继承属性，
    // 元素自身的 computed 值只反映继承结果，直接报元素自己等于没报。
    let blocked = null;
    if (cs.pointerEvents === 'none') blocked = 'pointer-events:none @ (self)';
    if (cs.visibility === 'hidden') blocked = (blocked ? blocked + ' + ' : '') + 'visibility:hidden @ (self)';
    for (let p = e.parentElement; p; p = p.parentElement) {
      const ps = getComputedStyle(p);
      if (ps.pointerEvents === 'none') { blocked = 'pointer-events:none @ ' + desc(p) + (blocked ? ' | ' + blocked : ''); break; }
      if (ps.visibility === 'hidden') { blocked = 'visibility:hidden @ ' + desc(p) + (blocked ? ' | ' + blocked : ''); break; }
    }
    const vis = (r.width > 0 && r.height > 0) && cs.visibility !== 'hidden' && cs.display !== 'none';
    const pe = cs.pointerEvents;
    cands.push({e: e, tag: tag, r: r, cs: cs, vis: vis, pe: pe, blocked: blocked,
                score: (vis ? 2 : 0) + (pe !== 'none' ? 2 : 0) + (blocked ? 0 : 3)});
  };
  for (const e of document.querySelectorAll('[contenteditable="true"]')) push(e, 'ce');
  for (const t of document.querySelectorAll('textarea')) push(t, 'ta');
  if (!cands.length) { out.found = false; out.ready = false; return out; }
  cands.sort((a, b) => b.score - a.score);
  const b = cands[0];
  out.found = true;
  out.ready = !!(b.vis && !b.blocked && b.pe !== 'none');
  out.tag = b.e.tagName;
  out.kind = b.tag;
  out.cls = String(b.e.className || '').slice(0, 60);
  out.editable = String(b.e.getAttribute && b.e.getAttribute('contenteditable'));
  out.vis = !!b.vis;
  out.pe = b.pe;
  out.blockedBy = b.blocked;
  out.x = Math.round(b.r.left + b.r.width / 2);
  out.y = Math.round(b.r.top + b.r.height / 2);
  out.w = Math.round(b.r.width);
  out.h = Math.round(b.r.height);
  out.activeIsEditor = document.activeElement === b.e;
  try {
    const s = window.getSelection();
    out.selInEditor = !!(s && s.anchorNode && s.anchorNode.parentElement &&
      s.anchorNode.parentElement.closest && s.anchorNode.parentElement.closest('[contenteditable="true"]') === b.e);
  } catch (err) { out.selInEditor = false; }
  out.hasSendBtn = !!document.querySelector('#flow-end-msg-send');
  out.text = String(b.e.value !== undefined && b.tag === 'ta' ? (b.e.value || '') : (b.e.innerText || b.e.textContent || ''));
  // 其它候选（便于定位「拿错了元素」）
  out.others = cands.slice(1, 4).map(c => ({cls: String(c.e.className || '').slice(0, 34),
     pe: c.pe, vis: c.vis, blocked: c.blocked, h: Math.round(c.r.height)}));
  return out;
})()"""

_JS_EDITOR_TEXT = r"""(() => {
  const cands = [];
  for (const e of document.querySelectorAll('[contenteditable="true"]')) cands.push(e);
  let e = null;
  for (const x of cands) { if (getComputedStyle(x).pointerEvents !== 'none') { e = x; break; } }
  if (!e) e = cands[0] || null;
  if (!e) e = document.querySelector('textarea');
  if (!e) return '';
  if (e.tagName === 'TEXTAREA' || e.tagName === 'INPUT') return e.value || '';
  return e.innerText || e.textContent || '';
})()"""

_JS_PLACE_CARET = r"""(() => {
  // 不依赖鼠标点击：直接用 Range+Selection 把光标落到编辑器末尾。
  // mouse 事件在输入区被 pointer-events:none 时会被完全丢弃，JS 落光标不受影响。
  const cands = [];
  for (const e of document.querySelectorAll('[contenteditable="true"]')) cands.push(e);
  let e = null;
  for (const x of cands) { if (getComputedStyle(x).pointerEvents !== 'none') { e = x; break; } }
  if (!e) e = cands[0] || document.querySelector('textarea');
  if (!e) return false;
  try { e.focus(); } catch (err) {}
  try {
    const r = document.createRange();
    r.selectNodeContents(e);
    r.collapse(false);
    const s = window.getSelection();
    s.removeAllRanges();
    s.addRange(r);
  } catch (err) {}
  return document.activeElement === e;
})()"""

_JS_SET_TEXT = r"""(({text}) => {
  // 最后降级手段：JS 层写入（CDP Input 通道整条失效时用）。
  // execCommand('insertText') 走的是浏览器的编辑命令管线，能触发 ProseMirror 的
  // DOMObserver → 框架内部 doc 会同步；失败再退回「直接改 DOM + beforeinput/input 事件」。
  const cands = [];
  for (const e of document.querySelectorAll('[contenteditable="true"]')) cands.push(e);
  let e = null;
  for (const x of cands) { if (getComputedStyle(x).pointerEvents !== 'none') { e = x; break; } }
  if (!e) e = cands[0] || document.querySelector('textarea');
  if (!e) return 'no-editor';
  try { e.focus(); } catch (err) {}
  try {
    const r = document.createRange();
    r.selectNodeContents(e);
    const s = window.getSelection();
    s.removeAllRanges();
    s.addRange(r);
  } catch (err) {}
  let mode = '';
  try {
    if (document.execCommand('insertText', false, text)) mode = 'execCommand';
  } catch (err) { mode = ''; }
  const cur = String(e.innerText || e.textContent || '');
  if (!mode || !cur.replace(/\s/g, '')) {
    if (e.tagName === 'TEXTAREA' || e.tagName === 'INPUT') {
      e.value = text;
    } else {
      while (e.firstChild) e.removeChild(e.firstChild);
      for (const line of String(text).split('\n')) {
        const p = document.createElement('p');
        if (line) p.textContent = line; else p.appendChild(document.createElement('br'));
        e.appendChild(p);
      }
    }
    try {
      e.dispatchEvent(new InputEvent('beforeinput', {bubbles: true, cancelable: true, inputType: 'insertText', data: text}));
      e.dispatchEvent(new InputEvent('input', {bubbles: true, cancelable: true, inputType: 'insertText', data: text}));
    } catch (err) {
      e.dispatchEvent(new Event('input', {bubbles: true}));
    }
    mode = 'dom-set';
  }
  return mode;
})"""

_JS_FILL_PREPARE = r"""(() => {
  const cands = [];
  for (const e of document.querySelectorAll('[contenteditable="true"]')) cands.push(e);
  let e = null;
  for (const x of cands) { if (getComputedStyle(x).pointerEvents !== 'none') { e = x; break; } }
  if (!e) e = cands[0] || document.querySelector('textarea');
  if (!e) return false;
  e.focus();
  if (e.tagName === 'TEXTAREA' || e.tagName === 'INPUT') { e.value = ''; }
  else { e.textContent = ''; }
  // DOM 清空后必须补发 input 事件，让 React 把「内容已变」登记进 state，
  // 否则发送按钮不触发（历史经验：草稿未注册时点发送无效）。
  e.dispatchEvent(new Event('input', {bubbles: true}));
  return true;
})()"""

_JS_EDITOR_RECT = r"""(() => {
  const cands = [];
  for (const e of document.querySelectorAll('[contenteditable="true"]')) cands.push(e);
  let e = null;
  for (const x of cands) { if (getComputedStyle(x).pointerEvents !== 'none') { e = x; break; } }
  if (!e) e = cands[0] || document.querySelector('textarea');
  if (!e) return null;
  e.scrollIntoView({block:'center'});
  const r = e.getBoundingClientRect();
  return {x: r.left + r.width/2, y: r.top + r.height/2, w: r.width, h: r.height};
})()"""


class DouyinWebviewPage:
    def __init__(self, ws_url):
        from doubao_manager import CdpSocket
        self._ws = CdpSocket(ws_url)
        self.keyboard = _Keyboard(self)
        self.mouse = _Mouse(self)
        # 最近一次 _fill 实际走通的通道：insertText / execCommand / dom-set ...
        # JS 降级能救回「CDP Input 整条失效」的页面，但 ProseMirror 的内部 doc
        # 未必同步 → 发出去的可能是空提示词。留痕便于事后归因（尤其白烧额度时）。
        self.last_fill_mode = ""

    # 评估
    def evaluate(self, js, arg=None):
        # 兼容 Playwright 的 page.evaluate(js, arg)：arg 作为首个参数传给函数表达式。
        # 注意：箭头函数直接后跟实参 (v)=>{...}("x") 是语法错误，必须整包裹一层括号。
        if arg is not None:
            try:
                argjs = json.dumps(arg, ensure_ascii=False, default=str)
            except Exception:
                argjs = "null"
            expr = "(%s)(%s)" % (js.strip(), argjs)
            return self._ws.evaluate(expr, True)
        return self._ws.evaluate(js, True)

    def wait_for_load_state(self, state=None, timeout=15000):
        # manager webview 无「加载状态」事件可等；睡眠一小段让 DOM 就绪即可，
        # 支持 state/__replace 形参以兼容 main.py 的调用（如 wait_for_load_state('load')）。
        time.sleep(min(0.8, (timeout or 2000) / 1000.0))

    def evaluate_handle(self, js):
        return self._ws.evaluate(js)

    def eval_on_selector_all(self, selector, fn_body):
        # fn_body 形如 "els => els.map(v => v.currentSrc || v.src).filter(Boolean)"
        fn = fn_body.strip()
        # 提取箭头函数体
        if "=>" in fn:
            body = fn.split("=>", 1)[1].strip()
        else:
            body = fn
        expr = ("(()=>{ const els = Array.from(document.querySelectorAll(%s)); " % _qs(selector)) + \
               "return " + body + "; })()"
        return self._ws.evaluate(expr)

    def wait_for_timeout(self, ms):
        time.sleep(ms / 1000)

    # 定位
    def locator(self, selector):
        return _Locator(self, selector)

    def get_by_role(self, role, name=None):
        return _Locator(self, None, name)

    def _list(self, selector, filter_text):
        if selector:
            js = "Array.from(document.querySelectorAll(%s)).map((e,i)=>({i:i,text:(e.innerText||'').trim()}))" % _qs(selector)
        else:
            js = "Array.from(document.querySelectorAll('button,[role=button],span,div,a')).map((e,i)=>({i:i,text:(e.innerText||'').trim()}))"
        arr = self._ws.evaluate(js) or []
        if filter_text is None:
            return arr
        if isinstance(filter_text, str) and not hasattr(filter_text, "search"):
            return [x for x in arr if filter_text in x["text"]]
        # re.Pattern
        pat = filter_text
        return [x for x in arr if pat.search(x["text"])]

    def _click(self, selector, idx=0, filter_text=None):
        """真实鼠标点击（定位元素中心 → Input.dispatchMouseEvent）。

        旧版用 JS 合成 e.click()，部分浮层/面板（如「自动 · 10s」设置面板）
        对它不响应；真实鼠标事件走浏览器完整输入管线，行为与人工点击一致。"""
        pat = filter_text.pattern if hasattr(filter_text, "search") else None
        lit = None if pat else (str(filter_text) if filter_text else None)
        js = r"""(({sel, idx, pat, lit}) => {
          let e = null;
          if (sel) { e = document.querySelectorAll(sel)[idx] || null; }
          else {
            const t = (el) => (el.innerText || el.textContent || '').trim();
            const list = Array.from(document.querySelectorAll('button, [role=button]'))
              .filter(el => (el.offsetParent !== null) && t(el) && t(el).length <= 30);
            if (pat) { try { const re = new RegExp(pat); e = list.find(el => re.test(t(el))); } catch (err) {} }
            else if (lit) { e = list.find(el => t(el).indexOf(lit) !== -1); }
            else { e = list.find(el => t(el).length <= 20); }
          }
          if (!e) return null;
          try { e.scrollIntoView({block: 'center'}); } catch (err) {}
          const r = e.getBoundingClientRect();
          if (!(r.width > 0 && r.height > 0)) return null;
          return {x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2)};
        })"""
        arg = json.dumps({"sel": selector, "idx": idx, "pat": pat, "lit": lit}, ensure_ascii=False)
        rect = None
        try:
            rect = self._ws.evaluate(js + "(" + arg + ")")
        except Exception:
            rect = None
        if not (rect and rect.get("x") is not None):
            return False
        self._ws.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": rect["x"], "y": rect["y"], "button": "left", "clickCount": 1})
        self._ws.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": rect["x"], "y": rect["y"], "button": "left", "clickCount": 1})
        return True

    def _insert_text(self, text):
        self._ws.send("Input.insertText", {"text": text})

    def _hover(self, selector, idx=0, filter_text=None):
        """真实鼠标移动到元素中心（只移动，不点击）。"""
        pat = filter_text.pattern if hasattr(filter_text, "search") else None
        lit = None if pat else (str(filter_text) if filter_text else None)
        js = r"""(({sel, idx, pat, lit}) => {
          let e = null;
          if (sel) { e = document.querySelectorAll(sel)[idx] || null; }
          else {
            const t = (el) => (el.innerText || el.textContent || '').trim();
            const list = Array.from(document.querySelectorAll('button, [role=button]'))
              .filter(el => (el.offsetParent !== null) && t(el) && t(el).length <= 30);
            if (lit) { e = list.find(el => t(el).indexOf(lit) !== -1); }
          }
          if (!e) return null;
          try { e.scrollIntoView({block: 'center'}); } catch (err) {}
          const r = e.getBoundingClientRect();
          if (!(r.width > 0 && r.height > 0)) return null;
          return {x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2)};
        })"""
        arg = json.dumps({"sel": selector, "idx": idx, "pat": pat, "lit": lit}, ensure_ascii=False)
        try:
            rect = self._ws.evaluate(js + "(" + arg + ")")
        except Exception:
            return False
        if not (rect and rect.get("x") is not None):
            return False
        self._ws.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": rect["x"], "y": rect["y"], "button": "none"})
        return True

    @property
    def url(self):
        """当前页面 URL。manager_run 会读 `page.url` 推会话 ID 给原片解析当 scope 用；
        旧 shim 没这个属性 → getattr(page,'url','') 恒为 ''，会话级严格模式静默失效。"""
        try:
            return self.evaluate("location.href") or ""
        except Exception:
            return ""

    def inner_text(self, selector="body"):
        """Playwright `page.inner_text(sel)` 的最小替身。"""
        try:
            return self.evaluate("((s)=>{const e=document.querySelector(s);return e?(e.innerText||''):'';})(%s)"
                                 % json.dumps(selector)) or ""
        except Exception:
            return ""

    def click_xy(self, x, y):
        """在视口坐标发一次真实鼠标点击（定位靠 JS 算，点击走浏览器输入管线）。"""
        x, y = int(x), int(y)
        self._ws.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
        self._ws.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
        return True

    def new_chat(self):
        """点侧边栏「新对话」。返回匹配到的按钮文本；没找到返回 ''。

        JS 只负责定位（`_JS_NEW_CHAT` 返回坐标），点击由这里发真实鼠标事件 ——
        「新对话」是个带 cursor-pointer 的 div（非 button/role=button），
        JS 合成 click 对这类 React 导航项不稳定。
        """
        import doubao_manager as _D
        try:
            hit = self.evaluate(_D._JS_NEW_CHAT)
        except Exception:
            return ""
        if not isinstance(hit, dict) or hit.get("x") is None:
            return ""
        try:
            self.click_xy(hit["x"], hit["y"])
        except Exception:
            return ""
        return str(hit.get("text") or "")

    def input_health(self):
        """输入区体检：返回编辑器是否真的可交互（存在/可见/未被禁用/已就绪）。"""
        try:
            return self._ws.evaluate(_JS_EDITOR_HEALTH) or {}
        except Exception:
            return {}

    def nudge(self):
        """轻推页面，把未就绪的编辑器唤醒。

        后台 webview / SPA 尚未 hydrate 时，输入区会停在 `visibility:hidden` +
        `pointer-events:none`。给一次真实的鼠标移动 + resize 事件常常就能让它完成渲染
        （不点击、不改变页面状态，安全）。
        """
        try:
            size = self.evaluate("({w: window.innerWidth, h: window.innerHeight})") or {}
            x = int((size.get("w") or 1200) / 2)
            y = int((size.get("h") or 800) / 3)
            self._ws.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y, "button": "none"})
        except Exception:
            pass
        try:
            self.evaluate("(() => { window.dispatchEvent(new Event('resize')); return true; })()")
        except Exception:
            pass

    def activate(self):
        """把该 webview target 提到前台**并解冻**。

        管理器里 4 个账号 webview 同时存在时，只有管理器 UI 当前显示的那个会完整
        渲染；其余在 Chromium 眼里是「后台标签」，SPA 的渲染循环被暂停 —— 表现就是
        `body.innerText` 里读不到 Seedance 文案（**半渲染**），上传参考图后输入区
        停在 `visibility:hidden` 态且不会自愈。

        2026-09-20 只发 `Page.bringToFront` 不够（实测半渲染账号照样失败），
        补一条 `Page.setWebLifecycleState('active')`：把页面从冻结/隐藏态解冻并
        促其重绘。该 domain 是 page 级的，page session 可直接调用。
        """
        ok = False
        try:
            self._ws.send("Page.bringToFront")
            ok = True
        except Exception:
            pass
        try:
            self._ws.send("Page.setWebLifecycleState", {"state": "active"})
            ok = True
        except Exception:
            pass
        return ok

    def keep_awake(self):
        """只解冻、**不置前**：等片期间周期性调用，防止后台 webview 被冻结。

        与 activate() 的区别：不发 `Page.bringToFront`，因此不抢管理器当前显示的
        账号卡片（用户看不到界面来回跳），适合在漫长的等片/取片过程中反复调用。

        背景：管理器里 4 个账号 webview 同时存在，非当前显示的会被 Chromium 按
        「后台标签」冻结 —— JS 停摆，取原片那发 `awaitPromise` 型 evaluate 直接
        挂满 60s 超时。等失败了再 activate 属于救火（已白烧 60s），这里做预防。
        """
        try:
            self._ws.send("Page.setWebLifecycleState", {"state": "active"})
            return True
        except Exception:
            return False

    def input_ready(self, timeout_s=25.0, activate_after=8.0, log=None):
        """等输入区进入就绪态。返回 (ok, health)。

        就绪 = 找得到编辑器 && 可见 && 未 pointer-events 禁用 && 可聚焦。
        过程中会周期性 nudge；超过 activate_after 秒还没好就 bringToFront 一次。
        """
        t0 = time.time()
        health = {}
        activated = False
        while time.time() - t0 < timeout_s:
            health = self.input_health()
            if health.get("ready"):
                return True, health
            if not activated and (time.time() - t0) >= activate_after:
                activated = True
                self.activate()
            self.nudge()
            if log:
                try:
                    log("输入区未就绪(t=%.1fs): %s" % (time.time() - t0,
                        health.get("blockedBy") or ("pe=%s vis=%s found=%s" % (health.get("pe"), health.get("vis"), health.get("found")))))
                except Exception:
                    pass
            time.sleep(1.5)
        return False, health

    def _editor_text(self):
        try:
            return self._editor_text_raw()
        except Exception:
            return ""

    def _editor_text_raw(self):
        return self._ws.evaluate(_JS_EDITOR_TEXT) or ""

    def screenshot(self, path, full_page=False):
        """截当前 webview 落盘。

        manager 通道以前没有这个方法 → main._save_debug_screenshot 里的
        `page.screenshot(...)` 每次都抛 AttributeError 被 except 吞掉，
        所以管理器后端失败时**一张调试截图都不会留下**（这就是这些失败只能靠日志猜的原因）。
        """
        try:
            self._ws.send("Page.enable")
        except Exception:
            pass
        res = self._ws.send("Page.captureScreenshot", {"format": "png"})
        data = res.get("data") or ""
        if not data:
            return False
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(base64.b64decode(data))
        return True

    def _press_key(self, key):
        if key.startswith("Control+") or "Control+A" in key:
            self._press_key("Control+A"); return
        if key == "Control+A":
            self._ws.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": "a", "code": "KeyA", "modifiers": 2, "windowsVirtualKeyCode": 65})
            self._ws.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": "a", "code": "KeyA", "modifiers": 2, "windowsVirtualKeyCode": 65})
            return
        if key in ("Enter", "Return"):
            self._ws.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "text": "\r"})
            self._ws.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13})
            return
        special = {
            "Delete": (46, "Delete", "Delete"),
            "Backspace": (8, "Backspace", "Backspace"),
            "Escape": (27, "Escape", "Escape"),
            "ArrowRight": (39, "ArrowRight", "ArrowRight"),
            "ArrowLeft": (37, "ArrowLeft", "ArrowLeft"),
            "ArrowUp": (38, "ArrowUp", "ArrowUp"),
            "ArrowDown": (40, "ArrowDown", "ArrowDown"),
        }
        if key in special:
            vk, kname, kcode = special[key]
            self._ws.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": kname, "code": kcode, "windowsVirtualKeyCode": vk})
            self._ws.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": kname, "code": kcode, "windowsVirtualKeyCode": vk})
            return
        # 旧版这里会把未识别的键名当文本 insertText 进输入框（按 Delete 变成
        # 打出 "Delete" 五个字母）。改成直接报错，避免悄悄污染提示词。
        raise RuntimeError("press_key: 不支持的按键 %r" % key)

    def _fill(self, text):
        """把 text 写进编辑器，**写完必须读回校验**，失败就重试 / 降级，最终仍失败则抛错。

        为什么必须「先体检 + 写完读回」（2026-09-17 实测）：
        - 豆包编辑器会停在**未就绪态**：点「新对话」/进视频模式/上传参考图后，
          `tiptap ProseMirror` 会被短暂置为 `visibility:hidden` + 继承 `pointer-events:none`。
          此状态下 `e.focus()` 不生效（不可聚焦）、`Input.insertText` 无处可落、
          `execCommand('insertText')` 直接返回 false —— 但 `querySelector` 照样能拿到它。
          旧实现只看「找得到 + focus 被调用」，于是把这种输入框当可用，盲写两次后
          统一报「提示词未能写入」，真实原因被盖掉。
        - 即使是就绪态，`Input.insertText` 也是「一次性盲写」：ack 成功 ≠ 文字落进 DOM。
          实测连续写入会出现随机丢字（500 字丢、600/1000 字成），页面忙碌时命中率更差。
        """
        if not text:
            # 清空意图：不校验、不重试（清空失败不致命）
            try:
                self._ws.evaluate(_JS_FILL_PREPARE)
            except Exception:
                pass
            return 0

        health = self.input_health()
        if not health.get("found"):
            raise RuntimeError("fill: %s —— 找不到豆包输入框(contenteditable/textarea)" % FILL_HARD_ERR)
        blocked = not bool(health.get("ready", not (health.get("blockedBy") or health.get("pe") == "none")))
        if blocked:
            # 输入区停在未就绪态（visibility:hidden / pointer-events:none）：先唤醒再等。
            # 不能在未就绪时盲写 —— focus() 不生效，insertText 无处可落，只会白等一轮。
            #
            # 【2026-09-20】必须周期性 `activate()`（Page.bringToFront），不能只 `nudge()`。
            # 后台 webview 里的豆包 SPA 靠可见性/动画帧推进渲染，标签不在前台时它**不会**
            # 自己从 hidden 态恢复；mousemove + resize 事件在后台标签同样可能被丢弃。
            # 实测：上传参考图后编辑器被重建为 hidden 态，旧实现 25s 内只 nudge 从不置前，
            # u_35463537 / u_42045857 连续两轮死在同一句报错上。
            deadline = time.time() + BLOCKED_WAIT_S
            t0 = time.time()
            last_act = -9.0          # 首轮立即置前
            while time.time() < deadline:
                if (time.time() - t0) - last_act >= 5.0:
                    try:
                        self.activate()
                    except Exception:
                        pass
                    last_act = time.time() - t0
                self.nudge()
                time.sleep(1.0)
                health = self.input_health()
                if health.get("ready"):
                    blocked = False
                    break
            if blocked:
                # 把「页面上到底有几个编辑器、各自什么状态」写进错误里 —— 否则下次
                # 只能靠猜：是「唯一的编辑器被隐藏」还是「挑错了元素」。2026-09-20
                # 就是靠这个疑问才不得不回头翻日志。
                others = health.get("others") or []
                diag = "候选 %d 个(contenteditable=%s/textarea=%s)" % (
                    (len(others) + 1) if health.get("found") else 0,
                    health.get("ceCount"), health.get("taCount"))
                if others:
                    diag += "；其它: " + "; ".join(
                        "%s pe=%s vis=%s h=%s%s" % (
                            (o.get("cls") or "?"), o.get("pe"), o.get("vis"), o.get("h"),
                            (" blocked=" + str(o.get("blocked"))) if o.get("blocked") else "")
                        for o in others[:3])
                raise RuntimeError(
                    "fill: %s —— 豆包输入框未就绪（%s），此状态下 focus()/insertText 全部无效，无法写入提示词；%s"
                    % (FILL_HARD_ERR,
                       health.get("blockedBy") or ("pe=%s vis=%s" % (health.get("pe"), health.get("vis"))),
                       diag)
                )

        want = _norm(text)[:24]
        attempts = 4
        last = ""
        for i in range(attempts):
            # ① 清空 + 落光标（JS 层，不依赖鼠标）
            try:
                self._ws.evaluate(_JS_FILL_PREPARE)
            except Exception:
                pass
            time.sleep(0.12)
            try:
                self._ws.evaluate(_JS_PLACE_CARET)
            except Exception:
                pass
            # ② 真实鼠标点一下（走框架完整输入管线，Lexical/ProseMirror 才真的落光标）
            try:
                rect = self.input_health()
                if rect.get("w") and rect.get("h"):
                    x, y = int(rect["x"]), int(rect["y"])
                    self._ws.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
                    self._ws.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
                    time.sleep(0.2)
            except Exception:
                pass
            # ③ Ctrl+A 兜底（若框架把清空前的内容恢复了，全选后 insertText 会直接覆盖）
            try:
                self._ws.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": "a", "code": "KeyA", "modifiers": 2, "windowsVirtualKeyCode": 65})
                self._ws.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": "a", "code": "KeyA", "modifiers": 2, "windowsVirtualKeyCode": 65})
                time.sleep(0.1)
            except Exception:
                pass
            # ④ 写入 + 读回校验
            try:
                self._ws.send("Input.insertText", {"text": text})
            except Exception:
                pass
            time.sleep(0.35)
            last = self._editor_text()
            if want and want in _norm(last):
                self.last_fill_mode = "insertText" + ("" if i == 0 else "(retry%d)" % i)
                return len(last)
            # ⑤ 降级：JS 层写入（CDP Input 通道失效时的唯一出路）
            try:
                mode = self.evaluate(_JS_SET_TEXT, {"text": text})
            except Exception:
                mode = None
            time.sleep(0.3)
            last = self._editor_text()
            if want and want in _norm(last):
                self.last_fill_mode = str(mode or "js")
                return len(last)
            time.sleep(0.25 + 0.4 * i)
        raise RuntimeError(
            "fill: %s —— 编辑器拒收提示词（4 次写入+降级后仍为空，实读 %d 字，输入区%s）"
            % (FILL_HARD_ERR,
               len(str(last or "").strip()),
               ("被 pointer-events/visibility 禁用" if blocked else "体检正常但未收字，疑似页面重渲染抢走输入"))
        )


    def _set_files(self, paths):
        self._ws.send("DOM.enable")
        root = self._ws.send("DOM.getDocument")
        rid = root.get("root", {}).get("nodeId")
        for sel in ['input[type="file"]', 'input[type=file]']:
            try:
                r = self._ws.send("DOM.querySelector", {"nodeId": rid, "selector": sel})
                nid = r.get("nodeId")
            except Exception:
                continue
            if nid and nid != 0:
                try:
                    self._ws.send("DOM.setFileInputFiles", {"nodeId": nid, "files": paths})
                    return True
                except Exception:
                    continue
        return False

    def expect_file_chooser(self, timeout=4000):
        class _C:
            def __init__(s, page):
                s._page = page
                s.value = type("F", (), {
                    "set_files": lambda self, p, _page=s._page: _page._set_files(list(p) if isinstance(p, (list, tuple)) else [str(p)])
                })()
            def __enter__(s):
                return s
            def __exit__(s, *a):
                return False
        return _C(self)

    def close(self):
        try:
            self._ws.close()
        except Exception:
            pass


def _junk():
    pass