#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import json
import copy
import logging
import feedparser
import requests
import datetime
import re
import random
import psutil
import hashlib
from logging.handlers import RotatingFileHandler
from threading import Thread, Lock

# --- 基础配置与路径 ---
DATA_DIR = "/data"

if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR, exist_ok=True)

# --- 配置路径 ---
CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')
# 已处理ID缓存文件
PROCESSED_FILE = os.path.join(DATA_DIR, 'processed.json')
# 日志
LOG_FILE = os.path.join(DATA_DIR, 'monitor.log')

# 日志配置
log_handler = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=1, encoding='utf-8')
console_handler = logging.StreamHandler(sys.stdout)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[log_handler, console_handler]
)
logger = logging.getLogger(__name__)

# --- 全局状态 ---
start_time = datetime.datetime.now()
last_rss_check_time = None
last_rss_error = None

# 全局配置和状态（线程安全）
global_config = None  # 全局配置对象 {system, users}
processed_ids = set()  # 已处理的条目ID集合
config_lock = Lock()  # 保护全局配置和 processed_ids 的锁

# --- 数据结构定义 ---

DEFAULT_SYSTEM_CONFIG = {
    'system': {
        'check_min_interval': 10,
        'check_max_interval': 30
    },
    'users': {}
}

def get_default_user_config():
    return {
        'keywords': [],         # [{"word": "xx", "include": [], "exclude": []}]
        'global_exclude': [],   # ["xx", "yy"]
        'defaults': {           # 默认模板
            'include': [],
            'exclude': []
        },
        'settings': {
            'match_summary': False,
            'full_word_match': False,
            'regex_match': False
        }
    }

# --- 配置管理 ---

def load_json(filepath, default):
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"加载 {filepath} 失败: {e}")
    return copy.deepcopy(default)

def save_json(filepath, data):
    try:
        temp = filepath + '.tmp'
        with open(temp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        os.replace(temp, filepath)
    except Exception as e:
        logger.error(f"保存 {filepath} 失败: {e}")

def load_config(force_reload=False):
    """加载配置到全局变量中"""
    global global_config, processed_ids

    with config_lock:
        # 如果已经加载过且不强制重载，直接返回
        if not force_reload and global_config is not None:
            return

        # 加载主配置
        config = load_json(CONFIG_FILE, DEFAULT_SYSTEM_CONFIG)

        if 'system' not in config:
            config['system'] = copy.deepcopy(DEFAULT_SYSTEM_CONFIG['system'])
        if 'users' not in config:
            config['users'] = {}
        for k, v in DEFAULT_SYSTEM_CONFIG['system'].items():
            if k not in config['system']:
                config['system'][k] = v

        global_config = config

        # 加载已处理ID（独立管理）
        processed_list = load_json(PROCESSED_FILE, [])
        processed_ids = set(processed_list)

def save_main_config():
    """保存主配置（system, users）"""
    with config_lock:
        data = {
            'system': global_config.get('system', {}),
            'users': global_config.get('users', {})
        }
    save_json(CONFIG_FILE, data)

def save_processed():
    """保存已处理ID缓存，包含限制逻辑"""
    with config_lock:
        p_list = list(processed_ids)
        if len(p_list) > 500:
            p_list = p_list[-500:]
            processed_ids.clear()
            processed_ids.update(p_list)
    save_json(PROCESSED_FILE, p_list)

def get_user_config(chat_id_str):
    """获取用户配置"""
    with config_lock:
        if chat_id_str not in global_config['users']:
            global_config['users'][chat_id_str] = get_default_user_config()
        return json.loads(json.dumps(global_config['users'][chat_id_str], ensure_ascii=False))

def validate_keyword(keyword):
    """验证关键词是否合法"""
    if not keyword or len(keyword) > 100:
        return False
    # 防止特殊字符注入
    if any(c in keyword for c in ['\n', '\r', '\0']):
        return False
    return True

# --- Telegram 交互 ---
def send_telegram_message(message, bot_token, chat_id, reply_to=None, max_retries=3):
    if not bot_token or not chat_id: return False

    for attempt in range(max_retries):
        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            data = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
            if reply_to: data["reply_to_message_id"] = reply_to
            resp = requests.post(url, data=data, timeout=10)
            if resp.status_code == 200:
                return True
            elif resp.status_code == 429:  # Rate limit
                retry_after = resp.json().get('parameters', {}).get('retry_after', 5)
                logger.warning(f"触发速率限制，等待 {retry_after} 秒")
                time.sleep(retry_after)
                continue
            else:
                logger.warning(f"发送消息失败，状态码: {resp.status_code}")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)  # 指数退避
        except Exception as e:
            logger.error(f"发送消息异常 (尝试 {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
    return False

def disable_telegram_webhook(bot_token):
    try:
        requests.post(f"https://api.telegram.org/bot{bot_token}/deleteWebhook", timeout=10)
    except Exception as e:
        logger.warning(f"删除 webhook 失败: {e}")

def set_telegram_bot_commands(bot_token):
    commands = [
        {"command": "add", "description": "添加规则 /add [clean|clean-i|clean-e] kw1 [kw2...] [+inc] [-exc]"},
        {"command": "del", "description": "删除规则 /del kw1 [kw2...]"},
        {"command": "list", "description": "查看规则"},
        {"command": "include", "description": "设置默认必含 /include kw1 [kw2...]"},
        {"command": "exclude", "description": "设置默认排除 /exclude kw1 [kw2...]"},
        {"command": "block", "description": "全局屏蔽 /block kw1 [kw2...]"},
        {"command": "unblock", "description": "取消屏蔽 /unblock kw1 [kw2...]"},
        {"command": "blocklist", "description": "查看全局屏蔽"},
        {"command": "setsummary", "description": "设置: 匹配摘要 on/off"},
        {"command": "setfullword", "description": "设置: 完整词匹配 on/off"},
        {"command": "setregex", "description": "设置: 正则匹配 on/off"},
        {"command": "setinterval", "description": "设置: 检测间隔 /setinterval 30 60。（仅管理员）"},
        {"command": "status", "description": "查看状态"},
        {"command": "help", "description": "帮助说明"},
    ]
    try:
        requests.post(f"https://api.telegram.org/bot{bot_token}/setMyCommands", json={"commands": commands}, timeout=10)
    except Exception as e:
        logger.warning(f"设置命令菜单失败: {e}")

def format_uptime():
    s = (datetime.datetime.now() - start_time).total_seconds()
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{int(d)}天{int(h)}小时{int(m)}分"

def bool_from_text(text):
    t = text.strip().lower()
    return t in ('on', 'true', '1', 'yes', 'y')

# --- 核心逻辑 ---
def telegram_command_listener():
    while True:
        try:
            bot_token = os.environ.get('TG_BOT_TOKEN', '')
            if bot_token: break
            time.sleep(5)
        except Exception as e:
            logger.warning(f"等待 BOT_TOKEN 时出错: {e}")
    
    disable_telegram_webhook(bot_token)
    set_telegram_bot_commands(bot_token)
    
    offset = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
            resp = requests.get(url, params={"timeout": 60, "offset": offset}, timeout=65)
            if resp.status_code != 200:
                time.sleep(5)
                continue
            
            data = resp.json()
            if not data.get("ok"):
                time.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                chat_id = None
                user_conf = None
                __uc_orig = None
                try:
                    message = update.get("message")
                    if not message: continue

                    chat_id = str(message.get("chat", {}).get("id"))
                    text = message.get("text", "").strip()
                    msg_id = message.get("message_id")

                    if not text: continue

                    parts = text.split(maxsplit=1)
                    cmd_raw = parts[0].split('@')[0].lower()
                    args_str = parts[1] if len(parts) > 1 else ""

                    user_conf = get_user_config(chat_id)
                    __uc_orig = json.dumps(user_conf, ensure_ascii=False, sort_keys=True)
                    users_keywords = user_conf['keywords']
                    users_defaults = user_conf['defaults']

                    if cmd_raw == "/add":
                        if not args_str:
                            send_telegram_message("❌ 请输入参数。示例：/add mk clean +出", bot_token, chat_id, msg_id)
                            continue

                        tokens = args_str.split()

                        # 1. 解析 flags 和 switches
                        flags = []
                        switches_inc = []
                        switches_exc = []
                        keywords = []

                        for t in tokens:
                            tl = t.lower()
                            if tl in ('clean', 'clean-i', 'clean-e'):
                                flags.append(tl)
                            elif t.startswith('+') and len(t) > 1:
                                switches_inc.append(t[1:])
                            elif t.startswith('-') and len(t) > 1:
                                switches_exc.append(t[1:])
                            else:
                                keywords.append(t)

                        if not keywords:
                            send_telegram_message("❌ 未识别到关键词", bot_token, chat_id, msg_id)
                            continue

                        # 验证关键词
                        invalid_keywords = [kw for kw in keywords if not validate_keyword(kw)]
                        if invalid_keywords:
                            send_telegram_message(f"❌ 关键词不合法: {', '.join(invalid_keywords)}", bot_token, chat_id, msg_id)
                            continue

                        logs = []
                        for kw in keywords:
                            # 查找或创建
                            rule = next((x for x in users_keywords if x['word'] == kw), None)
                            is_new = False
                            if not rule:
                                rule = {"word": kw, "include": [], "exclude": []}
                                users_keywords.append(rule)
                                is_new = True

                            # 处理 clean
                            if 'clean' in flags:
                                rule['include'] = []
                                rule['exclude'] = []
                            else:
                                if 'clean-i' in flags: rule['include'] = []
                                if 'clean-e' in flags: rule['exclude'] = []

                            # 处理 defaults (仅新建且未clean时)
                            if is_new and not flags:
                                for d_inc in users_defaults.get('include', []):
                                    if d_inc not in rule['include']: rule['include'].append(d_inc)
                                for d_exc in users_defaults.get('exclude', []):
                                    if d_exc not in rule['exclude']: rule['exclude'].append(d_exc)

                            # 处理 switches
                            for inc in switches_inc:
                                if inc not in rule['include']: rule['include'].append(inc)
                            for exc in switches_exc:
                                if exc not in rule['exclude']: rule['exclude'].append(exc)

                            # 生成日志
                            info = f"<b>{kw}</b>"
                            extras = []
                            if rule['include']: extras.append(f"➕ 必含: [{','.join(rule['include'])}]")
                            if rule['exclude']: extras.append(f"⛔ 排除: [{','.join(rule['exclude'])}]")
                            if extras: info += " " + " ".join(extras)
                            logs.append(info)

                        send_telegram_message("✅ 规则已更新：\n" + "\n".join(logs), bot_token, chat_id, msg_id)

                    elif cmd_raw == "/del":
                        targets = args_str.split()
                        if not targets:
                            send_telegram_message("❌ 请指定要删除的关键词", bot_token, chat_id, msg_id)
                            continue
                        deleted = []
                        for kw in targets:
                            initial_len = len(user_conf['keywords'])
                            user_conf['keywords'] = [r for r in user_conf['keywords'] if r['word'] != kw]
                            if len(user_conf['keywords']) < initial_len:
                                deleted.append(kw)
                        if deleted:
                            send_telegram_message(f"🗑️ 已删除: {', '.join(deleted)}", bot_token, chat_id, msg_id)
                        else:
                            send_telegram_message("⚠️ 未找到匹配的规则", bot_token, chat_id, msg_id)

                    elif cmd_raw == "/list":
                        msg_lines = ["<b>📋 您的配置</b>"]
                        defs = []
                        if users_defaults.get('include'): defs.append(f"默认必含: {','.join(users_defaults['include'])}")
                        if users_defaults.get('exclude'): defs.append(f"默认排除: {','.join(users_defaults['exclude'])}")
                        if defs:
                            msg_lines.append("<i>默认模板:</i>")
                            msg_lines.extend([f"  {d}" for d in defs])
                            msg_lines.append("")
                        if users_keywords:
                            msg_lines.append(f"<i>监控规则 ({len(users_keywords)}):</i>")
                            for i, r in enumerate(users_keywords):
                                line = f"{i+1}. <b>{r['word']}</b>"
                                extras = []
                                if r.get('include'): extras.append(f"➕ 包含: {', '.join(r['include'])}")
                                if r.get('exclude'): extras.append(f"⛔ 排除: {', '.join(r['exclude'])}")
                                if extras: line += f" ({' '.join(extras)})"
                                msg_lines.append(line)
                        else:
                            msg_lines.append("（暂无监控规则）")
                        g_exc = user_conf.get('global_exclude', [])
                        if g_exc:
                            msg_lines.append("")
                            msg_lines.append(f"<i>全局屏蔽:</i> {', '.join(g_exc)}")
                        send_telegram_message("\n".join(msg_lines), bot_token, chat_id, msg_id)

                    elif cmd_raw == "/include":
                        if not args_str:
                            user_conf['defaults']['include'] = []
                            send_telegram_message("✅ 已清空默认必含关键词", bot_token, chat_id, msg_id)
                        else:
                            kws = args_str.split()
                            user_conf['defaults']['include'] = list(dict.fromkeys(kws))
                            send_telegram_message(f"✅ 默认必含已设为: {', '.join(kws)}", bot_token, chat_id, msg_id)

                    elif cmd_raw == "/exclude":
                        if not args_str:
                            user_conf['defaults']['exclude'] = []
                            send_telegram_message("✅ 已清空默认排除关键词", bot_token, chat_id, msg_id)
                        else:
                            kws = args_str.split()
                            user_conf['defaults']['exclude'] = list(dict.fromkeys(kws))
                            send_telegram_message(f"✅ 默认排除已设为: {', '.join(kws)}", bot_token, chat_id, msg_id)

                    elif cmd_raw in ("/block", "/unblock"):
                        kws = args_str.split()
                        if not kws:
                            send_telegram_message(f"❌ 请指定关键词", bot_token, chat_id, msg_id)
                            continue
                        g_exc = user_conf.get('global_exclude', [])
                        changed = False
                        if cmd_raw == "/block":
                            for k in kws:
                                if k not in g_exc:
                                    g_exc.append(k)
                                    changed = True
                            if changed:
                                user_conf['global_exclude'] = g_exc
                                send_telegram_message(f"🚫 已添加到全局屏蔽", bot_token, chat_id, msg_id)
                        else:
                            initial_len = len(g_exc)
                            user_conf['global_exclude'] = [x for x in g_exc if x not in kws]
                            if len(user_conf['global_exclude']) < initial_len:
                                send_telegram_message(f"✅ 已解除屏蔽", bot_token, chat_id, msg_id)
                            else:
                                send_telegram_message("⚠️ 未找到相关屏蔽词", bot_token, chat_id, msg_id)

                    elif cmd_raw == "/blocklist":
                        g_exc = user_conf.get('global_exclude', [])
                        if not g_exc:
                            send_telegram_message("🚫 全局屏蔽列表为空", bot_token, chat_id, msg_id)
                        else:
                            send_telegram_message(f"<b>🚫 全局屏蔽列表</b>\n{', '.join(g_exc)}", bot_token, chat_id, msg_id)

                    elif cmd_raw == "/setsummary":
                        val = bool_from_text(args_str)
                        user_conf['settings']['match_summary'] = val
                        send_telegram_message(f"🔎 摘要匹配: {'开启' if val else '关闭'}", bot_token, chat_id, msg_id)

                    elif cmd_raw == "/setfullword":
                        val = bool_from_text(args_str)
                        user_conf['settings']['full_word_match'] = val
                        send_telegram_message(f"🧩 完整词匹配: {'开启' if val else '关闭'}", bot_token, chat_id, msg_id)

                    elif cmd_raw == "/setregex":
                        val = bool_from_text(args_str)
                        user_conf['settings']['regex_match'] = val
                        send_telegram_message(f"🧠 正则匹配: {'开启' if val else '关闭'}", bot_token, chat_id, msg_id)

                    elif cmd_raw == "/setinterval":
                        admin_id = os.environ.get('TG_CHAT_ID', '').strip()
                        if chat_id != admin_id:
                            send_telegram_message("⛔ 只有管理员可以使用此命令", bot_token, chat_id, msg_id)
                            continue
                        parts = args_str.split()
                        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                            with config_lock:
                                global_config['system']['check_min_interval'] = int(parts[0])
                                global_config['system']['check_max_interval'] = int(parts[1])
                            save_main_config()
                            send_telegram_message(f"⏱️ 间隔已设为 {parts[0]}-{parts[1]}秒", bot_token, chat_id, msg_id)
                        else:
                            send_telegram_message("❌ 格式: /setinterval 30 60", bot_token, chat_id, msg_id)

                    elif cmd_raw in ("/help", "/start"):
                        is_admin = (chat_id == os.environ.get('TG_CHAT_ID', '').strip())
                        msg = (
                            "<b>👋 NodeSeek 监控机器人</b>\n\n"
                            "<b>📝 规则管理</b>\n"
                            "/add [clean] 词1 [词2...] [+必含] [-排除] - <i>批量添加</i>\n"
                            "/del 词1 [词2...] - <i>批量删除</i>\n"
                            "/list - <i>查看规则</i>\n"
                            "/block /unblock - <i>全局屏蔽</i>\n\n"
                            "<b>⚙️ 默认模板</b>\n"
                            "/include 词1 [词2...] - <i>设默认必含</i>\n"
                            "/exclude 词1 [词2...] - <i>设默认排除</i>\n\n"
                            "<b>🔧 个人设置</b>\n"
                            "/setsummary on/off - <i>匹配摘要</i>\n"
                            "/setfullword on/off - <i>完整词</i>\n"
                            "/setregex on/off - <i>正则</i>\n"
                        )
                        if is_admin: msg += "\n<b>👮 管理员</b>\n/setinterval\n"
                        msg += "\n/status - <i>查看状态</i>"
                        send_telegram_message(msg, bot_token, chat_id, msg_id)
                        
                    elif cmd_raw == "/status":
                        mem = psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
                        uptime = format_uptime()
                        min_int = global_config['system']['check_min_interval']
                        max_int = global_config['system']['check_max_interval']
                        proc_count = len(processed_ids)
                        my_rules = len(user_conf['keywords'])
                        settings = user_conf.get('settings', {})
                        match_summary = "开" if settings.get('match_summary') else "关"
                        full_word = "开" if settings.get('full_word_match') else "关"
                        regex = "开" if settings.get('regex_match') else "关"
                        sys_info = ""
                        if chat_id == os.environ.get('TG_CHAT_ID', '').strip():
                            sys_info = (
                                f"\n<b>💻 系统指标</b>\n"
                                f"检测间隔: {min_int}-{max_int}s\n"
                                f"已处理ID: {proc_count}\n"
                                f"连续错误: {last_rss_error or '无'}\n"
                            )
                        msg = (
                            f"<b>📊 状态报告</b>\n"
                            f"运行时间: {uptime}\n"
                            f"内存占用: {mem:.1f} MB\n"
                            f"您的规则: {my_rules} 条\n"
                            f"匹配摘要: {match_summary} | 全词匹配: {full_word} | 正则: {regex}\n"
                            f"{sys_info}"
                            f"\n最后检测: {last_rss_check_time.strftime('%H:%M:%S') if last_rss_check_time else '从未'}"
                        )
                        send_telegram_message(msg, bot_token, chat_id, msg_id)
                except Exception as e:
                    logger.error(f"处理消息异常: {e}")
                finally:
                    if user_conf is not None and __uc_orig is not None:
                        __uc_new = json.dumps(user_conf, ensure_ascii=False, sort_keys=True)
                        if __uc_new != __uc_orig:
                            with config_lock:
                                global_config['users'][chat_id] = user_conf
                            save_main_config()
            time.sleep(1)
        except Exception as e:
            logger.error(f"指令监听异常: {e}")
            time.sleep(5)

def check_rss_feed():
    global last_rss_check_time, last_rss_error
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        resp = requests.get("https://rss.nodeseek.com/", headers=headers, timeout=30)
        if resp.status_code != 200: return

        feed = feedparser.parse(resp.content)
        if not feed.entries: return

        last_rss_check_time = datetime.datetime.now()
        last_rss_error = None
        bot_token = os.environ.get('TG_BOT_TOKEN')
        if not bot_token: return

        regex_cache = {}

        for entry in feed.entries:
            link = getattr(entry, 'link', '').strip()
            if not link: continue
            if hasattr(entry, 'id') and entry.id: key = entry.id
            else: key = hashlib.md5(link.encode()).hexdigest()

            # 原子去重：检查并立即占位，避免发送阶段出现重复推送窗口
            reserved = False
            with config_lock:
                if key in processed_ids:
                    continue
                processed_ids.add(key)
                reserved = True

            # 尽快落盘，降低进程异常退出导致的重复通知窗口
            if reserved:
                save_processed()

            title = getattr(entry, 'title', '').strip()
            summary = getattr(entry, 'summary', '') or getattr(entry, 'description', '')
            author = getattr(entry, 'author', '') or getattr(entry, 'dc_creator', '') or 'unknown'

            # 开始处理
            def clean_html(t): return re.sub(r'<[^>]+>', '', t).strip()
            title = clean_html(title)
            summary = clean_html(summary)
            author = clean_html(author)

            pub_date_str = ""
            try:
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    dt_utc = datetime.datetime(*entry.published_parsed[:6])
                    dt_bj = dt_utc + datetime.timedelta(hours=8)
                    pub_date_str = dt_bj.strftime('%Y-%m-%d %H:%M:%S')
                elif hasattr(entry, 'updated_parsed') and entry.updated_parsed:
                    dt_utc = datetime.datetime(*entry.updated_parsed[:6])
                    dt_bj = dt_utc + datetime.timedelta(hours=8)
                    pub_date_str = dt_bj.strftime('%Y-%m-%d %H:%M:%S')
            except Exception as e:
                logger.debug(f"解析发布时间失败: {e}")

            # 跟踪本条目发送结果，用于失败回滚
            send_attempts = 0
            send_success = 0

            # 遍历所有用户进行匹配
            with config_lock:
                users_copy = copy.deepcopy(global_config['users'])
            for chat_id, user_conf in users_copy.items():
                keywords = user_conf['keywords']
                if not keywords: continue
                settings = user_conf['settings']
                match_summary = settings.get('match_summary', False)
                full_word = settings.get('full_word_match', False)
                use_regex = settings.get('regex_match', False)

                text_to_check = title.lower()
                if match_summary: text_to_check += " " + summary.lower()

                is_blocked = False
                for block in user_conf.get('global_exclude', []):
                    if check_match(text_to_check, block.lower(), False, use_regex, regex_cache):
                        is_blocked = True
                        break
                if is_blocked: continue

                matched_rules = []
                for rule in keywords:
                    base = rule['word'].lower()
                    if not check_match(text_to_check, base, full_word, use_regex, regex_cache): continue

                    hit_ex = False
                    for ex in rule.get('exclude', []):
                        if check_match(text_to_check, ex.lower(), False, use_regex, regex_cache):
                            hit_ex = True
                            break
                    if hit_ex: continue

                    includes = rule.get('include', [])
                    if includes:
                        hit_in = False
                        for inc in includes:
                            if check_match(text_to_check, inc.lower(), False, use_regex, regex_cache):
                                hit_in = True
                                break
                        if not hit_in: continue
                    matched_rules.append(rule['word'])

                if matched_rules:
                    send_attempts += 1
                    kws_str = ", ".join(matched_rules)
                    msg = (
                        f"<b>🎯 发现命中帖子</b>\n"
                        f"• <b>标题</b>：{title}\n"
                        f"• <b>匹配</b>：{kws_str}\n"
                        f"• <b>作者</b>：{author}\n"
                        f"• <b>时间</b>：{pub_date_str}\n"
                        f"• <b>链接</b>：{link}"
                    )
                    if send_telegram_message(msg, bot_token, chat_id):
                        send_success += 1
                        logger.info(f"向用户 {chat_id} 推送: {title} (规则: {kws_str})")
                    else:
                        logger.warning(f"向用户 {chat_id} 推送失败: {title} (规则: {kws_str})")

            # 命中但全部发送失败：回滚去重标记，允许下轮重试，避免丢消息
            if send_attempts > 0 and send_success == 0:
                with config_lock:
                    if key in processed_ids:
                        processed_ids.remove(key)
                save_processed()
                logger.warning(f"条目全部推送失败，已回滚去重标记等待重试: {key}")
            
    except Exception as e:
        last_rss_error = str(e)
        logger.error(f"RSS检测失败: {e}")

def validate_regex(pattern):
    """验证正则表达式是否安全（长度限制 + 编译测试）"""
    if not pattern or len(pattern) > 100:
        return False
    try:
        re.compile(pattern)
        return True
    except:
        return False

def safe_regex_search(compiled_re, text, timeout_chars=10000):
    """安全的正则搜索，通过限制文本长度防止 ReDoS"""
    # 截断过长文本，防止病态输入
    if len(text) > timeout_chars:
        text = text[:timeout_chars]
    try:
        return bool(compiled_re.search(text))
    except:
        return False

def check_match(text, pattern, full_word, use_regex, cache):
    if not pattern: return False
    if use_regex:
        if not validate_regex(pattern):
            logger.warning(f"跳过不安全的正则表达式: {pattern}")
            return False
        try:
            if pattern not in cache: cache[pattern] = re.compile(pattern, re.IGNORECASE)
            return safe_regex_search(cache[pattern], text)
        except Exception as e:
            logger.error(f"正则匹配失败: {e}")
            return False
    if full_word:
        if pattern not in cache: cache[pattern] = re.compile(rf"\b{re.escape(pattern)}\b", re.IGNORECASE)
        return bool(cache[pattern].search(text))
    return pattern in text

def restart_program(reason):
    logger.info(f"重启: {reason}")
    os.execv(sys.executable, [sys.executable] + sys.argv)

def monitor_loop():
    logger.info("启动 RSS 监控循环")
    error_count = 0
    while True:
        try:
            check_rss_feed()
            error_count = 0
        except Exception as e:
            error_count += 1
            logger.error(f"监控循环错误: {e}")
            if error_count >= 15: restart_program("连续错误过多")
            
        try:
            proc = psutil.Process()
            mem = proc.memory_info().rss / 1024 / 1024
            uptime_h = (datetime.datetime.now() - start_time).total_seconds() / 3600
            if uptime_h > 24 or mem > 800: restart_program(f"维护重启 (Mem:{mem:.0f}MB, Time:{uptime_h:.1f}h)")
        except Exception as e:
            logger.warning(f"获取进程信息失败: {e}")

        sys_conf = global_config.get('system', {})
        mn = sys_conf.get('check_min_interval', 10)
        mx = sys_conf.get('check_max_interval', 30)
        wait = random.uniform(mn, mx)
        logger.info(f"等待 {wait:.1f}s ...")
        time.sleep(wait)

if __name__ == "__main__":
    if not os.environ.get('TG_BOT_TOKEN'):
        print("错误: 请设置 TG_BOT_TOKEN 环境变量")
        sys.exit(1)

    # 初始化全局配置和已处理ID
    load_config()

    t = Thread(target=telegram_command_listener, daemon=True)
    t.start()
    monitor_loop()