# -*- coding: utf-8 -*-
"""豆包管理器后端驱动（Step 2）。
通过管理器开放的 CDP(127.0.0.1:9223) 物化某个豆包账号的 doubao webview 目标，
再用原始 CDP 在该登录态页面上驱动生成。不依赖加密桥 / Ed25519 握手 / 白名单。

用法（命令行）：
  1) 启动管理器并物化所有 doubao webview：  python doubao_manager.py
  2) 对某个账号提交一条生成并轮询成片：     python doubao_manager.py send --account 萧总 "提示词"
"""
import json
import os
import socket
import subprocess
import time
import sys
from pathlib import Path

import requests
import websocket

# 管理器路径/端口可配置：默认端口 9223，exe 默认不假定（可空）。
# 实际驱动只依赖「管理器已打开的 CDP 端口」，与安装位置无关 → 换机器/换盘/换目录都能用。
MANAGER_EXE = Path(r"D:\programs\免验证绿色版\豆包管理器.exe")  # 仅「自动拉起」用；可被参数覆盖或置空
MANAGER_PORT = 9223
CDP_HOST = "127.0.0.1"


def _log(msg):
    """本模块自己打日志。

    曾经这里漏了定义，导致 open_account / discover_accounts / ensure_manager_running
    等一切带日志的函数一执行就 NameError——「自动点开账号卡片」因此从来没生效过。
    """
    print("[doubao_manager] %s" % msg, flush=True)


def _resolve_exe(exe=None):
    """返回要尝试启动的管理器 exe 路径；None 表示不假定（靠已打开的 CDP）。"""
    if exe:
        return Path(exe)
    # 仅当调用方要求自动拉起时才去常见位置探测，绝不硬假设单一路径
    for cand in (
        MANAGER_EXE,
        Path("D:/programs/豆包管理器/豆包管理器.exe"),
        Path("C:/豆包管理器/豆包管理器.exe"),
        Path.home() / "Desktop" / "豆包管理器.exe",
        Path.home() / "Desktop" / "免验证绿色版" / "豆包管理器.exe",
    ):
        try:
            if cand.exists():
                return cand
        except Exception:
            continue
    return None


def wait_tcp(port=MANAGER_PORT, tries=60, every=0.5):
    for _ in range(tries):
        s = socket.socket(); s.settimeout(0.4)
        try:
            s.connect((CDP_HOST, port)); s.close(); return True
        except OSError:
            pass
        finally:
            s.close()
        time.sleep(every)
    return False


def ensure_manager_running(port=MANAGER_PORT, exe=None):
    """确保管理器可用：连接 CDP 端口。若端口不通且能找到 exe 路径就尝试拉起它（可配置）。
    不假定安装位置：管理器装哪、开没开，只认 CDP 端口。"""
    if not wait_tcp(port, tries=4, every=0.3):
        exe_path = _resolve_exe(exe)
        if exe_path:
            _log("管理器未在 %s 启动，尝试拉起 %s" % (port, exe_path))
            try:
                kwargs = {"cwd": str(exe_path.parent)}
                if os.name == "nt":
                    # Windows：脱离父进程、独立进程组，避免被字字动画退出时连带杀掉
                    kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                else:
                    kwargs["start_new_session"] = True
                subprocess.Popen([str(exe_path)], **kwargs)
            except Exception as e:
                _log("拉起管理器失败: %s" % e)
        else:
            _log("管理器未启动，且没找到管理器 exe（可在插件设置里填 manager_exe 路径）")
    if not wait_tcp(port):
        raise RuntimeError("豆包管理器 CDP %s:%s 连不上（请确认豆包管理器已打开）" % (CDP_HOST, port))
    _log("管理器 CDP 就绪 %s:%s" % (CDP_HOST, port))


class CdpSocket:
    # 60s：带网络请求的重型 evaluate（原片解析/出片探测内部 fetch 串行可达 20s+，
    # 旧值 30s 在豆包页面繁忙时误杀；2026-09-19 实测 manager 通道出片后探测全挂）。
    TIMEOUT_S = 60.0

    def __init__(self, ws_url):
        self.ws_url = ws_url
        self.target_url = ""   # 目标对应的页面 URL：target id 变化后重映射用
        self.ws = websocket.create_connection(ws_url, timeout=self.TIMEOUT_S)
        self._id = 0
        try:
            self._remember_target_url()
        except Exception:  # noqa: BLE001
            pass

    def send(self, method, params=None, _retry=True):
        self._id += 1
        mid = self._id
        try:
            self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            while True:
                msg = self.ws.recv()
                if isinstance(msg, bytes):
                    msg = msg.decode("utf-8", "replace")
                try:
                    data = json.loads(msg)
                except Exception:
                    continue
                if data.get("id") == mid:
                    if data.get("error"):
                        raise RuntimeError("%s: %s" % (method, data["error"].get("message")))
                    return data.get("result", {})
        except (OSError, socket.timeout, websocket.WebSocketException) as e:
            # 连接级故障（10060/超时/对端断开）：出片探测这类 awaitPromise 重型
            # evaluate 会让渲染进程忙上十几秒，TCP 被判死后旧 socket 报废。
            # 重建连接；Runtime.evaluate 按幂等处理自动重试一次——本插件的
            # 大型 evaluate 全是只读探测（取文本/坐标/官方接口），会点按钮、
            # 写输入框的小 evaluate 都在毫秒级完成，撞不上这个超时，无重复副作用。
            try:
                self.ws.close()
            except Exception:  # noqa: BLE001
                pass
            if _retry and method == "Runtime.evaluate":
                self._reconnect()
                # 光重连没用：后台 webview 被冻结时 JS 根本不执行，下一发照样挂满
                # TIMEOUT_S 再超时一次（单次调用空耗 2×60s）。先解冻+置前再重试。
                self._wake_target()
                return self.send(method, params, _retry=False)
            raise

    def _wake_target(self):
        """解冻并置前台（fire-and-forget，不读回执）。

        管理器里非当前显示的账号 webview 在 Chromium 眼里是后台标签，会被冻结：
        JS 不执行、`awaitPromise` 型 evaluate 一直挂到超时。表现就是「豆包管理器里
        视频明明已经生成，插件却迟迟取不回原片」——日志上是成片已出之后连续
        `原片解析异常: Connection timed out`。
        此时重建 socket 无效（socket 是好的，是渲染进程没在跑），必须先把页面激活。

        【2026-09-21 唤醒风暴修复】`Page.bringToFront` 是跨进程焦点切换，对
        Electron 主进程有真实成本。4 个账号 webview 同时超时时，每次失败都置前
        会形成焦点风暴（实测把管理器整个压崩：页面卡死、刷新无用、进程退出）。
        因此：
          - `setWebLifecycleState('active')` 便宜且不抢焦点 → 每次都发；
          - `bringToFront` 有成本 → 实例级 30s 冷却，风暴期内最多置前一次。
        """
        now = time.time()
        for method, params in (("Page.setWebLifecycleState", {"state": "active"}),
                               ("Page.bringToFront", None)):
            if method == "Page.bringToFront":
                if now - getattr(self, "_last_btf", 0.0) < 30.0:
                    continue   # 30s 内已置前过，只解冻不再抢焦点
                self._last_btf = now
            try:
                self._id += 1
                self.ws.send(json.dumps({"id": self._id, "method": method,
                                         "params": params or {}}))
            except Exception:  # noqa: BLE001
                return
        time.sleep(0.3)

    def _targets(self):
        from urllib.parse import urlparse
        u = urlparse(self.ws_url)
        port = u.port or MANAGER_PORT
        return requests.get("http://%s:%d/json/list" % (CDP_HOST, port), timeout=5).json()

    def _remember_target_url(self):
        tid = self.ws_url.rstrip("/").rsplit("/", 1)[-1]
        for t in self._targets():
            if t.get("id") == tid:
                self.target_url = str(t.get("url") or "")
                return

    def _reconnect(self):
        try:
            self.ws = websocket.create_connection(self.ws_url, timeout=self.TIMEOUT_S)
            return
        except Exception:  # noqa: BLE001
            pass
        # 原地址直连失败：目标可能被管理器重建（ws 地址/target id 都变了）
        try:
            targets = self._targets()
        except Exception:  # noqa: BLE001
            targets = []
        tid = self.ws_url.rstrip("/").rsplit("/", 1)[-1]
        # 1) 按原 target id 重查（多数只是 socket 断，目标还在）
        for t in targets:
            if t.get("id") == tid and t.get("webSocketDebuggerUrl"):
                self.ws_url = t["webSocketDebuggerUrl"]
                self.target_url = str(t.get("url") or self.target_url or "")
                self.ws = websocket.create_connection(self.ws_url, timeout=self.TIMEOUT_S)
                return
        # 2) id 已消失 = webview 被重建：按连接时记下的页面 URL 重映射。
        #    必须整条 URL（去 query）精确匹配——管理器里多个账号 webview 都是
        #    doubao.com 页面，模糊匹配会串到别人的账号上。
        want = (self.target_url or "").split("?")[0]
        if want:
            for t in targets:
                if (str(t.get("url") or "").split("?")[0] == want
                        and t.get("webSocketDebuggerUrl")):
                    self.ws_url = t["webSocketDebuggerUrl"]
                    self.ws = websocket.create_connection(self.ws_url, timeout=self.TIMEOUT_S)
                    _log("CDP 目标已被管理器重建，按 URL 重映射成功: %s" % want[:80])
                    return
        raise OSError("CDP 重连失败（目标已不可达）: %s" % self.ws_url)

    def evaluate(self, expression, await_promise=True):
        res = self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": bool(await_promise),
            "userGesture": True,
        })
        r = res.get("result", {})
        if res.get("exceptionDetails"):
            raise RuntimeError("exception: %s" % res["exceptionDetails"])
        return r.get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def json_dumps(o):
    s = json.dumps(o, ensure_ascii=False)
    return s


class ManagerBridge:
    def __init__(self, port=MANAGER_PORT, host=CDP_HOST):
        self.http = "http://%s:%s" % (host, port)

    def list(self):
        return requests.get(self.http + "/json/list", timeout=5).json()

    def doubao_webviews(self):
        """列出可用的豆包账号窗口。

        管理器不同版本/不同调用方式下，豆包账号窗口的 target 类型可能是
        webview（管理器内嵌）也可能是 page（独立标签），这里都认；
        只排除管理器自己的界面（file:// 的 renderer）。
        """
        out = []
        for t in self.list():
            url = t.get("url") or ""
            if "doubao.com" not in url:
                continue
            ttype = t.get("type")
            if ttype == "webview" or (ttype == "page" and not url.startswith("file://")):
                out.append(t)
        return out

    def renderer(self):
        for t in self.list():
            if t.get("type") == "page" and "index.html" in (t.get("url") or ""):
                return t
        return None

    def open_account(self, name):
        """点开管理中名称为 name 的账号卡片，物化其 doubao webview，返回该目标 dict。"""
        r = self.renderer()
        if not r:
            raise RuntimeError("找不到管理器 renderer")
        s = CdpSocket(r["webSocketDebuggerUrl"])
        try:
            done = s.evaluate("""(() => {
                const cards = document.querySelectorAll('.account-card');
                for (const c of cards) {
                  const t = (c.innerText||c.textContent||'').trim();
                  if (t.indexOf(%s) !== -1) { c.click(); return true; }
                }
                return false;
              })()""" % json_dumps(name))
            _log("点账号卡片 %s -> %s" % (name, done))
        finally:
            s.close()
        t0 = time.time()
        while time.time() - t0 < 20:
            ws = self.doubao_webviews()
            if ws:
                return ws
            time.sleep(1)
        return self.doubao_webviews()

    def account_names(self):
        """读 renderer 里所有可点的账号卡片名（去重、保序）。

        卡片结构：第一行是「豆」logo，真正账号名在「豆包 · 萧总」这一行。
        旧实现取首行会得到 "豆"（导致按名字点卡片永远匹配不上），
        这里优先从「豆包 · xxx」提取，再退回逐行找非 logo 行。
        """
        r = self.renderer()
        if not r:
            return []
        s = CdpSocket(r["webSocketDebuggerUrl"])
        try:
            names = s.evaluate(r'''(() => {
                const out = [];
                for (const c of document.querySelectorAll('.account-card')) {
                  const t = (c.innerText || c.textContent || '').trim();
                  let name = '';
                  const m = t.match(/\u8c46\u5305\s*\u00b7\s*([^\n\u00b7]+)/);
                  if (m) name = m[1].trim();
                  if (!name) {
                    for (const line of t.split(/\n/)) {
                      const l = line.trim();
                      if (!l || l === '\u8c46' || l === '\u22ee' || l.indexOf('\u8c46\u5305 \u00b7') !== -1) continue;
                      name = l; break;
                    }
                  }
                  if (name && out.indexOf(name) === -1) out.push(name);
                }
                return out;
              })()''')
            return names or []
        except Exception:
            return []
        finally:
            s.close()

    def discover_accounts(self):
        """一次性物化所有账号，返回 {name: webview_ws}。顺序=renderer 卡片顺序。"""
        names = self.account_names()
        if not names:
            raise RuntimeError("管理器里没有账号卡片（renderer 空）")
        ws_before = {t["webSocketDebuggerUrl"] for t in self.doubao_webviews()}
        result = {}
        for nm in names:
            # 若该账号目标已物化，直接挂到其结果，不重复点卡片
            current = {t["webSocketDebuggerUrl"]: t for t in self.doubao_webviews()}
            newones = [t for k, t in current.items() if k not in ws_before]
            if newones and len(result) < len(names):
                result[names[len(result)]] = newones[0]
                ws_before = set(current)
                continue
            self.open_account(nm)
            time.sleep(1.0)
            cur = self.doubao_webviews()
            newones = [t for t in cur if t["webSocketDebuggerUrl"] not in ws_before]
            if newones:
                result[nm] = newones[0]
                ws_before = {t["webSocketDebuggerUrl"] for t in cur}
            else:
                _log("账号 %s 未能新物化 webview，跳过" % nm)
        return result


def pick_webview(webviews, index=0):
    if not webviews:
        raise RuntimeError("没有可用的 doubao webview；请先在管理器里点开账号")
    return webviews[index]


def ensure_doubao_webviews(port=MANAGER_PORT, exe=None, log=None):
    """返回可用的豆包账号窗口；没有时自动拉起管理器并点开账号卡片物化。

    「账号已登录」≠「webview 存在」：管理器只有被点开的账号才会有 webview，
    管理器重启后 webview 会全部消失。这里按顺序做到：
      1) 端口不通 → 自动拉起管理器（配了 exe 或在常见路径能找到）
      2) 等 renderer 界面就绪（刚启动时账号卡片还没渲染，读不到）
      3) 自动点开全部账号卡片，把 webview 物化出来
    """
    def _say(msg):
        if log:
            try:
                log(msg)
            except Exception:
                pass

    # 1) 管理器没开就尝试拉起
    try:
        ensure_manager_running(port=port, exe=exe)
    except Exception as e:  # noqa: BLE001
        _say("豆包管理器不可用：%s" % e)
        return []

    br = ManagerBridge(port=port)
    try:
        wvs = br.doubao_webviews()
    except Exception as e:  # noqa: BLE001
        _say("读取管理器目标失败：%s" % e)
        return []
    if wvs:
        return wvs

    # 2) 等 renderer 就绪并把账号卡片名读出来
    names = []
    for _ in range(20):
        try:
            wvs = br.doubao_webviews()
        except Exception:  # noqa: BLE001
            wvs = []
        if wvs:
            return wvs
        try:
            names = br.account_names()
        except Exception:  # noqa: BLE001
            names = []
        if names:
            break
        time.sleep(1.0)

    _say("管理器没有已打开的豆包账号窗口，账号卡片: %s，尝试自动点开…" % (names or "无"))
    if names:
        try:
            br.discover_accounts()
        except Exception as e:  # noqa: BLE001
            _say("自动点开账号卡片失败: %s" % e)

    # 3) 等 webview 目标冒出来（点开卡片到建好 target 有延迟）
    for _ in range(15):
        try:
            wvs = br.doubao_webviews()
        except Exception:  # noqa: BLE001
            wvs = []
        if wvs:
            _say("已物化豆包账号窗口: %d 个" % len(wvs))
            return wvs
        time.sleep(1.0)
    return []


# --------------------------------------------------------- doubao 驱动工具（复用 main.py 的 DOM 经验）

SEND_BTN_SELECTORS = ["#flow-end-msg-send", "button[class*='send']", "button:has-text('发送')"]

_JS_SURVEY = r"""(() => {
  const out = { title: document.title, url: location.href, ready: document.readyState };
  out.inputs = [];
  const q = (s) => { try { const e = document.querySelector(s); return e ? (e.tagName + (e.className||'').toString().slice(0,40)) : null; } catch(e) { return null; } };
  out.send_box = q("#flow-end-msg-send");
  let contenteditable = null;
  const ce = document.querySelectorAll('[contenteditable="true"]');
  contenteditable = ce.length ? (ce[0].tagName + ' n=' + ce.length) : null;
  out.contenteditable = contenteditable;
  const ta = document.querySelector('textarea');
  out.textarea = ta ? ta.tagName : null;
  // 新建对话/历史侧边按钮（文本匹配）
  const btns = Array.from(document.querySelectorAll('button, [role="button"]'));
  const wants = ['新对话','新建对话','新建','start chat','Generatea','生成视频','视频生成','视频模式','+'];
  out.buttons = [];
  for (const b of btns) {
    const t = (b.innerText||b.textContent||'').trim().replace(/\s+/g,' ');
    if (!t || t.length > 18) continue;
    for (const w of wants) { if (t.indexOf(w) !== -1) { out.buttons.push(t); break; } }
  }
  out.videos = document.querySelectorAll('video').length;
  return out;
})()"""

_JS_NEW_CHAT = r"""(() => {
  // 侧边栏「新对话」在 2026-09 的豆包上是
  //   <div class="group/sidebar_nav_item cursor-pointer ..."><span>新对话</span><span>Ctrl Shift K</span></div>
  // ——**既不是 button 也没有 role=button**，而且按钮文本带快捷键提示（长度 17）。
  // 旧实现两条都踩：「t.length <= 14」把它滤掉，「只能点 button/[role=button]/a」再滤一次，
  // 于是 manager 后端从来没真的新建过会话（日志长期打「未点到新对话」）。
  // 现在：剥掉尾部快捷键提示后按文本匹配，并接受「带 cursor-pointer / nav_item 线索」
  // 的容器元素；只返回坐标，由 Python 侧发真实鼠标事件（JS 合成 click 对这类 React
  // 导航项不稳，插件里其它点击早就统一改成真实鼠标事件了）。
  const wantTexts = ['新对话','新建对话','新建事项','新会话'];
  const clean = (x) => String(x || '')
      .replace(/\s+/g, ' ').trim()
      .replace(/\s*(Ctrl|⌘|Cmd|Alt|Shift|Control)(\s*[+＋]?\s*[A-Za-z0-9])*\s*$/gi, '')
      .trim();
  const exact = (t) => { for (const w of wantTexts) { if (t === w) return w; } return ''; };
  const cands = [];
  for (const el of document.querySelectorAll('button, [role="button"], a, div, li, span')) {
    let t = '';
    try { t = clean(el.innerText || el.textContent || ''); } catch (e) { continue; }
    if (!t || t.length > 18) continue;
    let hit = false;
    for (const w of wantTexts) { if (t === w || t.indexOf(w) === 0 || t.endsWith(w)) { hit = true; break; } }
    if (!hit) continue;
    let r = null;
    try { r = el.getBoundingClientRect(); } catch (e) { continue; }
    if (!(r.width > 0 && r.height > 0)) continue;
    const c = el.closest ? el.closest('button, [role="button"], a') : null;
    const cls = String(el.className || '');
    const hint = /cursor-pointer|cursor_point|nav_item|nav-item/i.test(cls) || el.tagName === 'BUTTON' || el.tagName === 'A';
    if (!c && !hint) continue;
    const target = c || el;
    let tr = null;
    try { tr = target.getBoundingClientRect(); } catch (e) { continue; }
    if (!(tr.width > 0 && tr.height > 0)) continue;
    cands.push({
      x: Math.round(tr.left + tr.width / 2), y: Math.round(tr.top + tr.height / 2),
      text: t, tag: target.tagName,
      score: (exact(t) ? 3 : 1) + (c ? 2 : 0) + (target === el ? 1 : 0)
    });
  }
  if (!cands.length) return false;
  cands.sort((a, b) => b.score - a.score);
  return cands[0];
})()"""

_JS_ENTER_VIDEO_MODE = r"""(() => {
  // 只找「编辑器里下方工具切换行的「BUTTON 视频生成」，排除左侧历史聊天里的「生成视频」。
  let ed = document.querySelector('[contenteditable="true"]') || document.querySelector('textarea');
  const edY = ed ? (ed.getBoundingClientRect().top + ed.getBoundingClientRect().height) : 620;
  const s = (x)=>{try{return (x.innerText||x.textContent||'').trim().replace(/\s+/g,' ');}catch(e){return '';} };
  const els = Array.from(document.querySelectorAll('button, [role="button"], div, span'));
  const cands = [];
  for (const el of els) {
    const t = s(el); if (t !== '视频生成' && t !== '生成视频') continue;
    let r = null; try { r = el.getBoundingClientRect(); } catch(e) { continue; }
    if (!r || r.width <= 0 || r.height <= 0) continue;
    if (r.left < 250) continue;            // 排除左侧历史会话列表项(x<250)
    if (r.top < edY - 60 || r.top > edY + 260) continue;  // 工具带在编辑器下方附近
    const isReal = /^(BUTTON|A)$/.test(el.tagName) || (el.getAttribute && el.getAttribute('role') === 'button');
    cands.push({el: el, top:r.top, left:r.left, isReal});
  }
  cands.sort((aa,bb)=>(bb.isReal - aa.isReal) || (aa.top - bb.top));
  if (cands.length) {
    cands[0].el.click();
    return {clicks: cands.length, real: cands[0].isReal, point: [Math.round(cands[0].left), Math.round(cands[0].top)]};
  }
  return false;
})()"""

_JS_UPLOAD_REVEAL = r"""(() => {
  const text = Array.from(document.querySelectorAll('button, [role=button], div, span'))
    .find(el => /上传|添加图片|添加参考图|附件|加图|参考图|补充图片|选择图片|从本地上传|图片\s*|图片按钮/i.test((el.innerText||el.textContent||'').trim()) && (el.innerText||el.textContent||'').length <= 16);
  if (text) { try { (text.closest('button,[role=button]') || text).click(); return true; } catch(e) { return 'clickerr'; } }
  // 兜底：直接聚焦任何隐藏 file input（有的版本 button 不明确，靠 input 触发）
  return false;
})()"""

_JS_MODEL_LABEL = r"""(() => {
  const body = (document.body && document.body.innerText) || '';
  const m = body.match(/(模型|视频模型)[\s：:]*([^\n]{1,30})/);
  const hasSeedream = body.indexOf('Seedream') !== -1;
  const hasSeedance = body.indexOf('Seedance') !== -1;
  const hasVideoUI = body.indexOf('视频模式') !== -1 || body.indexOf('生成视频') !== -1;
  // 附加诊断（不改判定门槛）：只在「输入区工具带」范围内找模型名。
  // 正文里到处都有 'Seedance'（首页/侧边栏/历史消息的营销与回复文案），
  // 全局 indexOf 会让「输入区已死的账号」也报 seedance=True。
  // 这个字段用于日志与后续收敛，避免直接改判定导致能用的账号被误判。
  let scopedSeedance = false, scopedModel = '';
  try {
    const ED = document.querySelector('[contenteditable="true"]') || document.querySelector('textarea');
    const edTop = ED ? ED.getBoundingClientRect().top : 0;
    for (const el of document.querySelectorAll('button, [role="button"], div, span')) {
      let t = '';
      try { t = (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' '); } catch (e) { continue; }
      if (!t || t.length > 40) continue;
      if (el.querySelector('button, [role="button"]')) continue;
      let r = null;
      try { r = el.getBoundingClientRect(); } catch (e) { continue; }
      if (!(r.width > 0 && r.height > 0)) continue;
      if (r.left < 250) continue;
      if (r.top < edTop - 140 || r.top > edTop + 340) continue;
      if (t.indexOf('Seedance') !== -1) { scopedSeedance = true; scopedModel = scopedModel || t; }
    }
  } catch (e) {}
  return { model: m ? m[2] : '', seedream: hasSeedream, seedance: hasSeedance, videoUI: hasVideoUI,
           modelScope: scopedModel, seedanceScope: scopedSeedance };
})()"""

_JS_FOCUS_EDITOR = r"""(() => {
  let editor = document.querySelector('[contenteditable="true"]');
  if (!editor) editor = document.querySelector('textarea');
  if (!editor) return false;
  editor.focus();
  const r = document.createRange(); r.selectNodeContents(editor);
  const sel = window.getSelection(); sel.removeAllRanges();
  return true;
})()"""

_JS_CLICK_SEND = r"""(() => {
  let send = document.querySelector('#flow-end-msg-send');
  if (!send) {
    const btns = Array.from(document.querySelectorAll('button'));
    send = btns.find(b => /发送|生成/.test((b.innerText||'').trim()));
  }
  if (!send) return false;
  send.click();
  return true;
})()"""


_JS_EDITOR_RECT = r"""(() => {
  let e = document.querySelector('[contenteditable="true"]');
  if (!e) e = document.querySelector('textarea');
  if (!e) return null;
  e.scrollIntoView({block:'center'});
  const r = e.getBoundingClientRect();
  return {x: r.left + r.width/2, y: r.top + r.height/2, w: r.width, h: r.height};
})()"""


_JS_DIAG = r"""(() => {
  const list = [];
  const collect = (sel, kind) => {
    Array.from(document.querySelectorAll(sel)).forEach((e, i) => {
      const r = e.getBoundingClientRect();
      list.push({sel, i, kind, tag: e.tagName,
        cls: (e.className||'').toString().slice(0,40),
        ph: (e.getAttribute&&e.getAttribute('placeholder'))||'',
        y: Math.round(r.top), h: Math.round(r.height),
        text: (e.textContent||'').slice(0,40)});
    });
  };
  collect('[contenteditable="true"]', 'ce');
  collect('textarea', 'ta');
  collect('input[type="text"]', 'in');
  return list;
})()"""


_JS_CLEAR_EDITOR = r"""(() => {
  const e = document.querySelector('[contenteditable="true"]') || document.querySelector('textarea');
  if (!e) return false;
  if (e.tagName === 'TEXTAREA' || e.tagName === 'INPUT') { e.value=''; }
  else { e.textContent=''; }
  e.dispatchEvent(new Event('input', {bubbles:true}));
  return true;
})()"""

_JS_ACTIVE = r"""(() => {
  const a = document.activeElement;
  return a ? {tag: a.tagName, cls: (a.className||'').toString().slice(0,40), text: (a.textContent||'').slice(0,60)} : null;
})()"""


def inspect_webview(ws_url):
    s = CdpSocket(ws_url)
    try:
        val = s.evaluate(_JS_SURVEY)
    finally:
        s.close()
    return val


def new_chat_and_wait(ws, timeout_s=15):
    """点侧边栏「新对话」并等编辑器就绪。返回匹配到的按钮文本（空串=没点到）。

    `_JS_NEW_CHAT` 现在只返回坐标（那个元素是带 cursor-pointer 的 div，
    不是 button/role=button），真实鼠标事件由这里发。
    """
    s = CdpSocket(ws)
    try:
        hit = s.evaluate(_JS_NEW_CHAT)
        done = ""
        if isinstance(hit, dict) and hit.get("x") is not None:
            x, y = int(hit["x"]), int(hit["y"])
            s.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
            s.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
            done = str(hit.get("text") or "")
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            try:
                val = s.evaluate("(document.querySelectorAll('[contenteditable=\"true\"]').length + document.querySelectorAll('textarea').length)")
                if val:
                    return done
            except Exception:
                pass
            time.sleep(1)
        return done
    finally:
        s.close()


def submit_on_webview(ws, prompt, enter_mode=True, dry=False):
    """在指定 doubao webview 上：进视频模式（可选）→ 鼠标点输入框 → 真实输入录入 → 点发送。"""
    s = CdpSocket(ws)
    out = {"mode": None, "focused": False, "typed": False, "send": None}
    try:
        if enter_mode:
            out["mode"] = s.evaluate(_JS_ENTER_VIDEO_MODE)
            time.sleep(1.5)
        rc = s.evaluate(_JS_EDITOR_RECT)
        if not rc:
            out["err"] = "no-editor-rect"
            return out
        s.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": rc["x"], "y": rc["y"], "button": "left", "clickCount": 1})
        s.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": rc["x"], "y": rc["y"], "button": "left", "clickCount": 1})
        out["focused"] = True
        time.sleep(0.4)
        try:
            s.send("Input.insertText", {"text": prompt})
            out["typed"] = True
        except Exception as e:
            out["typed"] = "err:" + str(e)
        time.sleep(0.5)
        if not dry:
            out["send"] = s.evaluate(_JS_CLICK_SEND)
        else:
            out["editor"] = s.evaluate("(()=>{const e=document.querySelector('[contenteditable=\"true\"]')||document.querySelector('textarea');return e?e.textContent:null})()")
        return out
    finally:
        s.close()


_JS_VIDEO_SRCS = r"""(() => (Array.from(document.querySelectorAll('video')).map(v => (v.currentSrc || v.src || '')).filter(s => s && s.indexOf('blob:') !== 0)))()"""

_JS_VIDEO_ALL = r"""(() => (Array.from(document.querySelectorAll('video')).map(v => ({src:(v.currentSrc||v.src||'').slice(0,120), poster:(v.poster||'').slice(0,80)})).slice(0,20)))()"""


def baseline_video_srcs(s):
    try:
        arr = s.evaluate(_JS_VIDEO_SRCS)
        return {str(x).split("?")[0] for x in (arr or []) if x}
    except Exception:
        return set()


def new_video_src(s, baseline):
    try:
        arr = s.evaluate(_JS_VIDEO_SRCS) or []
    except Exception:
        return None
    hit = None
    for v in arr:
        v = str(v or "")
        if not v:
            continue
        if v.split("?")[0] in baseline:
            continue
        hit = v
    return hit


def run_generate(ws, prompt, wait_s=240, poll_s=6):
    """完整链路：进视频模式 → 清空 → 键入提示词 → 发送 → 等新成片（基线外 video src）。
    返回 {"ok":bool,"video":src或None,"detail":...}。"""
    s = CdpSocket(ws)
    out = {"ok": False, "video": None, "detail": ""}
    try:
        out["mode"] = s.evaluate(_JS_ENTER_VIDEO_MODE)
        time.sleep(1.5)
        s.evaluate(_JS_CLEAR_EDITOR)
        time.sleep(0.2)
        baseline = baseline_video_srcs(s)
        _log("历史视频基线: %d 条" % len(baseline))
        rc = s.evaluate(_JS_EDITOR_RECT)
        s.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": rc["x"], "y": rc["y"], "button": "left", "clickCount": 1})
        s.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": rc["x"], "y": rc["y"], "button": "left", "clickCount": 1})
        time.sleep(0.3)
        s.send("Input.insertText", {"text": prompt})
        time.sleep(0.5)
        # 用 Enter 发送（比点按钮更可靠；此前点按钮曾因编辑器草稿未注册而不触发）
        s.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "text": "\r"})
        s.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13})
        out["sent"] = True
        _log("已发送（Enter），等成片")
        t0 = time.time()
        while time.time() - t0 < wait_s:
            now = int(time.time() - t0)
            if now % 30 < poll:
                _log("已等 %d 秒" % now)
            url = new_video_src(s, baseline)
            if url:
                out["ok"] = True
                out["video"] = url
                out["detail"] = "new-video"
                return out
            time.sleep(poll)
        out["detail"] = "timeout"
        return out
    finally:
        s.close()


def upload_reference(ws, path, timeout_s=40):
    """用 CDP DOM.setFileInputFiles 把参考图(首帧) 传给 doubao 输入区的 file input。
    等价 main.py 的 _upload_reference_images 走 input[type=file]。"""
    s = CdpSocket(ws)
    try:
        # 找到可见的上传按钮并点击，触发 file input（豆包靠点上传才有真实 input）
        clicked = s.evaluate(_JS_UPLOAD_REVEAL)
        time.sleep(1.0)
        # DOM 绑定 node
        s.send("DOM.enable")
        root = s.send("DOM.getDocument")
        root_id = root.get("root", {}).get("nodeId")
        if not root_id:
            raise RuntimeError("DOM root 拿不到")
        best = None
        for expr in ['input[type="file"]', 'input[type=file]', 'input[accept*="image"]']:
            r = s.send("DOM.querySelector", {"nodeId": root_id, "selector": expr})
            nid = r.get("nodeId")
            if nid and nid != 0:
                best = nid
                break
        if not best:
            raise RuntimeError("找不到输入文件的 file input（先点上传按钮）")
        s.send("DOM.setFileInputFiles", {"nodeId": best, "files": [path]})
        time.sleep(2)
        return True
    finally:
        s.close()


def wait_gen_finish(ws):  # 占位
    pass


if __name__ == "__main__":
    sys.argv = sys.argv if len(sys.argv) > 1 else ["__main__", "probe"]
    cmd = sys.argv[1]
    ensure_manager_running()
    br = ManagerBridge()
    wv = br.doubao_webviews()
    if not wv:
        _log("无 doubao 账号窗口，尝试打开 萧总")
        wv = br.open_account("萧总")
    if cmd in ("probe", "inspect"):
        for i, t in enumerate(wv):
            try:
                pal = inspect_webview(t["webSocketDebuggerUrl"])
                _log("webview %d @ %s: %s" % (i, pal.get("title"), json.dumps(pal, ensure_ascii=False)))
            except Exception as e:
                _log("webview %d inspect err: %s" % (i, e))
    elif cmd == "mode":
        idx = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        t = wv[idx]
        s = CdpSocket(t["webSocketDebuggerUrl"])
        try:
            done = s.evaluate(_JS_ENTER_VIDEO_MODE)
            time.sleep(1.5)
            _log("进入视频模式点击: %s" % done)
            _log("模式后状态: %s" % json.dumps(s.evaluate(_JS_SURVEY), ensure_ascii=False))
        finally:
            s.close()
    elif cmd == "diag":
        idx = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        t = wv[idx]
        s = CdpSocket(t["webSocketDebuggerUrl"])
        try:
            _log(json.dumps(s.evaluate(_JS_DIAG), ensure_ascii=False))
        finally:
            s.close()
    elif cmd == "shot":
        idx = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        t = wv[idx]
        s = CdpSocket(t["webSocketDebuggerUrl"])
        try:
            import base64
            res = s.send("Page.enable")
            res = s.send("Page.captureScreenshot", {"format": "png"})
            data = res.get("data", "")
            path = Path(__file__).with_name("dbm_shot_%d.png" % idx)
            path.write_bytes(base64.b64decode(data))
            _log("saved %s" % path)
        finally:
            s.close()
    elif cmd == "dry":
        # 安全验证：进视频模式 → 清空输入框 → 鼠标点击 → 真实输入 → 读回 + activeElement，不发送。
        prompt = sys.argv[2] if len(sys.argv) > 2 else "一只橘猫在草地上奔跑"
        idx = int(sys.argv[3]) if len(sys.argv) > 3 else 0
        t = wv[idx]
        s = CdpSocket(t["webSocketDebuggerUrl"])
        try:
            mode = s.evaluate(_JS_ENTER_VIDEO_MODE); time.sleep(1.5)
            s.evaluate(_JS_CLEAR_EDITOR); time.sleep(0.2)
            rc = s.evaluate(_JS_EDITOR_RECT)
            s.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": rc["x"], "y": rc["y"], "button": "left", "clickCount": 1}); time.sleep(0.1)
            s.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": rc["x"], "y": rc["y"], "button": "left", "clickCount": 1}); time.sleep(0.3)
            s.send("Input.insertText", {"text": prompt}); time.sleep(0.4)
            ed = s.evaluate("(()=>{const e=document.querySelector('[contenteditable=\"true\"]')||document.querySelector('textarea');return e?e.textContent:null})()")
            act = s.evaluate(_JS_ACTIVE)
            _log(json.dumps({"mode": mode, "editor": ed, "active": act}, ensure_ascii=False))
        finally:
            s.close()
    elif cmd == "send":
        prompt = sys.argv[2] if len(sys.argv) > 2 else "一只橘猫在草地上奔跑，阳光明媚，写实风格"
        idx = int(sys.argv[3]) if len(sys.argv) > 3 else 0
        t = wv[idx] if wv else None
        if not t:
            _log("无可用的 doubao webview")
            sys.exit(1)
        _log("在 webview[%d] 提交: %r" % (idx, prompt))
        _log(json.dumps(submit_on_webview(t["webSocketDebuggerUrl"], prompt), ensure_ascii=False))
    elif cmd == "up":
        # 上传参考图（首帧/尾帧）到 doubao 输入框，验证图生视频的"图"这一步。
        ref = sys.argv[2] if len(sys.argv) > 2 else r"D:\programs\字字动画_9_0_9\_internal\plugins\video_plugins\video_plugin_doubao_web\_ref.png"
        idx = int(sys.argv[3]) if len(sys.argv) > 3 else 0
        t = wv[idx]
        _log("进视频模式 + 上传参考图 %s" % ref)
        ok = upload_reference(t["webSocketDebuggerUrl"], ref)
        _log("upload ok: %s" % ok)
    elif cmd == "run":
        # 完整生成链路。提示词从 utf-8 文件读取（argv 传中文会被外壳乱码）。
        import pathlib
        pf = sys.argv[2] if len(sys.argv) > 2 else None
        idx = int(sys.argv[3]) if len(sys.argv) > 3 else 0
        if not pf:
            prompt = "一只橘猫在草地上奔跑，阳光明媚，写实风格"
        else:
            prompt = pathlib.Path(pf).read_text("utf-8").strip()
        t = wv[idx]
        _log("对 webview[%d] start... prompt=%.40r" % (idx, prompt))
        res = run_generate(t["webSocketDebuggerUrl"], prompt)
        _log(json.dumps(res, ensure_ascii=False, default=str))
    elif cmd == "poll":
        # 只监听当前 webview 是否出现新成片（不重发，避免重复提交）。
        idx = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        wait_s = int(sys.argv[3]) if len(sys.argv) > 3 else 240
        t = wv[idx]
        s = CdpSocket(t["webSocketDebuggerUrl"])
        try:
            baseline = baseline_video_srcs(s)
            _log("基线: %d 条，监听新成片..." % len(baseline))
            t0 = time.time()
            res = {"ok": False, "video": None, "detail": "timeout"}
            while time.time() - t0 < wait_s:
                url = new_video_src(s, baseline)
                if url:
                    res.update(ok=True, video=url, detail="new-video")
                    break
                time.sleep(6)
            _log(json.dumps(res, ensure_ascii=False, default=str))
        finally:
            s.close()
    else:
        _log("未知命令 %s" % cmd)
    print("OK")