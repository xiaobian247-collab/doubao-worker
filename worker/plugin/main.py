# -*- coding: utf-8 -*-
"""
豆包网页直出插件 (video_plugin_doubao_web)
========================================
原创实现，不依赖豆包管理器。
每个账号独立 Playwright 持久化目录；额度不足当天跳过并轮询下一账号。
成片按字字动画约定落盘：{viewer_index:04d}_video_{timestamp}.mp4
"""

import atexit
import asyncio
import hashlib
import json
import os
import random
import queue
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

plugin_dir = Path(__file__).parent
# __file__ = plugins/video_plugins/video_plugin_doubao_web/main.py，
# 三层 dirname 才是 plugin_utils.py 所在的 plugins 目录（写两层永远找不到）。
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
# 插件自身目录也必须进 sys.path：宿主用 importlib 的 spec_from_file_location +
# exec_module 加载 main.py，不会把插件目录加进 sys.path。少了这一行，
# handle_action 里的 `import doubao_manager`（管理器后端）会在设置页
# 「自动查找 / 启动并检测」时抛 No module named 'doubao_manager'。
# 用 append 而非 insert(0)：避免插件目录抢在标准库/宿主模块前造成遮蔽。
if str(plugin_dir) not in sys.path:
    sys.path.append(str(plugin_dir))
from plugin_utils import load_plugin_config  # noqa: E402

_PLUGIN_FILE = __file__
_PLUGIN_VERSION = "0.7.17"
_DOUBAO_HOME = "https://www.doubao.com/chat/"
_PROMPT_MAX_CHARS = 2000
_ACCOUNTS_FILE = plugin_dir / "accounts.json"
_PROFILES_DIR = plugin_dir / "profiles"
_SESSION_COOKIES = (
    "sessionid",
    "sessionid_ss",
    "uid_tt",
    "sid_tt",
    "passport_auth_status",
)
# 只认「拦住本次生成」的文案。免费额度用完改走会员/积分付费，不算耗尽。
# 「升级会员」「免费额度」是页面常驻营销词，绝不能当失败条件。
_QUOTA_BLOCK_RE = re.compile(
    r"(?:视频|图片)?(?:生成)?额度(?:已经|已)?(?:不足|用完|耗尽|为零|为0|没有了|没了)"
    r"|(?:近\s*\d+\s*天|今日|今天|本日)[^\n]{0,30}(?:视频|图片)?(?:生成)?(?:免费)?(?:次数|额度|机会)?[^\n]{0,30}(?:已经|已|都)?(?:达到|达)?(?:上限|限制|用完|耗尽|为零|为0|没有了|没了|不足)"
    r"|预计[^\n]{0,30}(?:恢复|回来)[^\n]{0,20}(?:为你服务|服务)"
    r"|(?:无法|不能|暂不能|不可)[^\n]{0,40}(?:额度不足|无[^\n]{0,8}额度|没有[^\n]{0,8}额度)"
    r"|(?:额度不足|无[^\n]{0,8}额度)[^\n]{0,40}(?:无法|不能|暂不能)[^\n]{0,12}生成"
)
_QUOTA_PAID_CONTINUE_RE = re.compile(
    r"将消耗|使用积分|会员额度|付费生成|使用会员|扣减积分|剩余积分|确认支付|立即支付"
)
_FAIL_RE = re.compile(
    r"视频[^\n]{0,12}(?:生成|制作)[^\n]{0,20}(?:失败|未成功)"
    r"|内容违规|未通过审核"
)
# 页签/浏览器已关闭的 Playwright 报错特征。等片时碰到必须立即失败换号：
# 死页面上所有检测永远静默失败，继续轮询只会白等满 timeout（实测单号空耗
# 26 分钟，页面上明明已出片也没人抓）。
_PAGE_CLOSED_RE = re.compile(
    r"(Target page, context or browser has been closed|Target closed"
    r"|Browser has been closed|has been disconnected)",
    re.I,
)

# ----------------------------------------------------------- 自动接管词表
# 豆包在真正开跑前/出片后可能弹出需要人工处理的界面：二次确认、会员付费、
# 人机验证、登录失效等。下面的词表用于自动识别并代用户处理，避免整条流水线
# 卡在「等人点一下」。全部可通过插件参数覆盖，词表不匹配时只会跳过、不会误伤。
_CAPTCHA_RE = re.compile(
    r"(安全验证|人机验证|机器人验证|请完成验证|请进行验证|拖动滑块|滑动验证|滑块验证"
    r"|请按顺序点击|请点击图中|完成拼图|向右滑动)"
)
_LOGIN_EXPIRED_RE = re.compile(
    r"(登录已过期|登录已失效|登录状态已失效|请重新登录|请先登录|身份已失效|重新登录后再试)"
)
# 页面正在生成/排队：有这些信号时不做任何打扰性操作。
# 「预计等待 5 分钟」是 Seedance 受理回复的原话——之前只认「还需/剩余」，
# 生成中会被误判为「没在生成」而补发提示词，白白重复扣一次额度。
_RUNNING_RE = re.compile(
    r"(视频)?(生成中|制作中|渲染中|合成中|排队中|加载中|创作中)"
    r"|正在(为您)?(生成|制作|渲染)"
    r"|预计(还需|剩余|等待)[^\n]{0,12}(秒|分钟|s|min)"
)
# 已出片信号：豆包会直接说「你的视频生成好了」。这类文案出现时必须停止补发，
# 否则会被当成「没在生成」而再次提交提示词，白白重复消耗一次额度。
# 注意不能匹配到「刚开始」的文案：受理回复「视频生成好后，我会主动发送给你」
# 里的「生成好后」是将来时（提交后半分钟就会出现），用 (?!后) 排除。
_DONE_RE = re.compile(
    r"视频(?:已经|已)?(?:生成|制作)(?:好|完成|完毕)(?!后)"
    r"|(?:你的|你要的)?视频(?:已经|已)?好了"
    r"|生成好了|制作好了|已经做好了|创作完成(?!后)|视频已就绪"
)
# 助手在向用户要确认（不点按钮，需要在输入框回复）
_ASK_CONFIRM_RE = re.compile(
    r"(请|麻烦|需要)[你您]?(先)?(确认|回复|答复|告知)"
    r"|回复\s*[「『\"']?\s*(确认|是|继续|开始|好的|yes)"
    r"|是否(确认|继续|开始|生成)"
    r"|确认(无误)?(后|之后)?[^\n]{0,8}(将|即|再|开始|继续)[^\n]{0,8}生成"
    r"|(确认|继续|开始)[^\n]{0,6}请回复"
    r"|如(果)?(确认|没问题|可以)[^。\n]{0,12}(请|就)?(回复|告诉|说)"
    r"|请(确认|选择)[^\n]{0,24}(是否|是否继续)"
)
# 弹窗按钮词表（归一化：去掉所有空白后比较）
_CONFIRM_BTN_WORDS = (
    "继续生成", "确认生成", "开始生成", "立即生成", "生成视频", "继续创作", "继续等待",
    "确认", "确定", "我知道了", "我已知悉", "知道了", "好的", "同意", "继续", "重试", "重新生成",
)
_CLOSE_BTN_RE_TXT = r"^(取消|关闭|暂不|稍后|不了|下次再说|以后再说|我知道了|知道了|好的)$"
_PAY_BTN_RE = re.compile(
    r"(开通会员|立即开通|去开通|升级会员|开通|立即支付|确认支付|去支付|购买|订阅|充值|付费"
    r"|会员额度|消耗积分|使用积分|积分|额度已用完|额度不足|额度用完|会员权益|会员专享|套餐)"
)
_DANGER_BTN_RE = re.compile(r"(删除|清空|注销|退出登录|解绑|永久|不再恢复)")
# 下拉/菜单/toast 浮层不是弹窗，点它会误选参数，必须排除
_NOT_DIALOG_CLASS_RE = re.compile(
    r"(dropdown|menu|popover|select|option|tooltip|toast|suggest|emoji|sticker)", re.I
)
# 消息卡片里的强确认按钮（不是 modal，但意图明确）
_STRONG_CONFIRM_WORDS = ("确认生成", "开始生成", "立即生成", "继续生成", "确认", "确定", "好的")

_DEFAULT_PARAMS = {
    "timeout": 900,
    "poll_interval": 2,
    "backend": "manager",           # generation 通道：manager=驱动豆包管理器已登录账号；browser=Playwright 独立浏览器
    "manager_port": 9223,           # manager 后端的 CDP 端口（管理器默认 9223，被占用可改）
    "manager_exe": "",              # 管理器 exe 路径（可空=不假定；端口通即可用，换机器也不用改）
    "headless": False,
    "duration": "10",
    "ratio": "16:9",
    "model": "seedance2.0fast",
    "enable_reference_image": True,
    "remove_watermark": True,
    "strict_no_watermark": False,   # 默认关：拿不到无水印就保底落带水印片并记待补抓（绝不重生成）
    "nowm_wait_seconds": 180,       # 出片后为拿无水印原片反复解析的等待上限（秒）
    "keep_window_open": True,
    "fresh_page_per_segment": True, # 每段分镜开全新标签页（生成完即关），隔离上一段/昨天的旧卡片，从源头防串片
    # ---- 防串片三件套（每次都当「干净窗口」用）----
    "new_chat_per_task": True,      # 每次生成前切到全新空会话：历史成片卡片不再挂在 DOM/fiber 里
    "min_video_wait_seconds": 20,   # 提交后最短抑制窗（秒）：视频至少几十秒才出片，窗口内的任何信号都不采信
    "recent_strict": True,          # 出片解析严格化：无时间戳/早于提交的创作节点一律不收（宁可空手重试，绝不抓旧片）
    # ---- 自动接管（无人值守）----
    "auto_pilot": True,             # 总开关：自动点掉确认/干扰弹窗、自动回复确认话术
    "auto_confirm_text": "确认",     # 豆包要求确认时自动回复的内容
    "auto_retry_text": "生成视频",    # 豆包只回文字不生成时自动补发的内容
    "auto_retry_times": 2,          # 补发次数上限（超过则判定该账号本轮失败）
    "auto_reply_wait": 25,          # 页面无「生成中」信号多少秒后，才认为需要补发/确认
    "allow_paid_generation": False, # 免费额度用完时，是否点「使用会员/积分继续」（关掉则切下一号）
    "captcha_wait_seconds": 300,    # 检测到人机验证后，留给人工处理的秒数
    "verify_mp4_header": True,      # 下载后校验 MP4 头，防止拿到错误文件
    "verify_mp4_complete": True,    # 下载后做 MP4 结构完整性校验（box 链自洽 + 含 moov），
                                    # 拦掉 CDN 断流造成的半截片；如遇特殊片源误判可关掉
    "download_seg_tries": 4,        # 单次下载内部的断点续传轮数（CDN 慢速长连接易断流）
    "debug_screenshot": True,       # 失败时截图到插件目录，便于定位
    "manager_input_ready_seconds": 25,  # 管理器通道：等豆包输入区「就绪」的最长秒数。
                                    # 点「新对话」/进视频模式/传完参考图后，tiptap 编辑器会
                                    # 短暂处于 visibility:hidden + pointer-events:none（未就绪），
                                    # 此时 focus()/insertText 全部无效 —— 必须等它就绪再写。
                                    # 网络慢/管理器刚启动可调大到 40~60。
    # ---- 管理器通道：额度预检 + 手动控制 ----
    "manager_quota_preflight": True,    # 管理器通道：传参考图/写提示词前先读会话尾部 DOM 判该号今日额度是否用尽
    "manager_accept_wait_seconds": 150, # 管理器通道：提交后多少秒仍无「已受理」信号就判该号本轮失败并换号（慢网/排队可调大）
    "manager_disable_accounts": "",     # 手动停用：豆包账号昵称或稳定id（u_xxx/450169...），逗号/顿号/分号/换行分隔，本轮一律跳过（不轮询到它）
    "manager_recover_accounts": "",     # 手动恢复：本轮清掉这些号的「今日无额度/冷却/失败/停用」标记，重新纳入轮询（填错号或想提前放行时用）
    # ---- 调度 ----
    "fail_cooldown_times": 3,       # 连续失败几次后进入短冷却
    "fail_cooldown_minutes": 15,    # 短冷却时长（分钟）；额度耗尽仍是冷却到次日
    "account_wait_seconds": 900,    # 所有账号都忙时，最多等多久
    "download_retries": 3,          # 单次下载失败重试次数（网络抖动）
    "fetch_output_dir": "",         # 补抓原片的输出目录（留空用插件 downloads 目录）
}

_accounts_lock = threading.Lock()
_browser_cache = {}  # account_id -> {"pw": pw, "ctx": ctx}
_browser_cache_lock = threading.Lock()
_KEEP_OPEN = True
# ------------------------------------------------------------------ 线程模型
# Playwright Sync API 对象有线程亲和性：创建和使用必须在同一线程。
# 1) 每个账号一个常驻 worker 线程，持有自己的 playwright 实例与浏览器；
# 2) 同一账号的所有浏览器操作都投递到该账号线程 → 天然没有跨线程问题；
# 3) 不同账号的线程互不影响 → 多账号真正并发出片（旧版是全局单 worker，全串行）；
# 4) 另有一个兜底 worker，服务还没有账号 id 的场景（如首次添加账号）。
_tls = threading.local()  # 每线程私有: is_worker / account_id / pw
_account_workers = {}  # account_id -> {"thread": t, "queue": q}
_account_workers_lock = threading.Lock()
_PW_WORKER = None
_PW_WORKER_QUEUE = queue.Queue()
_PW_WORKER_LOCK = threading.Lock()
# 正在生成中的账号：避免多个分镜同时挤同一个账号
_BUSY_ACCOUNTS = set()
_BUSY_LOCK = threading.Lock()


def _worker_loop(queue_obj, account_id=None):
    # 关键：给 worker 线程设置一个独立的、非运行的 asyncio 事件循环。
    # Playwright 的 sync_playwright() 用 asyncio.get_event_loop().is_running()
    # 检测是否在 asyncio loop 里；Python 3.12 下当前线程若无 loop，
    # get_event_loop() 可能返回主线程正在运行的 loop → 误报
    # "using Playwright Sync API inside the asyncio loop"。给它自己的
    # 非运行 loop 即可避免误判。
    try:
        asyncio.set_event_loop(asyncio.new_event_loop())
    except Exception:
        pass
    _tls.is_worker = True
    _tls.account_id = account_id or ""
    try:
        while True:
            item = queue_obj.get()
            if item is None:
                break
            fn, done, box = item
            try:
                box["ok"] = fn()
            except BaseException as e:  # noqa: BLE001
                box["err"] = e
            finally:
                done.set()
    finally:
        # 线程退出前停掉本线程的 playwright，避免遗留 driver 进程
        _pw_stop_shared()


def _dispatch(st, fn, args, kwargs):
    done = threading.Event()
    box = {}
    st["queue"].put((lambda: fn(*args, **kwargs), done, box))
    done.wait()
    if "err" in box:
        raise box["err"]
    return box["ok"]


def _get_account_worker(account_id):
    with _account_workers_lock:
        st = _account_workers.get(account_id)
        if st is None or not st["thread"].is_alive():
            q = queue.Queue()
            th = threading.Thread(
                target=_worker_loop, args=(q, account_id),
                daemon=True, name=f"doubao-{account_id}",
            )
            st = {"thread": th, "queue": q}
            _account_workers[account_id] = st
            th.start()
        return st


def _run_on_account(account_id, fn, *args, **kwargs):
    """把 fn 投递到该账号的专属线程执行（已在该线程则直接跑）。"""
    if getattr(_tls, "account_id", None) == account_id:
        return fn(*args, **kwargs)
    return _dispatch(_get_account_worker(account_id), fn, args, kwargs)


def _ensure_pw_worker():
    global _PW_WORKER
    with _PW_WORKER_LOCK:
        if _PW_WORKER is None or not _PW_WORKER.is_alive():
            _PW_WORKER = threading.Thread(
                target=_worker_loop, args=(_PW_WORKER_QUEUE, None),
                daemon=True, name="doubao-pw-worker",
            )
            _PW_WORKER.start()
    return _PW_WORKER


def _run_off_loop(fn, *args, **kwargs):
    """兜底执行：用于还没有账号 id 的动作（纯数据操作等）。"""
    _ensure_pw_worker()
    if threading.current_thread() is _PW_WORKER:
        return fn(*args, **kwargs)
    return _dispatch({"queue": _PW_WORKER_QUEUE}, fn, args, kwargs)


def _shutdown():
    if not _KEEP_OPEN:
        try:
            with _browser_cache_lock:
                entries = list(_browser_cache.values())
            for entry in entries:
                try:
                    entry["ctx"].close()
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass


atexit.register(_shutdown)


class QuotaExhausted(Exception):
    pass


class GenerateFailed(Exception):
    pass


class NeedHuman(Exception):
    """需要人工介入（人机验证超时等）。按账号失败处理，可切下一号。"""
    pass


class NeedLogin(Exception):
    """登录态失效，需要重新登录。"""
    pass


class NoWatermark(Exception):
    """拿不到无水印原片，且配置要求必须无水印（已拒绝退回带水印成片）。

    按账号失败处理：该号累计失败达阈值后冷却，本轮切下一账号重试。
    """
    pass


def _log(msg, level="INFO"):
    print(f"[doubao_web] {msg}", flush=True)



# worker 线程内共享的 playwright 单例。
# 【重要】同一个事件循环线程里绝对不能再 enter 第二次 sync_playwright()：
#   sync_playwright().start() 首次进入会用 _own_loop=True 的 greenlet 事件循环，
#   该 loop 在绿色线程切换间保持 running；同一线程再次 enter 时
#   asyncio.get_running_loop() 会返回它且 is_running()==True，
#   触发 "using Playwright Sync API inside the asyncio loop" 误报。
#   因此每个 worker 线程生命周期内只 enter 一次，该账号的 launch/CDP 全复用它。
def _get_pw_shared():
    """惰性获取「当前线程」唯一的 playwright 实例（每线程只 enter 一次）。"""
    from playwright.sync_api import sync_playwright
    pw = getattr(_tls, "pw", None)
    if pw is None:
        pw = sync_playwright().start()
        _tls.pw = pw
    return pw


def _pw_stop_shared():
    """关停当前线程的 playwright 实例（不影响其他账号线程）。"""
    pw = getattr(_tls, "pw", None)
    if pw is not None:
        try:
            pw.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            _tls.pw = None
        except Exception:  # noqa: BLE001
            pass



def get_info():
    return {
        "name": "豆包网页直出插件",
        "description": "直接自动化豆包网页版生成视频（无需豆包管理器）。多账号并发出片，"
                       "自动接管确认弹窗与二次确认，额度耗尽/连续失败自动换号与分级冷却。",
        "version": _PLUGIN_VERSION,
        "author": "local",
    }


def get_params():
    params = dict(_DEFAULT_PARAMS)
    try:
        params.update(load_plugin_config(_PLUGIN_FILE) or {})
    except Exception:
        pass
    return params


# ---------------------------------------------------------------- accounts

def _empty_store():
    return {"accounts": [], "next_index": 1, "last_used_id": ""}


def _load_store():
    with _accounts_lock:
        if not _ACCOUNTS_FILE.exists():
            return _empty_store()
        try:
            data = json.loads(_ACCOUNTS_FILE.read_text(encoding="utf-8"))
            data.setdefault("accounts", [])
            data.setdefault("next_index", 1)
            data.setdefault("last_used_id", "")
            return data
        except Exception:
            return _empty_store()


def _save_store(data):
    with _accounts_lock:
        tmp = str(_ACCOUNTS_FILE) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(_ACCOUNTS_FILE))


def _profile_dir(account_id):
    return _PROFILES_DIR / account_id


def _today():
    return date.today().isoformat()


def _is_quota_blocked(acc):
    """额度耗尽：冷却到当天结束（次日自动解除）。"""
    until = str(acc.get("quota_exhausted_until") or "")
    return bool(until) and until >= _today()


def _now_iso(offset_minutes=0):
    return (datetime.now() + timedelta(minutes=offset_minutes)).isoformat(timespec="seconds")


def _is_cooling(acc):
    """短冷却中（连续失败触发），与「额度耗尽」分开管理。"""
    until = str(acc.get("cooldown_until") or "")
    if not until:
        return False
    try:
        return datetime.fromisoformat(until) > datetime.now()
    except Exception:
        return False


def _cooldown_left_text(acc):
    until = str(acc.get("cooldown_until") or "")
    if not until:
        return ""
    try:
        left = datetime.fromisoformat(until) - datetime.now()
        mins = max(0, int(left.total_seconds() // 60))
        return f"冷却中（约 {mins} 分钟）"
    except Exception:
        return ""


def _account_state(acc):
    if _is_quota_blocked(acc):
        return "今日额度已用完"
    if _is_cooling(acc):
        return _cooldown_left_text(acc) or "冷却中"
    if not acc.get("logged_in"):
        return "未登录"
    return "可用"


def _fingerprint_summary(fp):
    if not fp:
        return ""
    w, h = fp.get("viewport") or [0, 0]
    return f"{w}x{h} · {fp.get('hardware_concurrency', '?')}核 · {fp.get('device_memory', '?')}G"


def _account_public(acc):
    fp = acc.get("fingerprint") or {}
    return {
        "id": acc.get("id"),
        "name": acc.get("name") or acc.get("id"),
        "logged_in": bool(acc.get("logged_in")),
        "quota_blocked": _is_quota_blocked(acc),
        "quota_exhausted_until": acc.get("quota_exhausted_until") or "",
        "last_error": acc.get("last_error") or "",
        "cooling": _is_cooling(acc),
        "cooldown_until": acc.get("cooldown_until") or "",
        "fail_count": int(acc.get("fail_count") or 0),
        "state": _account_state(acc),
        "busy": acc.get("id") in _BUSY_ACCOUNTS,
        "fingerprint": fp,
        "fingerprint_summary": _fingerprint_summary(fp),
    }


def _make_fingerprint(account_id):
    """每个账号固定一套指纹：由账号 id 做种子，登录前后保持不变。"""
    rng = random.Random(int(hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:16], 16))
    viewport = rng.choice((
        (1366, 768),
        (1440, 900),
        (1536, 864),
        (1600, 900),
        (1920, 1080),
        (1280, 800),
    ))
    return {
        "viewport": list(viewport),
        "device_scale_factor": rng.choice((1, 1, 1.25, 1.5)),
        "hardware_concurrency": rng.choice((4, 6, 8, 8, 12, 16)),
        "device_memory": rng.choice((4, 8, 8, 16)),
        "color_scheme": rng.choice(("light", "light", "dark")),
        "locale": "zh-CN",
        "timezone_id": "Asia/Shanghai",
    }


def _ensure_fingerprints(store=None):
    store = store or _load_store()
    changed = False
    for acc in store.get("accounts") or []:
        if not acc.get("fingerprint"):
            acc["fingerprint"] = _make_fingerprint(acc["id"])
            changed = True
    if changed:
        _save_store(store)
    return store


def _ensure_legacy_migrated():
    """把 MVP 单 profile 迁成账号1，避免已登录会话丢失。"""
    store = _load_store()
    legacy = plugin_dir / "browser_profile"
    if store["accounts"]:
        return
    if legacy.exists() and any(legacy.iterdir()):
        acc_id = "acc_1"
        dest = _profile_dir(acc_id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            shutil.move(str(legacy), str(dest))
        store["accounts"].append({
            "id": acc_id,
            "name": "账号1",
            "logged_in": True,
            "quota_exhausted_until": "",
            "last_error": "",
            "fingerprint": _make_fingerprint(acc_id),
        })
        store["next_index"] = 2
        store["last_used_id"] = acc_id
        _save_store(store)
        _log("已将首次登录会话迁移为 账号1")


def _pick_accounts(store):
    """从 last_used 的下一个开始轮询，跳过未登录 / 额度耗尽 / 冷却中。"""
    accounts = list(store.get("accounts") or [])
    if not accounts:
        return []
    last = store.get("last_used_id") or ""
    ids = [a["id"] for a in accounts]
    start = 0
    if last in ids:
        start = (ids.index(last) + 1) % len(ids)
    ordered = accounts[start:] + accounts[:start]
    usable = []
    for acc in ordered:
        if not acc.get("logged_in"):
            continue
        if _is_quota_blocked(acc):
            continue
        if _is_cooling(acc):
            continue
        usable.append(acc)
    return usable


def _acquire_account(account_id):
    """占用账号（同一账号同时只跑一个生成任务）。"""
    with _BUSY_LOCK:
        if account_id in _BUSY_ACCOUNTS:
            return False
        _BUSY_ACCOUNTS.add(account_id)
        return True


def _release_account(account_id):
    with _BUSY_LOCK:
        _BUSY_ACCOUNTS.discard(account_id)


def _mark_quota(account_id, error=""):
    """额度耗尽：冷却到当天结束。"""
    store = _load_store()
    for acc in store["accounts"]:
        if acc["id"] == account_id:
            acc["quota_exhausted_until"] = _today()
            acc["last_error"] = str(error or "额度不足")[:300]
            acc["fail_count"] = 0
            break
    _save_store(store)


def _mark_failure(account_id, error, cooldown_after=3, cooldown_minutes=15):
    """非额度类失败：累计次数，达到阈值后短冷却，避免坏号被反复重试。"""
    store = _load_store()
    for acc in store["accounts"]:
        if acc["id"] == account_id:
            acc["last_error"] = str(error)[:300]
            n = int(acc.get("fail_count") or 0) + 1
            acc["fail_count"] = n
            if n >= max(1, int(cooldown_after or 3)):
                acc["cooldown_until"] = _now_iso(int(cooldown_minutes or 15))
                acc["fail_count"] = 0
                _log(f"账号 {account_id} 连续失败 {n} 次，冷却 {cooldown_minutes} 分钟")
            break
    _save_store(store)


def _mark_success(account_id):
    """出片成功：清空失败计数与冷却。"""
    store = _load_store()
    for acc in store["accounts"]:
        if acc["id"] == account_id:
            acc["fail_count"] = 0
            acc["cooldown_until"] = ""
            acc["last_error"] = ""
            break
    _save_store(store)


def _mark_error(account_id, error):
    store = _load_store()
    for acc in store["accounts"]:
        if acc["id"] == account_id:
            acc["last_error"] = str(error)[:300]
            break
    _save_store(store)


def _mark_used(account_id, logged_in=None):
    store = _load_store()
    store["last_used_id"] = account_id
    if logged_in is not None:
        for acc in store["accounts"]:
            if acc["id"] == account_id:
                acc["logged_in"] = bool(logged_in)
                if logged_in:
                    acc["last_error"] = ""
                break
    _save_store(store)


# ---------------------------------------------------------------- browser

def _has_session_cookies(ctx):
    names = {c["name"] for c in ctx.cookies() if "doubao" in c.get("domain", "")}
    return any(n in names for n in _SESSION_COOKIES)


_LOGIN_PANEL_JS = r"""
() => {
  const t = (document.body && document.body.innerText) || '';
  if (!/(扫码登录|短信登录|验证码登录|手机登录|一键登录|立即登录|请先登录|登录后|登录以继续|登录豆包)/.test(t)) {
    return false;
  }
  const btns = Array.from(document.querySelectorAll('button,[role="button"],a'));
  return btns.some((b) => {
    if (!b.offsetParent) return false;
    const s = (b.innerText || '') + ' ' + (b.getAttribute('aria-label') || '');
    return /登录|扫码/.test(s);
  });
}
"""
# 被重定向到通行证/登录页
_LOGIN_URL_RE = re.compile(r"(passport\.|accounts\.|/passport|/login\b|sign_in|signin)", re.I)


def _has_login_panel(page):
    """页面是否摆着登录面板（cookie 还在但登录态已失效时会这样）。"""
    try:
        return bool(page.evaluate(_LOGIN_PANEL_JS))
    except Exception:  # noqa: BLE001
        return False


def _looks_logged_in(page, ctx=None):
    """登录态判定：URL 重定向 → 登录面板 → session cookie → 编辑器。

    只查 cookie 不够：豆包 cookie 未过期但会话被踢时，页面会摆出扫码登录面板，
    此时继续提交会静默失败。这里逐层收紧。
    """
    try:
        url = str(page.url or "")
    except Exception:  # noqa: BLE001
        url = ""
    if url and _LOGIN_URL_RE.search(url):
        return False
    try:
        if ctx is not None and _has_session_cookies(ctx):
            return not _has_login_panel(page)
        has_editor = page.locator("[contenteditable='true'], textarea").count() > 0
        if not has_editor:
            return False
        if _has_login_panel(page):
            return False
        return page.get_by_text("登录", exact=True).count() == 0
    except Exception:  # noqa: BLE001
        return False


def _stealth_init_script(fp):
    cores = int(fp.get("hardware_concurrency") or 8)
    mem = int(fp.get("device_memory") or 8)
    return f"""
(() => {{
  const cores = {cores};
  const mem = {mem};
  try {{ Object.defineProperty(navigator, 'webdriver', {{ get: () => undefined }}); }} catch (e) {{}}
  try {{ Object.defineProperty(navigator, 'hardwareConcurrency', {{ get: () => cores }}); }} catch (e) {{}}
  try {{ Object.defineProperty(navigator, 'deviceMemory', {{ get: () => mem }}); }} catch (e) {{}}
  try {{ Object.defineProperty(navigator, 'languages', {{ get: () => ['zh-CN', 'zh'] }}); }} catch (e) {{}}
  try {{ Object.defineProperty(navigator, 'language', {{ get: () => 'zh-CN' }}); }} catch (e) {{}}
  window.chrome = window.chrome || {{ runtime: {{}}, loadTimes: function() {{}}, csi: function() {{}} }};
  const proto = WebGLRenderingContext && WebGLRenderingContext.prototype;
  if (proto && proto.getParameter) {{
    const raw = proto.getParameter;
    proto.getParameter = function (p) {{
      if (p === 37445) return 'Google Inc. (Intel)';
      if (p === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)';
      return raw.apply(this, arguments);
    }};
  }}
}})();
"""


def _find_chromium_executable():
    """找本机 Edge/Chrome 可执行文件（回退用，不依赖被劫持的 PLAYWRIGHT_BROWSERS_PATH）。"""
    candidates = (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    )
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def _free_port():
    """找一个空闲端口（绑定 0 让系统分配）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _kill_stale_msedge(account_id):
    """杀掉仍持有该账号 profile 的残留 msedge（防 profile 锁）。

    用不含内嵌双引号的 Where-Object 写法，避免 -Filter \"...\" 在 subprocess 直传
    PowerShell 时被误解析（PowerShell 转义符是反引号）。
    """
    marker = f"video_plugin_doubao_web*profiles*{account_id}"
    ps_exe = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    if not os.path.exists(ps_exe):
        ps_exe = r"C:\Windows\System32\windowspowershell\v1.0\powershell.exe"
    cmd = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -eq 'msedge.exe' -and "
        f"$_.CommandLine -like '*{marker}*' }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    try:
        subprocess.run(
            [ps_exe, "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True, timeout=30,
        )
        _log(f"已清理 {account_id} 残留 msedge 进程")
    except Exception:  # noqa: BLE001
        pass


def _try_cdp_attach(pw, cdp_port):
    """尝试连接已在运行的常驻浏览器（CDP）；成功返回 Browser，否则 None。"""
    if not cdp_port:
        return None
    try:
        return pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
    except Exception:  # noqa: BLE001
        return None


def _launch(profile_dir, headless=False, fingerprint=None, cdp_port=None):
    fp = fingerprint or _make_fingerprint(Path(profile_dir).name)
    pw = _get_pw_shared()
    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    vw, vh = fp.get("viewport") or [1440, 900]
    ctx = None
    last_err = None
    exe_path = _find_chromium_executable()
    for channel in ("msedge", "chrome", None):
        try:
            args = [
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ]
            if cdp_port:
                args += [f"--remote-debugging-port={cdp_port}", "--remote-allow-origins=*"]
            kwargs = {
                "headless": headless,
                "viewport": {"width": int(vw), "height": int(vh)},
                "device_scale_factor": float(fp.get("device_scale_factor") or 1),
                "locale": fp.get("locale") or "zh-CN",
                "timezone_id": fp.get("timezone_id") or "Asia/Shanghai",
                "color_scheme": fp.get("color_scheme") or "light",
                "ignore_default_args": ["--enable-automation"],
                "args": args,
                "extra_http_headers": {"Accept-Language": "zh-CN,zh;q=0.9"},
            }
            if channel:
                kwargs["channel"] = channel
            elif exe_path:
                kwargs["executable_path"] = exe_path
            else:
                continue  # 没有可用的可执行文件，跳过 bundled 回退
            ctx = pw.chromium.launch_persistent_context(str(profile_dir), **kwargs)
            ctx.add_init_script(_stealth_init_script(fp))
            _log(
                f"browser launched (channel={channel or 'exe'}, "
                f"profile={Path(profile_dir).name}, fp={_fingerprint_summary(fp)})"
            )
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            ctx = None
    if ctx is None:
        _pw_stop_shared()
        raise Exception(f"PLUGIN_ERROR:::无法启动浏览器: {last_err}")
    return pw, ctx


def _browser_connected(entry):
    try:
        ctx = entry.get("ctx")
        if ctx is None:
            return False
        browser = getattr(ctx, "browser", None)
        if browser is not None:
            return bool(browser.is_connected())
        # 持久化上下文没有 browser 对象：用一次 RPC 探测存活（浏览器死了会抛异常）
        ctx.cookies()
        return True
    except Exception:  # noqa: BLE001
        return False


def _get_browser(account_id, headless=False):
    """返回账号的常驻浏览器 (pw, ctx)。

    复用顺序：
    1) 进程内缓存已有且存活 → 直接复用；
    2) 账号存了 cdp_port 且窗口还在 → CDP attach 复用常驻窗口（跨进程/跨重启不锁 profile）；
    3) 杀残留 msedge 释放 profile 锁，启动新浏览器并持久化 cdp_port。
    """
    with _browser_cache_lock:
        entry = _browser_cache.get(account_id)
        if entry is not None and _browser_connected(entry):
            return entry["pw"], entry["ctx"]
        if entry is not None:
            # 旧实例失效，清理再重启
            try:
                entry["ctx"].close()
            except Exception:  # noqa: BLE001
                pass
            # 注意：pw 是共享单例（见 _get_pw_shared），不能 stop，否则影响其他账号；
            # close ctx 已关闭对应浏览器/profile。
            _browser_cache.pop(account_id, None)
        store = _ensure_fingerprints()
        acc = next((a for a in store["accounts"] if a["id"] == account_id), None)
        fp = (acc or {}).get("fingerprint") or _make_fingerprint(account_id)
        cdp_port = int((acc or {}).get("cdp_port") or 0)

        # 1) CDP 复用仍在运行的常驻窗口（跨进程/跨重启）
        if cdp_port:
            # 复用 worker 线程内唯一的 playwright 单例，禁止二次 sync_playwright().start()
            # （绿色线程 event loop 常驻，二次 enter 会误报 asyncio loop）。
            pw2 = _get_pw_shared()
            browser2 = _try_cdp_attach(pw2, cdp_port)
            if browser2 is not None:
                ctx2 = browser2.contexts[0] if browser2.contexts else None
                if ctx2 is not None:
                    _browser_cache[account_id] = {"pw": pw2, "ctx": ctx2}
                    _log(f"账号 {account_id} 已通过 CDP 复用常驻窗口 (port={cdp_port})")
                    return pw2, ctx2
                try:
                    browser2.close()
                except Exception:  # noqa: BLE001
                    pass
            # attach 失败：pw2 为共享单例，不能 stop（可能有其他账号在用），
            # 落到第 2) 步用同一 pw 重新 launch。

        # 2) 杀残留 msedge 释放 profile 锁，再启动（带 CDP 端口并持久化）
        _kill_stale_msedge(account_id)
        if not cdp_port:
            cdp_port = _free_port()
        pw, ctx = _launch(_profile_dir(account_id), headless=headless, fingerprint=fp, cdp_port=cdp_port)
        try:
            for a in store["accounts"]:
                if a["id"] == account_id:
                    a["cdp_port"] = cdp_port
                    break
            _save_store(store)
        except Exception:  # noqa: BLE001
            pass
        _browser_cache[account_id] = {"pw": pw, "ctx": ctx}
        _log(f"账号 {account_id} 浏览器窗口已保留（常驻, cdp={cdp_port}）")
        return pw, ctx


def _close_browser(account_id):
    with _browser_cache_lock:
        entry = _browser_cache.pop(account_id, None)
    if entry:
        try:
            entry["ctx"].close()
        except Exception:  # noqa: BLE001
            pass
        # pw 是共享单例（见 _get_pw_shared），close ctx 已关闭该浏览器/profile，
        # 不在此 stop 共享 pw。
        # 显式关闭时确保残留 msedge 也一并退出（CDP 复用可能只断开连接）
        _kill_stale_msedge(account_id)
        _log(f"账号 {account_id} 浏览器窗口已关闭")


def _open_for_login(account_id, wait_s=480):
    # 用常驻浏览器（登录完保留窗口，不关闭）
    pw, ctx = _get_browser(account_id, headless=False)
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(_DOUBAO_HOME, wait_until="domcontentloaded", timeout=60000)
        _log(f"请在窗口中登录账号 {account_id}（最长 {wait_s} 秒）…")
        deadline = time.time() + wait_s
        while time.time() < deadline:
            if _has_session_cookies(ctx):
                time.sleep(3)
                _mark_used(account_id, logged_in=True)
                _log(f"{account_id} 登录成功")
                return True
            time.sleep(3)
        _mark_used(account_id, logged_in=False)
        _mark_error(account_id, "登录超时")
        return False
    except Exception as e:  # noqa: BLE001
        _mark_error(account_id, str(e)[:200])
        return False


# ------------------------------------------------- 豆包管理器路径/启动
# 「账号已登录」不等于「webview 存在」：管理器只给被点开的账号建 webview，
# 管理器重启后 webview 会全部消失。下面两个动作让 UI 能：
#   find_manager   → 自动扫出管理器 exe（填的路径/运行中进程/常见目录）
#   ensure_manager → 管理器没开就拉起它，并自动点开账号卡片物化 webview
_MANAGER_EXE_NAME = "豆包管理器.exe"


def _exe_in_dir(d):
    """在目录里找管理器 exe（用户填文件夹时用）。"""
    try:
        d = Path(str(d))
    except Exception:
        return ""
    if not d.is_dir():
        return ""
    for pat in (_MANAGER_EXE_NAME, "*豆包*管理器*.exe", "*管理器*.exe"):
        try:
            for f in sorted(d.glob(pat)):
                if f.is_file():
                    return str(f)
        except Exception:
            continue
    return ""


def _resolve_manager_exe(hint=""):
    """把用户填的路径解析成 exe；填文件夹也能用（找里面的管理器 exe）。"""
    hint = str(hint or "").strip().strip('"')
    if not hint:
        return ""
    try:
        p = Path(hint)
    except Exception:
        return ""
    if p.is_file() and p.suffix.lower() == ".exe":
        return str(p)
    if p.is_dir():
        return _exe_in_dir(p)
    return ""


def _running_manager_path():
    """从正在运行的管理器进程读出 exe 路径（最准的自动发现）。"""
    cmds = [
        ["wmic", "process", "where", "name='%s'" % _MANAGER_EXE_NAME, "get", "ExecutablePath", "/value"],
        ["powershell", "-NoProfile", "-Command",
         "(Get-Process -Name '豆包管理器' -ErrorAction SilentlyContinue | Select-Object -First 1).Path"],
    ]
    for cmd in cmds:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=8,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            continue
        for line in (out.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            if "=" in line and line.lower().startswith("executablepath"):
                line = line.split("=", 1)[1].strip()
            if line.lower().endswith(".exe"):
                try:
                    if Path(line).is_file():
                        return line
                except Exception:
                    continue
    return ""


def _scan_for_manager(root, depth=2):
    """在 root 下有限深度地找 豆包管理器.exe。

    绿色版可能被解压到任意目录名，所以不能只认固定路径；但也不能全盘递归。
    策略：本目录先找，再只往名字像的目录（豆包/管理器/免验证/programs/…）里钻。
    """
    try:
        root = Path(str(root))
    except Exception:
        return ""
    if not root.is_dir():
        return ""
    hit = _exe_in_dir(root)
    if hit:
        return hit
    if depth <= 0:
        return ""
    try:
        subs = sorted([p for p in root.iterdir() if p.is_dir()])[:60]
    except Exception:
        return ""
    for sub in subs:
        name = sub.name.lower()
        if not any(k in name for k in ("豆包", "管理器", "免验证", "green", "program", "tool", "软件", "app")):
            continue
        hit = _exe_in_dir(sub)
        if hit:
            return hit
        if depth > 1:
            hit = _scan_for_manager(sub, depth - 1)
            if hit:
                return hit
    return ""


def _manager_candidates(hint=""):
    """列出可能的管理器 exe：用户填的 > 运行中进程 > 常见安装位置/各盘符。"""
    out = []

    def add(p):
        p = str(p or "").strip().strip('"')
        if p and p not in out:
            out.append(p)

    add(_resolve_manager_exe(hint))
    add(_running_manager_path())
    home = Path.home()
    roots = [
        r"D:\programs\免验证绿色版",
        r"D:\programs\豆包管理器",
        r"D:\豆包管理器",
        r"C:\豆包管理器",
        r"C:\programs\豆包管理器",
        str(home / "Desktop"),
        str(home / "Desktop" / "免验证绿色版"),
        str(home / "Desktop" / "豆包管理器"),
        str(home / "Downloads"),
        str(home),
    ]
    if os.name == "nt":
        for drive in "CDEFGH":
            base = Path(drive + ":\\")
            try:
                if not base.exists():
                    continue
            except Exception:
                continue
            roots.append(str(base))
            for sub in ("programs", "Program Files", "Program Files (x86)", "tools", "软件", "apps"):
                roots.append(str(base / sub))
    for d in roots:
        add(_scan_for_manager(d, depth=2))
    return out


def _manager_action(action, data=None):
    """在独立线程跑（管理器未开时拉起 + 等就绪可能十秒级，不能堵住调用线程）。"""
    data = data or {}
    box = {}
    done = threading.Event()

    def _runner():
        try:
            box["ok"] = _manager_action_impl(action, data)
        except BaseException as e:  # noqa: BLE001
            box["err"] = e
        finally:
            done.set()

    threading.Thread(target=_runner, daemon=True, name="doubao-manager-action").start()
    if not done.wait(timeout=180):
        return {"ok": False, "error": "操作超时（管理器启动较慢，请稍后重试）"}
    if "err" in box:
        return {"ok": False, "error": str(box["err"])[:300]}
    return box["ok"]


def _manager_action_impl(action, data):
    try:
        import doubao_manager as D
    except ImportError as e:  # noqa: BLE001
        raise Exception(
            f"管理器后端模块导入失败（{e}）。请确认插件目录下存在 doubao_manager.py，"
            f"且安装的是完整插件包（不是只含 main.py/ui 的精简包）。"
        ) from e

    port = int(data.get("port") or get_params().get("manager_port") or 9223)
    hint = str(data.get("path") or get_params().get("manager_exe") or "").strip()

    if action == "find_manager":
        cands = _manager_candidates(hint)
        return {"ok": True, "candidates": cands, "port": port}

    # ensure_manager：拉起（未开时）+ 物化账号窗口
    exe = _resolve_manager_exe(hint)
    _log(f"管理器启动/检测：exe={exe or '(未指定，靠端口)'} port={port}")
    wvs = D.ensure_doubao_webviews(
        port=port, exe=exe or None,
        log=lambda m: _log("  " + str(m)),
    )
    accounts = []
    try:
        accounts = D.ManagerBridge(port=port).account_names()
    except Exception as e:  # noqa: BLE001
        _log(f"读管理器账号卡片失败: {e}")
    if not wvs:
        return {
            "ok": False, "path": exe or hint, "accounts": accounts, "webviews": 0,
            "error": "没有可用的豆包账号窗口。请确认管理器里至少有一个账号已登录"
                     + ("（读到的账号卡片：%s）" % "、".join(accounts) if accounts else "（读不到账号卡片）"),
        }
    return {
        "ok": True, "path": exe or hint, "accounts": accounts, "webviews": len(wvs),
        "message": "管理器已就绪：账号窗口 %d 个" % len(wvs),
    }


def handle_action(action, data=None):
    """按账号路由动作：涉及浏览器的动作必须在「该账号的线程」里执行。

    浏览器对象与 playwright 实例都属于账号线程，跨线程调用会报
    "cannot switch to a different thread"，所以这里先解析出账号再派发。
    """
    data = data or {}
    _ensure_legacy_migrated()
    if action in ("find_manager", "ensure_manager"):
        # 管理器启动/检测：与 Playwright 无关，单独线程跑，不占用账号 worker
        return _manager_action(action, data)
    if action == "close_browsers":
        return _close_browsers_action()
    acc_id = str(data.get("id") or "")
    if action == "open_browser" and not acc_id:
        store = _load_store()
        if store["accounts"]:
            acc_id = store["accounts"][0]["id"]
            data = dict(data)
            data["id"] = acc_id
    if acc_id:
        return _run_on_account(acc_id, _handle_action_impl, action, data)
    return _run_off_loop(_handle_action_impl, action, data)


def _close_browsers_action():
    """逐个账号在自己的线程里关窗口（跨账号串行派发，避免跨线程操作对象）。"""
    store = _load_store()
    ids = [a["id"] for a in (store.get("accounts") or [])]
    with _browser_cache_lock:
        ids += [k for k in _browser_cache.keys() if k not in ids]
    for aid in ids:
        try:
            _run_on_account(aid, _close_browser, aid)
        except Exception as e:  # noqa: BLE001
            _log(f"关闭账号 {aid} 窗口失败: {str(e)[:100]}")
    return {"ok": True, "message": "已关闭全部豆包浏览器窗口"}


def _handle_action_impl(action, data=None):
    data = data or {}
    _ensure_legacy_migrated()

    if action == "list_accounts":
        store = _ensure_fingerprints()
        return {"ok": True, "accounts": [_account_public(a) for a in store["accounts"]]}

    if action == "add_account":
        store = _load_store()
        idx = int(store.get("next_index") or 1)
        acc_id = f"acc_{idx}"
        name = str(data.get("name") or f"账号{idx}").strip() or f"账号{idx}"
        store["accounts"].append({
            "id": acc_id,
            "name": name,
            "logged_in": False,
            "quota_exhausted_until": "",
            "last_error": "",
            "fingerprint": _make_fingerprint(acc_id),
        })
        store["next_index"] = idx + 1
        _save_store(store)
        _profile_dir(acc_id).mkdir(parents=True, exist_ok=True)
        return {"ok": True, "account": _account_public(store["accounts"][-1])}

    if action == "rename_account":
        acc_id = str(data.get("id") or "")
        name = str(data.get("name") or "").strip()
        store = _load_store()
        for acc in store["accounts"]:
            if acc["id"] == acc_id:
                if name:
                    acc["name"] = name
                _save_store(store)
                return {"ok": True, "account": _account_public(acc)}
        return {"ok": False, "error": "账号不存在"}

    if action == "delete_account":
        acc_id = str(data.get("id") or "")
        store = _load_store()
        store["accounts"] = [a for a in store["accounts"] if a["id"] != acc_id]
        if store.get("last_used_id") == acc_id:
            store["last_used_id"] = ""
        try:
            _save_store(store)
        except Exception as e:  # noqa: BLE001
            _log(f"删除账号保存失败: {e}")
            return {"ok": False, "error": f"删除账号保存失败: {e}"}
        try:
            _close_browser(acc_id)
        except Exception:  # noqa: BLE001
            pass
        # 无条件杀该账号残留 msedge（无论是否在缓存里），确保 profile 目录可删
        _kill_stale_msedge(acc_id)
        dest = _profile_dir(acc_id)
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        _log(f"已删除账号 {acc_id}")
        return {"ok": True}

    if action == "close_browsers":
        return _close_browsers_action()

    if action == "fetch_original":
        # 不走生成流程、不消耗额度：打开会话 → 官方接口解析无水印原片 → 落盘
        try:
            paths = _fetch_original_impl(data)
            return {"ok": True, "path": paths[0], "paths": list(paths)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e).replace("PLUGIN_ERROR:::", "")}

    if action == "list_pending":
        # 返回待补抓清单，供 UI 一键补抓无水印原片（不重新生成）
        return {"ok": True, "items": _load_pending()}

    if action == "reset_quota":
        acc_id = str(data.get("id") or "")
        store = _load_store()
        for acc in store["accounts"]:
            if acc["id"] == acc_id:
                acc["quota_exhausted_until"] = ""
                acc["cooldown_until"] = ""
                acc["fail_count"] = 0
                acc["last_error"] = ""
                _save_store(store)
                return {"ok": True, "account": _account_public(acc)}
        return {"ok": False, "error": "账号不存在"}

    if action == "login_account":
        acc_id = str(data.get("id") or "")
        store = _load_store()
        if not any(a["id"] == acc_id for a in store["accounts"]):
            return {"ok": False, "error": "账号不存在"}
        ok = _open_for_login(acc_id)
        store = _load_store()
        acc = next((a for a in store["accounts"] if a["id"] == acc_id), None)
        return {
            "ok": ok,
            "account": _account_public(acc) if acc else None,
            "error": None if ok else "登录超时，请重试",
        }

    if action == "open_browser":
        acc_id = str(data.get("id") or "")
        store = _load_store()
        if not store["accounts"]:
            _handle_action_impl("add_account", {"name": "账号1"})
            store = _load_store()
        if not acc_id:
            acc_id = store["accounts"][0]["id"]
        if not any(a["id"] == acc_id for a in store["accounts"]):
            return {"ok": False, "error": "账号不存在"}
        # 直接打开常驻窗口（不阻塞等登录），供人工查看/登录
        pw, ctx = _get_browser(acc_id, headless=False)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(_DOUBAO_HOME, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:120]}
        return {"ok": True, "message": f"账号 {acc_id} 浏览器窗口已打开并保留"}

    return {"ok": False, "error": f"未知动作: {action}"}


# ---------------------------------------------------------------- page flow

def _ensure_video_mode(page):
    try:
        btn = page.locator("button").filter(has_text="视频生成")
        if btn.count() > 0:
            btn.first.click(timeout=5000)
            page.wait_for_timeout(1500)
            _log("已点击「视频生成」进入视频模式")
            return True
    except Exception:  # noqa: BLE001
        pass
    _log("未找到视频入口按钮，直接用提示词触发视频生成")
    return False


def _normalize_key(text):
    return re.sub(r"[\s.\-·/]+", "", str(text or "").lower())


def _open_dropdown(page, label):
    """点开一个下拉触发（按钮/元素文本含 label，文本较短）。"""
    for base in ("button, [role='button']", "div, span"):
        loc = page.locator(base).filter(has_text=re.compile(re.escape(label)))
        n = loc.count()
        if n == 0:
            continue
        for i in range(min(n, 12)):
            try:
                t = (loc.nth(i).inner_text() or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if not t or len(t) > 24 or label not in t:
                continue
            try:
                loc.nth(i).click(timeout=2500)
                page.wait_for_timeout(600)
                _log(f"已点开「{label}」下拉")
                return True
            except Exception:  # noqa: BLE001
                continue
    return False


def _click_option(page, texts):
    for text in texts:
        if not text:
            continue
        try:
            loc = page.locator("button, [role='option'], span, div").filter(has_text=re.compile(re.escape(text)))
            if loc.count() > 0:
                loc.first.click(timeout=2500)
                page.wait_for_timeout(400)
                _log(f"已选择「{text}」")
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _current_dropdown_text(page, label):
    """读下拉触发器的当前文本（如「模型 / Seedance 2.0 Fast」）。"""
    for base in ("button, [role='button']", "div, span"):
        try:
            loc = page.locator(base).filter(has_text=re.compile(re.escape(label)))
            n = loc.count()
        except Exception:  # noqa: BLE001
            continue
        for i in range(min(n, 12)):
            try:
                t = (loc.nth(i).inner_text() or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if t and len(t) <= 24 and label in t and t != label:
                return t
    return None


def _iter_candidates(page, base):
    try:
        loc = page.locator(base)
        n = loc.count()
    except Exception:  # noqa: BLE001
        return []
    return [(loc, i) for i in range(min(n, 80))]


def _pick_option_from_open_menu(page, label, want):
    """在已展开的下拉里点选目标选项（第一行精确匹配，跳过触发器）。"""
    label_key = _normalize_key(label)
    want_keys = [w for w in want if len(w) >= 3]
    # 优先：真实选项。豆包模型选项是 DIV[role=menuitem]，inner_text 形如
    # "Seedance 2.0 Fast\n快速出片选择"（第一行为选项名）。兼容 role=option。
    for base in ("[role='menuitem']", "[role='option']", "[class*='option']"):
        for loc, i in _iter_candidates(page, base):
            try:
                t = (loc.nth(i).inner_text() or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if not t:
                continue
            first = t.split("\n")[0].strip()  # 只取第一行选项名
            key = _normalize_key(first)
            if key and (key in want or (want_keys and any(w in key for w in want_keys))):
                try:
                    loc.nth(i).click(timeout=2500)
                    page.wait_for_timeout(500)
                    _log(f"{label} → 「{first}」")
                    return True
                except Exception:  # noqa: BLE001
                    continue
    # 兜底：通用元素，但要跳过触发器本身（触发器文本形如 "模型 / Seedance 2.0 Fast"，
    # 归一化后同时含 label 和 want，会再点一次把展开的下拉关掉。选项不含 label 词）。
    for loc, i in _iter_candidates(page, "button, div, span"):
        try:
            t = (loc.nth(i).inner_text() or "").strip()
        except Exception:  # noqa: BLE001
            continue
        if not t or len(t) > 40:
            continue
        key = _normalize_key(t)
        if not key or key == label_key:
            continue
        if label_key and label_key in key:
            continue
        if key in want or (want_keys and any(w in key for w in want_keys)):
            try:
                loc.nth(i).click(timeout=2500)
                page.wait_for_timeout(500)
                _log(f"{label} → 「{t.strip()}」")
                return True
            except Exception:  # noqa: BLE001
                continue
    return False


def _select_dropdown(page, label, value, aliases=()):
    """点开 label 下拉并选 value（归一化匹配，含别名）。

    幂等：先读触发器当前文本，若已显示目标值则直接返回，避免多余点击与竞态。
    选择后校验触发器文本，失败则关闭重开重试一次。
    """
    value = str(value or "").strip()
    if not value or value.lower() in ("auto", "seedance"):
        return False
    want = {_normalize_key(v) for v in [value, *aliases] if v}
    if not want:
        return False
    # 子串匹配只用长度≥3 的目标键，避免裸数字（如时长 "10"）误匹配（"5" in "15"）。
    want_keys = [w for w in want if len(w) >= 3]
    # 幂等预检查：触发器已显示目标值（如「模型 / Seedance 2.0 Fast」）就直接跳过。
    cur = _current_dropdown_text(page, label)
    if cur is not None:
        cur_key = _normalize_key(cur)
        if any(w in cur_key for w in want_keys):
            _log(f"{label} 已是「{cur}」，跳过选择")
            return True
    for attempt in (1, 2):
        opened = _open_dropdown(page, label)
        if _pick_option_from_open_menu(page, label, want):
            # 选择后校验：触发器文本现在应含目标值。触发器不可读（如时长按钮是
            # "自动 · 10s" 不含 "时长" 词）时视为成功，不重试。
            page.wait_for_timeout(700)
            after = _current_dropdown_text(page, label)
            if after is None:
                _log(f"{label} 已选择（触发器不可读，视为成功）")
                return True
            after_key = _normalize_key(after)
            if any(w in after_key for w in want_keys):
                _log(f"{label} 已生效为「{after}」")
                return True
            _log(f"{label} 选择后校验未通过（当前「{after}」），重试")
        if opened:
            try:
                page.keyboard.press("Escape")
            except Exception:  # noqa: BLE001
                pass
            page.wait_for_timeout(300)
    # 兜底：直接点（兼容本来就是显式选项、无需展开的情况）
    return _click_option(page, [value, *aliases])


def _ratio_hint(ratio):
    """豆包网页端没有比例控件，比例通过提示词注入（模型默认横屏）。
    browser / manager 两个后端共用，避免两边映射漂移。"""
    return {
        "9:16": "竖屏 9:16 画面",
        "16:9": "横屏 16:9 画面",
        "1:1": "1:1 方形画面",
        "4:3": "4:3 画面",
        "3:4": "3:4 竖构图画面",
    }.get(str(ratio or "").strip(), "")


_PICK_EXACT_JS = r"""
(value) => {
  const norm = (s) => String(s || '').replace(/\s+/g, '');
  const want = norm(value);
  if (!want) return false;
  const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  let hit = null;
  for (const el of document.querySelectorAll('div,span,li,button,[role="menuitem"],[role="option"]')) {
    if (!vis(el)) continue;
    if (norm(el.innerText || el.textContent || '') !== want) continue;
    let leaf = true;
    for (const c of el.children) { if (norm(c.innerText || '') === want) { leaf = false; break; } }
    if (leaf) hit = el;
  }
  if (!hit) return false;
  hit.click();
  return true;
}
"""


def _pick_exact(page, value):
    """在当前可见元素里找「文本恰好等于 value」的叶子元素并点击。
    只做精确匹配，避免点到包含全部选项的容器行。"""
    try:
        return bool(page.evaluate(_PICK_EXACT_JS, value))
    except Exception:
        return False


_SETTINGS_TRIGGER_RE = re.compile(r"^[^·]{1,14}·\d+s$")

_PANEL_OPEN_JS = r"""(() => !!document.querySelector('[data-slot="slider-track"]'))()"""

_TRIGGER_TEXT_JS = r"""
(() => {
  for (const el of document.querySelectorAll('button, [role="button"], div, span, li')) {
    const r = el.getBoundingClientRect();
    if (!(r.width > 0 && r.height > 0)) continue;
    const t = (el.innerText || '').trim().replace(/\s+/g, '');
    if (/^[^·]{1,14}·\d+s$/.test(t)) return t;
  }
  return '';
})()
"""


def _read_settings_trigger(page):
    """读「自动 · 10s」触发器当前文本（含比例·时长两个当前值）。"""
    try:
        return str(page.evaluate(_TRIGGER_TEXT_JS) or "")
    except Exception:
        return ""


def _open_settings_panel(page):
    """点开「自动 · 10s」画面比例/时长设置面板。

    面板是开关型（再点触发器会关上），所以点击后必须校验真的开了；
    没开（点关了）就再点一次，最多重试 3 轮。
    触发器用「·数字s」文本过滤定位——侧边栏按钮过百，按索引扫描既慢又会漏。
    2026-09-17：manager webview 里触发器可能不带 button/role（侧边栏收起、
    自定义组件），只搜 button 会「未找到设置面板」→ 时长/比例全丢；
    这里 button 搜不到时兜底搜全部可见元素。
    """
    for base in ("button, [role='button']", "div, span, li"):
        loc = page.locator(base).filter(has_text=re.compile(r"·\s*\d+s"))
        n = min(loc.count(), 20)
        for _ in range(3):
            for i in range(n):
                try:
                    t = re.sub(r"\s+", "", loc.nth(i).inner_text() or "")
                except Exception:
                    continue
                if not t or not _SETTINGS_TRIGGER_RE.match(t):
                    continue
                try:
                    loc.nth(i).click(timeout=2500)
                    page.wait_for_timeout(700)
                    try:
                        if page.evaluate(_PANEL_OPEN_JS):
                            return True
                    except Exception:
                        pass
                except Exception:
                    continue
    return False


_FOCUS_SLIDER_JS = r"""
(() => {
  const norm = (s) => String(s || '').replace(/\s+/g, '');
  let section = null;
  for (const sec2 of document.querySelectorAll('section, div')) {
    const r = sec2.getBoundingClientRect();
    if (!(r.width > 0 && r.height > 0)) continue;
    const t2 = norm(sec2.innerText || '');
    if (t2.startsWith('时长') && t2.length <= 24 && sec2.querySelector('[data-slot="slider-track"]')) { section = sec2; break; }
  }
  if (!section) return {ok: false, reason: 'no-section'};
  const thumb = section.querySelector('[data-slot="slider-thumb"], [role="slider"]');
  if (!thumb) return {ok: false, reason: 'no-thumb'};
  thumb.focus();
  return {ok: document.activeElement === thumb};
})()
"""


def _set_duration_slider(page, secs):
    """把时长滑杆调到目标秒数，返回实际生效的秒数（失败返回 None）。

    豆包时长是 Radix 连续滑杆（约 4~15s，每格 1s），点轨道/点数字标签都不响应，
    只能：聚焦 thumb 后按方向键步进，每步 ±1s。触发器文字实时显示当前值，
    所以每步后读触发器校准，直到等于目标或不再变化。
    """
    try:
        target = int(round(float(str(secs).strip())))
    except Exception:
        return None
    try:
        info = page.evaluate(_FOCUS_SLIDER_JS)
    except Exception:
        return None
    if not (isinstance(info, dict) and info.get("ok")):
        return None
    final = None
    for _ in range(16):
        m = re.search(r"·(\d+)s$", _read_settings_trigger(page))
        if not m:
            break
        cur = int(m.group(1))
        if cur == target:
            final = cur
            break
        try:
            page.keyboard.press("ArrowRight" if target > cur else "ArrowLeft")
        except Exception:
            break
        page.wait_for_timeout(300)
    if final is None:
        m = re.search(r"·(\d+)s$", _read_settings_trigger(page))
        if m:
            final = int(m.group(1))
    return final


def _detect_prompt_duration(prompt):
    """从提示词里检测时长要求（时长参数为 auto 时的兑底）。
    优先认「时长/持续/长度 xx 秒」，兜底认任意「xx 秒」。返回秒数或 None。"""
    if not prompt:
        return None
    m = re.search(r"(?:时长|持续|长度)[^。；;\n]{0,8}?(\d{1,2})(?:\.\d+)?\s*(?:秒|s\b)", prompt)
    if not m:
        m = re.search(r"(\d{1,2})(?:\.\d+)?\s*秒", prompt)
    if not m:
        return None
    try:
        v = float(m.group(1))
    except Exception:
        return None
    if v <= 0 or v > 60:
        return None
    return int(v)


def _apply_video_settings(page, duration, ratio, prompt=""):
    """在「自动 · 10s」设置面板里设置时长和画面比例。

    新版豆包（2026-09 探针确认）该面板含两行设置：
      画面比例: 自动 3:4 4:3 9:16 16:9 1:1 21:9（药丸选项，直接点）
      时长:     Radix 连续滑杆，约 4~15s（聚焦后按方向键步进）
    返回实际生效情况 {"duration": bool, "ratio": bool}——面板打不开/滑杆不可用
    时对应项为 False，调用方必须把没生效的项注入提示词，否则用户显式选择的
    时长会完全丢失（2026-09-17 manager 通道实测：两个账号面板都打不开，
    选的时长静默丢失）。
    """
    d = str(duration or "").strip()
    r = str(ratio or "").strip()
    if (not d or d.lower() == "auto") and prompt:
        detected = _detect_prompt_duration(prompt)
        if detected:
            _log(f"时长参数为 auto：从提示词检测到要求约 {detected} 秒，改用滑杆设置")
            d = str(detected)
    need_d = bool(d) and d.lower() != "auto"
    need_r = bool(r) and r.lower() != "auto"
    if not need_d and not need_r:
        return {"duration": True, "ratio": True}
    ok_d = not need_d
    ok_r = not need_r
    if not _open_settings_panel(page):
        _log("警示: 未找到「自动 · 10s」设置面板（豆包 UI 可能改版）——本次时长/比例"
             "走提示词兜底（显式时长会自动注入提示词，不会丢失）；若需精确控制，"
             "请在插件设置里把时长选为固定秒数并更新插件")
        return {"duration": ok_d, "ratio": ok_r}
    page.wait_for_timeout(600)
    if need_d:
        got = _set_duration_slider(page, d)
        if got is None:
            _log(f"警示: 时长滑杆不可用，未能设为 {d}s（保留提示词注入兜底）")
        elif got == int(round(float(d))):
            _log(f"视频时长 → 「{got}s」")
            ok_d = True
        else:
            _log(f"警示: 时长滑杆停在「{got}s」（要求 {d}s，面板档位限制）")
            ok_d = True  # 面板已生效到档位上限，不再注入
    if need_r:
        if _pick_exact(page, r):
            page.wait_for_timeout(400)
            _log(f"画面比例 → 「{r}」")
            ok_r = True
        else:
            _log(f"画面比例：面板中未找到「{r}」（保留提示词注入兜底）")
    try:
        page.keyboard.press("Escape")
    except Exception:  # noqa: BLE001
        pass
    page.wait_for_timeout(300)
    return {"duration": ok_d, "ratio": ok_r}


def _apply_options(page, duration, ratio, model, prompt=""):
    """应用模型/时长/比例。返回 {"model": bool, "duration": bool, "ratio": bool}
    表示各项是否真正生效——调用方据此把没生效的项注入提示词兜底。"""
    model = str(model or "").strip()
    model_ok = True
    if model and model.lower() not in ("auto", "seedance"):
        model_alias = {
            "seedance2.0fast": ["Seedance 2.0 Fast"],
            "seedance2.0mini": ["Seedance 2.0 Mini"],
            "seedance2.5": ["Seedance 2.5", "Dreamina Seedance 2.5"],
        }.get(model, [])
        model_ok = bool(_select_dropdown(page, "模型", model, model_alias))
    # 时长+比例共用「自动 · 10s」设置面板（面板内含两行选项）
    sett = _apply_video_settings(page, duration, ratio, prompt=prompt)
    return {"model": model_ok,
            "duration": bool(sett.get("duration", True)),
            "ratio": bool(sett.get("ratio", True))}


def _collect_reference_paths(context, plugin_params):
    """字字动画约定：首帧、尾帧、参考图片MAP，去重后最多 9 张。"""
    if not plugin_params.get("enable_reference_image", True):
        return []
    refs = context.get("reference_images") or {}
    if refs and "参考图片MAP" not in refs:
        if all(isinstance(k, int) or (isinstance(k, str) and str(k).isdigit()) for k in refs.keys()):
            refs = {"参考图片MAP": dict(refs)}
    ordered = []
    for key in ("first_frame_path", "end_frame_path"):
        val = context.get(key)
        if val:
            ordered.append(val)
    if isinstance(refs, dict):
        for key in ("首帧", "尾帧"):
            val = refs.get(key)
            if val:
                ordered.append(val)
        ref_map = refs.get("参考图片MAP") or {}
        if isinstance(ref_map, dict):
            def _map_key(k):
                try:
                    return int(k)
                except Exception:
                    return str(k)
            for _, val in sorted(ref_map.items(), key=lambda kv: _map_key(kv[0])):
                if val:
                    ordered.append(val)
        for val in refs.values():
            if isinstance(val, str):
                ordered.append(val)
    seen = set()
    paths = []
    for raw in ordered:
        path = str(raw).strip().strip('"')
        if not path or path in seen:
            continue
        if not os.path.isfile(path):
            _log(f"参考图不存在，已跳过: {path}")
            continue
        ext = Path(path).suffix.lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"):
            _log(f"参考图格式不支持，已跳过: {path}")
            continue
        seen.add(path)
        paths.append(path)
        if len(paths) >= 9:
            break
    return paths


def _upload_reference_images(page, paths):
    if not paths:
        return
    _log(f"准备上传参考图 {len(paths)} 张")
    uploaded = 0
    for path in paths:
        ok = False
        try:
            file_inputs = page.locator("input[type='file']")
            n = file_inputs.count()
            if n > 0:
                file_inputs.nth(n - 1).set_input_files(path)
                ok = True
        except Exception as e:  # noqa: BLE001
            _log(f"直接填 file input 失败: {e}")
        if not ok:
            clicked = False
            for label in ("上传", "图片", "添加图片", "附件"):
                loc = page.get_by_role("button", name=re.compile(label))
                if loc.count() == 0:
                    loc = page.locator("button, [role='button']").filter(has_text=re.compile(label))
                if loc.count() == 0:
                    continue
                try:
                    with page.expect_file_chooser(timeout=4000) as fc_info:
                        loc.first.click(timeout=3000)
                    fc_info.value.set_files(path)
                    ok = True
                    clicked = True
                    break
                except Exception:  # noqa: BLE001
                    continue
            if not clicked and not ok:
                _log(f"未找到上传入口，跳过: {path}")
                continue
        if ok:
            uploaded += 1
            _log(f"已上传参考图 {uploaded}/{len(paths)}: {Path(path).name}")
            page.wait_for_timeout(1500)
    if uploaded == 0:
        raise Exception("PLUGIN_ERROR:::未能把参考图上传到豆包输入框，请确认页面已进入视频生成")
    page.wait_for_timeout(800)


def _fill_prompt(page, prompt, timeout_s=30):
    """只把提示词填进编辑器（先清空旧内容再写入），不点发送。

    填和发送必须拆开：manager 后端（webview shim）需要在发送前校验
    「编辑器里真的是这条提示词」，旧版一气呵成发出去才发现写错，为时已晚。
    timeout_s：主流程提交保持 30s；自动补发/确认传短超时——输入框被弹窗
    遮挡时不能把 4 秒一轮的等待循环堵成 30 秒一步（实测连堵 38 次×30s）。
    """
    ms = int(timeout_s * 1000)
    # manager 通道：先做输入区体检。输入框未就绪（visibility:hidden / pointer-events:none）
    # 时 focus() 不生效、insertText 无处可落（2026-09-17 实测：insertText/execCommand/
    # 逐字按键全部落空），这里尽早就把原因写进日志，别退化成一堆重试后报「内容为空」。
    try:
        if hasattr(page, "input_health"):
            health = page.input_health() or {}
            if health.get("found") and not health.get("ready"):
                _log("警示: 豆包输入区未就绪(%s)，将尝试唤醒等待"
                     % (health.get("blockedBy") or ("pe=%s vis=%s" % (health.get("pe"), health.get("vis")))))
    except Exception:  # noqa: BLE001
        pass
    editor = page.locator("[contenteditable='true']").first
    if editor.count() == 0:
        editor = page.locator("textarea").first
    editor.wait_for(state="visible", timeout=ms)
    editor.click(timeout=ms)
    # 先把输入框里已有的内容彻底清空，再写入提示词。
    # 豆包视频模式切进来后，输入框常被自动塞入「生成视频」之类的默认文本/草稿；
    # 不清空的话，fill 失败时走 insert_text 只会把内容追加在后，发出去就变成
    # 「生成视频 + 真实内容」，豆包按字面先出/连带出这一步无关画面。
    try:
        editor.fill("")
    except Exception:  # noqa: BLE001
        try:
            editor.press("Control+A")
            editor.press("Delete")
        except Exception:  # noqa: BLE001
            pass
    page.wait_for_timeout(120)
    try:
        editor.fill(prompt, timeout=ms)
    except Exception as e:  # noqa: BLE001
        # ⚠️ 这里绝不能无条件吞掉异常再退到 `page.keyboard.insert_text`：
        # 那是「不做任何校验的裸写」，会把上面那条明确原因（输入区未就绪/
        # 编辑器拒收提示词）替换成后面统一的「编辑器未含提示词」，真实根因就此丢失。
        if "输入区不可用" in str(e):
            raise
        try:
            page.keyboard.insert_text(prompt)
        except Exception:  # noqa: BLE001
            raise e
    page.wait_for_timeout(500)


def _press_send(page, timeout_s=10):
    """点发送按钮。成功返回 True；按钮不存在/不可点返回 False 或抛错。"""
    send = page.locator("#flow-end-msg-send")
    if send.count() == 0:
        send = page.get_by_role("button").filter(has_text=re.compile("发送|生成"))
    send.first.click(timeout=int(timeout_s * 1000))
    return True


def _fill_prompt_and_send(page, prompt, timeout_s=30):
    _fill_prompt(page, prompt, timeout_s=timeout_s)
    _press_send(page, timeout_s=timeout_s)
    _log("已提交生成请求")


_ORIGINAL_URL_JS = r"""
async (args) => {
  const dbg = {paths: {}};
  // args.min_ts：本次任务提交时间（秒），用于 recent 模式下只认「提交之后创建」的节点。
  // args.scope：'chat' = 补抓指定会话，严格只认当前打开的会话、禁用全局列表回退；
  //             'recent' = 生成流程内解析刚生成的片，允许回退到账号全局最近/创作列表。
  const minTs = Number((args && args.min_ts) || 0);
  const scope = (args && args.scope) || 'recent';
  dbg.scope = scope;
  // 提交前页面上已存在的视频地址（baseline）。常驻窗口连续/跨天复用时，页面卡片里还挂着
  // 昨天/上一段的旧片；凡命中这些地址的候选都是旧片，必须丢弃，绝不能回退成他们。
  const skipUrls = new Set((args && args.skip_urls) || []);
  // 串片硬防线（节点级）：提交前页面上/账号里的创作节点 ID。基线只收集 <video> 是远远不够的
  // ——豆包成片卡片不点播放根本不挂 <video>，基线常为空；必须把「提交前就存在的节点」按
  // node_id 记下来，解析时逐个剔除，否则账号创作列表里的旧分镜会排到最新被当成本次成片。
  const skipNodeIds = new Set(((args && args.skip_node_ids) || []).map(String));
  // strict_recent：出片解析严格化。无时间戳的节点一律不收（以前是「保留交给排序兜底」→
  // 实测就是这里把账号里更早的旧片放进来当成本次成片）。
  const strictRecent = !!(args && args.strict_recent);
  // ts_window：提交时间的回溯窗口（秒），只容忍节点时钟偏差，默认 60。
  const tsWindow = Number((args && args.ts_window) || 0);
  // no_wm_only：只认真·无水印源。免费号页面播放流现在普遍是
  // logo_type=video_gen_watermark_dyn（动态水印），「出片探测」这类调用绝不能把
  // 带水印的卡片最高画质链当作出片结果返回，置 true 时禁用 wmFallback 回退。
  const noWmOnly = !!(args && args.no_wm_only);
  const keyPath = (u) => { try { return String(u || '').split('?')[0]; } catch (e) { return ''; } };
  const isSkipped = (u) => !!keyPath(u) && skipUrls.has(keyPath(u));
  const getApi = (path) => {
    if (/^https?:\/\//i.test(path)) return path;
    try {
      const entries = (performance.getEntriesByType && performance.getEntriesByType('resource')) || [];
      for (const entry of entries.slice().reverse()) {
        const url = String(entry.name || '');
        if (url.includes(path)) return url;
      }
      const sam = entries.slice().reverse().find(e => /\/samantha\//.test(String(e.name || '')));
      if (sam && sam.name) { const p = new URL(sam.name); p.pathname = path; return p.href; }
    } catch (e) {}
    return path;
  };
  const post = async (url, body) => {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 8000);
    try {
      const resp = await fetch(getApi(url), {
        method: 'POST',
        headers: {accept: 'application/json', 'content-type': 'application/json', 'agw-js-conv': 'str', origin: location.origin, referer: location.href},
        credentials: 'include',
        body: JSON.stringify(body || {}),
        signal: ctrl.signal
      });
      clearTimeout(timer);
      return await resp.json();
    } catch (e) {
      clearTimeout(timer);
      throw e;
    }
  };
  const uniqueBy = (arr, fn, limit) => {
    const seen = new Set(), out = [];
    for (const item of arr || []) {
      const key = fn(item);
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(item);
      if (limit && out.length >= limit) break;
    }
    return out;
  };
  const isVideoNode = (n) => !!n && (Number(n.nodeType) === 6 || Number(n.type) === 6 || /^v\d/i.test(String(n.key || '')) || /\.mp4$/i.test(String(n.name || '')) || /video|创作|生成|视频/i.test(String(n.type || '')));
  // 节点创建时间：豆包各接口字段名不统一，这里宽松匹配后统一成「秒」。
  // 只认 create 类时间；update_time/modify 若作为唯一时间字段才兜底用。
  // 关键：绝不能把 update_time 当「创建时间」用——老节点在轮询/翻页/读创作空间时会刷新 update_time，
  // 一旦误用，上一分镜的旧片会被误判成「最新」，导致后续分镜抓到的原片串成第一段。
  const CREATE_TIME_RE = /(^|_)(create|created|creation|gmt_?create|ctime)(_|$)|^create$/i;
  const UPDATE_TIME_RE = /(^|_)(update|modify|gmt_?modify)(_|$)|\btimestamp\b/i;
  const computeTime = (value, re) => {
    if (!value || typeof value !== 'object') return 0;
    for (const k of Object.keys(value)) {
      if (!re.test(k)) continue;
      const v = value[k];
      if (typeof v === 'number' || (typeof v === 'string' && /^\d{9,13}$/.test(String(v)))) {
        let n = Number(v);
        if (n > 1e12) n = Math.floor(n / 1000);
        return n;
      }
    }
    return 0;
  };
  const nodeTime = (value) => computeTime(value, CREATE_TIME_RE) || computeTime(value, UPDATE_TIME_RE) || 0;
  // 只取「本次提交之后创建」的节点，并按创建时间从新到旧排，保证命中的是本段分镜的成片。
  // 多分镜连续生成时，上一段分镜的节点对这一段来说是「旧片」，必须用 min_ts 严格排除。
  const pickVideos = (nodes) => {
    let videos = nodes.filter(isVideoNode);
    // 第一道硬防线：提交前就存在的节点，一个都不要（否则旧分镜会排到最新被当成成片）
    if (skipNodeIds.size) videos = videos.filter(v => !skipNodeIds.has(String(v.id)));
    if (minTs) {
      // 宽松窗口只为容忍节点时钟偏差（创建时间可能比提交时间晚一点）；
      // 只需把明显早于本次提交的旧分镜节点剔除。多分镜连续生成时若窗口过大，
      // 紧邻的上一段分镜会被误当成「本次成片」而串片，故默认收紧为 60s 且按 ts 从新到旧排。
      const win = tsWindow > 0 ? tsWindow : 60;
      const t0 = minTs - win;
      const fresh = videos.filter(v => {
        if (!v.ts) return !strictRecent;   // 严格模式：拿不到时间戳的节点不收（宁缺毋滥）
        return v.ts >= t0;                 // 只认本次提交前后创建的节点
      });
      // 严格模式下过滤结果为空也必须认空——以前「为空就回退全量」正是串片入口
      videos = (fresh.length || !strictRecent) ? fresh : [];
    }
    return videos.sort((a, b) => (Number(b.ts) || 0) - (Number(a.ts) || 0));
  };
  const isConfirmedNoWatermark = (url) => {
    const text = String(url || '').toLowerCase();
    if (!text) return false;
    // 官方出片接口明确标记的无水印流（get_without_watermark 的 download_url 即此类）
    if (/lr=unwatermarked|logo_type=unwatermarked/.test(text)) return true;
    try {
      const parsed = new URL(url, location.href);
      if (/(^|[.-])videoweb-download\.doubao\.com$/i.test(parsed.hostname) && 'true' === parsed.searchParams.get('download')) return true;
      if (/(^|[.-])videoweb\.doubao\.com$/i.test(parsed.hostname) && 'true' === parsed.searchParams.get('download')) return true;
      if (parsed.hostname.includes('doubao.com') && parsed.pathname.includes('/download') && 'true' === parsed.searchParams.get('download')) return true;
    } catch (e) {}
    return /download=true/.test(text) && /video_mp4|mime_type=video_mp4/.test(text);
  };
  let __scan = 0;
  const collectCreationNodes = (value, out, seen, depth) => {
    if (depth > 10 || !value || typeof value !== 'object' || seen.has(value)) return;
    seen.add(value);
    if (++__scan > 20000) return;  // 硬上限，防止会话节点过多时 evaluate 被拖死（之前卡几分钟）
    const id = value.id != null ? String(value.id) : '';
    const key = value.key != null ? String(value.key) : '';
    const name = value.name != null ? String(value.name) : '';
    if (/^\d{8,}$/.test(id) && (key || name || value.node_type != null || value.type != null)) {
      out.push({id, key, name, nodeType: Number(value.node_type || value.nodeType || 0), type: value.type != null ? value.type : (value.nodeType != null ? value.nodeType : undefined), ts: nodeTime(value), msgId: value.message_id || value.messageId || '', convId: value.conversation_id || value.conversationId || ''});
    }
    if (Array.isArray(value)) value.forEach(item => collectCreationNodes(item, out, seen, depth + 1));
    else {
      const vals = Object.values(value);
      for (const child of vals.slice(0, 160)) if (child && typeof child === 'object') collectCreationNodes(child, out, seen, depth + 1);
    }
  };
  const collectNodeIds = (value, out, seen, depth) => {
    if (depth > 8 || value === null || typeof value !== 'object' || seen.has(value)) return;
    seen.add(value);
    const direct = value.node_id || value.nodeId || value.node_id_str || value.nodeIdStr;
    if (/^\d{8,}$/.test(String(direct || ''))) out.push(String(direct));
    if (Array.isArray(value)) value.forEach(item => collectNodeIds(item, out, seen, depth + 1));
    else {
      for (const [key, child] of Object.entries(value)) {
        const text = (typeof child === 'string' || typeof child === 'number') ? String(child) : '';
        if (text && text.length <= 180 && /(^|_|\b)(node_id|nodeid)(_|$|\b)/i.test(key)) out.push(text.trim());
        if (child && typeof child === 'object') collectNodeIds(child, out, seen, depth + 1);
      }
    }
  };

  const nodeIds = [];
  // 兜底候选：chat 卡片 fiber 里 douyin vas 的 videoModel（含各清晰度 main_url，base64）。
  // 免费额度生成的视频带平台动态水印，get_download_info 对其返回 null（无创作空间节点可查），
  // 此时只能回退到卡片里这份最高画质直链，整条补抓链路才不至于空手而归。
  //
  // 【串片硬防线】prefer_urls = Python 侧刚抓到的本次 <video> src。兜底链必须只在
  // 「挂着本次新片的那张卡片」内部提取 videoModel：
  //   聊天流 DOM 里历史卡片排在最前，而 records 是按 DOM 顺序拍平的 —— 老实现取
  //   「全页第一个 videoModel」，命中必然是上一段分镜/上一次生成的最高画质链，
  //   这就是「下载到的永远是上一个视频」的根因（2026-09-17 实测：页面上同时挂着
  //   上一段 7d77ca** 两个清晰度 + 本次 7d77cd** 两个清晰度，老实现稳定取到 7d77ca**）。
  //   同一张卡片内的 videoModel 与 <video> 必然是同一个视频，故按卡片归属提取才可靠。
  const preferKeys = new Set(((args && args.prefer_urls) || []).map(keyPath).filter(Boolean));
  let wmFallback = null;
  // 本次成片的视频 vid（v0 开头）：路径 D 官方去水印接口 get_without_watermark 的入参。
  let nowmVids = [];

  // 路径 A: chat 页 video → React fiber → message_id → message_node_info → node_id
  try {
    const vids = Array.from(document.querySelectorAll('video'));
    dbg.paths.a = {vids: vids.length};
    const anchors = [];
    const anchorSrcs = [];   // 与 anchors 一一对应：该卡片里 <video> 的地址（判「是不是本次新片卡片」）
    for (const v of vids) {
      anchors.push(v.closest('[class*="message"],[class*="card"],[class*="item"],[class*="chat"]') || v);
      anchorSrcs.push(String(v.currentSrc || v.src || ''));
    }
    if (!anchors.length) {
      // 视频卡片常懒渲染/在视口外，不能按「可见」过滤，否则会被漏掉；
      // 只要求是有子节点的 message 容器，取其末尾若干条向上爬 fiber 收集视频节点。
      const cards = Array.from(document.querySelectorAll('[class*="message" i]'))
        .filter(el => el && el.children && el.children.length);
      dbg.paths.a.cards = cards.length;
      anchors.push(...cards.slice(-60));
    }
    if (anchors.length) {
      const records = [];
      const newCardRecords = [];   // 仅「本次新片卡片」贡献的 records（兜底链只从这里取）
      const nodesA = [];
      let newCardHits = 0;
      const collectRecordLike = (value, out, seenSet, depth) => {
        if (!value || typeof value !== 'object' || depth > 5 || seenSet.has(value)) return;
        seenSet.add(value);
        const keys = Object.keys(value);
        if (/video|media|creation|item|work|asset|download|origin|original|play|url/.test(keys.join(' ').toLowerCase())) out.push(value);
        for (const key of keys.slice(0, 80)) {
          const child = value[key];
          if (child && typeof child === 'object') collectRecordLike(child, out, seenSet, depth + 1);
        }
      };
      for (let ai = 0; ai < anchors.length; ai++) {
        const card = anchors[ai];
        const isNewCard = !!preferKeys.size && preferKeys.has(keyPath(anchorSrcs[ai] || ''));
        if (isNewCard) newCardHits++;
        const localRecords = [];
        let cur = card;
        while (cur && cur !== document.documentElement) {
          const fk = Object.keys(cur).find(k => k.startsWith('__reactFiber') || k.startsWith('__reactProps'));
          let fiber = fk ? cur[fk] : null;
          let guard = 0;
          while (fiber && guard < 80) {
            guard++;
            const props = fiber.memoizedProps || fiber.pendingProps || fiber;
            collectRecordLike(props, localRecords, new WeakSet(), 0);
            // 增强：直接从该卡片 fiber 子树收集视频创作节点，绕开 message_id 中转，
            // 即使 <video> 未渲染或字段名不同也能定位（仍限定在当前会话页面内）。
            collectCreationNodes(props, nodesA, new WeakSet(), 0);
            fiber = fiber.return;
          }
          cur = cur.parentElement;
        }
        records.push(...localRecords);
        if (isNewCard) newCardRecords.push(...localRecords);
      }
      dbg.paths.a.newCardHits = newCardHits;
      dbg.paths.a.newCardRecords = newCardRecords.length;
      const messageIds = [];
      const collectMessageIds = (value, out, seenSet, depth) => {
        if (depth > 8 || value === null || typeof value !== 'object' || seenSet.has(value)) return;
        seenSet.add(value);
        const direct = value.message_id || value.messageId || value.message_id_str || value.messageIdStr;
        if (/^\d{8,}$/.test(String(direct || ''))) out.push(String(direct));
        for (const [key, child] of Object.entries(value)) {
          const text = (typeof child === 'string' || typeof child === 'number') ? String(child) : '';
          if (text && text.length <= 180 && /(^|_|\b)(message_id|messageid|msg_id|msgid|creation_task_id|creationtaskid|task_id|taskid)(_|$|\b)/i.test(key)) out.push(text.trim());
          if (child && typeof child === 'object') collectMessageIds(child, out, seenSet, depth + 1);
        }
      };
      for (const rec of records) collectMessageIds(rec, messageIds, new WeakSet(), 0);
      // 大 id = 新消息，补抓历史会话时优先命中最近一条
      const ids = [...new Set(messageIds.filter(id => /^\d{8,}$/.test(id)))]
        .sort((x, y) => (x.length !== y.length ? y.length - x.length : (y > x ? 1 : -1)))
        .slice(0, 30);
      dbg.paths.a.messageIds = ids.length;
      // 精准：路径 A 不依赖 message_node_info 网络中转（该请求在 chat 页常因登录态/CORS 卡死 20s，
      // 且本质是绕道账号全局，不符合「只认当前会话」的精准要求）。纯靠 fiber 收集的视频节点定位。
      // 增强：把直接从 fiber 收集到的视频节点也作为候选（不依赖 message_node_info 中转）
      // 按创建时间从新到旧，优先命中本次/最近生成的视频。
      // 必须用 min_ts 过滤：常驻窗口在同一会话里连续生成多段分镜时，当前页 fiber 会同时残留
      // 上一段分镜的视频节点；若不过滤，这些「旧片」会被当成候选、在 get_download_info 阶段优先命中，
      // 造成第一段分镜之后全部抓成上一段的原片。
      const videosA = pickVideos(nodesA);
      dbg.paths.a.fiberVideos = videosA.length;
      dbg.paths.a.nodesACount = nodesA.length;
      const dumpNode = (n) => {
        const ks = Object.keys(n || {});
        return {
          id: String(n.id != null ? n.id : '').slice(0, 20),
          key: String(n.key != null ? n.key : '').slice(0, 16),
          name: String(n.name != null ? n.name : '').slice(0, 16),
          type: n.type != null ? String(n.type).slice(0, 20) : (n.nodeType != null ? String(n.nodeType).slice(0, 12) : ''),
          urlKeys: ks.filter(k => /url|src|mp4|download|media/i.test(k)).map(k => k + '=' + String(n[k] != null ? n[k] : '').slice(0, 40)).slice(0, 3)
        };
      };
      dbg.paths.a.samples = nodesA.slice(0, 6).map(dumpNode);
      // 精准嗅探：统计 nodesA 的 type 分布，并找出含媒体/url 字段的节点（视频节点大概率在其中）
      const typeCounts = {};
      const mediaNodes = [];
      for (const n of nodesA) {
        const t = n.type != null ? String(n.type) : (n.nodeType != null ? String(n.nodeType) : 'None');
        typeCounts[t] = (typeCounts[t] || 0) + 1;
        const ks = Object.keys(n);
        const isMedia = ks.some(k => /url|mp4|media|video|download|asset|cover|src/i.test(k)) ||
                        /mp4|视频|video|创作|生成|原图|原片/i.test(String(n.name || '') + ' ' + String(n.key || ''));
        if (isMedia && mediaNodes.length < 10) mediaNodes.push(dumpNode(n));
      }
      dbg.paths.a.typeCounts = typeCounts;
      dbg.paths.a.mediaNodes = mediaNodes;
      for (const v of videosA) nodeIds.push(v.id);
      // 从 records 里提取 videoModel → 解码最高画质 main_url（优先 h264，兼容性最好）
      try {
        const pickWmFrom = (list) => {
          for (const rec of list) {
            if (!rec || typeof rec.videoModel !== 'string' || !rec.videoModel.includes('video_list')) continue;
            let model = null;
            try { model = JSON.parse(rec.videoModel); } catch (e) { continue; }
            const vl = Object.values((model && model.video_list) || {});
            if (!vl.length) continue;
            const best = vl.find(v => v && v.codec_type === 'h264') || vl[0];
            const b64 = String((best && best.main_url) || '');
            if (!b64) continue;
            let url = '';
            try { url = atob(b64); } catch (e) { continue; }
            // 命中「提交前已存在的旧片地址」一律丢弃
            if (/^https?:\/\//.test(url) && !isSkipped(url)) return url;
          }
          return null;
        };
        // 1) 首选：只在「挂着本次新片 <video> 的那张卡片」里找 videoModel —— 同卡片 ⇒ 同一个视频。
        if (newCardRecords.length) wmFallback = pickWmFrom(newCardRecords);
        // 2) 没传 prefer_urls（补抓等旧调用路径）时，才允许退回全页 records（沿用老行为）。
        //    传了 prefer_urls 却在新卡片里找不到 ⇒ 直接放弃兜底，宁缺毋滥：退回全页
        //    就等于把上一段分镜的最高画质链当成成片（「抓上一个」的入口）。
        if (!wmFallback && !preferKeys.size) wmFallback = pickWmFrom(records);
        // 3) vid 提取与兜底链同规矩：传了 prefer_urls 就只认「本次新片卡片」里的 vid，
        //    否则官方去水印接口可能把上一段的片子洗成无水印抓回来（串片换皮重现）。
        const vidRe = /\bv0[0-9a-z]{22,}\b/gi;
        const vidSrcList = (preferKeys.size && newCardRecords.length) ? newCardRecords
                        : (preferKeys.size ? [] : records);
        const vidsSeen = new Set();
        for (const rec of vidSrcList) {
          const mm = String((rec && (rec.videoModel || rec.play_info || rec.playInfo)) || '').matchAll(vidRe);
          for (const m of mm) vidsSeen.add(m[0]);
          const directVid = rec && (rec.vid || rec.video_id || rec.videoId);
          if (directVid && /^v0[0-9a-z]{22,}$/i.test(String(directVid))) vidsSeen.add(String(directVid));
        }
        nowmVids = [...vidsSeen].slice(0, 6);
        dbg.paths.a.nowmVids = nowmVids.length;
      } catch (e) {}
      dbg.paths.a.wmFallback = !!wmFallback;
    }
  } catch (e) { dbg.paths.a.error = String(e); }

  // 路径 C: node_lastest_used → 最新用过的视频节点（最可能是刚生成的那个）。
  // chat 模式也启用：视频 node_id 在豆包全局创作 store / 最近使用接口里，不在聊天 DOM fiber 中
  // （路径 A 对多数会话拿不到视频节点），故 chat 模式同样走此路取最近视频，优先取最新的几个。
  __scan = 0;  // 重置扫描预算：路径 A 的 fiber 爬取会耗光全局计数，导致后续路径 collect 全空
  try {
    const payloads = [{nodeType: 6, size: 50}, {node_type: 6, size: 50}];
    const nodes = [];
    for (const payload of payloads) {
      try {
        const info = await post('/samantha/aispace/node_lastest_used', payload);
        collectCreationNodes(info.data || info, nodes, new WeakSet(), 0);
      } catch (e) {}
    }
    const videos = pickVideos(nodes);
    dbg.paths.c = {videos: videos.length};
    for (const v of videos.slice(0, 3)) nodeIds.push(v.id);
  } catch (e) { dbg.paths.c.error = String(e); }

  // 路径 B: homepage → 「我的创作」根节点 → node_info 分页 → 视频节点。
  // 生成(recent)模式：按 min_ts 取最新节点，无水印解析的主力路径（实测对创作空间真实节点
  // get_download_info 稳定返回 videoweb-download.doubao.com 无水印直链，而聊天卡片里的
  // creation_task_id 不是 aispace 节点 id，查询只会得到 data:null）。
  // 补抓(chat)模式：同样启用，但严格按 conversation_id === 会话 ID 过滤，一个都不多收，
  // 避免抓到账号全局创作列表里其他会话的视频。
  const chatConvId = String((args && args.conv_id) || '');
  __scan = 0;  // 重置扫描预算（同路径 C）
  try {
    const home = await post('/samantha/aispace/homepage', {});
    const homeNodes = [];
    collectCreationNodes(home.data || home, homeNodes, new WeakSet(), 0);
    const root = homeNodes.find(n => /我的创作|我的作品/.test(String(n.name || '')))
      || homeNodes.find(n => n.nodeType === 1 && n.key === n.id)
      || homeNodes[0];
    dbg.paths.b = {homeNodes: homeNodes.length, root: root ? root.id : null};
    if (root && root.id) {
      const info = await post('/samantha/aispace/node_info', {
        node_id: root.id, need_full_path: true,
        sort_param: {need_sort_config: true, sort_order: 1, sort_type: 0},
        size: 50
      });
      const nodes = [];
      collectCreationNodes(info.data || info, nodes, new WeakSet(), 0);
      let videos = pickVideos(nodes);
      if (chatConvId) {
        // 会话硬过滤：只收当前会话的节点。生成模式带上页面会话 ID 后同样过滤，
        // 从源头掐掉「抓到账号里其它会话旧片」这条串片路径。
        const convVideos = videos.filter(v => String(v.convId || '') === chatConvId);
        dbg.paths.b.convVideos = convVideos.length;
        // 严格会话过滤：conversation_id 不命中的节点一个都不收，宁缺毋滥
        videos = convVideos;
      }
      dbg.paths.b.videos = videos.length;
      for (const v of videos) nodeIds.push(v.id);
    }
  } catch (e) { dbg.paths.b.error = String(e); }

  // 路径 D: 官方去水印接口 get_without_watermark（2026-09-17 抓包证实：对 Seedance
  // 成片返回 data.download_video[vid].download_url，lr=unwatermarked 无水印直链；
  // 而免费号的页面播放链是 video_gen_watermark_dyn 动态水印，不再是无水印源）。
  // 请求体字段名未见于抓包，两种候选形态依次试，以响应+正向校验为准，不会误放行。
  if (nowmVids.length) {
    dbg.paths.d = {vids: nowmVids.length};
    const bodies = [{vid_list: nowmVids}, {vids: nowmVids}];
    for (let bi = 0; bi < bodies.length; bi++) {
      try {
        const json = await post('/creativity/resource/get_without_watermark', bodies[bi]);
        const map = (json && json.data && json.data.download_video) || {};
        dbg.paths.d['t' + bi] = {code: json && json.code, hits: Object.keys(map).length};
        for (const vid of Object.keys(map)) {
          let du = String(map[vid].download_url || map[vid].downloadUrl || '');
          if (du && !/^https?:/i.test(du)) { try { du = atob(du); } catch (e) { continue; } }
          if (du && !isSkipped(du) && isConfirmedNoWatermark(du)) {
            return {ok: true, url: du, vid, nodeId: null, wm: false, dbg};
          }
        }
      } catch (e) { dbg.paths.d['t' + bi] = {err: String(e).slice(0, 80)}; }
    }
  }

  // 对候选 node_id 并行尝试 get_download_info，正向校验无水印（并发避免串行逐个请求卡成几分钟）
  const uniqueIds = uniqueBy(nodeIds.filter(id => /^\d{8,}$/.test(String(id)) && !skipNodeIds.has(String(id))), String, 30);
  dbg.totalCandidates = uniqueIds.length;
  const tryOne = async (nodeId) => {
    try {
      const json = await post('/samantha/aispace/get_download_info', {requests: [{node_id: nodeId}]});
      const info0 = (json && json.data && json.data.download_infos && json.data.download_infos[0]) || {};
      const mainUrl = info0.main_url || info0.mainUrl || '';
      if (!dbg.paths.a.diagCodes) dbg.paths.a.diagCodes = [];
      if (dbg.paths.a.diagCodes.length < 4) dbg.paths.a.diagCodes.push({id: String(nodeId).slice(0,12), code: json && json.code, hasUrl: !!mainUrl, wm: mainUrl ? !isConfirmedNoWatermark(mainUrl) : null});
      if (json && json.code === 0 && mainUrl && isConfirmedNoWatermark(mainUrl)) {
        return {ok: true, url: mainUrl, nodeId};
      }
    } catch (e) {
      if (!dbg.paths.a.diagCodes) dbg.paths.a.diagCodes = [];
      if (dbg.paths.a.diagCodes.length < 4) dbg.paths.a.diagCodes.push({id: String(nodeId).slice(0,12), err: String(e).slice(0,60)});
    }
    return null;
  };
  const results = await Promise.all(uniqueIds.slice(0, 12).map(tryOne));
  for (const r of results) {
    if (r && r.ok) return {ok: true, url: r.url, nodeId: r.nodeId, dbg};
  }
  // 无水印直链拿不到时，回退页面卡片里的最高画质（免费额度生成自带平台动态水印，无法避开）；
  // no_wm_only（出片探测等）下不回退——带水印链不是「出片」证据，返回它会掩盖真实进度。
  if (wmFallback && !noWmOnly) return {ok: true, url: wmFallback, nodeId: null, wm: true, dbg};
  return {ok: false, dbg};
}
"""


def _resolve_original_video_url(page, attempts=3, delay_s=1.5, min_ts=0, scope="recent",
                                dbg_out=None, conv_id="", baseline_urls=None,
                                skip_node_ids=None, strict_recent=False, ts_window=0,
                                prefer_urls=None, meta_out=None, no_wm_only=False):
    """在豆包页面上下文里解析无水印原片 URL。

    多路径回退：chat fiber→message_node_info / homepage→node_info / node_lastest_used
    → get_download_info，并对 main_url 做正向无水印校验。创作节点可能延迟就绪，
    故多次尝试并间隔等待。
      - scope="recent"：生成流程内解析刚生成的片，min_ts 用于只认「提交后创建」的节点，
        并允许回退到账号全局最近/创作列表（找不到本次成片时兜底）。
      - scope="chat"：补抓指定会话的原片，严格只认当前打开的会话（路径 A），
        禁用全局列表回退，避免抓到账号其他会话的视频。
      - baseline_urls：本次提交前页面上已存在的视频地址（常驻窗口复用时会有昨天的旧片）。
        解析结果里凡是命中这些旧地址的 URL 一律弃用——它们只能是旧片（含页面卡片回退的
        最高画质直链，那很可能就是昨天/上一段的视频），防止抓回旧片。
      - skip_node_ids：本次提交前页面上/账号里的创作节点 ID（节点级基线）。豆包成片卡片不点
        播放就不挂 <video>，baseline_urls 常为空；节点级基线才是「旧片真的排不到最新」的硬防线。
      - strict_recent：严格模式。拿不到创建时间的节点直接不收，且时间过滤结果为空也不回退全量
        （旧行为「为空回退全量」正是把账号里旧分镜当成本次成片的入口）。
      - ts_window：提交时间回溯窗口（秒），只用于容忍节点时钟偏差，默认 60。
      - prefer_urls：本次刚抓到的成片 <video> src（Python 侧已知）。带水印兜底链
        （wmFallback）**只允许从「挂着这些地址的那张卡片」里取 videoModel**；传了它却
        在新卡片里找不到就放弃兜底。不传则退回旧的全页行为（补抓路径仍这么用）。
      - meta_out：可选 dict，成功时回填 {"wm": bool, "nodeId": str}。调用方可据此判断
        拿到的到底是无水印原片（wm=False）还是带平台水印的页面卡片兜底链（wm=True）——
        2026-09-19 实测免费号 <video> 播放链同样是动态水印，wm=True 时两条都不能当原片。
      - no_wm_only：只认真·无水印源（get_without_watermark / get_download_info 校验链），
        禁用带水印卡片链回退。出片探测用它：带水印链被当成「出片」会让上层直接落水印片。
    """
    # 取片前先解冻页面：manager 通道的 webview 若在后台被 Chromium 冻结，JS 不执行，
    # 下面那发 awaitPromise 型 evaluate 会直接挂满超时再失败（表现「成片已出、
    # 插件迟迟取不回原片」）。
    # 用 keep_awake 而不是 activate：只解冻、不发 Page.bringToFront，
    # 不抢管理器当前显示的账号卡片（取片会被反复调用，抢焦点会让界面一直跳）。
    # Playwright 的 page 没有这些方法，hasattr 判断后自动跳过，browser 通道不变。
    try:
        if hasattr(page, "keep_awake"):
            page.keep_awake()
        elif hasattr(page, "activate"):
            page.activate()
    except Exception:  # noqa: BLE001
        pass
    min_ts = int(min_ts or 0)
    skip_nodes = sorted({str(x) for x in (skip_node_ids or []) if str(x).strip()})
    baseline = set()
    for u in (baseline_urls or []):
        try:
            baseline.add(str(u).split("?")[0])
        except Exception:  # noqa: BLE001
            pass
    last_dbg = None
    last_err = None
    for i in range(attempts):
        try:
            # 先确认页面不在导航中，避免 evaluate 期间 execution context 被销毁
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:  # noqa: BLE001
                pass
            result = page.evaluate("(" + _ORIGINAL_URL_JS + ")",
                                   {"min_ts": min_ts, "scope": scope, "conv_id": conv_id,
                                    "skip_urls": sorted(baseline),
                                    "skip_node_ids": skip_nodes,
                                    "strict_recent": bool(strict_recent),
                                    "ts_window": int(ts_window or 0),
                                    "no_wm_only": bool(no_wm_only),
                                    "prefer_urls": [str(u) for u in (prefer_urls or []) if u]})
            if isinstance(result, dict) and result.get("ok"):
                url = result.get("url") or ""
                # 旧片防线：任何命中「提交前已在页面上的视频」的 URL 都直接按旧片丢弃，
                # 否则常驻窗口会把昨天的片当成今天的无水印原片抓回来。
                if url and url.split("?")[0] in baseline:
                    _log(f"解析到的是提交前已存在的旧片地址，弃用: {url[:90]}")
                    last_dbg = {"stale": True, "ok": False}
                    continue
                if meta_out is not None:
                    try:
                        meta_out.clear()
                        meta_out.update({"wm": bool(result.get("wm")), "nodeId": result.get("nodeId")})
                    except Exception:  # noqa: BLE001
                        pass
                if result.get("wm"):
                    dg = result.get("dbg") or {}
                    pa = (dg.get("paths") or {}).get("a") or {}
                    pd = (dg.get("paths") or {}).get("d") or {}
                    _log(f"无水印直链不可得，回退页面最高画质（含平台动态水印）: {url[:90]}"
                         f" | 节点候选={dg.get('totalCandidates')}"
                         f" vid候选={pa.get('nowmVids')}"
                         f" 去水印接口={pd}"
                         f" download_info={pa.get('diagCodes')}")
                else:
                    _log(f"已解析到无水印原片地址: {url[:90]} | nodeId={result.get('nodeId')} 会话候选数={ (result.get('dbg') or {}).get('totalCandidates') }")
                return url
            last_dbg = result
        except Exception as e:  # noqa: BLE001
            last_err = str(e)[:240]
            _log(f"原片解析异常(第{i + 1}次): {last_err}")
        if i < attempts - 1:
            # 导航导致上下文销毁时，先等导航真正结束再重试（否则会连环崩溃）
            try:
                page.wait_for_load_state("load", timeout=15000)
            except Exception:  # noqa: BLE001
                pass
            page.wait_for_timeout(int(delay_s * 1000))
    if isinstance(last_dbg, dict) and isinstance(last_dbg.get("dbg"), dict):
        dbg = last_dbg["dbg"]
        a = (dbg.get("paths") or {}).get("a", {})
        b = (dbg.get("paths") or {}).get("b", {})
        typeCounts = a.get("typeCounts") or {}
        mediaNodes = a.get("mediaNodes") or []
        media_str = [f"{m.get('id')}|{m.get('type')}|{m.get('name')}|{m.get('urlKeys')}" for m in mediaNodes][:5]
        b_str = (f"homeNodes={b.get('homeNodes')} root={b.get('root')} videos={b.get('videos')} "
                 f"convVideos={b.get('convVideos')} err={str(b.get('error') or '')[:60]}")
        summary = (f"scope={dbg.get('scope')} vids={a.get('vids')} cards={a.get('cards')} "
                   f"fiberVideos={a.get('fiberVideos')} nodesA={a.get('nodesACount')} msgIds={a.get('messageIds')} "
                   f"nodeIds={a.get('nodeIds')} total={dbg.get('totalCandidates')} "
                   f"typeCounts={typeCounts} media={media_str} pathB[{b_str}]")
        _log(f"未解析到无水印原片: {summary}")
    else:
        _log(f"未解析到无水印原片（多次尝试失败）: {str(last_dbg)[:200]}")
    if dbg_out is not None:
        if isinstance(last_dbg, dict) and isinstance(last_dbg.get("dbg"), dict):
            dbg = last_dbg["dbg"]
            a = (dbg.get("paths") or {}).get("a", {})
            b = (dbg.get("paths") or {}).get("b", {})
            typeCounts = a.get("typeCounts") or {}
            mediaNodes = a.get("mediaNodes") or []
            media_str = [f"{m.get('id')}|{m.get('type')}|{m.get('name')}|{m.get('urlKeys')}" for m in mediaNodes][:5]
            b_str = (f"homeNodes={b.get('homeNodes')} root={b.get('root')} videos={b.get('videos')} "
                     f"convVideos={b.get('convVideos')} err={str(b.get('error') or '')[:60]}")
            dbg_out["summary"] = (f"scope={dbg.get('scope')} vids={a.get('vids')} cards={a.get('cards')} "
                                  f"fiberVideos={a.get('fiberVideos')} nodesA={a.get('nodesACount')} msgIds={a.get('messageIds')} "
                                  f"nodeIds={a.get('nodeIds')} total={dbg.get('totalCandidates')} "
                                  f"typeCounts={typeCounts} media={media_str} pathB[{b_str}]")
        else:
            dbg_out["summary"] = f"EVAL_ERROR: {last_err}  last_dbg={str(last_dbg)[:80]}"
    return None


_TRIGGER_PLAY_JS = r"""
async (args) => {
  // 成片卡片不点播放就不挂 <video>、也不发 get_play_info——这是以前「要人手动
  // 点一下播放才抓得到」的根源。这里替用户做这一下：
  //   1) 优先从最近消息卡片的 fiber 里挖 vid，直接调 get_play_info 拿可播地址；
  //   2) 挖不到就定位最新成片卡片，返回其中心坐标，由 Python 发真实鼠标点击
  //      （合成 el.click() 对豆包部分浮层无效，probe_v17 已验证真实点击可触发）。
  const dbg = {cards: 0, vid: null, from: null, clicked: null, note: ''};
  const skipUrls = new Set((args && args.skip_urls) || []);
  const keyPath = (u) => { try { return String(u || '').split('?')[0]; } catch (e) { return ''; } };
  const getApi = (path) => {
    if (/^https?:\/\//i.test(path)) return path;
    try {
      const entries = (performance.getEntriesByType && performance.getEntriesByType('resource')) || [];
      for (const entry of entries.slice().reverse()) {
        const url = String(entry.name || '');
        if (url.includes(path)) return url;
      }
      const sam = entries.slice().reverse().find(e => /\/samantha\//.test(String(e.name || '')));
      if (sam && sam.name) { const p = new URL(sam.name); p.pathname = path; return p.href; }
    } catch (e) {}
    return path;
  };
  const post = async (url, body) => {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 8000);
    try {
      const resp = await fetch(getApi(url), {
        method: 'POST',
        headers: {accept: 'application/json', 'content-type': 'application/json', 'agw-js-conv': 'str', origin: location.origin, referer: location.href},
        credentials: 'include',
        body: JSON.stringify(body || {}),
        signal: ctrl.signal
      });
      clearTimeout(timer);
      return await resp.json();
    } catch (e) {
      clearTimeout(timer);
      throw e;
    }
  };

  // ---- 1) 最近消息卡片 fiber 里挖 vid / videoModel 直链 ----
  // 硬预算：豆包聊天页的 React fiber 树非常大，无上限扫描会把主线程卡住
  // 几分钟甚至拖崩渲染进程（_ORIGINAL_URL_JS 的 __scan 上限就是为这个加的）。
  const VID_RE = /^v0[0-9a-z]{12,40}$/;
  let vid = null, directUrl = null;
  const t0 = performance.now();
  const TIME_BUDGET_MS = 500;
  let budget = 4000;   // 全局对象访问上限
  const over = () => budget <= 0 || performance.now() - t0 > TIME_BUDGET_MS;
  const seenObj = new WeakSet();
  const scanForVideo = (value, depth) => {
    if (over() || !value || typeof value !== 'object' || seenObj.has(value) || depth > 5) return;
    budget -= 1;
    seenObj.add(value);
    for (const key of Object.keys(value).slice(0, 60)) {
      const v = value[key];
      if (typeof v === 'string') {
        if (!vid && VID_RE.test(v) && /vid|video/i.test(key)) vid = v;
        else if (!directUrl && /^https?:\/\//.test(v) && /douyinvod|video_mp4/i.test(v) && !skipUrls.has(keyPath(v))) directUrl = v;
      } else if (v && typeof v === 'object') {
        if (!directUrl && typeof v.videoModel === 'string' && v.videoModel.includes('video_list')) {
          try {
            const model = JSON.parse(v.videoModel);
            const list = Object.values((model && model.video_list) || {});
            const best = list.find(x => x && x.codec_type === 'h264') || list[0];
            const b64 = String((best && best.main_url) || '');
            const url = b64 ? atob(b64) : '';
            if (/^https?:\/\//.test(url) && !skipUrls.has(keyPath(url))) directUrl = url;
          } catch (e) {}
        }
        scanForVideo(v, depth + 1);
      }
      if ((vid && directUrl) || over()) return;
    }
  };
  try {
    const cards = Array.from(document.querySelectorAll('[class*="message" i]'))
      .filter(el => el && el.children && el.children.length);
    dbg.cards = cards.length;
    // 从最新一条消息往回扫（cards 是 DOM 顺序 = 旧→新）：vid / directUrl 都是「先命中即定」，
    // 若按旧→新扫，上一段分镜卡片会先被命中，播放触发与直链就都落到上一个视频上。
    for (const card of cards.slice(-3).reverse()) {
      let cur = card;
      let guard = 0;
      while (cur && cur !== document.documentElement && guard < 16 && !over()) {
        guard++;
        const fk = Object.keys(cur).find(k => k.startsWith('__reactFiber') || k.startsWith('__reactProps'));
        if (fk) {
          let fiber = cur[fk];
          let g2 = 0;
          while (fiber && g2 < 20 && !over()) {
            g2++;
            scanForVideo(fiber.memoizedProps || fiber.pendingProps || fiber, 0);
            fiber = fiber.return;
          }
        }
        cur = cur.parentElement;
      }
      if ((vid && directUrl) || over()) break;
    }
    dbg.scanned = 4000 - budget;
    if (over()) dbg.note = (dbg.note ? dbg.note + ';' : '') + 'budget-exhausted';
  } catch (e) { dbg.note = 'fiber:' + String(e).slice(0, 60); }

  if (vid) {
    try {
      const j = await post('/samantha/video/get_play_info', {vid: vid});
      const pi = (j && j.data && j.data.play_infos) || [];
      const hit = pi.find(p => p && p.main) || null;
      const u = hit ? String(hit.main || '') : '';
      if (u && !skipUrls.has(keyPath(u))) { dbg.from = 'play_info'; return {ok: true, url: u, vid: vid, dbg: dbg}; }
    } catch (e) { dbg.note = 'play_info:' + String(e).slice(0, 60); }
  }
  if (directUrl) { dbg.from = 'video_model'; return {ok: true, url: directUrl, vid: vid, dbg: dbg}; }

  // ---- 2) 找最新成片卡片，返回中心坐标（Python 发真实点击）----
  let target = null, area = 0;
  const cards2 = Array.from(document.querySelectorAll('[class*="message" i]'))
    .filter(el => el && el.children && el.children.length);
  for (let i = cards2.length - 1; i >= 0 && !target; i--) {
    const card = cards2[i];
    const txt = String(card.innerText || '');
    if (!/视频|生成|好了|播放/.test(txt)) continue;   // 只对含成片语义的消息动手
    const media = card.querySelectorAll('img, video, [style*="background-image"], [class*="video" i]:not(video)');
    for (const m of media) {
      const r = m.getBoundingClientRect();
      if (r.width < 140 || r.height < 100) continue;  // 头像/图标级小块不算
      if (r.width * r.height > area) { area = r.width * r.height; target = m; }
    }
  }
  if (!target) {
    // 兜底：消息容器选择器在改版后失效时，全页找「最靠下的大媒体块」
    // （成片卡片是聊天区里最大的图；头像/图标被尺寸过滤，顶栏/输入框区域排除）
    const vh = window.innerHeight;
    let bottomMost = -1;
    const all = document.querySelectorAll('img, [style*="background-image"], [class*="video" i]:not(video)');
    for (const m of all) {
      const r = m.getBoundingClientRect();
      if (r.width < 140 || r.height < 100 || r.height > vh * 0.7) continue;
      if (r.top < 60 || r.bottom > vh * 0.86) continue;
      if (r.bottom > bottomMost) { bottomMost = r.bottom; target = m; }
    }
    if (target) dbg.note = (dbg.note ? dbg.note + ';' : '') + 'fallback-global';
  }
  if (target) {
    try {
      target.scrollIntoView({block: 'center'});
      await new Promise((resolve) => setTimeout(resolve, 600));
    } catch (e) {}
    const r = target.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) {
      dbg.clicked = {x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2)};
    }
  }
  return {ok: false, card: dbg.clicked, vid: vid, dbg: dbg};
}
"""


def _trigger_video_play(page, baseline=None):
    """替用户「点一下播放」：成片卡片不播放就没有任何可抓信号（懒挂载）。

    1) 页面内优先从卡片 fiber 挖 vid，直接调 get_play_info 拿可播地址（不动 UI）；
    2) 挖不到就定位最新成片卡片，由这里发一次真实鼠标点击——播放开始后
       <video> 挂载、get_play_info 请求出现，网络/DOM 信号在后续轮询周期自然命中。
    页面已关闭的异常向上抛（由等待循环统一按换号处理），其余异常交调用方忽略。
    """
    skip = sorted({str(u).split("?")[0] for u in (baseline or set()) if u})
    res = page.evaluate(_TRIGGER_PLAY_JS, {"skip_urls": skip})
    if not isinstance(res, dict):
        return None
    dbg = res.get("dbg") or {}
    url = str(res.get("url") or "")
    if url and url.split("?")[0] not in set(skip):
        _log(f"已从卡片数据直接解析到成片播放地址 (via {dbg.get('from')})")
        return url
    card = res.get("card") or {}
    x, y = card.get("x"), card.get("y")
    if isinstance(x, (int, float)) and isinstance(y, (int, float)):
        page.mouse.click(float(x), float(y))
        _log(f"已主动点击成片卡片触发播放 ({int(x)},{int(y)})")
    else:
        _log(f"暂未找到可触发的成片卡片（cards={dbg.get('cards')}, note={dbg.get('note') or '-'}）")
    return None


# ------------------------------------------------------- 补抓已生成会话的原片
# 场景：视频早就生成好了（页面里点过、或上一次流水线只落了带水印片），
# 现在只想把「这个会话」的无水印原片重新拉下来。生成流程走 generate，
# 这里只做「按会话链接取回原片」，不消耗任何生成额度。
def _chat_url_from(value):
    """把粘贴的会话链接/纯会话 ID 归一化成完整 chat URL。"""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.isdigit():
        return f"{_DOUBAO_HOME}{raw}"
    m = re.search(r"/chat/(\d{6,})", raw)
    if m:
        return f"{_DOUBAO_HOME}{m.group(1)}"
    return raw if raw.lower().startswith("http") else ""


# ------------------------------------------------------- 待补抓清单
# 生成流程里若一时拿不到无水印原片，会先保底落带水印片并记下会话链接，
# 之后用「补抓原片」功能一键取回无水印版本（不重新生成、不消耗额度）。
_PENDING_FILE = plugin_dir / "pending_nowm.json"
_pending_lock = threading.Lock()


def _load_pending():
    if not _PENDING_FILE.exists():
        return []
    try:
        data = json.loads(_PENDING_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def _save_pending(items):
    items = items[-200:]
    with _pending_lock:
        tmp = str(_PENDING_FILE) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(_PENDING_FILE))


def _add_pending(entry):
    items = _load_pending()
    items = [it for it in items
             if not (str(it.get("chat_url") or "") == str(entry.get("chat_url") or "")
                     and str(it.get("file") or "") == str(entry.get("file") or ""))]
    items.append(entry)
    _save_pending(items)
    _log(f"已记入待补抓清单: {entry.get('chat_url')} -> {entry.get('file')}")


def _drop_pending(chat_url):
    chat_url = str(chat_url or "")
    if not chat_url:
        return
    items = _load_pending()
    left = [it for it in items if str(it.get("chat_url") or "") != chat_url]
    if len(left) != len(items):
        _save_pending(left)
        _log(f"已从待补抓清单移除: {chat_url}")


def _download_to(url, output_dir, cookies, filename, verify_header, retries, replace_existing=False):
    """下载；若目标文件已存在且 replace_existing，则先写临时名再原子替换，避免覆盖中断损坏原片。"""
    out = Path(output_dir)
    real = out / filename
    if replace_existing and real.exists():
        tmp_name = filename + ".nowm_tmp.mp4"
        tmp_path = _download(url, str(out), cookies, tmp_name, verify_header, retries)
        os.replace(tmp_path, str(real))
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        return str(real)
    return _download(url, output_dir, cookies, filename, verify_header, retries)


def _pick_page(ctx, url_marker=""):
    """挑一个可用的页签：优先已打开目标会话的，跳过已关闭的残留页。

    常驻窗口被复用很久后，ctx.pages 里可能留着已关闭的页签，直接拿 pages[0]
    会在 goto/wait 时抛 "Target page ... has been closed"。
    """
    fallback = None
    for p in list(getattr(ctx, "pages", []) or []):
        try:
            if p.is_closed():
                continue
        except Exception:  # noqa: BLE001
            continue
        try:
            if url_marker and url_marker in (p.url or ""):
                return p
        except Exception:  # noqa: BLE001
            pass
        if fallback is None:
            fallback = p
    return fallback or ctx.new_page()


def _open_chat_page(ctx, chat_url, wait_s=4):
    """打开会话页并尽量触发视频卡片渲染（卡片常懒加载）。"""
    page = _pick_page(ctx, chat_url)
    try:
        page.bring_to_front()
    except Exception:  # noqa: BLE001
        pass
    try:
        page.goto(chat_url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:  # noqa: BLE001
        _log(f"打开会话页异常: {str(e)[:100]}")
        if page.is_closed():
            page = ctx.new_page()
            page.goto(chat_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(int(wait_s) * 1000)
    # 仅在没有视频渲染时才滚动触发懒加载，最多 3 次、每次 1.2s
    for _ in range(3):
        try:
            if page.locator("video").count():
                break
            page.mouse.wheel(0, 12000)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(1200)
    return page


def _fetch_original_with_account(acc, chat_url, output_dir, filename, plugin_params,
                                 progress_callback=None, min_ts=0):
    """用某个账号的常驻窗口打开会话并取回无水印原片；失败返回 None。"""
    name = acc.get("name") or acc["id"]
    if progress_callback:
        progress_callback(f"账号 {name}：打开会话", 20)
    pw, ctx = _get_browser(acc["id"], headless=bool(plugin_params.get("headless", False)))
    page = _open_chat_page(ctx, chat_url)
    if not _looks_logged_in(page, ctx):
        raise NeedLogin(f"账号 {name} 登录态已失效")
    # 补抓场景该会话必有视频卡片，但消息由 WS 驱动 + 虚拟列表懒渲染，冷启动可能 >20s
    # 才挂载 <video>；持续滚到消息底部直到卡片出现（生成流程不走这里，不受影响）。
    _wait_deadline = time.time() + 30
    while time.time() < _wait_deadline:
        try:
            if page.locator("video").count():
                break
            page.evaluate(
                "() => { const c = document.querySelector('[class*=\"message-list\" i]') ||"
                " document.querySelector('[class*=\"scroll\" i]');"
                " if (c) c.scrollTop = c.scrollHeight;"
                " window.scrollTo(0, document.body.scrollHeight); }"
            )
            page.mouse.wheel(0, 8000)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(1500)
    # 滚到底也没等到 <video>：成片卡片不播放就不挂载，主动替用户点一下播放
    try:
        if not page.locator("video").count():
            _trigger_video_play(page)
            page.wait_for_timeout(2500)
    except Exception as e:  # noqa: BLE001
        if _PAGE_CLOSED_RE.search(str(e)):
            raise
        _log(f"补抓触发播放失败（已忽略）: {str(e)[:100]}")
    if progress_callback:
        progress_callback("解析无水印原片", 60)
    dbg_holder = {}
    # 会话 ID：创作空间视频节点带 conversation_id，严格过滤只认本会话的节点
    m_conv = re.search(r"/chat/(\d{6,})", chat_url or "")
    conv_id = m_conv.group(1) if m_conv else ""
    url = _resolve_original_video_url(page, attempts=4, delay_s=3, scope="chat", min_ts=int(min_ts or 0),
                                      conv_id=conv_id, dbg_out=dbg_holder)
    if not url:
        raise Exception(f"该账号下没有这个会话的原片（诊断: {dbg_holder.get('summary', '')}）")
    if progress_callback:
        progress_callback("下载原片", 85)
    cookies = {c["name"]: c["value"] for c in ctx.cookies() if "doubao" in c.get("domain", "")}
    # 覆盖式下载：若目标文件已存在（通常是上一次落下的带水印片），先写临时名再原子替换，
    # 既拿到无水印原片又不破坏字字动画那边已引用的路径。
    return _download_to(
        url, output_dir, cookies, filename,
        bool(plugin_params.get("verify_mp4_header", True)),
        int(plugin_params.get("download_retries", 3) or 3),
        replace_existing=True,
    )


def _fetch_original_impl(context):
    """按会话链接补抓无水印原片，按字字动画约定落盘，返回 [path]。"""
    _ensure_legacy_migrated()
    chat_url = _chat_url_from(context.get("chat_url") or context.get("chat_id") or context.get("url"))
    # 若该会话来自「生成时未能取回无水印、保底落带水印」的待补抓记录，
    # 复用当时的提交时间戳作为 min_ts，只认「本次生成」的视频节点，
    # 避开「生成视频发早了」那次更早创建的干扰视频。
    pending_item = next((it for it in _load_pending()
                         if str(it.get("chat_url") or "") == chat_url), None)
    min_ts = int(pending_item.get("submitted_at") or 0) if pending_item else 0
    if pending_item:
        _log(f"补抓沿用生成提交时间 min_ts={min_ts}，以锁定本次生成的视频")
    if not chat_url:
        raise Exception("PLUGIN_ERROR:::请填写豆包会话链接或会话 ID")
    output_dir = context.get("output_dir") or context.get("project_path") or str(plugin_dir / "downloads")
    plugin_params = {**get_params(), **(context.get("plugin_params") or {})}
    progress_callback = context.get("progress_callback")
    filename = context.get("filename") or _output_filename(context)
    account_id = str(context.get("account_id") or context.get("id") or "").strip()

    store = _load_store()
    accounts = [a for a in store["accounts"] if a.get("logged_in")]
    if not accounts:
        raise Exception("PLUGIN_ERROR:::没有已登录的豆包账号")
    if account_id:
        picked = [a for a in accounts if a["id"] == account_id]
        if not picked:
            raise Exception(f"PLUGIN_ERROR:::账号 {account_id} 不存在或未登录")
        accounts = picked

    errors = []
    for acc in accounts:
        name = acc.get("name") or acc["id"]
        try:
            path = _run_on_account(
                acc["id"], _fetch_original_with_account,
                acc, chat_url, output_dir, filename, plugin_params, progress_callback,
                min_ts=min_ts,
            )
            if path:
                _log(f"补抓成功：{path}")
                _drop_pending(chat_url)  # 已补到无水印原片，移出待补抓清单
                return [path]
            errors.append(f"{name}: 该账号下没有这个会话的原片")
        except NeedLogin as e:
            _mark_used(acc["id"], logged_in=False)
            errors.append(f"{name}: {str(e)[:80]}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {str(e)[:300]}")
    raise Exception("PLUGIN_ERROR:::未取回该会话的无水印原片。" + "；".join(errors))


def _latest_assistant_text(page):
    """读取页面里可能含状态提示的文本（优先读消息/弹层，再读全页正文）。

    额度不足/生成失败提示可能在正文里（不在 toast/modal），因此正文要读足够长
    并优先挑出含额度/积分/会员/失败关键词的片段，避免被截断漏判。
    """
    candidates = []
    try:
        texts = page.eval_on_selector_all(
            "[class*='message'], [class*='toast'], [class*='modal'], [role='alert'], [role='dialog']",
            """els => els.slice(-10).map(e => (e.innerText || '').trim()).filter(t => t && t.length < 800)""",
        )
        candidates.extend(texts)
    except Exception:  # noqa: BLE001
        pass
    try:
        body = page.inner_text("body")
        # 正文里优先挑含状态关键词的片段
        body = body or ""
        for kw in ("额度", "积分", "会员", "余额", "次数", "上限", "恢复", "免费", "生成失败", "失败", "违规"):
            idx = body.find(kw)
            if idx >= 0:
                candidates.append(body[max(0, idx - 60):idx + 160])
        candidates.append(body[:4000])
    except Exception:  # noqa: BLE001
        pass
    # 去重合并，优先返回含关键词的
    joined = "\n".join(candidates)
    return joined[:6000]


def _scan_page_status(page):
    text = _latest_assistant_text(page)
    if not text:
        return None
    # 先判「额度耗尽」——这是硬性失败，必须优先（否则文案里同时含
    # 「额度用完」和「继续生成」时，会被下面的付费继续分支误放行）
    m = _QUOTA_BLOCK_RE.search(text)
    if m:
        return ("quota", m.group(0)[:40])
    # 仅当没有额度耗尽信号时，才考虑「免费用完改走付费/积分」→ 继续等
    if _QUOTA_PAID_CONTINUE_RE.search(text) and not (
        "无法生成" in text or "不能生成" in text or "暂不能生成" in text
    ):
        return None
    m = _FAIL_RE.search(text)
    if m:
        return ("fail", m.group(0)[:40])
    return None


# ---------------------------------------------------------------- 自动接管
# 无人值守核心：豆包在出片前后可能弹出需要人工点一下的界面（二次确认、会员付费、
# 合规提示、营销弹窗），或只回一段文字问「是否确认」。以前这些都会让任务卡到超时。
# 这里用一次页面快照把「检测 + 代点」做完，Python 侧只负责结果分类与补发话术。
_AUTOPILOT_JS = r"""
(cfg) => {
  const norm = (s) => String(s || '').replace(/\s+/g, '');
  const vis = (el) => !!(el && el.getClientRects && el.getClientRects().length && el.offsetParent !== null);
  const bodyText = (document.body && document.body.innerText) || '';
  const tail = bodyText.slice(-4000);
  const running = new RegExp(cfg.running, 'i').test(tail);
  // 出片信号提前计算：词表外的弹窗（unknown_dialog）也要用它兜底——
  // 否则一个识别不了的浮层会把「已出片」信号整个挡住，一直空等到超时
  const base = cfg.baseline || [];
  const freshVid = Array.from(document.querySelectorAll('video')).some((v) => {
    const s = String(v.currentSrc || v.src || '').split('?')[0];
    return !!s && base.indexOf(s) < 0;
  });
  const doneMatch = new RegExp(cfg.done, 'i').test(tail);

  if (new RegExp(cfg.captcha, 'i').test(tail)) {
    return {action: 'captcha', detail: '页面出现人机验证', running: running, tail: tail.slice(-600)};
  }

  const sels = [
    "[role='dialog']",
    "[class*='modal' i]", "[class*='Modal' i]",
    "[class*='popup' i]", "[class*='Popup' i]",
    "[class*='dialog' i]", "[class*='Dialog' i]",
    "[class*='drawer' i]", "[class*='Drawer' i]"
  ];
  let dlg = null, best = 0;
  for (const sel of sels) {
    let list = [];
    try { list = Array.from(document.querySelectorAll(sel)); } catch (e) { continue; }
    for (const el of list) {
      if (!vis(el)) continue;
      const cls = String(el.className || '');
      if (cls && new RegExp(cfg.notDialog, 'i').test(cls)) continue;
      const w = el.offsetWidth || 0, h = el.offsetHeight || 0;
      if (w < 160 || h < 80) continue;
      if (w * h > best) { best = w * h; dlg = el; }
    }
    if (dlg) break;
  }

  if (dlg) {
    const dlgText = (dlg.innerText || '').slice(0, 2000);
    const btns = Array.from(dlg.querySelectorAll('button,[role="button"],a[class*="btn" i]')).filter(vis);
    const normed = btns.map(b => ({el: b, t: norm(b.innerText || b.getAttribute('aria-label') || '')}));

    if (new RegExp(cfg.captcha, 'i').test(dlgText)) {
      return {action: 'captcha', detail: dlgText.slice(0, 80), running: running, tail: dlgText};
    }
    if (new RegExp(cfg.login, 'i').test(dlgText)) {
      return {action: 'login', detail: dlgText.slice(0, 80), running: running, tail: dlgText};
    }
    // 危险操作弹窗：只点「取消/关闭」，绝不点确认
    if (new RegExp(cfg.danger, 'i').test(dlgText)) {
      const neg = normed.find(x => new RegExp(cfg.close, 'i').test(x.t));
      if (neg) { neg.el.click(); return {action: 'danger_dismiss', detail: neg.t, running: running, tail: dlgText}; }
      return {action: 'danger_block', detail: dlgText.slice(0, 80), running: running, tail: dlgText};
    }
    const isPay = new RegExp(cfg.pay, 'i').test(dlgText)
      || normed.some(x => new RegExp(cfg.pay, 'i').test(x.t));
    if (isPay) {
      if (cfg.allowPaid) {
        const ok = normed.find(x => cfg.confirmWords.indexOf(x.t) >= 0);
        if (ok) { ok.el.click(); return {action: 'paid_continue', detail: ok.t, running: running, tail: dlgText}; }
      }
      const close = normed.find(x => new RegExp(cfg.close, 'i').test(x.t));
      if (close) { close.el.click(); return {action: 'quota', detail: close.t, running: running, tail: dlgText}; }
      return {action: 'quota', detail: dlgText.slice(0, 80), running: running, tail: dlgText};
    }
    // 普通确认弹窗：点白名单里的正向按钮
    const ok = normed.find(x => cfg.confirmWords.indexOf(x.t) >= 0);
    if (ok) { ok.el.click(); return {action: 'confirm', detail: ok.t, running: running, tail: dlgText}; }
    // 只有关闭类按钮：关掉，避免遮挡后续操作
    const close = normed.find(x => new RegExp(cfg.close, 'i').test(x.t));
    if (close) { close.el.click(); return {action: 'close', detail: close.t, running: running, tail: dlgText}; }
    // 词表外的弹窗：若背后已经出片，优先按 done 上报，绝不能让浮层挡住出片信号
    if (freshVid || doneMatch) {
      return {action: 'done', detail: '弹窗背后已出片', running: running, tail: tail.slice(-600)};
    }
    return {action: 'unknown_dialog', detail: dlgText.slice(0, 100), running: running, tail: dlgText};
  }

  if (running) return {action: 'none', detail: '', running: true, tail: tail.slice(-600)};
  // 成片已在页面上：DOM 里出现了「提交前没有的」video，才算本次出片。
  // 历史会话里残留的旧 <video> 不算，否则会误判已出片、该补发时也不补发。
  // （freshVid/doneMatch 已在函数开头计算）
  if (freshVid) {
    return {action: 'done', detail: '页面已有新成片', running: false, tail: tail.slice(-600)};
  }
  if (doneMatch) {
    return {action: 'done', detail: '页面提示已出片', running: false, tail: tail.slice(-600)};
  }
  if (new RegExp(cfg.ask, 'i').test(tail)) {
    // 助手要确认：可能是消息卡片里的按钮，优先点按钮（比打字更稳）
    const strong = cfg.strongWords || [];
    const pageBtns = Array.from(document.querySelectorAll('button,[role="button"]')).filter(vis);
    const hit = pageBtns.find((b) => {
      const t = norm(b.innerText || b.getAttribute('aria-label') || '');
      return t.length > 0 && t.length <= 8 && strong.indexOf(t) >= 0;
    });
    if (hit) {
      hit.click();
      return {action: 'confirm', detail: norm(hit.innerText), running: false, tail: tail.slice(-600)};
    }
    const m = tail.match(new RegExp(cfg.ask, 'i'));
    return {action: 'need_confirm', detail: 'ask:' + (m ? m[0].slice(0, 60) : ''), running: false, tail: tail.slice(-600)};
  }
  return {action: 'none', detail: '', running: false, tail: tail.slice(-600)};
}
"""


def _normalize_btn(text):
    return re.sub(r"\s+", "", str(text or ""))


def _send_followup(page, text, timeout_s=30):
    """在输入框补发一句话（确认/重新生成）。失败不影响主流程。

    等片期间的补发/确认务必传短超时（如 6s）：输入框被弹窗遮挡时若按默认
    30s 硬等，每个轮询周期都会被堵满，整个等待循环基本停摆。
    """
    try:
        _fill_prompt_and_send(page, text, timeout_s=timeout_s)
        _log(f"已自动补发: 「{text}」")
        return True
    except Exception as e:  # noqa: BLE001
        _log(f"自动补发失败: {str(e)[:120]}")
        return False


def _autopilot_once(page, plugin_params, state, progress_callback=None, submitted_at=0.0,
                    baseline=None, retry_prompt=None):
    """一次自动接管检查。

    返回 None 表示无需干预；否则抛出 QuotaExhausted / NeedLogin / NeedHuman，
    或完成一次代点/补发。state 用于跨 tick 去重与限次。
    baseline 是提交前页面上已有的视频地址，用于区分「本次新出的片」和「历史旧片」。
    """
    if not plugin_params.get("auto_pilot", True):
        return None
    cfg = {
        "captcha": _CAPTCHA_RE.pattern,
        "login": _LOGIN_EXPIRED_RE.pattern,
        "ask": _ASK_CONFIRM_RE.pattern,
        "running": _RUNNING_RE.pattern,
        "done": _DONE_RE.pattern,
        "pay": _PAY_BTN_RE.pattern,
        "danger": _DANGER_BTN_RE.pattern,
        "close": _CLOSE_BTN_RE_TXT,
        "notDialog": _NOT_DIALOG_CLASS_RE.pattern,
        "confirmWords": [_normalize_btn(w) for w in _CONFIRM_BTN_WORDS],
        "strongWords": [_normalize_btn(w) for w in _STRONG_CONFIRM_WORDS],
        "allowPaid": bool(plugin_params.get("allow_paid_generation", False)),
        "baseline": sorted(baseline or []),
    }
    try:
        snap = page.evaluate(_AUTOPILOT_JS, cfg)
    except Exception as e:  # noqa: BLE001
        msg = str(e)[:120]
        # 页签/浏览器已关闭：必须向上抛，让等待循环立即失败换号，
        # 而不是每个轮询周期打一条日志空转到超时（曾空转 26 分钟）
        if _PAGE_CLOSED_RE.search(msg):
            raise
        _log(f"自动接管扫描异常: {msg}")
        return None
    if not isinstance(snap, dict):
        return None

    action = snap.get("action") or "none"
    detail = str(snap.get("detail") or "")[:120]
    now = time.time()
    # 「连续无动静」计时：任何实际动作/信号（含 running）都会清零。
    # 补发提示词 = 重复提交 = 重复扣额度，必须连续多个周期都毫无动静才允许触发。
    if action != "none" or snap.get("running"):
        state["idle_since"] = 0.0

    # 人机验证：不绕过，只提示并等待人工完成
    if action == "captcha":
        if not state.get("captcha_since"):
            state["captcha_since"] = now
            _log("检测到人机验证，等待人工完成…")
        if progress_callback:
            progress_callback("等待人工完成人机验证", 5)
        limit = int(plugin_params.get("captcha_wait_seconds", 300) or 300)
        if now - state["captcha_since"] > limit:
            raise NeedHuman("人机验证超时未完成")
        return "captcha"
    if state.get("captcha_since"):
        state["captcha_since"] = 0.0
        _log("人机验证已通过，继续等待成片")

    if action == "login":
        raise NeedLogin("登录态失效，请重新登录该账号")

    if action == "quota":
        raise QuotaExhausted(detail or "额度不足（需付费才能继续）")

    if action == "done":
        # 已经出片：只等取回，绝不补发提示词（补发 = 重复提交、重复扣额度）
        if not state.get("done_since"):
            state["done_since"] = now
            _log(f"页面提示已出片（{detail}），进入取片阶段")
        return "done"

    if action == "danger_block":
        _log(f"检测到危险操作弹窗，已跳过不点: {detail}")
        return "danger_block"

    if action == "unknown_dialog":
        # 词表外的弹窗：不乱点、也不补发（补发只会对着被遮挡的输入框反复超时，
        # 实测曾连续 38 次各堵 30 秒）。记一条日志让人知道是什么挡住了页面，
        # 等它自行消失；弹窗背后若已出片，JS 侧已优先按 done 上报，不会落到这里。
        if detail and detail not in state.setdefault("unknown_seen", []):
            state["unknown_seen"].append(detail)
            _log(f"检测到未识别弹窗，等待其消失（不自动点、不补发）: {detail[:80]}")
        return "unknown_dialog"

    if action in ("confirm", "close", "paid_continue", "danger_dismiss"):
        # 防抖：同一个按钮 3 秒内不重复点
        if state.get("last_detail") == detail and now - state.get("last_ts", 0) < 3:
            return action
        state["last_detail"] = detail
        state["last_ts"] = now
        tip = {
            "confirm": "已自动点掉确认弹窗",
            "close": "已自动关闭弹窗",
            "paid_continue": "已自动确认「使用会员/积分继续」",
            "danger_dismiss": "已自动取消危险弹窗",
        }[action]
        _log(f"{tip}: 「{detail}」")
        if progress_callback:
            progress_callback(tip)
        return action

    wait_s = int(plugin_params.get("auto_reply_wait", 25) or 25)
    if submitted_at and (now - submitted_at) < wait_s:
        return None

    # 没在生成、也没出片：豆包可能在等确认，或只回了文字没开生成
    if action == "need_confirm":
        # 同一句确认话术只回一次：豆包的确认文案会留在会话历史里，每个轮询
        # 周期都会重新匹配到它——不去重就会反复补发「确认」，每多发一次就
        # 可能多触发一次生成、多扣一次额度（实测一晚同段双 nodeId 双倍扣）
        if detail and detail in state.setdefault("confirmed_asks", []):
            return None
        max_confirm = int(plugin_params.get("auto_confirm_times", 2) or 2)
        if state.get("confirm", 0) < max_confirm:
            text = str(plugin_params.get("auto_confirm_text") or "确认").strip() or "确认"
            # 失败也计数：否则每个轮询周期都会对着被遮挡的输入框硬点一次
            state["confirm"] = state.get("confirm", 0) + 1
            if _send_followup(page, text, timeout_s=6):
                if detail:
                    state["confirmed_asks"].append(detail)
                return "confirm_sent"
        return None

    tail = str(snap.get("tail") or "")
    if not snap.get("running") and len(tail) > 40:
        # 要求「连续 wait_s 秒毫无动静」才补发：单轮误判（信号文案变动、页面
        # 响应慢）不至于触发重发——重发 = 重复提交 = 重复扣一次额度
        if not state.get("idle_since"):
            state["idle_since"] = now
            return None
        if now - state["idle_since"] < wait_s:
            return None
        max_retry = int(plugin_params.get("auto_retry_times", 2) or 2)
        if state.get("retry", 0) < max_retry:
            # 重试时绝不再发「生成视频」这种裸指令：豆包只收到「生成视频」会按字面意
            # 生成一段随机/无关的视频，塞进创作空间成为一个新节点——多段分镜连发时，
            # 这段垃圾视频会被当成「本次成片」抓取，导致第一个分镜之后全串片。
            # 因此补发必须带本次的完整内容提示词（retry_prompt）；没有内容就不补发。
            raw_text = str(plugin_params.get("auto_retry_text") or "").strip()
            if raw_text and ("生成视频" in raw_text or not retry_prompt):
                raw_text = ""  # 绕过裸「生成视频」默认值
            text = (retry_prompt or raw_text or "").strip()
            if text:
                # 成功失败都计入次数：失败不计数会每个轮询周期都重试（每次最长空耗 30s）
                state["retry"] = state.get("retry", 0) + 1
                if _send_followup(page, text, timeout_s=6):
                    if progress_callback:
                        progress_callback("未检测到生成，已自动补发原提示词")
                    return "retry_sent"
                # 失败后重新计时：输入框被遮挡时隔 wait_s 再试，别每个周期都硬点
                state["idle_since"] = now
                _log(f"自动补发失败（{state['retry']}/{max_retry}），本轮不再补发")
    return None


def _find_ready_video_url(captured):
    """从本次会话捕获的网络响应里找「成片就绪」信号：douyin 视频 URL。

    豆包出片时，浏览器网络层会先出现 douyin.com/douyinvod 的视频响应，
    比 DOM video 元素的 currentSrc 更及时、更可靠。返回最新一条，找不到返回 None。
    """
    if not captured:
        return None
    hit = None
    # 必须遍历快照：captured 由浏览器响应回调在别的线程 append，
    # 边迭代边 append 会抛 "deque mutated during iteration"，
    # 一旦抛出，本次成片检测整体失效 → 一直等不到片 → 误判超时并重复提交。
    for item in list(captured):
        try:
            u, ct = item[0], item[1]
            u = u or ""
            ct = (ct or "").lower()
        except Exception:  # noqa: BLE001
            continue
        is_douyin = ("douyinvod" in u) or ("douyin.com" in u)
        if not is_douyin:
            continue
        looks_video = (
            "/video/" in u
            or "/video/tos/" in u
            or u.rstrip().lower().endswith(".mp4")
            or "video/mp4" in ct
        )
        if looks_video:
            hit = u
    return hit


# ---------------------------------------------- 会话隔离（每次开干净会話）
# 豆包 /chat/ 会恢复上次会话，历史成片卡片就一直挂在 DOM/React fiber 里；只要有历史卡片，
# 基线/解析随时可能把它当成本次成片（这就是「总是抓到以前生成过的视频」的主因）。
# 所以每次生成前先切到一个全新空会话，让页面从零开始，再叠加基线与时间窗两道防线。
_CHAT_STATE_JS = r"""
() => {
  const vids = document.querySelectorAll('video').length;
  // 只数消息区里的大封面：侧边栏的历史会话缩略图也是 rc_gen 图，尺寸很小，必须排除，
  // 否则「新会话」永远判不空，每次都白点一遍新对话。
  const covers = [...document.querySelectorAll('img')]
      .filter(i => /rc_gen|video_cover/.test(i.currentSrc || i.src || ''))
      .filter(i => { const r = i.getBoundingClientRect(); return r.width >= 120 && r.height >= 80; })
      .length;
  const conv = (() => {
    try {
      const m = location.pathname.match(/\/chat\/([0-9A-Za-z_-]{6,})/);
      if (m) return m[1];
      const u = new URL(location.href);
      return u.searchParams.get('conversation_id') || u.searchParams.get('conversationId') || '';
    } catch (e) { return ''; }
  })();
  return {vids: vids, covers: covers, conv: conv, href: location.href};
}
"""

_NEW_CHAT_ENTRY_JS = r"""
() => {
  // 找「新对话」入口：文本 / aria-label / title 三路匹配，取侧边栏最靠上的那个。
  const rx = /(新对话|新建对话|开启新对话|新的对话|新会话|新聊天|new chat|start new)/i;
  const out = [];
  for (const el of document.querySelectorAll('button,a,[role="button"],[role="menuitem"]')) {
    const text = ((el.innerText || el.textContent || '') + '').trim();
    const aria = el.getAttribute('aria-label') || '';
    const title = el.getAttribute('title') || '';
    const probe = text + ' ' + aria + ' ' + title;
    if (text.length > 24 || !rx.test(probe)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) continue;
    out.push({x: r.left + r.width / 2, y: r.top + r.height / 2,
              label: (text || aria || title).slice(0, 20)});
  }
  out.sort((a, b) => (a.y - b.y) || (a.x - b.x));
  return out.slice(0, 6);
}
"""

_BASELINE_NODE_IDS_JS = r"""
() => {
  // 提交前把页面 fiber 里的创作节点 ID 收干净：豆包卡片不播放就没有 <video>，
  // URL 级基线常为空，节点级基线才是防串片的硬防线。
  const out = new Set();
  const re = /^\d{8,}$/;
  let budget = 20000;
  const seen = new WeakSet();
  const walk = (v, d) => {
    if (!v || typeof v !== 'object' || d > 10 || budget-- < 0) return;
    if (seen.has(v)) return;
    seen.add(v);
    if (Array.isArray(v)) { for (const it of v.slice(0, 60)) walk(it, d + 1); return; }
    let keys;
    try { keys = Object.keys(v); } catch (e) { return; }
    for (const k of keys.slice(0, 80)) {
      if (k === 'return' || k === 'child' || k === 'sibling' || k === '_owner' || k === 'stateNode') continue;
      let val;
      try { val = v[k]; } catch (e) { continue; }
      if (val == null) continue;
      if (typeof val === 'object') { walk(val, d + 1); continue; }
      if (typeof val !== 'string' && typeof val !== 'number') continue;
      const sk = String(k).toLowerCase();
      const sv = String(val);
      if (re.test(sv) && (sk.indexOf('node') >= 0 || sk.indexOf('task') >= 0 || sk === 'id')) out.add(sv);
    }
  };
  for (const el of [...document.querySelectorAll('video,img')].slice(0, 60)) {
    for (const k in el) {
      if (k.indexOf('__reactFiber$') !== 0 && k.indexOf('__reactProps$') !== 0) continue;
      try { walk(el[k], 0); } catch (e) {}
    }
  }
  return [...out].slice(0, 80);
}
"""


def _page_chat_state(page):
    """读当前页面的会话状态（视频卡片数 / 成片封面数 / 会话 ID）。"""
    try:
        return page.evaluate(_CHAT_STATE_JS) or {}
    except Exception:  # noqa: BLE001
        return {}


def _chat_is_empty(state):
    """空会话判定：页面上既没有视频，也没有成片封面图。"""
    st = state or {}
    try:
        return int(st.get("vids") or 0) == 0 and int(st.get("covers") or 0) == 0
    except Exception:  # noqa: BLE001
        return False


def _start_fresh_chat(page, progress_callback=None):
    """确保本次生成跑在一个全新空会话里（页面从零开始，历史卡片不再干扰）。

    返回 (是否干净, 描述)。拿不到干净会话也不阻断生成——基线与时间窗仍会兜住，
    只是要多一条告警日志，便于事后定位。
    """
    st = _page_chat_state(page)
    if st and _chat_is_empty(st):
        _log(f"当前已是干净会话（conv={st.get('conv') or '新建'}），无需切换")
        return True, "已是新会话"
    _log(f"检测到页面带历史内容（video={st.get('vids')} 封面={st.get('covers')} "
         f"conv={st.get('conv')}），切到新对话…")
    if progress_callback:
        progress_callback("切换到新对话")
    for _ in range(2):
        try:
            hits = page.evaluate(_NEW_CHAT_ENTRY_JS) or []
        except Exception as e:  # noqa: BLE001
            hits = []
            _log(f"扫描「新对话」入口异常: {str(e)[:100]}")
        if hits:
            _log(f"找到 {len(hits)} 个候选入口: {[h.get('label') for h in hits]}")
        for h in hits:
            try:
                page.mouse.click(float(h.get("x") or 0), float(h.get("y") or 0))
                page.wait_for_timeout(2500)
            except Exception:  # noqa: BLE001
                continue
            st2 = _page_chat_state(page)
            if st2 and _chat_is_empty(st2):
                _log(f"已切到新对话（入口「{h.get('label')}」），页面已清空")
                return True, "已切到新对话"
        # 兜底：重新打开无参数首页（多数情况下会落到空会话）
        try:
            page.goto(_DOUBAO_HOME, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            st3 = _page_chat_state(page)
            if st3 and _chat_is_empty(st3):
                _log("重载首页后已是空会话")
                return True, "重载首页后为空会话"
        except Exception as e:  # noqa: BLE001
            _log(f"重载首页失败: {str(e)[:100]}")
    _log("未能切到空会话：本次改用「严格基线 + 最短等待窗」防串片（历史卡片仍可能干扰，已记录）")
    return False, "未能新建会话"


def _baseline_node_ids(page):
    """提交前采集页面上已存在的创作节点 ID（节点级基线，防串片的硬防线）。"""
    try:
        ids = page.evaluate(_BASELINE_NODE_IDS_JS) or []
    except Exception:  # noqa: BLE001
        ids = []
    out = [str(i) for i in ids if str(i).strip()]
    if out:
        _log(f"已记录 {len(out)} 个历史创作节点 ID，解析时只认新增节点")
    return out


def _conv_id_from_url(url):
    """从页面 URL 提取会话 ID（生成模式也会带上它做会话级过滤）。"""
    m = re.search(r"/chat/(\d{6,})", str(url or ""))
    return m.group(1) if m else ""


def _baseline_video_srcs(page):
    """提交生成前记录页面上已存在的视频地址 / 成片封面。

    豆包 /chat/ 会保留历史会话，页面里常有上一条视频的 <video> 或成片封面图；
    不排除就会一提交就「拿到成片」，实际下载到旧片。这里先采基线，等待时只认新增的。
    """
    srcs = []
    try:
        srcs += page.eval_on_selector_all(
            "video",
            "els => els.map(v => v.currentSrc || v.src || v.poster || '').filter(Boolean)",
        ) or []
    except Exception:  # noqa: BLE001
        pass
    try:
        # 成片封面（rc_gen_image/video_cover）也算历史痕迹：不点播放时它是唯一能看到的旧片信号
        srcs += page.eval_on_selector_all(
            "img",
            "els => els.map(i => i.currentSrc || i.src || '')"
            ".filter(s => /rc_gen|video_cover/.test(s))",
        ) or []
    except Exception:  # noqa: BLE001
        pass
    base = {str(s).split("?")[0] for s in srcs if s}
    _log(f"已记录 {len(base)} 条历史视频/封面地址，等待时只认新增成片")
    return base


def _new_video_src(page, baseline):
    """返回本次生成新增的视频地址（不在基线里的最后一个），没有则 None。"""
    try:
        vids = page.eval_on_selector_all(
            "video", "els => els.map(v => v.currentSrc || v.src).filter(Boolean)"
        )
    except Exception:  # noqa: BLE001
        return None
    hit = None
    for v in vids:
        v = str(v or "")
        if not v:
            continue
        if v.split("?")[0] in baseline:
            continue
        hit = v
    return hit


def _wait_for_video(page, timeout_s, poll_s, progress_callback=None, captured=None,
                    baseline=None, plugin_params=None, submitted_at=0.0, retry_prompt=None,
                    baseline_nodes=None, conv_id=""):
    deadline = time.time() + timeout_s
    last_seen = ""
    t0 = time.time()
    last_report = 0
    baseline = baseline or set()
    baseline_nodes = baseline_nodes or []
    plugin_params = plugin_params or {}
    # 最短抑制窗：视频成片至少要几十秒，提交后立刻出现的任何「视频地址」只可能是页面里
    # 历史卡片的残留/预览请求。窗口内一律不采信，等过了才认信号。（显式判 None，
    # 用户填 0 就是关掉这道门，不能被 `or 20` 吃掉）
    _mv = plugin_params.get("min_video_wait_seconds")
    min_elapsed = 20.0 if _mv in (None, "") else max(0.0, float(_mv))
    strict_recent = bool(plugin_params.get("recent_strict", True))
    state = {"confirm": 0, "retry": 0, "last_detail": "", "last_ts": 0.0,
             "captcha_since": 0.0, "done_since": 0.0, "last_probe": 0.0,
             "idle_since": 0.0, "unknown_seen": [],
             "last_play_at": 0.0, "play_triggers": 0}
    last_err = ""
    while time.time() < deadline:
        elapsed = time.time() - t0
        now = time.time()
        # 页签/浏览器已被关闭（用户关窗、浏览器崩溃）：继续轮询毫无意义——
        # 所有检测都会静默失败，最后白等满 timeout 才换号。立即失败切换下一账号。
        try:
            if page.is_closed():
                raise NeedHuman("豆包页签已被关闭，无法继续等待成片")
        except NeedHuman:
            raise
        except Exception:  # noqa: BLE001
            pass
        try:
            # 等待期间周期上报进度，避免前端长时间停在「生成中 0%」像卡死
            if progress_callback and elapsed - last_report >= 20:
                last_report = elapsed
                mins, secs = int(elapsed) // 60, int(elapsed) % 60
                progress_callback(f"生成中 {mins}分{secs}秒", min(85, 5 + int(elapsed) // 10))
            # 无人值守：先处理确认弹窗 / 补发确认话术，再看成片
            _autopilot_once(page, plugin_params, state, progress_callback, submitted_at, baseline,
                            retry_prompt=retry_prompt)
            # 网络信号优先：豆包出片时网络响应已含 douyin 视频 URL，比 DOM 更及时
            net_url = _find_ready_video_url(captured)
            if net_url and net_url.split("?")[0] not in baseline and elapsed >= min_elapsed:
                return net_url
            # DOM 兜底：只认基线之外的新增视频，避免拿到历史会话里的旧片
            new_src = _new_video_src(page, baseline)
            if new_src and elapsed >= min_elapsed:
                return new_src
            # 页面已提示出片，但网络/DOM 信号都还没同步（豆包成片卡片常懒渲染，
            # DOM 里可能根本没有 <video>）：直接问官方接口要一次，别干等超时。
            if state.get("done_since") and elapsed >= min_elapsed \
                    and now - state.get("last_probe", 0.0) >= 10:
                state["last_probe"] = now
                try:
                    # 带 min_ts + 基线 + 节点基线：done 探测只认「本次提交之后创建」的节点。
                    # 不带会取账号全局最新节点——上一段分镜/别的会话的旧片会在
                    # 这一步被当成成片抓回（串片）。
                    done_url = _resolve_original_video_url(
                        page, attempts=1, min_ts=int(submitted_at or 0),
                        baseline_urls=sorted(baseline), skip_node_ids=baseline_nodes,
                        strict_recent=strict_recent, conv_id=conv_id,
                        no_wm_only=True,
                    )
                except Exception as e:  # noqa: BLE001
                    done_url = None
                    _log(f"出片后取原片异常: {str(e)[:100]}")
                if done_url:
                    _log("页面已提示出片，改从官方接口取回原片")
                    return done_url
                # 官方接口拿不到（免费额度视频可能不进创作空间/节点延迟就绪）：
                # 替用户「点一下播放」。成片卡片不播放就没有 <video>/get_play_info，
                # 这正是以前要人手动点播放才抓得到的原因；播起来后网络/DOM 信号
                # 在下个轮询周期自然命中。
                if now - state.get("last_play_at", 0.0) >= 10 and state.get("play_triggers", 0) < 20:
                    state["last_play_at"] = now
                    state["play_triggers"] = state.get("play_triggers", 0) + 1
                    play_url = _trigger_video_play(page, baseline)
                    if play_url:
                        return play_url
            status = _scan_page_status(page)
            if status:
                kind, kw = status
                if kw != last_seen:
                    last_seen = kw
                    _log(f"页面提示: {kw}")
                if kind == "quota":
                    raise QuotaExhausted(kw)
                if kind == "fail":
                    raise GenerateFailed(kw)
        except (QuotaExhausted, GenerateFailed, NeedLogin, NeedHuman):
            raise
        except Exception as e:  # noqa: BLE001
            # 以前这里静默吞掉所有异常，成片检测悄悄失效也看不出来，
            # 最后只会得到一个莫名其妙的「等待成片超时」。这里至少留下痕迹。
            msg = str(e)[:160]
            # 页签/浏览器已关闭：立即失败换号，绝不空等满 timeout
            if _PAGE_CLOSED_RE.search(msg):
                raise NeedHuman(f"豆包页签/浏览器已关闭: {msg[:80]}")
            if msg != last_err:
                last_err = msg
                _log(f"等待成片检测异常（已忽略）: {msg}")
        time.sleep(poll_s)
    raise Exception("等待成片超时")


def _output_filename(context):
    viewer_index = context.get("viewer_index", 0)
    try:
        viewer_index = int(viewer_index)
    except Exception:
        viewer_index = 0
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return f"{viewer_index:04d}_video_{timestamp}.mp4"


def _looks_like_mp4(path):
    """弱校验：文件头含 ftyp（MP4/MOV 家族），避免拿到 HTML 错误页或空文件。"""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
        return b"ftyp" in head
    except Exception:  # noqa: BLE001
        return False


def _mp4_integrity(path):
    """强校验：解析顶层 box 链，判断 MP4 是否被截断。返回 (ok, note)。

    为什么必须做：豆包 CDN 传无水印原片是「慢速长连接」，实测出现过连接提前结束、
    只写进一部分就当成功返回的情况 —— 2026-09-17 的 0006 成片，mdat box 头声明
    29,773,217 字节，实际文件只有 11,567,126 字节，尾部 moov 根本没写下来；
    播放器读不到索引，播到末尾就崩，用户看到的就是「视频不完整」。
    旧校验只看「前 4096 字节含 ftyp」，半截文件照样通过 → 被当成品回传。

    判据：box 长度必须自洽、不得越界，且必须同时存在 moov（元数据）与 mdat（媒体数据）。
    截断必然使最后一个 box 越界或头部不完整，这里一定抓得到。
    """
    try:
        size = os.path.getsize(path)
        if size < 1024:
            return False, f"文件过小({size} 字节)"
        pos = 0
        guard = 0
        has_moov = False
        has_mdat = False
        with open(path, "rb") as f:
            while pos < size:
                guard += 1
                if guard > 512:
                    return False, "box 数量异常"
                f.seek(pos)
                hdr = f.read(8)
                if len(hdr) < 8:
                    return False, f"box 头部截断@{pos}"
                bsz, btyp = struct.unpack(">I4s", hdr)
                btyp = btyp.decode("latin1", "replace")
                head = 8
                if bsz == 1:
                    ext = f.read(8)
                    if len(ext) < 8:
                        return False, f"{btyp} largesize 截断@{pos}"
                    bsz = struct.unpack(">Q", ext)[0]
                    head = 16
                elif bsz == 0:
                    bsz = size - pos          # 标准语义：延伸到文件尾
                if bsz < head:
                    return False, f"{btyp} size 非法({bsz})@{pos}"
                if pos + bsz > size:
                    return False, f"{btyp} box 越界（需要 {pos + bsz}，文件仅 {size}）→ 文件被截断"
                if btyp == "moov":
                    has_moov = True
                elif btyp == "mdat":
                    has_mdat = True
                pos += bsz
        if not has_moov:
            return False, "缺 moov（元数据未写入，文件被截断）"
        if not has_mdat:
            return False, "缺 mdat（无媒体数据）"
        return True, "ok"
    except Exception as e:  # noqa: BLE001
        return False, f"解析异常: {str(e)[:80]}"


def _download(video_url, output_dir, cookies, filename, verify_header=True, retries=3,
              verify_full=True, seg_tries=4):
    """带重试的下载：网络抖动/连接被重置时自动重来，重试仍失败才抛错。"""
    retries = max(1, int(retries or 1))
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            return _download_once(video_url, output_dir, cookies, filename, verify_header,
                                  verify_full=verify_full, seg_tries=seg_tries)
        except Exception as e:  # noqa: BLE001
            last_err = e
            _log(f"下载失败（第 {attempt}/{retries} 次）: {str(e)[:150]}")
            if attempt < retries:
                time.sleep(2 * attempt)
    raise last_err


def _download_once(video_url, output_dir, cookies, filename, verify_header=True,
                   verify_full=True, seg_tries=4):
    """单次下载（内部自带分段续传）。一律先写 <name>.part，全部校验通过才原子改名。

    三点硬要求（都来自「取到半截视频」的真实事故）：
      1. **长度对齐**：响应带 Content-Length 时，落盘字节数必须完全一致；
      2. **结构完整**：MP4 顶层 box 链必须自洽且含 moov（见 _mp4_integrity）；
      3. **原子落盘**：校验不过就删 .part，绝不留下半截文件冒充成品。
    中断时（超时/连接重置）用 Range 从断点续传，不重头再来 —— 该 CDN 下行实测可低到
    几十 KB/s，一次 30MB 的片重下代价太高。
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / filename
    part = path.with_name(path.name + ".part")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Referer": "https://www.doubao.com/",
    }
    got = 0
    expected_total = 0
    err = None
    for seg in range(1, max(1, int(seg_tries or 1)) + 1):
        if seg > 1:
            time.sleep(min(1.5 * (seg - 1), 6))
        have = part.stat().st_size if part.exists() else 0
        req_headers = dict(headers)
        mode = "wb"
        if have > 0:
            req_headers["Range"] = f"bytes={have}-"
            mode = "ab"
        try:
            with requests.get(video_url, stream=True, timeout=(15, 120),
                              cookies=cookies, headers=req_headers) as r:
                if have > 0 and r.status_code == 206:
                    pass                                   # 服务端接受续传
                else:
                    if have > 0:
                        # 200 = 不支持 Range（或 Range 被忽略）→ 必须从头写，
                        # 用 append 拼会得到一个前后错位的废文件
                        have, mode = 0, "wb"
                    r.raise_for_status()
                clen = int(r.headers.get("Content-Length") or 0)
                if clen:
                    expected_total = clen + (have if mode == "ab" else 0)
                got = have
                with open(part, mode) as f:
                    for chunk in r.iter_content(1 << 16):
                        if not chunk:
                            continue
                        f.write(chunk)
                        got += len(chunk)
        except Exception as e:  # noqa: BLE001
            err = e
            _log(f"下载中断（第 {seg} 段，已收 {got} 字节）: {str(e)[:110]}")
            continue
        if expected_total and got != expected_total:
            err = Exception(f"下载不完整：收到 {got} / 声明 {expected_total} 字节"
                            f"（差 {expected_total - got}）")
            _log(f"下载字节数不对（第 {seg} 段）: {str(err)[:130]}")
            continue       # 下一段用 Range 续传补齐
        err = None
        break
    if err is not None:
        part.unlink(missing_ok=True)
        raise err

    if got < 1024:
        part.unlink(missing_ok=True)
        raise Exception(f"下载文件过小（{got} 字节），可能不是有效成片")
    if verify_header and not _looks_like_mp4(part):
        part.unlink(missing_ok=True)
        raise Exception(f"下载文件不是有效 MP4（{got} 字节，无 ftyp 头），已丢弃")
    if verify_full:
        ok, note = _mp4_integrity(part)
        if not ok:
            part.unlink(missing_ok=True)
            raise Exception(f"下载的 MP4 结构不完整（{note}，{got} 字节），已丢弃并重试")
    try:
        os.replace(str(part), str(path))     # 原子替换：对外只会看到完整文件
    except Exception:  # noqa: BLE001
        shutil.move(str(part), str(path))
    _log(f"成片已回传到字字动画: {path}（{got} 字节，结构校验通过）")
    return str(path)


def _download_video(page, ctx, video_url, output_dir, filename, plugin_params, progress_callback=None,
                   submitted_at=0.0, chat_url="", account_id="", baseline=None,
                   baseline_nodes=None, conv_id=""):
    """下载链路：无水印原片 → 页面下载按钮 → 反复重试解析 → 保底带水印成片。

    无水印原片拿不到时，**默认保底落带水印成片并记入待补抓清单**（绝不重生成、绝不丢片）；
    开启 strict_no_watermark 才抛 NoWatermark。min_ts 让解析只认本次生成的节点，
    避免抓到账号历史里的旧片。
    """
    verify_mp4 = bool(plugin_params.get("verify_mp4_header", True))
    verify_full = bool(plugin_params.get("verify_mp4_complete", True))
    seg_tries = int(plugin_params.get("download_seg_tries", 4) or 4)
    retries = int(plugin_params.get("download_retries", 3) or 3)
    want_no_wm = bool(plugin_params.get("remove_watermark", True))
    strict = bool(plugin_params.get("strict_no_watermark", False))
    strict_recent = bool(plugin_params.get("recent_strict", True))
    min_ts = int(submitted_at or 0)

    def _cookies():
        return {c["name"]: c["value"] for c in ctx.cookies() if "doubao" in c.get("domain", "")}

    def _try(url, tag):
        try:
            return _download(url, output_dir, _cookies(), filename, verify_mp4, retries,
                             verify_full=verify_full, seg_tries=seg_tries)
        except Exception as e:  # noqa: BLE001
            _log(f"{tag}下载失败: {str(e)[:150]}")
            return None

    if want_no_wm:
        # 1) 官方接口解析（带 min_ts 过滤，只认本次生成的节点）
        if progress_callback:
            progress_callback("解析无水印原片", 88)
        original_url = _resolve_original_video_url(page, attempts=4, delay_s=3, min_ts=min_ts,
                                                   baseline_urls=baseline,
                                                   skip_node_ids=baseline_nodes,
                                                   strict_recent=strict_recent, conv_id=conv_id)
        if original_url:
            if progress_callback:
                progress_callback("下载原片", 92)
            path = _try(original_url, "无水印原片")
            if path:
                return path
        # 2) 页面上的下载按钮
        if progress_callback:
            progress_callback("尝试页面下载", 90)
        try:
            path = _try_page_download(page, output_dir, filename)
        except Exception as e:  # noqa: BLE001
            _log(f"页面下载按钮失败: {str(e)[:120]}")
            path = None
        if path:
            return path
        # 3) 创作节点常延迟就绪：在 nowm_wait_seconds 内反复解析，给足机会，绝不轻易放弃
        deadline = time.time() + max(0, int(plugin_params.get("nowm_wait_seconds", 180) or 0))
        ri = 0
        while time.time() < deadline:
            ri += 1
            _log(f"原片暂未就绪，等待后重试解析（第 {ri} 轮）…")
            page.wait_for_timeout(8000)
            original_url = _resolve_original_video_url(page, attempts=2, delay_s=3, min_ts=min_ts,
                                                   baseline_urls=baseline,
                                                   skip_node_ids=baseline_nodes,
                                                   strict_recent=strict_recent, conv_id=conv_id)
            if original_url:
                path = _try(original_url, f"无水印原片（重试{ri}）")
                if path:
                    return path
        if strict:
            raise NoWatermark("未能取到无水印原片（严格模式：不落带水印成片）")
        _log("无水印原片暂不可用，先保底落带水印成片并记入待补抓清单（可后续一键补抓，不重生成）")
        if progress_callback:
            progress_callback("保底落带水印成片", 90)
        path = _download(video_url, output_dir, _cookies(), filename, verify_mp4, retries)
        if chat_url:
            _add_pending({
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "account_id": account_id,
                "chat_url": chat_url,
                "file": path,
                "filename": filename,
                "output_dir": output_dir,
                # 记录真正提交生成的时间，供后续补抓用 min_ts 锁定「本次」视频，
                # 避开「生成视频发早了」那次更早创建的干扰节点。
                "submitted_at": int(submitted_at or 0),
            })
        return path

    if progress_callback:
        progress_callback("下载成片", 90)
    return _download(video_url, output_dir, _cookies(), filename, verify_mp4, retries)


def _save_debug_screenshot(page, tag="fail"):
    """失败时截图到插件目录，便于事后定位页面卡在哪一步。"""
    try:
        p = plugin_dir / f"debug_{tag}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        page.screenshot(path=str(p), full_page=False)
        _log(f"已保存诊断截图: {p}")
        return str(p)
    except Exception as e:  # noqa: BLE001
        _log(f"保存诊断截图失败: {str(e)[:100]}")
        return None


def _generate_with_account(acc, context, plugin_params, prompt, output_dir, filename, progress_callback):
    timeout_s = int(plugin_params.get("timeout", 900))
    poll_s = int(plugin_params.get("poll_interval", 4))
    headless = bool(plugin_params.get("headless", False))
    duration = plugin_params.get("duration", "10")
    ratio = plugin_params.get("ratio") or plugin_params.get("aspect_ratio") or "16:9"
    model = plugin_params.get("model", "seedance")

    name = acc.get("name") or acc["id"]
    if progress_callback:
        progress_callback(f"账号 {name}")
    store = _ensure_fingerprints()
    acc = next((a for a in store["accounts"] if a["id"] == acc["id"]), acc)
    pw, ctx = _get_browser(acc["id"], headless=headless)

    # 只保留最近若干条响应：豆包长连接（event-stream）会持续推送，不限量会吃内存
    captured = deque(maxlen=200)
    def _hook_resp(resp):
        try:
            ct = (resp.headers.get("content-type") or "").lower()
            if "douyinvod" in resp.url or "douyin.com" in resp.url or "json" in ct or "event-stream" in ct:
                captured.append((resp.url, ct, resp))
        except Exception:  # noqa: BLE001
            pass
    try:
        ctx.on("response", _hook_resp)
    except Exception:  # noqa: BLE001
        pass

    page = None
    try:
        # 多分镜连续生成时，同一账号复用一个常驻页会让上一段/昨天的视频卡片残留在 DOM/
        # React fiber 里，是「后面分镜抓成昨天/上一段旧片」的最大来源。改为每段分镜
        # 开一个全新标签页（登录态由同一持久化 profile 共享），本段自带干净 DOM，
        # baseline/旧片过滤才真正从源头隔离；生成完在 finally 关掉该页。
        fresh_page = bool(plugin_params.get("fresh_page_per_segment", True))
        if fresh_page:
            page = ctx.new_page()
        else:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
        if progress_callback:
            progress_callback("打开豆包")
        page.goto(_DOUBAO_HOME, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        if not _looks_logged_in(page, ctx):
            _mark_used(acc["id"], logged_in=False)
            raise NeedLogin(f"账号 {name} 未登录或登录态已失效，请在插件设置里点「登录」")

        # 会话隔离：切到全新空会话，让本次生成从干净页面开始。
        # 常驻窗口会恢复上次会话，历史成片卡片留在 DOM/fiber 里，是「抓到以前生成过的
        # 视频」的主因；先清场，再叠加基线与最短等待窗，三道防线一起上。
        if bool(plugin_params.get("new_chat_per_task", True)):
            try:
                _start_fresh_chat(page, progress_callback)
            except Exception as e:  # noqa: BLE001
                _log(f"切换新对话失败（已忽略，继续用基线防线）: {str(e)[:120]}")

        if progress_callback:
            progress_callback("提交生成")
        # 提交前先扫一次：关掉可能挡住输入框的引导/营销弹层（不补发话术）
        try:
            _autopilot_once(page, plugin_params, {}, progress_callback, 0.0)
        except Exception:  # noqa: BLE001
            pass
        _ensure_video_mode(page)
        applied = _apply_options(page, duration, ratio, model, prompt=prompt)
        # 豆包网页端没有比例控件，比例通过提示词注入（模型默认横屏）
        ratio_hint = _ratio_hint(ratio)
        if ratio_hint and ratio_hint not in prompt:
            prompt = f"{ratio_hint}。{prompt}"
        # 设置面板没生效（打不开/滑杆不可用）时，把显式选择的时长注入提示词，
        # 否则用户在页面上选的时长会完全丢失（2026-09-17 实测两个 manager 账号
        # 面板都打不开，选的时长静默丢掉）。auto 模式提示词自带时长，无需注入。
        d_explicit = str(duration or "").strip()
        if not applied.get("duration", True) and d_explicit and d_explicit.lower() != "auto":
            prompt = f"视频时长{d_explicit}秒。{prompt}"
            _log(f"警示: 时长设置未生效，已注入提示词兜底（时长{d_explicit}秒）")
        # 防御：提示词如果以「生成视频/请生成视频/帮我生成视频」开头（多分镜流水线条/习惯性前缀），
        # 单独发给豆包会被当成一条独立指令而偏出画面；只要后面还有正文，就先剥掉这层引导语。
        _lead = re.match(r"^(请\s*)?(帮我\s*)?生成视频[\s。．.．:：,，、]?", prompt, re.I)
        if _lead:
            _rest = prompt[_lead.end():].strip()
            if _rest:
                prompt = _rest
                _log("已剥掉提示词开头的「生成视频」引导语")
        ref_paths = _collect_reference_paths(context, plugin_params)
        if ref_paths:
            if progress_callback:
                progress_callback(f"上传参考图 {len(ref_paths)} 张")
            _upload_reference_images(page, ref_paths)
        else:
            _log("本次没有可用参考图，按文生视频提交")
        # 提交前记录两层基线：URL/封面级 + 创作节点级（节点级才是防串片硬防线，
        # 因为成片卡片不点播放就不挂 <video>，URL 基线常为空）
        baseline = _baseline_video_srcs(page)
        baseline_nodes = _baseline_node_ids(page)
        conv_id = _conv_id_from_url(page.url)
        _log(f"本次会话 ID={conv_id or '未知'}，节点基线={len(baseline_nodes)} 条")
        _fill_prompt_and_send(page, prompt)
        submitted_at = time.time()
        # 提交后开始捕获响应：清空历史记录，只认本次生成后的 douyin 视频 URL
        captured.clear()
        # 提交后先等任务真正接单。页面上「免费额度用完将使用会员」不算失败。
        page.wait_for_timeout(2500)
        # 新会话提交后 URL 才会带上会话 ID：补取一次，让解析按会话过滤（recent 模式也能收紧）
        if not conv_id:
            conv_id = _conv_id_from_url(page.url)
            if conv_id:
                _log(f"提交后取到会话 ID={conv_id}，解析限定在本会话内")
        if progress_callback:
            progress_callback("生成中", 0)
        try:
            video_url = _wait_for_video(
                page, timeout_s, poll_s, progress_callback, captured,
                baseline=baseline, plugin_params=plugin_params, submitted_at=submitted_at,
                retry_prompt=prompt, baseline_nodes=baseline_nodes, conv_id=conv_id,
            )
        except Exception:
            if plugin_params.get("debug_screenshot", True) and page is not None:
                _save_debug_screenshot(page, "wait")
            raise
        _log(f"拿到成片地址: {video_url[:120]}")
        # 抓取链接只用于诊断（nowm_capture.json），失败绝不能影响已到手的成片
        try:
            _capture_video_links(ctx, captured)
        except Exception as e:  # noqa: BLE001
            _log(f"记录视频链接失败（已忽略）: {str(e)[:120]}")

        path = _download_video(
            page, ctx, video_url, output_dir, filename, plugin_params, progress_callback,
            submitted_at=submitted_at, chat_url=page.url, account_id=acc["id"],
            baseline=baseline, baseline_nodes=baseline_nodes, conv_id=conv_id,
        )
        _mark_used(acc["id"], logged_in=True)
        return path
    except Exception:
        if plugin_params.get("debug_screenshot", True) and page is not None:
            try:
                _save_debug_screenshot(page, "error")
            except Exception:  # noqa: BLE001
                pass
        raise
    finally:
        # 关键：卸载响应监听。窗口常驻复用时若不摘掉，每生成一次就多一个 handler
        try:
            ctx.remove_listener("response", _hook_resp)
        except Exception:  # noqa: BLE001
            pass
        global _KEEP_OPEN
        # 窗口保留：默认不关闭常驻浏览器；仅当用户关闭“保留窗口”选项时才关闭
        keep = bool(plugin_params.get("keep_window_open", True))
        _KEEP_OPEN = keep
        # 每段分镜开的新标签页用完后关掉，避免多段生成后标签页越堆越多，
        # 也保证下一段分镜从干净空白页开始（旧卡残留在页上是串片根源之一）。
        if fresh_page and page is not None:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
        if not keep:
            _close_browser(acc["id"])


def _try_page_download(page, output_dir, filename):
    """尝试点视频消息的下载按钮拿原片；失败返回 None，由调用方退回 URL 下载。"""
    try:
        card = page.locator("video").last
        card.hover(timeout=4000)
        page.wait_for_timeout(1200)
    except Exception:  # noqa: BLE001
        return None
    dl_selectors = (
        '[aria-label*="下载"]',
        '[title*="下载"]',
        '[class*="download"]',
        '[class*="Download"]',
        '[class*="icon-download"]',
    )
    for sel in dl_selectors:
        loc = page.locator(sel)
        if loc.count() == 0:
            continue
        try:
            with page.expect_download(timeout=8000) as dl_info:
                loc.last.click(timeout=4000)
            d = dl_info.value
            path = os.path.join(output_dir, filename)
            d.save_as(path)
            size = os.path.getsize(path) if os.path.exists(path) else 0
            ok, note = _mp4_integrity(path) if size >= 1024 else (False, f"仅 {size} 字节")
            if not ok:
                # 页面下载按钮同样会给出半截文件（浏览器下载被中断），一样要过结构校验
                _log(f"页面下载的文件不完整（{note}），弃用并继续尝试其它方式: {path}")
                try:
                    os.remove(path)
                except Exception:  # noqa: BLE001
                    pass
                continue
            _log(f"已通过页面下载按钮保存原片: {path}（{size} 字节，结构校验通过）")
            return path
        except Exception as e:  # noqa: BLE001
            _log(f"页面下载按钮尝试失败: {str(e)[:100]}")
    return None


def _capture_video_links(ctx, captured):
    """成片后统一读取缓存的响应体，把视频链接/水印字段写到插件目录，供定位无水印原片。"""
    if not captured:
        return
    out = []
    seen = set()
    # 同 _find_ready_video_url：遍历快照，避免响应回调并发 append 时抛
    # "deque mutated during iteration"（这会连带把已到手的成片判成失败）
    for u, ct, resp in list(captured):
        try:
            body = resp.text()[:400000]
        except Exception:  # noqa: BLE001
            continue
        for mm in re.finditer(r'https?://[^"\s\\]+(?:douyinvod|douyin\.com)[^"\s\\]*', body):
            key = mm.group(0).split("?")[0]
            if key not in seen:
                seen.add(key)
                out.append({"url": mm.group(0)[:400], "from": u[:120], "content_type": ct})
        for mm in re.finditer(r'.{0,80}(?:no_watermark|noWatermark|watermark).{0,120}', body, re.IGNORECASE):
            ctx_ = mm.group(0)[:220]
            if ctx_ not in seen:
                seen.add(ctx_)
                out.append({"watermark_ctx": ctx_, "from": u[:120]})
    if out:
        cap_path = plugin_dir / "nowm_capture.json"
        try:
            cap_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
            _log(f"已捕获视频链接/水印字段 -> {cap_path} ({len(out)} 条)")
        except Exception as e:  # noqa: BLE001
            _log(f"写入捕获文件失败: {e}")


def _generate_impl(context):
    _ensure_legacy_migrated()
    prompt = (context.get("prompt") or "").strip()
    output_dir = context.get("output_dir", context.get("project_path", "."))
    plugin_params = {**get_params(), **(context.get("plugin_params") or {})}
    # 参数来源诊断：「页面上改的设置不生效」最常见的两个原因都能在这里现形：
    #   1) 引擎在 context.plugin_params 里带了旧值/默认值，覆盖了插件页面存的值；
    #   2) 插件页面根本没保存成功（config.json 里没有）。
    # 下次再反馈「设置不生效」，把这两行日志发来即可定位。
    try:
        _file_pp = get_params()
        _ctx_pp = dict(context.get("plugin_params") or {})
        _over = {k: (_file_pp.get(k), _ctx_pp.get(k)) for k in _ctx_pp
                 if k in _DEFAULT_PARAMS and _ctx_pp.get(k) != _file_pp.get(k)}
        if _over:
            _log(f"警示: 引擎传入参数覆盖了插件页面设置（插件值, 引擎值）: {_over}")
        _log("[参数] backend=%s model=%s duration=%s ratio=%s auto_pilot=%s "
             "auto_confirm_text=%r auto_retry_text=%r allow_paid=%s"
             % (plugin_params.get("backend"), plugin_params.get("model"),
                plugin_params.get("duration"), plugin_params.get("ratio"),
                plugin_params.get("auto_pilot"), plugin_params.get("auto_confirm_text"),
                plugin_params.get("auto_retry_text"), plugin_params.get("allow_paid_generation")))
    except Exception:  # noqa: BLE001
        pass
    progress_callback = context.get("progress_callback")

    if not prompt:
        raise Exception("PLUGIN_ERROR:::提示词为空")
    if len(prompt) > _PROMPT_MAX_CHARS:
        prompt = prompt[:_PROMPT_MAX_CHARS]

    store = _load_store()
    if not store["accounts"]:
        raise Exception("PLUGIN_ERROR:::还没有豆包账号。请打开插件设置，添加账号并登录")

    filename = _output_filename(context)
    _backend = str(plugin_params.get("backend") or "").strip().lower()
    # 日志自证版本：插件模块随引擎进程启动加载一次，改代码必须重启引擎才生效；
    # 曾有「改完没重启，测试仍跑旧逻辑」被误判为修复无效
    _log(f"[路由] backend='{_backend}' → "
         f"{'manager 后端 (manager_run)' if _backend == 'manager' else 'web 直出 (Playwright)'} "
         f"(插件 v{_PLUGIN_VERSION})")
    # 豆包管理器后端：不走 Playwright 独立浏览器，改为驱动管理器里已登录的 doubao webview
    if _backend == "manager":
        try:
            import sys as _sys
            _mgr_dir = str(plugin_dir)
            if _mgr_dir not in _sys.path:
                _sys.path.insert(0, _mgr_dir)
            import manager_run
        except Exception as e:  # noqa: BLE001
            _log(f"manager 后端加载失败: {e}")
        else:
            return manager_run.generate(context, main_module=_sys.modules[__name__])
    cool_after = int(plugin_params.get("fail_cooldown_times", 3) or 3)
    cool_min = int(plugin_params.get("fail_cooldown_minutes", 15) or 15)
    wait_limit = int(plugin_params.get("account_wait_seconds", 900) or 900)
    errors = []
    tried = set()
    wait_deadline = time.time() + wait_limit

    while True:
        store = _load_store()
        candidates = _pick_accounts(store)
        if not candidates:
            logged = [a for a in store["accounts"] if a.get("logged_in")]
            if not logged:
                raise Exception("PLUGIN_ERROR:::没有已登录的豆包账号。请在插件设置里点「登录」")
            if all(_is_quota_blocked(a) for a in logged):
                raise Exception("PLUGIN_ERROR:::全部已登录账号今日额度已用完，请换号或明天再试")
            if all(_is_cooling(a) for a in logged):
                raise Exception(
                    f"PLUGIN_ERROR:::全部已登录账号处于冷却中（{_cooldown_left_text(logged[0])}），请稍后再试"
                )
            raise Exception("PLUGIN_ERROR:::暂无可用账号（额度用完或冷却中）")

        # 挑一个「本轮没试过 + 没被别的分镜占用」的账号
        picked = None
        for acc in candidates:
            if acc["id"] in tried:
                continue
            if not _acquire_account(acc["id"]):
                continue
            picked = acc
            break

        if picked is None:
            all_tried = all(a["id"] in tried for a in candidates)
            if all_tried or time.time() > wait_deadline:
                break
            if progress_callback:
                progress_callback("等待可用账号", 0)
            time.sleep(5)
            continue

        name = picked.get("name") or picked["id"]
        _log(f"使用账号 {name}")
        try:
            path = _run_on_account(
                picked["id"], _generate_with_account,
                picked, context, plugin_params, prompt, output_dir, filename, progress_callback,
            )
            _mark_success(picked["id"])
            _mark_used(picked["id"], logged_in=True)
            return [path]
        except QuotaExhausted as e:
            msg = str(e)
            _log(f"账号 {name} 额度不足，切换下一账号: {msg}")
            _mark_quota(picked["id"], msg)
            errors.append(f"{name}: 额度不足({msg})")
        except NoWatermark as e:
            # 严格无水印模式下没拿到原片：直接终止，绝不换号重新生成（每日额度宝贵）。
            # 带 PLUGIN_ERROR::: 前缀会被最外层原样抛出，不会进入换号循环。
            _log(f"账号 {name} 严格无水印模式失败，已终止（未重生成）: {e}")
            raise Exception(f"PLUGIN_ERROR:::严格无水印模式未取到原片，已终止（未重新生成）：{str(e)[:100]}")
        except NeedLogin as e:
            msg = str(e)
            _log(f"账号 {name} 登录态失效，切换下一账号: {msg}")
            _mark_used(picked["id"], logged_in=False)
            _mark_error(picked["id"], msg)
            errors.append(f"{name}: {msg}")
        except NeedHuman as e:
            msg = str(e)
            _log(f"账号 {name} 需要人工处理，切换下一账号: {msg}")
            _mark_failure(picked["id"], msg, cool_after, cool_min)
            errors.append(f"{name}: {msg}")
        except GenerateFailed as e:
            msg = str(e)
            _log(f"账号 {name} 生成失败，切换下一账号: {msg}")
            _mark_failure(picked["id"], msg, cool_after, cool_min)
            errors.append(f"{name}: 生成失败({msg})")
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if msg.startswith("PLUGIN_ERROR:::"):
                raise
            _log(f"账号 {name} 失败: {msg}")
            _mark_failure(picked["id"], msg, cool_after, cool_min)
            errors.append(f"{name}: {msg}")
        finally:
            _release_account(picked["id"])
            tried.add(picked["id"])

    detail = "；".join(errors) if errors else "无可用账号"
    raise Exception(f"PLUGIN_ERROR:::所有账号均未能出片。{detail}")


def generate(context):
    """公开入口。

    引擎可能在 asyncio 事件循环线程里调用，这里转到独立线程，避免调度里的
    等待阻塞引擎；真正的账号级并发由 _run_on_account 保证（每账号一线程）。
    """
    if getattr(_tls, "is_worker", False):
        return _generate_impl(context)
    box = {}
    done = threading.Event()

    def _runner():
        try:
            box["ok"] = _generate_impl(context)
        except BaseException as e:  # noqa: BLE001
            box["err"] = e
        finally:
            done.set()

    threading.Thread(target=_runner, daemon=True, name="doubao-generate").start()
    done.wait()
    if "err" in box:
        raise box["err"]
    return box["ok"]


def fetch_original(context):
    """公开入口：按会话链接补抓无水印原片，返回 [path]（与 generate 一致）。

    context 支持：chat_url / chat_id / url、output_dir、account_id、
    viewer_index、filename。用法与 generate 相同，引擎可直接取回落盘路径。
    """
    if getattr(_tls, "is_worker", False):
        return _fetch_original_impl(context)
    box = {}
    done = threading.Event()

    def _runner():
        try:
            box["ok"] = _fetch_original_impl(context)
        except BaseException as e:  # noqa: BLE001
            box["err"] = e
        finally:
            done.set()

    threading.Thread(target=_runner, daemon=True, name="doubao-fetch").start()
    done.wait()
    if "err" in box:
        raise box["err"]
    return box["ok"]


