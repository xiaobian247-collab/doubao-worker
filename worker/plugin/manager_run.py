# -*- coding: utf-8 -*-
"""manager_run.py —— 豆包管理器生产后端。

对一个生成任务：轮询"管理器里已登录的全部豆包账号"（按 .account-card 物化每个
账号的 webview），每账号独立新会话（防串片），进入视频模式（校验 Seedance）→
可选图生视频参考图→填提示词→Enter 发送→自动确认参数→轮询成片（<video>/卡片链
带平台动态水印，仅作保底；无水印原片走官方 get_without_watermark /
get_download_info 解析）→ 下载回传字字动画输出目录。

账号级调度（额度/冷却/轮询）持久化到插件目录 manager_state.json，遵循 main.py
的"额度耗尽冷却到当天+失败短冷却"语义。页面交互走 doubao_manager + webview_page。
"""
import json
import os
import re
import sys
import time
from pathlib import Path
from datetime import datetime, date

import requests

import doubao_manager as D
import webview_page

# 主插件模块：不用 import main(会产生第二个加载副本，丢失 _output_filename 等)。
# 由 _generate_impl 传入「引擎同一次加载的 main 模块」，见 generate(main_module=...)。
M = None
_RESOLVED_MAIN = False


def _ensure_main(main_module=None):
    global M, _RESOLVED_MAIN
    if main_module is not None:
        M = main_module
        _RESOLVED_MAIN = True
        return
    if M is not None and _RESOLVED_MAIN:
        return
    # 兜底：找到引擎已加载的包名 main（plugin_video_plugins_doubao.main …）
    for _name, _mod in list(sys.modules.items()):
        if _name.endswith('.main') and ('video_plugin_doubao' in _name or 'doubao' in _name):
            M = _mod
            _RESOLVED_MAIN = True
            return
    import main as _m  # noqa: E402
    M = _m
    _RESOLVED_MAIN = True

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
_PLUGIN_DIR = Path(__file__).parent
_STATE_FILE = _PLUGIN_DIR / "manager_state.json"
_MAX_WAIT = 620
_POLL = 6
# 豆包「额度用尽」真实文案是「今日视频生成免费次数用完了…」。下面两组词只喂给
# 前置体检的 JS（JS 用不了 Python 正则）：EXHAUST 命中「已经用完」、HAVE 是豆包真正
# 受理时的正向信号，用「谁在会话尾部最后出现」定性，避免把受理话术误判成没额度。
# 提交后会话内/失败后的两处判定统一走 browser 通道久经考验的 M._QUOTA_BLOCK_RE。
_QUOTA_EXHAUST_MARKERS = (
    "次数用完了", "次数已用完", "次数已达上限", "生成次数已达上限", "剩余次数不足",
    "额度已用完", "免费额度已用", "额度不足", "额度已耗尽", "额度用尽", "今日额度",
    "明天再来找我", "明天再来",
)
_QUOTA_HAVE_MARKERS = (
    "将消耗每日免费额度", "将消耗今日", "预计等待", "正在为你生成", "已开始生成",
    "视频生成中", "排队中",
)
_GEN_FAIL_WORDS = ("生成失败", "出片失败", "创作失败", "没生成出来")
_COOLDOWN_WORDS = ("冷却", "请稍后再试", "频繁", "过于频繁")


def _load_state():
    try:
        return json.loads(_STATE_FILE.read_text("utf-8"))
    except Exception:
        return {"accounts": {}, "last_used": None}


def _save_state(st):
    try:
        _STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), "utf-8")
    except Exception:
        pass


def _progress(cb, text, pct=None):
    if cb:
        try:
            cb(text, pct) if pct is not None else cb(text)
        except Exception:
            pass


def _log(*a):
    M._log(*a)


def _quota_blocked(st, acc):
    a = st["accounts"].get(acc)
    return bool(a and a.get("quota_until") == str(date.today()))


def _cooling(st, acc):
    a = st["accounts"].get(acc)
    return bool(a and a.get("cooldown_until") and a["cooldown_until"] > datetime.now().isoformat())


def _mark_quota(st, acc):
    st["accounts"].setdefault(acc, {})["quota_until"] = str(date.today())
    _save_state(st)


# 前置额度体检：读取页面上「最近一条豆包消息」的定性。免费账号真正没额度时，豆包
# 会在对话里回「今日视频生成免费次数用完了…」，这条回复一直留在当前会话底部——
# 在切新对话、上传参考图、写提示词之前先扫一眼，命中就整号跳过，省掉一次注定失败
# 的生成（旧版只有提交后的反应式判定，白白耗掉上传+等待的几分钟）。
# 只看对话尾部（tail）并按「额度用尽/受理中」谁最后出现定性，避免把历史里更早的
# 「用完」误判成当前状态。签名额度接口(a_bogus/msToken 一次性)不可重放、页面也不
# 空闲轮询，DOM 文本是当前唯一稳定的前置信号。
_QUOTA_STATE_JS = ("""
(function(){
  var t = (document.body && document.body.innerText) || '';
  var tail = t.slice(-2500);
  var EX = %s;
  var HAVE = %s;
  var lastEx = -1, exSnip = '';
  for (var i = 0; i < EX.length; i++) {
    var k = tail.lastIndexOf(EX[i]);
    if (k > lastEx) { lastEx = k; exSnip = tail.slice(Math.max(0, k - 24), k + 46); }
  }
  var lastHave = -1;
  for (var j = 0; j < HAVE.length; j++) {
    var h = tail.lastIndexOf(HAVE[j]);
    if (h > lastHave) lastHave = h;
  }
  var state = 'unknown';
  if (lastEx >= 0 && lastEx > lastHave) state = 'exhausted';
  else if (lastHave >= 0 && lastHave > lastEx) state = 'ok';
  return JSON.stringify({state: state, ex: lastEx, have: lastHave, snip: exSnip});
})()
""" % (json.dumps(list(_QUOTA_EXHAUST_MARKERS), ensure_ascii=False),
       json.dumps(list(_QUOTA_HAVE_MARKERS), ensure_ascii=False)))


def _quota_state(page):
    """返回 ('exhausted'|'ok'|'unknown', 命中文案片段)。"""
    try:
        raw = page.evaluate(_QUOTA_STATE_JS)
        d = json.loads(raw) if isinstance(raw, str) else (raw or {})
        return str(d.get("state") or "unknown"), str(d.get("snip") or "")
    except Exception:  # noqa: BLE001
        return "unknown", ""


# 账号稳定标识：豆包把账号级 user id 存进 localStorage.flow_tea_user_id，跨管理器
# 重启/重建 webview 都不变（2026-09-19 实测 4 个号各有唯一值）。额度/冷却/轮询状态
# 必须挂在这个 id 上——挂在 webview 目标 id 上时，重启后目标 id 变了，「今日无额度」
# 标记成孤儿，同一个没额度的号又被当新号轮询到（就是「明知陈智没额度还往它发」）。
_ACCT_KEY_JS = r"""(() => {
  var o = {uid: '', name: ''};
  try {
    var u = localStorage.getItem('flow_tea_user_id');
    if (u && u.length > 3) o.uid = 'u_' + u;
    else {
      var s = localStorage.getItem('for_ban_appeal_sec_user_id');
      if (s && s.length > 3) o.uid = 's_' + s.slice(-24);
    }
    var nk = ['flow_user_nickname', 'nickname', 'nick_name', 'user_nickname', 'user_name'];
    for (var i = 0; i < nk.length; i++) {
      var v = localStorage.getItem(nk[i]);
      if (v) { try { v = JSON.parse(v); } catch (e) {} if (typeof v === 'string' && v && v.length <= 24) { o.name = v; break; } }
    }
  } catch (e) {}
  return JSON.stringify(o);
})()"""


def _resolve_account_identity(target):
    """取该 webview 所属账号的稳定标识（+ 尽力拿昵称）；读不到 uid 才回退目标 id。"""
    wsu = target.get("webSocketDebuggerUrl") or ""
    tid = str(target.get("id") or "")
    if not wsu:
        return {"key": (tid or ("tgt_" + str(len(tid)))), "name": ""}
    uid, name = "", ""
    try:
        p = webview_page.DouyinWebviewPage(wsu)
        try:
            raw = p.evaluate(_ACCT_KEY_JS)
            d = json.loads(raw) if isinstance(raw, str) else (raw or {})
            uid = str(d.get("uid") or "").strip()
            name = str(d.get("name") or "").strip()
        finally:
            try:
                p.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        _log("读取账号稳定标识失败（回退 target id）: %s" % str(e)[:80])
    return {"key": (uid or tid or ("tgt_" + wsu[-12:])), "name": name}


# ---- 手动账号控制（更好控制）----
# 用户可在插件设置里用「昵称或稳定 id」直接停用/恢复某个豆包号，不用等自动判定、
# 也不用等明天额度重置：
#   manager_disable_accounts：列在此的号本轮及以后一律跳过（自动记 status.disabled=True）
#   manager_recover_accounts：本轮开始时清掉这些号的「今日额度/冷却/失败/停用」标记，
#                             重新纳入轮询。想恢复一个被自动判没额度的号就用它。
# 匹配同时认稳定 id（u_xxx / 去掉前缀的数字）、昵称、以及昵称里的子串，容错用户手输。
def _acct_tokens(raw):
    return {t.strip().lower() for t in re.split(r"[,，、;；\s\n]+", str(raw or "")) if t.strip()}


def _acct_matches(a, tokens):
    if not tokens:
        return False
    key = str(a.get("key") or "").strip().lower()
    name = str(a.get("name") or "").strip().lower()
    cands = {key}
    if key.startswith("u_") or key.startswith("s_"):
        cands.add(key[2:])
    if name:
        cands.add(name)
    for t in tokens:
        if t in cands:
            return True
        # 昵称子串匹配（用户常只填「陈智」两三个字）
        if name and len(t) >= 2 and t in name:
            return True
    return False


def _disabled(st, acc):
    return bool(st["accounts"].get(acc, {}).get("disabled"))


def _apply_manual_controls(st, acct, plugin_params):
    """按设置里的停用/恢复名单更新账号状态；返回是否有变更（需要落盘时由调用方保存）。"""
    dis = _acct_tokens(plugin_params.get("manager_disable_accounts"))
    rec = _acct_tokens(plugin_params.get("manager_recover_accounts"))
    if not dis and not rec:
        return False
    changed = False
    for a in acct:
        key = a["key"]
        ent = st["accounts"].setdefault(key, {})
        if a.get("name"):
            ent["name"] = a["name"]
        if _acct_matches(a, rec):
            if ent.get("disabled") or ent.get("quota_until") or ent.get("cooldown_until") or ent.get("fail"):
                changed = True
            ent["disabled"] = False
            ent["quota_until"] = ""
            ent["cooldown_until"] = ""
            ent["fail"] = 0
            ent["last_error"] = ""
            M._log("账号 %s（%s）已恢复：清掉额度/冷却/失败/停用标记" % (key[:14], a.get("name") or "—"))
        if _acct_matches(a, dis):
            if not ent.get("disabled"):
                changed = True
            ent["disabled"] = True
            M._log("账号 %s（%s）已按设置手动停用：本轮及以后跳过" % (key[:14], a.get("name") or "—"))
    return changed


def _migrate_legacy_target_keys(st, acct):
    """把 0.7.9 之前挂在「易变 webview 目标 id」上的状态迁到账号稳定 id 上。

    老状态的键是 32 位十六进制目标 id；管理器和插件都换过键法后，「今日无额度」
    标记会留在死掉的目标 id 上、稳定键读不到 → 明知没额度的号又被当新号每次都先跑
    （用户体感「默认还是第一个打开陈志，完全没改变」）。当前 webview 目标 id 仍能和
    某个旧条目对上的，就把额度/停用/冷却等「更狠」的一侧并进稳定键并删旧条目。"""
    changed = False
    keymap = {}  # 旧目标 id -> 稳定 key，用于同步 next_used
    for a in acct:
        tid = str(a["target"].get("id") or "")
        if not tid or tid == a["key"]:
            continue
        legacy = st["accounts"].get(tid)
        if not legacy:
            continue
        cur = st["accounts"].setdefault(a["key"], {})
        if legacy.get("quota_until") and not cur.get("quota_until"):
            cur["quota_until"] = legacy["quota_until"]
            changed = True
        if legacy.get("disabled"):
            cur["disabled"] = True
            changed = True
        lc = legacy.get("cooldown_until") or ""
        if lc and lc > (cur.get("cooldown_until") or ""):
            cur["cooldown_until"] = lc
            changed = True
        if legacy.get("name") and not cur.get("name"):
            cur["name"] = legacy["name"]
            changed = True
        del st["accounts"][tid]
        keymap[tid] = a["key"]
        changed = True
        M._log("迁移旧账号状态: 目标id %s -> 稳定id %s（额度=%s）"
               % (tid[:8], a["key"][:14], legacy.get("quota_until") or "—"))
    nu = st.get("next_used")
    if nu in keymap:
        st["next_used"] = keymap[nu]
        changed = True
    return changed


def _mark_fail(st, acc, msg):
    a = st["accounts"].setdefault(acc, {})
    n = int(a.get("fail", 0)) + 1
    a["fail"] = n
    a["last_error"] = str(msg)[:200]
    if n >= 3:
        a["cooldown_until"] = datetime.fromtimestamp(time.time() + 15 * 60).isoformat()
        a["fail"] = 0
    _save_state(st)


def _mark_ok(st, acc):
    st["accounts"].setdefault(acc, {}).update(fail=0, cooldown_until="", quota_until="")
    st["next_used"] = acc
    _save_state(st)


def _mark_attempted(st, acc):
    """失败也推进轮询指针：next_used 只在成功时推进会让我们每次都从
    「上一个成功账号」之后开始——若前面的账号持续失败（输入区未就绪等），
    每一轮 generate 都先死磕同一个号，看起来就是「账号不轮询」。
    失败后同样把指针移到该账号，下一轮自动从下一个账号开跑。"""
    st["next_used"] = acc
    _save_state(st)


_MODEL_ALIAS = {
    "seedance2.0fast": ["Seedance 2.0 Fast"],
    "seedance2.0mini": ["Seedance 2.0 Mini"],
    "seedance2.5": ["Seedance 2.5", "Dreamina Seedance 2.5"],
}


def _apply_page_options(page, plugin_params, prompt=""):
    """把 UI 选的模型/时长/比例应用到页面（时长+比例走「自动 · 10s」设置面板）。

    返回各项是否真正生效 {"model","duration","ratio"}——manager webview 里设置
    面板经常打不开（2026-09-17 实测两个账号都打不开），没生效的项由调用方
    注入提示词兜底，否则用户显式选的时长会完全丢失。
    模型没选上时（下拉偶发点不开）：激活窗口后单独重试一次并校验触发器文本。
    """
    res = {"model": True, "duration": True, "ratio": True}
    try:
        model = (plugin_params.get("model") or "seedance").strip()
        duration = str(plugin_params.get("duration") or "10").strip()
        ratio = str(plugin_params.get("ratio") or plugin_params.get("aspect_ratio") or "16:9").strip()
        applied = M._apply_options(page, duration, ratio, model, prompt=prompt)
        if isinstance(applied, dict):
            res.update(applied)
        if not res.get("model") and model and model.lower() not in ("auto", "seedance"):
            try:
                page.activate()
            except Exception:  # noqa: BLE001
                pass
            page.wait_for_timeout(1200)
            if M._select_dropdown(page, "模型", model, _MODEL_ALIAS.get(model, [])):
                cur = M._current_dropdown_text(page, "模型") or ""
                key = M._normalize_key(cur)
                want = M._normalize_key(model)
                if not want or want in key or any(M._normalize_key(a) in key
                                                  for a in _MODEL_ALIAS.get(model, [])):
                    res["model"] = True
                    _log("模型重试后已生效: %s" % (cur.replace("\n", " ")[:40]))
            if not res.get("model"):
                _log("警示: 模型未能切到 %s（页面停留在其它模型），本次成片可能不是所选模型"
                     "——建议前台点开该账号窗口手动确认模型下拉" % model)
        _log("已应用选项 model=%s duration=%s ratio=%s 生效=%s" % (model, duration, ratio, res))
    except Exception as e:
        _log("应用模型/时长/比例选修时跳过: %s" % e)
        res = {"model": False, "duration": False, "ratio": False}
    return res


_LAST_BASELINE = {"urls": set(), "nodes": []}
# 本次等待窗的受理状态：_wait_new_page 写入，账号循环的异常处理读取。
# 受理后（额度已耗、豆包正在生成）绝不换号重发——那等于同一段分镜重复出片。
_LAST_WAIT = {"accepted": False, "chat_url": ""}

# 「真正的受理信号」：豆包**实际开跑**时才会出现的话术（预计等待 X 分钟 / 生成中 N% / 排队中）。
# 助手的客套话「视频生成中，请稍候。」（正文散文，实际没开跑）刻意不匹配——
# 2026-09-19 实测：豆包回复「已确认全部设定，开始生成…」后并没有真的生成，
# 等 620s 全部超时；只有用户手动发「视频呢」它才开跑。所以受理检测必须收紧，
# 长时间见不到受理信号就重发完整提示词，绝不能被客套话骗到超时。
_ACCEPT_RE = re.compile(r"预计\s*(?:等待|还需)|生成中\s*\(?\d{1,3}\s*%|排队中|创作任务已提交")
# 助手要确认的问法（含旧版三条 + 常见变体）
_CONFIRM_ASK_RE = re.compile(r"参数确认|确认后我再开始|确认后我开始|是否确认|请确认|回复\s*[「'\"」]?\s*确认")


def _wait_new_page(page, prompt, wait_s, cb, plugin_params=None):
    """发送→自动确认→轮询出片。返回 <video> src / 官方接口原片 URL，或 None。

    防串片与 browser 通道同款：提交前先记两层基线（URL/封面 + 创作节点 ID），
    只认基线之外的新增视频；再叠一道最短等待窗——视频要几十秒才出片，窗口内
    出现的视频地址只可能是页面上历史卡片的残留。基线存到 _LAST_BASELINE，
    供出片后解析原片时排除旧节点。
    确认话术读插件设置 auto_confirm_text（旧版写死「确认」，用户自定义的
    话术从来不生效）。
    2026-09-19 三步恢复链（对齐 browser 通道的无人值守能力）：
      1) 见不到「真实受理信号」超过 accept_wait 秒 → 重发完整提示词（豆包经常
         确认完参数却不真正开跑，客套话「视频生成中」骗不了 _ACCEPT_RE）；
      2) 页面提示已出片但 DOM 没有 <video>（卡片懒挂载）→ 官方接口探测原片；
      3) 探测不到 → 真实鼠标点一下成片卡片触发播放，<video> 挂载后下轮命中。
    """
    global _LAST_BASELINE, _LAST_WAIT
    _LAST_WAIT = {"accepted": False, "chat_url": str(getattr(page, "url", "") or "")}
    confirm_text = str(((plugin_params or {}).get("auto_confirm_text")) or "确认").strip() or "确认"
    retry_limit = int(((plugin_params or {}).get("auto_retry_times")) or 2)
    try:
        accept_wait = float(((plugin_params or {}).get("manager_accept_wait_seconds")) or 150)
    except Exception:  # noqa: BLE001
        accept_wait = 150.0
    try:
        baseline = M._baseline_video_srcs(page)
        baseline_nodes = M._baseline_node_ids(page)
    except Exception as e:  # noqa: BLE001
        _log("采集基线失败（忽略，继续走最短等待窗）: %s" % str(e)[:100])
        baseline, baseline_nodes = set(), []
    _LAST_BASELINE = {"urls": set(baseline or set()), "nodes": list(baseline_nodes or [])}
    min_elapsed = 20.0
    try:
        _mv = (M.get_params() or {}).get("min_video_wait_seconds")
        min_elapsed = 20.0 if _mv in (None, "") else max(0.0, float(_mv))
    except Exception:  # noqa: BLE001
        pass
    _fill_prompt_with_check(page, prompt)
    # 受理前不能报「生成中」——豆包经常确认完参数却不开跑（幽灵提交），
    # 上层界面长时间挂在「正在生成」全靠这里说真话
    _progress(cb, "等待豆包受理", 5)
    t0 = time.time()
    # 记录本次真实提交时刻：出片后解析原片要用它当 min_ts（而不是「出片那一刻 - 30s」，
    # 后者会把生成耗时算错，让上一段分镜的节点落进时间窗）。
    _LAST_BASELINE["submitted_at"] = t0
    confirm_sends = 0
    retries = 0
    accepted = False      # 见到「真实受理信号/出片信号」= 豆包真的开跑了
    warned_ask = False    # 确认到底的告警只打一次
    # 本轮是否真的读到了页面正文。CDP 超时/冻结时读不到，此时**绝不能**据此判定
    # 「豆包未受理」并重发提示词——重发=重复提交=重复扣额度+重复出片
    # （2026-09-21 实测：一段分镜被提交 3 次，豆包里出了 2 个视频）。
    last_read_ok = False
    warned_noread = False  # 「读不到页面故跳过重发」只提示一次
    warned_delivered = False  # 「提示词指纹已在会话里（已送达）故不再重发」只提示一次
    last_progress = 0.0
    # 上次见到「真实进度」的时刻：受理信号/出片信号/新视频/发送确认/重发提示词都算。
    # 超过 accept_wait 秒见不到 → 重发完整提示词（上限 auto_retry_times）。
    last_activity = t0
    last_probe = 0.0
    last_trigger = 0.0
    last_keepawake = 0.0   # 上次给 webview 解冻的时刻（等片期间每 15s 一次）
    last_activate = 0.0    # 上次 bringToFront 的时刻（失败恢复用，30s 冷却防焦点风暴）
    cdp_fail = 0          # 连续页面通信失败计数：瞬时 10060/超时（重连后自愈）不杀任务，
                          # 连续多轮才判定页面真死了（2026-09-19 实测一次瞬时超时直接
                          # 把「已出片待抓取」的账号判成失败）
    total_cdp_fail = 0    # 全程通信失败累计：窗口耗尽时用于区分「真没出片」和
                          # 「出片了但 CDP 通道挂了看不到」（2026-09-19 实测后者被误报
                          # 成「超时未出片」，管理器窗口里明明已有成片）
    # 无人值守自动接管状态：manager 后端此前漏传这套状态，导致 _autopilot_once 从未被
    # 调用——豆包「参数确认」是模态按钮弹窗，只靠 body.innerText 正则往输入框打字「确认」
    # 根本 dismiss 不掉，循环空转到 CDP 断连（"目标不可达"）。这里对齐 browser 通道。
    ap_state = {"confirm": 0, "retry": 0, "last_detail": "", "last_ts": 0.0,
                "captcha_since": 0.0, "done_since": 0.0, "last_probe": 0.0,
                "idle_since": 0.0, "unknown_seen": [], "confirmed_asks": [],
                "last_play_at": 0.0, "play_triggers": 0}
    max_confirm = int(((plugin_params or {}).get("auto_confirm_times")) or 2)
    while time.time() - t0 < wait_s:
        now = time.time()
        elapsed = now - t0
        try:
            _LAST_WAIT["chat_url"] = str(getattr(page, "url", "")
                                         or _LAST_WAIT.get("chat_url") or "")
            # 取「垂直位置最靠下的新增 video」= 聊天流里最新卡片的成片。
            # 不能像旧版那样遍历取最后一个：页面可能同时挂着上一段分镜的 <video>
            # （历史卡片懒渲染补挂时会排在 DOM 末尾），取最后一个就会把旧片当成本次成片。
            vs = page.eval_on_selector_all(
                "video",
                "els => els.map(v => ({src: v.currentSrc || v.src || '',"
                " y: Math.round(v.getBoundingClientRect().top)}))"
                ".filter(o => o.src && o.src.indexOf('blob:') !== 0)")
            fresh = None
            best_y = None
            for o in (vs or []):
                v = str((o or {}).get("src") or "")
                if not v or v.split("?")[0] in baseline:
                    continue
                y = (o or {}).get("y")
                y = float(y) if isinstance(y, (int, float)) else float("-inf")
                if fresh is None or y >= best_y:
                    fresh, best_y = v, y
            if fresh and elapsed >= min_elapsed:
                return fresh
            # 等片期间周期性保活：后台 webview 被 Chromium 冻结后 JS 停摆，取原片的
            # awaitPromise 型 evaluate 会挂满 60s 才超时。这里每 15s 解冻一次做预防
            # （只解冻不置前，不抢管理器显示焦点），别等超时了再救火。
            if now - last_keepawake >= 15.0:
                last_keepawake = now
                try:
                    page.keep_awake()
                except Exception:  # noqa: BLE001
                    pass
            # 页面导航/重建的瞬间 document.body 会是 null，裸读 innerText 直接抛
            # TypeError（2026-09-21 实测：被误判成「账号页面通信失效」，整轮失败）。
            body = page.evaluate("((document.body && document.body.innerText)||'')") or ""
            last_read_ok = True   # 本轮确实读到页面正文，才允许据此判定「未受理」
            # 会话内额度检测（2026-09-19 修正「对着无额度账号反复点确认、不换号」）：
            # 切新对话后本次提交若被豆包回「今日…次数用完了」，这不是幽灵提交，
            # 继续点确认/重发只会对一条拒绝反复确认、空耗整个等待窗还不轮询。
            # 一旦未受理且命中「拦住本次生成」的额度文案（复用 browser 通道久经考验的
            # _QUOTA_BLOCK_RE，它已排除「将消耗/使用积分/会员额度」等常驻营销词），
            # 立即抛 QuotaExhausted 让上层秒换号。
            if not accepted and M._QUOTA_BLOCK_RE.search(body):
                _log("豆包回复今日额度已用尽（会话内检测），停止确认并换号")
                raise M.QuotaExhausted("豆包回复：今日视频生成免费次数已用完")
            if M._DONE_RE.search(body):
                # 页面说已出片但 DOM 没有 <video>（卡片懒挂载，manager 通道同病）：
                # 先问官方接口要原片，再真实点击成片卡片触发播放。
                accepted = True
                _LAST_WAIT["accepted"] = True
                last_activity = now
                if now - last_probe >= 15:
                    last_probe = now
                    try:
                        # no_wm_only：探测只认真·无水印结果。带水印卡片链不是「本次出片」
                        # 的证据（可能就是上一段残留），返回它会让下面直接落水印片。
                        # attempts 1→3：出片探测是纯读请求（无副作用、不重复扣额度），
                        # 冻结刚被 keep_awake 解开时首探常落空，多试两次才拿得到。
                        u = M._resolve_original_video_url(
                            page, attempts=3, delay_s=1.0, min_ts=int(t0),
                            scope="recent", baseline_urls=sorted(baseline),
                            skip_node_ids=list(baseline_nodes),
                            conv_id=M._conv_id_from_url(getattr(page, "url", "") or ""),
                            no_wm_only=True)
                    except Exception as e:  # noqa: BLE001
                        u = None
                        _log("出片探测异常（忽略）: %s" % str(e)[:100])
                    if u:
                        _log("页面已提示出片，官方接口直接取回无水印原片")
                        return u
                if now - last_trigger >= 15:
                    last_trigger = now
                    try:
                        M._trigger_video_play(page, baseline)
                        time.sleep(2.0)
                    except Exception as e:  # noqa: BLE001
                        _log("触发播放异常（忽略）: %s" % str(e)[:100])
            elif _ACCEPT_RE.search(body):
                if not accepted:
                    accepted = True
                    _LAST_WAIT["accepted"] = True
                    _progress(cb, "豆包已受理，生成中", 10)
                    _log("豆包已真正受理（出现预计等待/进度信号）")
                last_activity = now
            # === 无人值守：先让自动接管处理模态弹窗（点「确认」按钮）===
            # manager 后端此前漏调 _autopilot_once：只靠 body.innerText 正则往输入框打字
            # 「确认」，而豆包的参数是模态按钮弹窗——打字 dismiss 不掉，循环空转直到
            # CDP 断连（"目标不可达"）。这里对齐 browser 通道（main.py 同款调用）。
            _ap = None
            try:
                _ap = M._autopilot_once(page, plugin_params, ap_state,
                                        cb, t0, baseline, retry_prompt=prompt)
            except (M.NeedLogin, M.NeedHuman, M.QuotaExhausted):
                raise
            except Exception as e:  # noqa: BLE001
                _log("自动接管扫描异常(忽略): %s" % str(e)[:100])

            _ap_confirm = ap_state.get("confirm", 0)
            if _ap in ("confirm", "confirm_sent", "paid_continue", "close", "danger_dismiss"):
                last_activity = now  # 接管动作算活动，避免刚点完就重发整段
            # 确认到底（autopilot 已发满次数 / 本轮额度用完）豆包仍在要确认：告警一次
            _total_confirms = _ap_confirm + confirm_sends
            if _total_confirms >= max_confirm and not warned_ask:
                warned_ask = True
                _log("警示: 已确认 %d 次豆包仍在要求确认——若反复出现，请在管理器里"
                     "前台点开该账号窗口检查（可能卡在付费/验证弹窗）" % _total_confirms)

            # 文字追问兜底：autopilot 未对此 ask 发过确认时，才按原逻辑补发确认话术。
            # 与 autopilot 的 need_confirm 打字互斥，避免重复发送（重复发=重复扣额度/
            # 重复触发一次生成）。仅当 autopilot 既没点按钮也没打字回复时才走这里。
            if _ap not in ("confirm", "confirm_sent", "paid_continue", "need_confirm") \
                    and _ap_confirm == 0:
                ask = _CONFIRM_ASK_RE.search(body)
                if ask and confirm_sends <= retries:
                    M._send_followup(page, confirm_text)
                    confirm_sends += 1
                    last_activity = now
                    warned_ask = False

            # 一直没见到真实受理信号（豆包确认完参数却不开跑）：重发完整提示词。
            # 绝不发裸「生成视频」——那会生成一段无关视频串进创作空间。
            # 【2026-09-19 重复出片修复】必须带 not accepted 前提：受理后豆包已在生成
            # （额度已耗），此时页面只是 CDP 超时读不到正文，last_activity 停更被误判
            # 「未受理」，重发=同一段分镜再生成一条。
            if now - last_activity >= accept_wait and not accepted and last_read_ok:
                # 【2026-09-21 重复提交第二道防线（用户实测 0.7.16 仍重复扣额度）】
                # manager 通道每账号每任务都切全新对话，本轮会话流里只可能有本次
                # 发送的内容——只要 body 里已出现本条提示词的指纹（前 40 字），
                # 就证明这条内容早已成功送达豆包。此刻「没看到受理信号」多半只是
                # 确认弹窗/生成启动还在路上；再重发一遍 = 豆包把同一段分镜生成两条，
                # 每天免费额度白白翻倍消耗。已送达 → 绝不重发，重置计时继续等。
                fingerprint = (prompt or "").strip()[:40]
                if fingerprint and fingerprint in body:
                    retries = retry_limit   # 已送达再重试无意义，用尽重发额度
                    if not warned_delivered:
                        warned_delivered = True
                        _log("警示: 会话里已存在本次提示词（内容已送达豆包），"
                             "不再重发——未受理只是确认/生成还在路上，继续等待")
                    last_activity = now
                elif retries < retry_limit:
                    retries += 1
                    M._send_followup(page, prompt)
                    last_activity = now
                    _log("警示: %d 秒未见受理信号，已重发完整提示词（第 %d/%d 次）"
                     % (int(accept_wait), retries, retry_limit))
                else:
                    # 重试用尽且豆包始终没受理：提前判失败，绝不空等满 620 秒——
                    # 这就是「界面显示生成中、实际啥也没发生」的幽灵提交体验
                    raise RuntimeError(
                        "豆包确认后未真正开跑（已自动重发 %d 次提示词仍未受理）"
                        % retry_limit)
            elif now - last_activity >= accept_wait and not accepted and not last_read_ok:
                # 到点了该重发，但这几轮根本读不到页面（CDP 超时/webview 冻结）。
                # 读不到 ≠ 没受理——豆包很可能已经在生成了。此时重发就是重复提交。
                if not warned_noread:
                    warned_noread = True
                    _log("警示: 已到重发时限但页面读不到（CDP 超时/冻结），已跳过重发——"
                         "读不到页面时重发=重复提交=重复扣额度+重复出片")
            if cb and not accepted and now - last_progress >= 20:
                last_progress = now
                _progress(cb, "等待豆包受理（已重发 %d/%d 次）" % (retries, retry_limit), 5)
            cdp_fail = 0
        except RuntimeError:
            raise
        except M.QuotaExhausted:
            raise
        except (M.NeedLogin, M.NeedHuman):
            # 自动接管（_autopilot_once）抛出的登录失效/人机验证：必须透传到上层
            # 立即换号或提示人工，绝不能落到下面的「except Exception」被当成瞬时 CDP
            # 超时空转（曾空转数十分钟）。
            raise
        except Exception as e:  # noqa: BLE001
            cdp_fail += 1
            total_cdp_fail += 1
            # 本轮没读到页面：不能作为「豆包未受理」的证据，禁止触发重发。
            last_read_ok = False
            msg = str(e)[:120]
            if cdp_fail >= 4:
                _log("页面通信连续 %d 轮失败，判定账号页面通信失效: %s" % (cdp_fail, msg))
                raise RuntimeError(
                    "页面通信连续超时/断连（CDP 无响应）——豆包可能已在后台出片，"
                    "请在管理器里前台点开该账号窗口确认；请在豆包管理器里重新点开该账号"
                    "（必要时重启管理器）后重试或直接一键补抓原片: %s" % msg)
            # 通信失败先解冻、少置前：cdp_fail>=1 只发 keep_awake（解冻，不抢焦点）；
            # activate（bringToFront）是跨进程焦点切换，30s 内最多一次——4 个账号
            # 同时失败时每次都置前会形成焦点风暴，实测把管理器整个压崩（2026-09-21）。
            if cdp_fail >= 1:
                try:
                    page.keep_awake()
                except Exception:  # noqa: BLE001
                    pass
                if cdp_fail >= 2 and now - last_activate >= 30.0:
                    last_activate = now
                    try:
                        page.activate()
                    except Exception:  # noqa: BLE001
                        pass
            if cdp_fail <= 2 or cdp_fail % 4 == 0:
                _log("页面通信异常（第 %d 轮，忽略重试）: %s" % (cdp_fail, msg))
        time.sleep(_POLL)
    if not accepted:
        _log("警示: 等待窗口耗尽，豆包始终未受理本次提交")
    if total_cdp_fail >= 3:
        # 窗口里多次通信超时：不能下「没出片」的结论，多半是通道挂了而片子早已生成
        raise RuntimeError(
            "等待期间页面通信超时 %d 次，无法确认出片状态（豆包可能已出片——请到管理器"
            "窗口确认后一键补抓原片，勿直接重生成）" % total_cdp_fail)
    return None


def _current_editor_text(page):
    """读当前输入框实际内容。必须与写入目标一致（同一个编辑器），
    否则会读到别的输入框、误判「没写入」而重发一遍。

    2026-09-17：改走 shim 的 `_editor_text()` —— 它会挑「没被 pointer-events
    禁用」的那个 contenteditable；旧实现一律取 `querySelector` 的第一个，
    在页面上有多个 contenteditable 时会读错元素、把「已写入」判成「没写入」。
    """
    try:
        if hasattr(page, "_editor_text"):
            return page._editor_text()
    except Exception:  # noqa: BLE001
        pass
    return page.evaluate("""(() => {
      let e = document.querySelector('[contenteditable="true"]');
      if (!e) e = document.querySelector('textarea');
      return e ? (e.value || e.innerText || e.textContent || '') : '';
    })()""")


def _norm_cmp(s):
    return re.sub(r"\s+", "", str(s or ""))


def _fill_prompt_with_check(page, prompt):
    """填提示词 → 发送前校验编辑器内容 → 发送（Enter 优先）。

    校验用宽松匹配：豆包输入框有字数上限，可能截断提示词尾部
    （编辑器内容是提示词前缀），这不算失败；只有编辑器为空/
    内容对不上才算失败，坚决不发送，避免浪费额度。

    2026-09-17：底层 `_fill` 已改为「写-读回校验-重试-降级」，这里再做 3 轮
    外层重试（每轮间隔递增）——纯竞态丢字基本在第 2 轮就自愈；而「输入区被
    pointer-events 禁用」这类硬错误会立刻抛指定原因，不再被重试掩盖。
    """
    np_ = _norm_cmp(prompt)
    got = ""
    last_err = None
    for attempt in range(3):
        try:
            M._fill_prompt(page, prompt)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            last_err = msg
            if "输入区不可用" in msg:
                # 硬错误：页面/输入区状态问题，重试无意义，立刻带着原因退出
                M._log("提示词写入失败(硬错误): %s" % msg[:200])
                raise RuntimeError("提示词未能写入豆包输入框（%s）" % msg[:160])
            M._log("第 %d 次填词异常: %s" % (attempt + 1, msg[:150]))
            page.wait_for_timeout(500)
            continue
        page.wait_for_timeout(400)
        got = (_current_editor_text(page) or "").strip()
        ng = _norm_cmp(got)
        if np_ in ng or (ng and ng in np_):
            break
        M._log("警示: 编辑器未含提示词(第 %d 次，len %d vs %d, 内容=%r)"
               % (attempt + 1, len(got), len(prompt), got[:60]))
        # 读一次输入区体检，把真实状态留在日志里（下次失败不用再猜）
        try:
            if hasattr(page, "input_health"):
                M._log("输入区体检: %s" % str(page.input_health())[:300])
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(400 + 500 * attempt)
    ng = _norm_cmp(got)
    if not (np_ in ng or (ng and ng in np_)):
        # 坚决不发送：只有图片没有提示词的任务照样消耗当日额度，
        # 生成的还是废片。直接判该账号失败，换下一账号。
        M._log("提示词 3 次均未写入输入框(内容=%r)，放弃发送以免浪费额度" % got[:60])
        raise RuntimeError("提示词未能写入豆包输入框（已拦截发送，未消耗额度）%s"
                           % (("；末次异常：" + str(last_err)[:120]) if last_err else ""))
    if len(ng) < len(np_) - 2:
        M._log("警示: 豆包输入框疑似截断提示词(%d/%d 字)，豆包只按已写入部分理解" % (len(ng), len(np_)))
    M._log("编辑器已含提示词(校验通过): '%s'" % got[:80])
    # 发送：优先真实 Enter（历史经验比点按钮可靠，草稿未注册时点按钮不触发），
    # 点按钮作为兑底（shim 未匹配到按钮时 _press_send 返回 False）。
    sent = False
    try:
        sent = bool(M._press_send(page))
        if sent:
            M._log("已点发送按钮")
    except Exception as e:
        M._log("点发送按钮失败: %s" % str(e)[:120])
    if not sent:
        try:
            page.keyboard.press("Enter")
            sent = True
            M._log("已改用 Enter 发送")
        except Exception as e:
            M._log("Enter 发送也失败: %s" % str(e)[:120])
    return sent


def _download(url, output_dir, filename, retries=3):
    """下载成片：复用主插件的加固下载链路。

    这里以前是自己写的裸 requests 流式下载 —— 没有字节数比对、没有 MP4 结构校验、
    也不写 .part，CDN 提前断流时就会把半截文件当成品返回（2026-09-17 的 `0006` 正是
    这样丢了尾部 moov，播放到末尾直接崩）。统一走 `main._download`：Content-Length
    比对 + box 链完整性校验 + 断点续传 + 原子落盘。manager 通道走 CDP 驱动、不持有
    cookie，传空即可。
    """
    os.makedirs(output_dir, exist_ok=True)
    return M._download(url, output_dir, {}, filename, True, retries)


def _input_ready(page, timeout_s=25.0, activate_after=8.0):
    """等输入区变成「真正可交互」。返回 (ok, health)。

    2026-09-17 实测根因：点「新对话」/进视频模式/上传参考图后，豆包会把 tiptap 编辑器
    短暂置为 `visibility:hidden` + 继承 `pointer-events:none`（后台 webview 未 hydrate
    时更久）。该状态下 `focus()` 不生效、`Input.insertText` 无处可落、
    `execCommand('insertText')` 直接返回 false —— 而 `querySelector` 照样能拿到元素，
    所以旧实现把它当可用账号，白跑完进模式/传参考图，最后统一报「提示词未能写入」。
    这里提前体检 + 唤醒 + 等就绪，并给出准确原因。
    """
    try:
        return page.input_ready(timeout_s=timeout_s, activate_after=activate_after,
                                log=lambda m: M._log("账号输入区: %s" % m))
    except Exception as e:  # noqa: BLE001
        return False, {"found": False, "blockedBy": "input_ready 异常: %s" % str(e)[:120]}


def _save_fail_shot(page, key):
    """失败落截图。manager 通道以前没有 screenshot 方法 → 一张调试图都留不下来。"""
    try:
        d = _PLUGIN_DIR / "shots"
        d.mkdir(exist_ok=True)
        p = d / ("fail_%s_%s.png" % (key[:10], datetime.now().strftime("%Y%m%d_%H%M%S")))
        if page.screenshot(str(p)):
            M._log("已存失败截图: %s" % p)
    except Exception as ex:  # noqa: BLE001
        M._log("失败截图未生成(忽略): %s" % str(ex)[:80])


def _stage_and_advice(msg):
    """把失败原因归到「阶段 + 建议操作」，用户侧报错不再是一串难读的拼接文本。
    注意判断顺序：最具体的优先（如「未真正开跑」的文案里也含「提示词」）。"""
    m = str(msg or "")
    if "未真正开跑" in m or "受理" in m:
        return "豆包未受理", "豆包确认完参数却不开跑；若反复出现，重启豆包管理器"
    if "提示词" in m:
        return "写入提示词", "前台点开该账号窗口让页面完成渲染后重试"
    if "输入区" in m or "pointer-events" in m or "visibility" in m:
        return "输入区未就绪", "在豆包管理器里前台点开该账号窗口（必要时重新登录）"
    if "Seedance 视频模式" in m:
        return "进入视频模式", "前台点开该账号窗口，手动确认「视频生成」模式可用"
    if "未解析到无水印原片" in m:
        return "无水印解析", "创作节点未就绪：稍后用待补抓清单一键补抓原片，或调大 nowm_wait_seconds"
    if "页面通信" in m:
        return "页面通信失效", "该账号 webview 可能冻结：到管理器前台点开该窗口（必要时重启管理器）；若豆包已出片，直接一键补抓原片，勿重生成"
    if "timed out" in m or "10060" in m or "连接" in m:
        return "CDP 连接", "多为 webview 假死：重启豆包管理器 + 字字动画后重试"
    if "超时未出片" in m:
        return "出片超时", "确认该账号窗口未最小化；多次出现请重启豆包管理器"
    return "生成流程", ""


def generate(context, main_module=None):
    _ensure_main(main_module)
    cb = context.get("progress_callback")
    # 必须与默认参数合并：字字动画可能只传部分参数，漏合并会让 ratio/duration 等取不到
    plugin_params = {**M.get_params(), **(context.get("plugin_params") or {})}
    prompt = (context.get("prompt") or "").strip()
    if not prompt:
        raise Exception("PLUGIN_ERROR:::提示词为空")
    output_dir = context.get("output_dir") or context.get("project_path") or str(_PLUGIN_DIR / "downloads")
    filename = context.get("filename") or M._output_filename(context)

    lead = re.match(r"^(请\s*)?(帮我\s*)?生成视频[\s。．.．:：,，、]?", prompt, re.I)
    if lead:
        rest = prompt[lead.end():].strip()
        if rest:
            prompt = rest

    # 豆包网页端没有比例控件，比例通过提示词注入（与 browser 后端一致）。
    # 旧版漏了这一步，manager 后端选的比例参数直接丢失。
    ratio = str(plugin_params.get("ratio") or plugin_params.get("aspect_ratio") or "16:9").strip()
    hint = M._ratio_hint(ratio)
    if hint and hint not in prompt:
        prompt = "%s。%s" % (hint, prompt)
        M._log("已注入比例提示: %s" % hint)

    refs = M._collect_reference_paths(context, plugin_params) or []

    _log("manager 后端：读取管理器已物化的豆包账号 webview…")
    port = int(plugin_params.get("manager_port") or 9223)
    manager_exe = plugin_params.get("manager_exe") or ""
    # 「账号已登录」≠「webview 已存在」：管理器只给「被点开」的账号建 webview，
    # 管理器重启后 webview 会全没。这里自动点开账号卡片把它们物化出来，
    # 不再要求用户先去管理器手点一个账号。
    try:
        wvs = D.ensure_doubao_webviews(port=port, exe=manager_exe, log=lambda m: M._log(m))
    except Exception as e:  # noqa: BLE001
        raise Exception(
            f"PLUGIN_ERROR:::连接豆包管理器失败（端口 {port}）：{str(e)[:120]}。"
            "请确认豆包管理器已启动（端口可在插件设置里改）"
        )
    if not wvs:
        names = []
        try:
            names = D.ManagerBridge(port=port).account_names()
        except Exception:  # noqa: BLE001
            pass
        raise Exception(
            "PLUGIN_ERROR:::管理器里没有可用的豆包账号 webview"
            f"（已自动尝试点开账号卡片；读到的账号卡片：{names or '无'}）。"
            "请确认管理器里至少有一个账号已登录（不要停在扫码/登录页）"
        )
    M._log("可用豆包账号(webview): %d 个" % len(wvs))
    st = _load_state()
    # 账号标识用「跨管理器重启稳定」的豆包 user id（webview 目标 id 会变，导致「今日
    # 无额度」标记丢失、同一个没额度的号又被当新号轮询到）。读不到才回退目标 id。
    acct = []
    seen_keys = set()
    for t in wvs:
        ident = _resolve_account_identity(t)
        k = ident["key"]
        if k in seen_keys:
            k = "%s#%s" % (k, str(t.get("id") or "")[:6])
        seen_keys.add(k)
        acct.append({"key": k, "name": ident.get("name") or "", "target": t})
    # 把昵称回写进状态（按稳定 id 存），下次即使豆包页读不到昵称也能对上名字
    for a in acct:
        if a["name"]:
            st["accounts"].setdefault(a["key"], {})["name"] = a["name"]
        elif st["accounts"].get(a["key"], {}).get("name"):
            a["name"] = st["accounts"][a["key"]]["name"]
    # 迁移 0.7.9 之前挂在易变目标 id 上的「今日无额度/停用/冷却」到稳定 id，
    # 否则老标记读不到，没额度的号会被当新号每轮先跑。
    if _migrate_legacy_target_keys(st, acct):
        _save_state(st)
    by_key = {a["key"]: a for a in acct}
    keys = [a["key"] for a in acct]
    # 应用设置里的「手动停用 / 恢复」名单
    if _apply_manual_controls(st, acct, plugin_params):
        _save_state(st)
    M._log("豆包账号清单: %s" % " | ".join(
        "%s%s[%s]" % (a["name"] or "?", a["key"][2:12],
                      ("停用" if _disabled(st, a["key"])
                       else "额度用完" if _quota_blocked(st, a["key"])
                       else "冷却" if _cooling(st, a["key"]) else "可用"))
        for a in acct))
    # 从 last_used 的下一个开始
    order = keys[:]
    if st.get("next_used") in keys:
        i = order.index(st["next_used"])
        order = order[i + 1:] + order[:i + 1]

    last_err = None
    errs = []          # 每个账号的失败原因都留着——只报最后一个会把「真正有货的账号
                       # 超时未出片」藏在「未能进入视频模式」后面，误导排查方向
    for key in order:
        t = by_key[key]["target"]
        if _disabled(st, key):
            M._log("账号 %s（%s）已手动停用，跳过" % (key[:10], (by_key[key].get("name") or "—")))
            errs.append("账号%s[%s·手动停用]: 已从设置 manager_disable_accounts 移除才能恢复" % (key[:8], by_key[key].get("name") or "?"))
            continue
        if _quota_blocked(st, key):
            M._log("账号 %s 今日额度已用完，跳过" % key[:10])
            errs.append("账号%s[%s·额度]: 今日额度已用完（可在设置 manager_recover_accounts 填它以立即恢复）" % (key[:8], by_key[key].get("name") or "?"))
            continue
        if _cooling(st, key):
            M._log("账号 %s 冷却中，跳过" % key[:10])
            continue
        _progress(cb, "账号生成中", 0)
        page = webview_page.DouyinWebviewPage(t["webSocketDebuggerUrl"])
        try:
            # 后台 webview 不激活，输入区/模型芯片都可能停在未渲染态（pointer-events:none）
            # —— 先把窗口提到前台再体检，别干等 25 秒后才激活
            try:
                page.activate()
            except Exception:  # noqa: BLE001
                pass
            # 前置额度体检：在等输入区就绪(25s)、切新对话、上传参考图、写提示词之前，
            # 先扫当前会话尾部——免费号没额度时豆包会把「次数用完了」一直挂在会话底部。
            # 命中即整号跳过（记 quota_until=今日），省掉一次注定失败的生成。
            if plugin_params.get("manager_quota_preflight", True):
                qs, qsnip = _quota_state(page)
                if qs == "exhausted":
                    _mark_quota(st, key)
                    _mark_attempted(st, key)
                    M._log("账号 %s 前置检测到今日额度用尽（%s），跳过：未上传参考图/未发提示词"
                           % (key[:10], (qsnip or "").replace("\n", " ")[:44]))
                    errs.append("账号%s[额度·前置]: 今日生成次数已用完 → 明天再试或换号" % key[:8])
                    continue
                elif qs == "ok":
                    M._log("账号 %s 前置体检: 页面显示有额度/受理中，继续" % key[:10])
            try:
                ready_s = float(plugin_params.get("manager_input_ready_seconds") or 25)
            except Exception:  # noqa: BLE001
                ready_s = 25.0
            ok_in, health = _input_ready(page, ready_s, activate_after=3.0)
            M._log("账号 %s 输入区: ready=%s %s" % (
                key[:10], ok_in,
                ("| " + str(health.get("blockedBy"))) if health.get("blockedBy") else
                "| pe=%s vis=%s w=%s h=%s" % (health.get("pe"), health.get("vis"), health.get("w"), health.get("h"))))
            if not ok_in:
                raise RuntimeError(
                    "输入区未就绪/不可交互（%s）：该账号窗口的豆包页面没有渲染出可编辑的输入框，"
                    "请在豆包管理器里重新点开（必要时重新登录）该账号"
                    % (health.get("blockedBy") or ("pe=%s vis=%s" % (health.get("pe"), health.get("vis"))))
                )
            new_chat = ""
            try:
                new_chat = page.new_chat()
            except Exception as e:  # noqa: BLE001
                M._log("点「新对话」异常: %s" % str(e)[:120])
            if not new_chat:
                # 没切到新对话 ⇒ 页面上会留着上一段的成片卡片，是串片的前置条件。
                # 不影响流程（下面双层基线会拦住），但必须留痕便于事后定位。
                M._log("警示: 未点到「新对话」按钮，本次沿用当前会话，历史卡片可能残留")
            else:
                M._log("已切到新对话（匹配按钮: %s）" % new_chat)
            time.sleep(1.5)
            page.evaluate(D._JS_ENTER_VIDEO_MODE)
            time.sleep(1.5)
            mod = page.evaluate(D._JS_MODEL_LABEL)
            if not mod.get("seedance"):
                # 后台 webview 渲染滞后会把模型芯片折叠出 body.innerText
                # （body 级 seedance=False 但芯片其实就在输入区旁）：先激活窗口再试一次
                try:
                    page.activate()
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(1.5)
                page.evaluate(D._JS_ENTER_VIDEO_MODE)
                time.sleep(1.5)
                mod = page.evaluate(D._JS_MODEL_LABEL)
            if not mod.get("seedance") and mod.get("seedanceScope"):
                # 芯片级信号兜底：输入区工具带旁确实存在 Seedance 芯片（modelScope
                # 形如「模型 Seedance 2.0 Fast」）→ 视频模式已就绪。2026-09-19 实测
                # 有账号 body 级匹配全 False 但芯片就在那儿，被误判「未能进入视频
                # 模式」跳过——这正是「明明能出片却报错」的直接原因之一。
                #
                # 【2026-09-20 补充取证】走这条兜底 = 页面**半渲染**：body.innerText
                # 读不到 Seedance 文案，说明渲染循环没跑完。两轮日志对比：
                #   半渲染（u_35463537 / u_42045857 / u_17255932）→ 上传参考图后
                #   **必然**报「输入区不可用」；
                #   完整渲染（u_45016991）→ 两轮都正常写入提示词。
                # 半渲染就是那个分水岭，所以这里先解冻（bringToFront +
                # setWebLifecycleState:active）并等 body 级真正命中，而不是直接放行。
                mod = dict(mod, seedance=True, model=mod.get("modelScope") or mod.get("model"))
                M._log("账号 %s 视频模式按芯片级信号确认（body 级未命中 → 页面半渲染）" % key[:10])
                try:
                    half_s = float(plugin_params.get("manager_halfrender_wait_seconds") or 12)
                except Exception:  # noqa: BLE001
                    half_s = 12.0
                page.activate()
                t_half = time.time()
                recovered = False
                while time.time() - t_half < max(0.0, half_s):
                    time.sleep(1.5)
                    try:
                        page.evaluate(D._JS_ENTER_VIDEO_MODE)
                        m2 = page.evaluate(D._JS_MODEL_LABEL)
                    except Exception:  # noqa: BLE001
                        m2 = {}
                    if m2.get("seedance"):
                        mod = m2
                        recovered = True
                        M._log("账号 %s 已恢复完整渲染（%.1fs，body 级命中）"
                               % (key[:10], time.time() - t_half))
                        break
                if not recovered:
                    M._log("警示: 账号 %s 等待 %.0fs 仍为半渲染（body 级未命中），继续但失败风险高"
                           % (key[:10], half_s))
            M._log("账号 %s 视频模型: %s" % (key[:10], mod))
            if not mod.get("seedance"):
                # 没真正进入 Seedance 视频模式就不能干活：上传/发提示词会落到错误模式，
                # 豆包不会回「确认」，只会干等 → 直接判该账号进不去视频模式，换下一账号。
                M._log("账号 %s 未能进入 Seedance 视频模式，跳过该账号" % key[:10])
                raise RuntimeError("未能进入 Seedance 视频模式")
            # 【2026-09-20】必须用「本账号局部副本」改提示词，不能原地改外层 `prompt`。
            # `prompt` 定义在 for 循环之外，下面的时长兜底又是 `prompt = "…" % prompt`，
            # 于是每失败一个账号就多前缀一次：3 个账号跑完实测写成
            # 「视频时长10秒。视频时长10秒。视频时长10秒。…」——提示词被逐轮污染，
            # 既可能撞豆包字数上限被截断，又让不同账号收到的内容不一致。
            cur_prompt = prompt
            applied = _apply_page_options(page, plugin_params, prompt=cur_prompt)
            if not applied.get("duration", True):
                d_explicit = str(plugin_params.get("duration") or "").strip()
                if d_explicit and d_explicit.lower() != "auto":
                    cur_prompt = "视频时长%s秒。%s" % (d_explicit, cur_prompt)
                    M._log("警示: 时长设置未生效，已注入提示词兜底（时长%s秒）" % d_explicit)
            if not any(applied.get(k) for k in ("model", "duration", "ratio")):
                # 【2026-09-20】三项选项全未生效 ⇒ 页面多半还停在「半渲染」态（模型面板、
                # 时长面板都没挂载出来）。这种账号继续往下走，上传完图必然卡在写提示词
                # 那一步；先置前 + 等就绪，至少把失败点提前、原因说清。
                M._log("警示: 模型/时长/比例三项均未生效，疑似页面半渲染，先置前等待就绪")
                ok_p, health_p = _input_ready(page, min(ready_s, 15.0), activate_after=0.0)
                M._log("页面就绪复查: ready=%s %s" % (
                    ok_p, health_p.get("blockedBy") or
                    "pe=%s vis=%s" % (health_p.get("pe"), health_p.get("vis"))))
            if refs:
                _progress(cb, "上传参考图")
                M._upload_reference_images(page, refs)
                # 【2026-09-20 根因修复】上传参考图后必须等输入区恢复可用，否则必失败。
                # 豆包在挂载图片预览时会重建输入区，新编辑器在**后台 webview** 里常停在
                # `visibility:hidden` + `pointer-events:none`（SPA 依赖可见性/rAF 推进，
                # 后台标签不重绘就不恢复）。实测 22:09 那一轮：u_35463537、u_42045857
                # 都是「4 张图全部上传成功 → 写提示词报输入区不可用」，
                # 而同批 u_45016991（页面已完全就绪）一次通过 —— 差别只在上传后的等待。
                ok_up, health_up = _input_ready(page, ready_s, activate_after=0.0)
                M._log("上传参考图后输入区: ready=%s %s" % (
                    ok_up,
                    ("| " + str(health_up.get("blockedBy"))) if health_up.get("blockedBy") else
                    "| pe=%s vis=%s w=%s h=%s" % (health_up.get("pe"), health_up.get("vis"),
                                                  health_up.get("w"), health_up.get("h"))))
            url = _wait_new_page(page, cur_prompt, _MAX_WAIT, cb, plugin_params=plugin_params)
            if not url:
                raise RuntimeError("超时未出片")
            M._log("成片: %s" % url[:100])
            # 无水印原片解析（对齐 browser 通道，2026-09-19 修正两处误判）：
            #   ① 创作节点/官方去水印接口常延迟就绪，5 秒窗口基本必失败——改为在
            #      nowm_wait_seconds 内循环重试；
            #   ② 「<video> 直链自带 lr=unwatermarked」的旧假设已失效：免费号播放流
            #      现在是 video_gen_watermark_dyn 动态水印，卡片兜底链与 <video> 直链
            #      同源同水印，二者都只是保底，拿到后记入待补抓清单。
            # 【2026-09-17 串片修复】参数语义必须与 browser 通道一致：
            #   · baseline_urls = 提交前页面上已存在的旧片地址；
            #   · min_ts = 本次真实提交时刻；
            #   · prefer_urls = 本次新片地址，兜底链/vid 只允许从它所属卡片里取。
            submitted_at = int(_LAST_BASELINE.get("submitted_at") or (time.time() - 30))
            hist_urls = sorted(_LAST_BASELINE.get("urls") or [])
            M._log("原片解析基线: 历史地址 %d 条, min_ts=%d" % (len(hist_urls), submitted_at))
            orig, meta, round_i = None, {}, 0
            try:
                nowm_wait = float(plugin_params.get("nowm_wait_seconds", 180) or 0)
            except Exception:  # noqa: BLE001
                nowm_wait = 180.0
            deadline = time.time() + max(0.0, nowm_wait)
            while True:
                round_i += 1
                try:
                    orig = M._resolve_original_video_url(
                        page, attempts=3, delay_s=1.5, min_ts=submitted_at,
                        scope="recent", baseline_urls=hist_urls,
                        skip_node_ids=_LAST_BASELINE.get("nodes") or [],
                        strict_recent=True, prefer_urls=[url], meta_out=meta,
                        conv_id=M._conv_id_from_url(getattr(page, "url", "") or ""))
                except Exception as e:
                    M._log("原片解析异常（第 %d 轮）: %s" % (round_i, str(e)[:120]))
                    orig, meta = None, {}
                if orig and not meta.get("wm"):
                    break
                if time.time() >= deadline:
                    break
                M._log("无水印原片暂未就绪，等待后重试解析（第 %d 轮，余 %.0fs）…"
                       % (round_i, max(0.0, deadline - time.time())))
                _progress(cb, "等待无水印原片就绪", 88)
                time.sleep(8)
            if orig and not meta.get("wm"):
                final_url = orig
            else:
                if meta.get("wm"):
                    M._log("无水印原片未就绪，保底用本次 <video> 直链（注意：播放链同样含动态水印）")
                final_url = url or orig
                if plugin_params.get("strict_no_watermark"):
                    raise M.NoWatermark("严格无水印模式：本轮未解析到无水印原片，拒绝落带水印成片")
            M._log("下载地址: %s" % final_url[:100])
            path = _download(final_url, output_dir, filename)
            _mark_ok(st, key)
            M._log("已下载: %s" % path)
            if not ("unwatermarked" in final_url or "download=true" in final_url):
                # 记入待补抓清单：之后一键补抓无水印版（不重生成、不耗额度）
                try:
                    M._add_pending({
                        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "account_id": key,
                        "chat_url": getattr(page, "url", "") or "",
                        "file": str(path),
                        "filename": filename,
                        "output_dir": str(output_dir),
                        "submitted_at": submitted_at,
                    })
                except Exception as e:  # noqa: BLE001
                    M._log("记入待补抓清单失败（忽略）: %s" % str(e)[:100])
            return [path]
        except Exception as e:
            msg = str(e)
            M._log("账号 %s 失败: %s" % (key[:10], msg))
            if isinstance(e, M.QuotaExhausted):
                # 循环内已确认豆包回了额度用尽：直接标记+换号，不再依赖下面那次
                # 可能因 CDP 抖动而失败的 body 重读（2026-09-19「卡在陈智号反复
                # 点确认不换号」的直接根因就是缺这条快速轮询路径）。
                _mark_quota(st, key)
                _mark_attempted(st, key)
                last_err = "额度已用完"
                errs.append("账号%s[额度]: 今日额度已用完 → 明天再试或换号" % key[:8])
                continue
            _save_fail_shot(page, key)
            body = ""
            try:
                body = page.evaluate("((document.body && document.body.innerText)||'')") or ""
            except Exception:
                pass
            if not _LAST_WAIT.get("accepted") and M._QUOTA_BLOCK_RE.search(body):
                _mark_quota(st, key)
                _mark_attempted(st, key)
                last_err = "额度已用完"
                errs.append("账号%s[额度]: 今日额度已用完 → 明天再试或换号" % key[:8])
                continue
            if _LAST_WAIT.get("accepted") and not any(w in body for w in _GEN_FAIL_WORDS):
                # 已受理 = 豆包正在生成（本段额度已耗）。此时换号重发 = 同一段分镜
                # 重复出片（2026-09-19 12:59 实测：CDP 瞬时超时被误判「未出片」，
                # 换号又生成了一条）。停止重试，记入待补抓清单，等原片生成后补抓。
                _mark_fail(st, key, msg)
                _mark_attempted(st, key)
                try:
                    M._add_pending({
                        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "account_id": key,
                        "chat_url": str(_LAST_WAIT.get("chat_url") or ""),
                        "file": "",
                        "filename": filename,
                        "output_dir": str(output_dir),
                        "submitted_at": int(_LAST_BASELINE.get("submitted_at") or 0),
                    })
                except Exception as pe:
                    M._log("记入待补抓清单失败（忽略）: %s" % str(pe)[:100])
                raise Exception(
                    "PLUGIN_ERROR:::豆包已受理本段分镜、正在生成（为避免重复出片已停止换号）。"
                    "账号页面通信失效：%s —— 请到豆包管理器窗口等它出完，"
                    "再到插件「待补抓清单」一键补抓无水印原片" % msg[:120])
            _mark_fail(st, key, msg)
            _mark_attempted(st, key)
            last_err = "失败(%s)" % msg[:60]
            stage, advice = _stage_and_advice(msg)
            errs.append("账号%s[%s]: %s%s" % (key[:8], stage, msg[:70],
                                              (" → " + advice) if advice else ""))
            continue
        finally:
            try:
                page.close()
            except Exception:
                pass

    raise Exception("PLUGIN_ERROR:::所有管理器豆包账号均未能出片。%s"
                    % ("；".join(errs) if errs else (last_err or "无可用账号")))
