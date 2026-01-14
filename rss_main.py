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
import gc  # 添加gc库用于主动垃圾回收
import psutil  # 添加psutil库用于监控内存使用
from logging.handlers import RotatingFileHandler
from threading import Thread

# 配置文件和日志文件路径（Windows兼容）
if os.name == 'nt':  # Windows系统
    DATA_DIR = os.path.join(os.getcwd(), 'data')
else:  # Unix/Linux系统
    DATA_DIR = '/data'

if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR, exist_ok=True)

CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')
LOG_FILE = os.path.join(DATA_DIR, 'monitor.log')
PID_FILE = os.path.join(DATA_DIR, 'monitor.pid')

# Windows系统不支持systemd服务
if os.name == 'nt':
    SERVICE_FILE = None
else:
    SERVICE_FILE = '/etc/systemd/system/rss_monitor.service'

# 日志配置
log_handler = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=1)
console_handler = logging.StreamHandler(sys.stdout)
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[log_handler, console_handler]
)
logger = logging.getLogger(__name__)

# 默认配置
DEFAULT_CONFIG = {
    'keywords': [],
    'exclude_keywords': [],
    'notified_entries': {},
    'settings': {
        'match_summary': True,          # 是否匹配摘要/正文
        'full_word_match': False,       # 是否完整词匹配
        'regex_match': False,           # 是否将关键词视为正则
        'check_min_interval': 30,       # 最小检测间隔秒
        'check_max_interval': 60,       # 最大检测间隔秒
        'max_notified_entries': 50      # 去重记录上限
    },
    'telegram': {
        'bot_token': '',
        'chat_id': ''
    }
}

start_time = datetime.datetime.now()
last_rss_check_time = None
last_rss_error = None
detection_counter_state = 0

def load_config():
    """加载配置文件"""
    # 尝试从主配置文件和备份文件加载配置
    config = None
    backup_file = CONFIG_FILE + '.bak'
    
    # 尝试从主配置文件加载
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
            logger.debug("从主配置文件加载配置成功")
        except json.JSONDecodeError:
            logger.error("主配置文件JSON格式错误")
            config = None
        except Exception as e:
            logger.error(f"加载主配置文件失败: {e}")
            config = None
    
    # 如果主配置文件加载失败，尝试从备份文件加载
    if config is None and os.path.exists(backup_file):
        try:
            logger.info("主配置文件加载失败，尝试从备份文件加载")
            with open(backup_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
            logger.info("从备份配置文件加载配置成功")
            # 如果从备份加载成功，则恢复到主配置文件
            save_config(config)
        except Exception as e:
            logger.error(f"从备份配置文件加载失败: {e}")
            config = None
    
    # 如果都失败了，使用默认配置
    if config is None:
        logger.warning("无法加载配置文件，使用默认配置")
        config = copy.deepcopy(DEFAULT_CONFIG)
        save_config(config)
    else:
        # 确保配置中包含所有必要的键
        if 'keywords' not in config:
            config['keywords'] = []
        if 'exclude_keywords' not in config:
            config['exclude_keywords'] = []
        if 'notified_entries' not in config:
            config['notified_entries'] = {}
        if 'settings' not in config or not isinstance(config['settings'], dict):
            config['settings'] = {}
        # 填充 settings 默认值
        for k, v in DEFAULT_CONFIG['settings'].items():
            if k not in config['settings']:
                config['settings'][k] = v
        if 'telegram' not in config:
            config['telegram'] = {'bot_token': '', 'chat_id': ''}
        elif not isinstance(config['telegram'], dict):
            config['telegram'] = {'bot_token': '', 'chat_id': ''}
        else:
            if 'bot_token' not in config['telegram']:
                config['telegram']['bot_token'] = ''
            if 'chat_id' not in config['telegram']:
                config['telegram']['chat_id'] = ''
    
    # --- 关键字结构迁移逻辑 Start ---
    # 将旧版 ["kw1", "kw2"] 转换为 [{"word": "kw1", "include": [], "exclude": []}, ...]
    new_keywords = []
    if isinstance(config['keywords'], list):
        for item in config['keywords']:
            if isinstance(item, str):
                # 旧格式：转换为新对象
                new_keywords.append({
                    "word": item.strip(),
                    "include": [],
                    "exclude": []
                })
            elif isinstance(item, dict) and 'word' in item:
                # 新格式：保持原样，确保字段完整
                if 'include' not in item:
                    item['include'] = []
                if 'exclude' not in item:
                    item['exclude'] = []
                new_keywords.append(item)
    config['keywords'] = new_keywords
    # --- 关键字结构迁移逻辑 End ---

    # 全局排除关键词列表标准化（保持旧逻辑，仅针对exclude_keywords）
    def normalize_str_list(lst):
        cleaned = []
        seen = set()
        for kw in lst:
            if not isinstance(kw, str):
                continue
            kw_clean = kw.strip()
            if not kw_clean:
                continue
            key = kw_clean.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(kw_clean)
        return cleaned
    
    config['exclude_keywords'] = normalize_str_list(config.get('exclude_keywords', []))
    
    return config

def save_config(config):
    """保存配置文件"""
    # 定义备份文件路径
    backup_file = CONFIG_FILE + '.bak'
    temp_file = CONFIG_FILE + '.tmp'
    
    try:
        # 检查配置对象大小，防止过大导致内存占用
        # 对历史记录进行清理，防止配置文件无限增长
        # 限制 notified_entries 记录数
        settings = config.get('settings', {})
        max_notified_entries = int(settings.get('max_notified_entries', DEFAULT_CONFIG['settings']['max_notified_entries']))
        if max_notified_entries > 0 and 'notified_entries' in config and len(config['notified_entries']) > max_notified_entries:
            # 按照时间排序，保留最新的50条
            sorted_entries = sorted(
                config['notified_entries'].items(),
                key=lambda item: item[1]['time'] if isinstance(item[1], dict) and 'time' in item[1] else '',
                reverse=True
            )[:max_notified_entries]
            config['notified_entries'] = dict(sorted_entries)
            logger.debug(f"配置保存前已限制通知记录为{max_notified_entries}条")
        
        # 限制 title_notifications 记录数
        if 'title_notifications' in config and len(config['title_notifications']) > 100:
            # 按照时间排序，保留最新的100条
            sorted_titles = sorted(
                config['title_notifications'].items(),
                key=lambda item: item[1]['time'] if isinstance(item[1], dict) and 'time' in item[1] else '',
                reverse=True
            )[:100]
            config['title_notifications'] = dict(sorted_titles)
            logger.debug("配置保存前已限制标题记录为100条")
        
        # 检查config对象是否有效且可序列化
        try:
            # 测试JSON序列化
            config_str = json.dumps(config, ensure_ascii=False)
            # 检查序列化后的配置文件大小，防止过大
            if len(config_str) > 1024 * 1024:  # 如果大于1MB
                logger.warning(f"配置文件过大 ({len(config_str)/1024:.2f} KB)，尝试清理")
                
                # 保留基本配置和历史通知记录，只清理非关键数据
                basic_config = {
                    'keywords': config.get('keywords', []),
                    'exclude_keywords': config.get('exclude_keywords', []),
                    'settings': config.get('settings', {}),
                    'telegram': config.get('telegram', {'bot_token': '', 'chat_id': ''}),
                    'notified_entries': config.get('notified_entries', {}),  # 必须保留历史记录！
                }
                
                # 只保留notified_entries的最新20条，但绝不清空
                if 'notified_entries' in config and config['notified_entries']:
                    sorted_entries = sorted(
                        config['notified_entries'].items(),
                        key=lambda item: item[1]['time'] if isinstance(item[1], dict) and 'time' in item[1] else '',
                        reverse=True
                    )[:20]  # 只保留最新的20条
                    basic_config['notified_entries'] = dict(sorted_entries)
                
                # 彻底移除title_notifications等其他数据
                # basic_config中不包含title_notifications等，自动被清理
                
                # 使用清理后的配置
                config = basic_config
                config_str = json.dumps(config, ensure_ascii=False)
                logger.info(f"配置文件清理后大小: {len(config_str)/1024:.2f} KB，保留通知记录 {len(basic_config['notified_entries'])} 条")
        except (TypeError, ValueError) as e:
            logger.error(f"配置对象序列化失败: {e}")
            # 如果序列化失败，回退到默认配置
            config = DEFAULT_CONFIG
        
        # 先写入临时文件
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
        
        # 如果原配置文件存在，先创建备份
        if os.path.exists(CONFIG_FILE):
            try:
                # 尝试复制原文件为备份
                import shutil
                shutil.copy2(CONFIG_FILE, backup_file)
            except Exception as e:
                logger.warning(f"创建配置文件备份失败: {e}")
        
        # 将临时文件重命名为正式配置文件
        os.replace(temp_file, CONFIG_FILE)
        
        # 执行垃圾回收
        gc.collect()
    except Exception as e:
        logger.error(f"保存配置文件失败: {e}")
        # 如果有备份，尝试从备份恢复
        if os.path.exists(backup_file):
            try:
                # 尝试从备份恢复
                import shutil
                shutil.copy2(backup_file, CONFIG_FILE)
                logger.info("已从备份恢复配置文件")
            except Exception as e2:
                logger.error(f"从备份恢复配置文件失败: {e2}")
    finally:
        # 清理可能残留的临时文件
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except Exception:
                pass

def send_telegram_message(message, config, reply_to_message_id=None):
    bot_token = config['telegram']['bot_token']
    chat_id = config['telegram']['chat_id']
    if not bot_token or not chat_id:
        logger.error("Telegram配置不完整")
        return False
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        data = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML"
        }
        if reply_to_message_id:
            data["reply_to_message_id"] = reply_to_message_id
        response = requests.post(url, data=data)
        if response.status_code == 200:
            logger.info(f"Telegram消息发送成功")
            return True
        else:
            detail = response.text
            try:
                detail = response.json()
            except Exception:
                pass
            logger.error(f"Telegram消息发送失败: {detail}")
            return False
    except Exception as e:
        logger.error(f"Telegram消息发送异常: {e}")
        return False

def disable_telegram_webhook(bot_token):
    """删除Webhook，确保可以使用getUpdates进行长轮询"""
    try:
        url = f"https://api.telegram.org/bot{bot_token}/deleteWebhook"
        resp = requests.post(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("ok"):
                logger.info("已删除Telegram webhook，使用getUpdates监听指令")
            else:
                logger.warning(f"删除Webhook返回非OK: {data}")
        else:
            logger.warning(f"删除Webhook失败: HTTP {resp.status_code} {resp.text}")
    except Exception as e:
        logger.warning(f"删除Webhook时异常: {e}")

def set_telegram_bot_commands(bot_token):
    """设置机器人菜单命令，方便在Telegram中查看"""
    commands = [
        {"command": "add", "description": "增加/更新 /add mk clean +卖 -吗"},
        {"command": "del", "description": "删除关键词 /del 关键字"},
        {"command": "list", "description": "查看关键词列表"},
        {"command": "block", "description": "全局屏蔽 /block 关键字"},
        {"command": "unblock", "description": "取消全局屏蔽 /unblock 关键字"},
        {"command": "blocklist", "description": "查看全局屏蔽列表"},
        {"command": "status", "description": "查看运行状态"},
        {"command": "setinterval", "description": "设置检测间隔 /setinterval 30"},
        {"command": "setnotifylimit", "description": "设置通知记录上限"},
        {"command": "setsummary", "description": "匹配摘要 on/off"},
        {"command": "help", "description": "查看帮助"},
    ]
    try:
        url = f"https://api.telegram.org/bot{bot_token}/setMyCommands"
        resp = requests.post(url, json={"commands": commands}, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("ok"):
                logger.info("已设置Telegram命令菜单")
            else:
                logger.warning(f"设置命令菜单返回非OK: {data}")
        else:
            logger.warning(f"设置命令菜单失败: HTTP {resp.status_code} {resp.text}")
    except Exception as e:
        logger.warning(f"设置命令菜单时异常: {e}")

def bool_from_text(text):
    """解析 on/off/true/false/1/0"""
    t = text.strip().lower()
    if t in ('on', 'true', '1', 'yes', 'y'):
        return True
    if t in ('off', 'false', '0', 'no', 'n'):
        return False
    return None

def format_uptime():
    delta = datetime.datetime.now() - start_time
    days = delta.days
    seconds = delta.seconds
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if minutes:
        parts.append(f"{minutes}分")
    parts.append(f"{seconds}秒")
    return ''.join(parts)

def check_rss_feed(config):
    """检查RSS源并匹配关键词"""
    global last_rss_check_time, last_rss_error
    # 确保config字典包含必要的键
    if 'keywords' not in config:
        config['keywords'] = []
    if 'exclude_keywords' not in config:
        config['exclude_keywords'] = []
    if 'settings' not in config or not isinstance(config['settings'], dict):
        config['settings'] = copy.deepcopy(DEFAULT_CONFIG['settings'])
    
    if 'notified_entries' not in config or not isinstance(config['notified_entries'], dict):
        config['notified_entries'] = {}
        
    if not config['keywords']:
        logger.warning("没有设置关键词，跳过检查")
        return
        
    max_retries = 3
    retry_delay = 10
    config_changed = False
    for attempt in range(max_retries):
        try:
            logger.info("开始获取NodeSeek RSS源...")
            headers = {
                'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
            # 使用 NodeSeek 官方 RSS
            response = requests.get("https://rss.nodeseek.com/", headers=headers, timeout=30)
            if response.status_code != 200:
                logger.error(f"获取RSS失败，HTTP状态码: {response.status_code}")
                if attempt < max_retries - 1:
                    current_retry_delay = retry_delay * (attempt + 1)
                    time.sleep(current_retry_delay)
                    continue
                return
                
            logger.info("开始解析RSS内容...")
            feed = feedparser.parse(response.content)
            if not hasattr(feed, 'entries') or not feed.entries:
                logger.error("RSS解析失败或没有找到条目")
                if attempt < max_retries - 1:
                    current_retry_delay = retry_delay * (attempt + 1)
                    time.sleep(current_retry_delay)
                    continue
                return
                
            logger.info(f"成功获取RSS，共找到 {len(feed.entries)} 条帖子")
            last_rss_check_time = datetime.datetime.now()
            last_rss_error = None
            
            match_summary = bool(config['settings'].get('match_summary', True))
            regex_match = bool(config['settings'].get('regex_match', False))
            full_word_match = bool(config['settings'].get('full_word_match', False))
            
            # 准备全局排除列表
            global_exclude = [k.strip().lower() for k in config['exclude_keywords'] if isinstance(k, str) and k.strip()]
            
            # 准备正则缓存
            regex_cache = {}
            
            processed_count = 0
            for entry in feed.entries:
                try:
                    processed_count += 1
                    title = entry.title if hasattr(entry, 'title') else ''
                    link = entry.link if hasattr(entry, 'link') else ''
                    summary = ''
                    if match_summary:
                        if hasattr(entry, 'summary') and entry.summary:
                            summary = entry.summary
                        elif hasattr(entry, 'description') and entry.description:
                            summary = entry.description
                    
                    # 清理HTML
                    summary = re.sub(r'<[^>]+>', '', summary or '').strip()
                    summary = re.sub(r'\s+', ' ', summary)
                    
                    # 提取作者
                    author = ''
                    if hasattr(entry, 'author') and entry.author:
                        author = entry.author
                    elif hasattr(entry, 'author_detail') and entry.author_detail:
                        author = entry.author_detail.get('name', '')
                    elif hasattr(entry, 'dc_creator') and entry.dc_creator:
                        author = entry.dc_creator
                    
                    if not title or not link:
                        continue
                        
                    title = re.sub(r'<[^>]+>', '', title).strip()
                    title = re.sub(r'\s+', ' ', title)
                    if author:
                        author = re.sub(r'<[^>]+>', '', author).strip()
                        author = re.sub(r'\s+', ' ', author)
                    else:
                        author = '未知'
                        
                    # 生成ID
                    post_id = None
                    post_id_patterns = [r'/post-(\d+)', r'/post/(\d+)', r'/topic/(\d+)', r'/thread/(\d+)', r'-(\d+)$']
                    for pattern in post_id_patterns:
                        match = re.search(pattern, link)
                        if match:
                            post_id = match.group(1)
                            break
                    if not post_id and hasattr(entry, 'guid'):
                        guid_match = re.search(r'(\d+)', str(entry.guid))
                        if guid_match: post_id = guid_match.group(1)
                    
                    # 生成唯一Key
                    if author and author != '未知':
                         author_cleaned = re.sub(r'[\s\u3000\u00A0]+', '', author)
                         author_cleaned = re.sub(r'[^\w\u4e00-\u9fff]', '', author_cleaned)
                         author_normalized = author_cleaned.lower()
                    else:
                        author_normalized = 'unknown'
                        
                    if post_id:
                        unique_key = f"{post_id}_{author_normalized}"
                    else:
                        import hashlib
                        link_hash = hashlib.md5(link.encode()).hexdigest()[:8]
                        unique_key = f"{link_hash}_{author_normalized}"
                    
                    if unique_key in config['notified_entries']:
                        continue
                        
                    # 构造匹配文本
                    title_lower = title.lower()
                    summary_lower = summary.lower()
                    combined_text = title_lower + (' ' + summary_lower if match_summary and summary_lower else '')
                    
                    # 匹配函数
                    def check_match(text, pattern_str, headers=None):
                        # pattern_str 已经是 lower 的
                        if regex_match:
                            try:
                                pat = regex_cache.get(pattern_str)
                                if pat is None:
                                    pat = re.compile(pattern_str, re.IGNORECASE)
                                    regex_cache[pattern_str] = pat
                                return pat.search(text) is not None
                            except:
                                return False
                        if full_word_match:
                            return re.search(rf"\b{re.escape(pattern_str)}\b", text) is not None
                        return pattern_str in text

                    # 1. 检查全局排除
                    hit_global_exclude = False
                    for ek in global_exclude:
                        if check_match(combined_text, ek):
                            hit_global_exclude = True
                            break
                    if hit_global_exclude:
                        continue
                        
                    matched_rules = []
                    
                    # 2. 遍历所有关键字规则
                    # config['keywords'] 现在是 [{"word":"xxx", "include":[], "exclude":[]}, ...]
                    for rule in config['keywords']:
                        main_kw = rule.get('word', '').strip()
                        if not main_kw: continue
                        main_kw_lower = main_kw.lower()
                        
                        # A. 必须包含主关键字
                        if not check_match(combined_text, main_kw_lower):
                            continue
                            
                        # B. 检查局部排除 (Exclude) - 任何命中则跳过
                        local_exclude = rule.get('exclude', [])
                        hit_local_exclude = False
                        for lek in local_exclude:
                            if not isinstance(lek, str) or not lek.strip(): continue
                            if check_match(combined_text, lek.strip().lower()):
                                hit_local_exclude = True
                                break
                        if hit_local_exclude:
                            continue
                            
                        # C. 检查局部必含 (Include) - 列表为空则通过，否则需命中至少一个
                        local_include = rule.get('include', [])
                        valid_include = [li for li in local_include if isinstance(li, str) and li.strip()]
                        
                        if valid_include:
                            hit_any_include = False
                            for lik in valid_include:
                                if check_match(combined_text, lik.strip().lower()):
                                    hit_any_include = True
                                    break
                            if not hit_any_include:
                                continue # 有必含条件但并未满足
                                
                        matched_rules.append(main_kw)

                    # 发送通知
                    if matched_rules:
                        config['notified_entries'][unique_key] = {
                            'title': title,
                            'author': author,
                            'link': link,
                            'keywords': matched_rules,
                            'time': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        }
                        config_changed = True
                        
                        join_kws = ', '.join(matched_rules)
                        message = (
                            "<b>🎯 发现命中帖子</b>\n"
                            f"• <b>标题</b>：{title}\n"
                            f"• <b>匹配规则</b>：{join_kws}\n"
                            f"• <b>作者</b>：{author}\n"
                            f"• <b>链接</b>：{link}"
                        )
                        if send_telegram_message(message, config):
                            logger.info(f"检测到规则 [{join_kws}] 命中: '{title}'")
                        else:
                            if unique_key in config['notified_entries']:
                                del config['notified_entries'][unique_key]
                                
                except Exception as e:
                    logger.error(f"处理RSS条目 error: {e}")
                    continue
                    
            if config_changed:
                save_config(config)
                
        except Exception as e:
            last_rss_error = f"检查RSS时出错: {str(e)}"
            logger.error(last_rss_error)
            if attempt < max_retries - 1:
                time.sleep(retry_delay * (attempt + 1))

def restart_program(reason):
    logger.info(f"准备重启程序，原因: {reason}")
    if os.path.exists(PID_FILE):
        os.remove(PID_FILE)
    logger.info("正在重启程序...")
    os.execv(sys.executable, [sys.executable] + sys.argv)

def monitor_loop():
    logger.info("开始RSS监控")
    
    consecutive_errors = 0
    error_streak = 0
    max_consecutive_errors = 5
    detection_counter = 0
    process = psutil.Process(os.getpid())
    total_mem_mb = psutil.virtual_memory().total / 1024 / 1024
    # 自动重启策略：内存超过30%总内存且至少400MB（上限2000MB），或连续错误>=15，或运行超过24小时
    auto_error_restart_threshold = 15
    auto_uptime_hours_threshold = 24
    auto_mem_threshold_mb = min(max(400, total_mem_mb * 0.3), 2000)

    try:
        while True:
            config = load_config()  # 每次检测前都重新加载配置
            settings = config.get('settings', {})
            min_interval = int(settings.get('check_min_interval', DEFAULT_CONFIG['settings']['check_min_interval']))
            max_interval = int(settings.get('check_max_interval', DEFAULT_CONFIG['settings']['check_max_interval']))

            if min_interval <= 0:
                min_interval = DEFAULT_CONFIG['settings']['check_min_interval']
            if max_interval <= 0 or max_interval < min_interval:
                max_interval = max(min_interval, DEFAULT_CONFIG['settings']['check_max_interval'])

            try:
                check_rss_feed(config)
                consecutive_errors = 0
                error_streak = 0
                detection_counter += 1
                global detection_counter_state
                detection_counter_state = detection_counter
                logger.info(f"完成第 {detection_counter} 次RSS检测")
                
                # 自动重启策略：运行时长 / 内存
                runtime_hours = (datetime.datetime.now() - start_time).total_seconds() / 3600
                if runtime_hours >= auto_uptime_hours_threshold:
                    restart_program(f"运行时长超过 {auto_uptime_hours_threshold} 小时")

                mem_mb = process.memory_info().rss / 1024 / 1024
                if mem_mb >= auto_mem_threshold_mb:
                    restart_program(f"内存占用 {mem_mb:.1f} MB 超过智能阈值 {auto_mem_threshold_mb:.1f} MB")
                    
            except Exception as e:
                consecutive_errors += 1
                error_streak += 1
                logger.error(f"RSS监控异常: {e}")
                
                # 如果连续错误次数过多，增加检查间隔
                if consecutive_errors >= max_consecutive_errors:
                    logger.warning(f"连续出现{consecutive_errors}次错误，增加检查间隔")
                    long_wait = max_interval * 2
                    logger.info(f"等待{long_wait}秒后恢复检查...")
                    time.sleep(long_wait)
                    consecutive_errors = 0
                    # 不重置 error_streak，保留用于自动重启判定
                
                # 自动重启策略：连续错误
                if error_streak >= auto_error_restart_threshold:
                    restart_program(f"连续错误 {error_streak} 次，触发自动重启")
            
            # 生成随机等待时间
            check_interval = random.uniform(min_interval, max_interval)
            next_check_time = datetime.datetime.now() + datetime.timedelta(seconds=check_interval)
            logger.info(f"等待{check_interval:.2f}秒后进行下一次检查 (预计时间: {next_check_time.strftime('%H:%M:%S')})")
            time.sleep(check_interval)
    except KeyboardInterrupt:
        logger.info("监控被用户中断")
    except Exception as e:
        logger.error(f"监控循环严重异常: {e}")
    finally:
        # 清理PID文件
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)

def telegram_command_listener():
    """监听Telegram消息，支持关键词管理指令"""
    config = load_config()
    bot_token = config['telegram']['bot_token']
    chat_id = config['telegram']['chat_id']
    if not bot_token or not chat_id:
        logger.error("Telegram配置不完整，无法启动指令监听")
        return
    # 确保未设置Webhook（否则getUpdates会冲突）
    disable_telegram_webhook(bot_token)
    # 设置菜单命令，便于在Telegram中查看
    set_telegram_bot_commands(bot_token)
    offset = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
            params = {"timeout": 60, "offset": offset}
            resp = requests.get(url, params=params, timeout=65)
            if resp.status_code != 200:
                logger.error(f"获取Telegram更新失败: HTTP {resp.status_code} {resp.text}")
                if resp.status_code == 409:
                    # webhook冲突时再次删除
                    disable_telegram_webhook(bot_token)
                time.sleep(5)
                continue

            try:
                data = resp.json()
            except Exception as e:
                logger.error(f"解析Telegram更新响应失败: {e}，原始内容: {resp.text}")
                time.sleep(5)
                continue

            if not data.get("ok"):
                logger.error(f"获取Telegram更新返回错误: {data}")
                if data.get("error_code") == 409:
                    disable_telegram_webhook(bot_token)
                time.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message")
                if not message:
                    continue
                if str(message.get("chat", {}).get("id")) != str(chat_id):
                    continue  # 只响应指定chat_id
                text_raw = message.get("text", "").strip()
                msg_id = message.get("message_id")
                if not text_raw:
                    continue
                parts = text_raw.split(" ", 1)
                command = parts[0].split('@')[0].lower()
                arg = parts[1].strip() if len(parts) > 1 else ""
                settings = config.get('settings', {})

                if command == "/add":
                    if not arg:
                        send_telegram_message(
                            "<b>格式错误</b>\n"
                            "请使用：/add 关键字 [clean|clean-i|clean-e] [+必含] [-排除]\n"
                            "示例：/add mk clean +卖 -吗", 
                            config, msg_id
                        )
                        continue
                    
                    # 解析参数
                    tokens = arg.split()
                    keyword = tokens[0]
                    # 解析操作符
                    flags = [t.lower() for t in tokens[1:] if t.lower() in ('clean', 'clean-i', 'clean-e')]
                    # 解析包含/排除 (+xxx / -xxx)
                    includes_new = [t[1:] for t in tokens[1:] if t.startswith('+') and len(t) > 1]
                    excludes_new = [t[1:] for t in tokens[1:] if t.startswith('-') and len(t) > 1]
                    
                    # 检查全局排除
                    if any(keyword.lower() == k.strip().lower() for k in config['exclude_keywords']):
                        send_telegram_message(f"⚠️ 关键词 <b>{keyword}</b> 已在全局排除列表，无法添加", config, msg_id)
                        continue
                        
                    # 查找现有记录
                    target_item = None
                    exists = False
                    for item in config['keywords']:
                        if item['word'] == keyword:
                            target_item = item
                            exists = True
                            break
                    
                    if not target_item:
                        target_item = {"word": keyword, "include": [], "exclude": []}
                        config['keywords'].append(target_item)
                    
                    # 处理清理标记
                    if 'clean' in flags:
                        target_item['include'] = []
                        target_item['exclude'] = []
                    else:
                        if 'clean-i' in flags: target_item['include'] = []
                        if 'clean-e' in flags: target_item['exclude'] = []
                        
                    # 合并新规则 (去重)
                    for inc in includes_new:
                        if inc not in target_item['include']:
                            target_item['include'].append(inc)
                    for exc in excludes_new:
                        if exc not in target_item['exclude']:
                            target_item['exclude'].append(exc)
                            
                    save_config(config)
                    
                    # 构建反馈消息
                    info_parts = [f"<b>{keyword}</b>"]
                    if target_item['include']:
                        info_parts.append(f"必含: [{', '.join(target_item['include'])}]")
                    if target_item['exclude']:
                        info_parts.append(f"排除: [{', '.join(target_item['exclude'])}]")
                    if not target_item['include'] and not target_item['exclude']:
                        info_parts.append("(普通匹配)")
                        
                    action_str = "更新" if exists else "添加"
                    send_telegram_message(f"✅ 已{action_str}规则：{' '.join(info_parts)}", config, msg_id)

                elif command == "/del":
                    if not arg:
                        send_telegram_message("请输入要删除的关键词，例如：/del 关键字", config, msg_id)
                        continue
                    keyword = arg.strip()
                    # 查找并删除（匹配word字段）
                    original_len = len(config['keywords'])
                    config['keywords'] = [k for k in config['keywords'] if k['word'] != keyword]
                    
                    if len(config['keywords']) < original_len:
                        save_config(config)
                        send_telegram_message(f"🗑️ 已删除关键词：<b>{keyword}</b>", config, msg_id)
                    else:
                        send_telegram_message(f"❓ 关键词 <b>{keyword}</b> 不存在", config, msg_id)
                        
                elif command == "/list":
                    if not config['keywords']:
                        kw_msg = "（空）"
                    else:
                        lines = []
                        for i, item in enumerate(config['keywords']):
                            # item 是 dict: {"word": "xx", "include": [], "exclude": []}
                            line = f"{i+1}. <b>{item['word']}</b>"
                            extras = []
                            if item.get('include'):
                                extras.append(f"➕{','.join(item['include'])}")
                            if item.get('exclude'):
                                extras.append(f"⛔{','.join(item['exclude'])}")
                            if extras:
                                line += f" ({' '.join(extras)})"
                            lines.append(line)
                        kw_msg = '\n'.join(lines)
                        
                    blk_list = '\n'.join([f"{i+1}. {k}" for i, k in enumerate(config['exclude_keywords'])]) if config['exclude_keywords'] else "（空）"
                    
                    send_telegram_message(
                        "<b>📌 当前监控规则</b>\n"
                        f"{kw_msg}\n\n"
                        "<b>🚫 全局排除关键词</b>\n"
                        f"{blk_list}", config, msg_id)
                        
                elif command == "/block":
                    if not arg:
                        send_telegram_message("请输入排除关键词，例如：/block 关键字", config, msg_id)
                        continue
                    keyword = arg.strip()
                    key_lower = keyword.lower()
                    # 检查是否在监控列表（检查 word 字段）
                    if any(key_lower == k['word'].strip().lower() for k in config['keywords']):
                        send_telegram_message(f"⚠️ 关键词 <b>{keyword}</b> 已在监控列表，无法加入全局排除", config, msg_id)
                        continue
                    if any(key_lower == k.strip().lower() for k in config['exclude_keywords']):
                        send_telegram_message(f"ℹ️ 排除关键词 <b>{keyword}</b> 已存在", config, msg_id)
                    else:
                        config['exclude_keywords'].append(keyword)
                        # 去重列表
                        config['exclude_keywords'] = list(dict.fromkeys([k.strip() for k in config['exclude_keywords'] if k.strip()]))
                        save_config(config)
                        send_telegram_message(f"🚫 已添加全局排除：<b>{keyword}</b>", config, msg_id)
                        
                elif command == "/unblock":
                    if not arg:
                        send_telegram_message("请输入要删除的排除关键词，例如：/unblock 关键字", config, msg_id)
                        continue
                    keyword = arg.strip()
                    to_remove = [k for k in config['exclude_keywords'] if k.strip().lower() == keyword.lower()]
                    if to_remove:
                        for k in to_remove:
                            config['exclude_keywords'].remove(k)
                        save_config(config)
                        send_telegram_message(f"🗑️ 已删除全局排除：<b>{keyword}</b>", config, msg_id)
                    else:
                        send_telegram_message(f"❓ 全局排除关键词 <b>{keyword}</b> 不存在", config, msg_id)
                        
                elif command == "/blocklist":
                    if not config['exclude_keywords']:
                        send_telegram_message("🚫 当前没有设置任何全局排除关键词", config, msg_id)
                    else:
                        blk_list = '\n'.join([f"{i+1}. {k}" for i, k in enumerate(config['exclude_keywords'])])
                        send_telegram_message(f"<b>🚫 全局排除关键词列表</b>\n{blk_list}", config, msg_id)
                        
                elif command == "/setsummary":
                    if not arg:
                        send_telegram_message("请输入 on/off，例如：/setsummary on", config, msg_id)
                        continue
                    val = bool_from_text(arg)
                    if val is None:
                        send_telegram_message("参数无效，请使用 on 或 off", config, msg_id)
                        continue
                    config['settings']['match_summary'] = val
                    save_config(config)
                    send_telegram_message(f"🔎 已{'开启' if val else '关闭'}摘要匹配", config, msg_id)
                    
                elif command == "/setfullword":
                    if not arg:
                        send_telegram_message("请输入 on/off，例如：/setfullword on", config, msg_id)
                        continue
                    val = bool_from_text(arg)
                    if val is None:
                        send_telegram_message("参数无效，请使用 on 或 off", config, msg_id)
                        continue
                    config['settings']['full_word_match'] = val
                    save_config(config)
                    send_telegram_message(f"🧩 已{'开启' if val else '关闭'}完整词匹配", config, msg_id)
                    
                elif command == "/setregex":
                    if not arg:
                        send_telegram_message("请输入 on/off，例如：/setregex on", config, msg_id)
                        continue
                    val = bool_from_text(arg)
                    if val is None:
                        send_telegram_message("参数无效，请使用 on 或 off", config, msg_id)
                        continue
                    config['settings']['regex_match'] = val
                    save_config(config)
                    send_telegram_message(f"🧠 已{'开启' if val else '关闭'}正则匹配", config, msg_id)
                    
                elif command == "/setinterval":
                    if not arg:
                        send_telegram_message("请输入两个数字：/setinterval 最小秒 最大秒", config, msg_id)
                        continue
                    parts_num = arg.split()
                    if len(parts_num) != 2 or not all(p.isdigit() for p in parts_num):
                        send_telegram_message("格式错误，请使用：/setinterval 30 60", config, msg_id)
                        continue
                    min_v, max_v = map(int, parts_num)
                    if min_v <= 0 or max_v <= 0 or max_v < min_v:
                        send_telegram_message("区间无效，请确保 >0 且最大值 >= 最小值", config, msg_id)
                        continue
                    config['settings']['check_min_interval'] = min_v
                    config['settings']['check_max_interval'] = max_v
                    save_config(config)
                    send_telegram_message(f"⏱️ 检测间隔已更新为 <b>{min_v}-{max_v}</b> 秒", config, msg_id)
                    
                elif command == "/setnotifylimit":
                    if not arg or not arg.isdigit():
                        send_telegram_message("请输入数字：/setnotifylimit 50（0 表示不限制）", config, msg_id)
                        continue
                    limit = int(arg)
                    config['settings']['max_notified_entries'] = limit
                    save_config(config)
                    send_telegram_message(f"📦 通知去重上限已设置为 <b>{limit}</b>", config, msg_id)
                    
                elif command == "/status":
                    mem_mb = psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
                    last_check = last_rss_check_time.strftime('%Y-%m-%d %H:%M:%S') if last_rss_check_time else "无记录"
                    last_err = last_rss_error or "无"
                    interval_info = f"{settings.get('check_min_interval', 0)}-{settings.get('check_max_interval', 0)} 秒"
                    match_info = f"摘要匹配={'开' if settings.get('match_summary', True) else '关'}，完整词={'开' if settings.get('full_word_match', False) else '关'}，正则={'开' if settings.get('regex_match', False) else '关'}"
                    
                    total_mem_mb = psutil.virtual_memory().total / 1024 / 1024
                    auto_mem_threshold_mb = min(max(400, total_mem_mb * 0.3), 2000)
                    restart_info = (
                        f"自动重启：内存>{auto_mem_threshold_mb:.0f}MB 或运行>24小时 或连续错误>=15"
                    )
                    
                    msg = (
                        "<b>📊 运行状态</b>\n"
                        f"• 运行时长：{format_uptime()}\n"
                        f"• 内存占用：{mem_mb:.1f} MB\n"
                        f"• 关键词：{len(config['keywords'])} 个，全局排除：{len(config['exclude_keywords'])} 个\n"
                        f"• 上次RSS成功：{last_check}\n"
                        f"• 上次错误：{last_err}\n"
                        f"• 检测间隔：{interval_info}\n"
                        f"• 匹配设置：{match_info}\n"
                        f"• 重启策略：{restart_info}\n"
                        f"• 已完成检测：{detection_counter_state}"
                    )
                    send_telegram_message(msg, config, msg_id)
                    
                elif command == "/help" or command == "/start":
                    help_msg = (
                        "<b>🛠️ 指令列表（升级版）</b>\n"
                        "/add 关键字 [clean/-i/-e] [+包含] [-排除]\n"
                        "  └─ 示例: /add mk +出 -吗\n"
                        "  └─ 覆盖: /add mk clean +卖\n"
                        "/del 关键字 - 删除关键词\n"
                        "/list - 查看详细规则列表\n"
                        "/block 关键字 - 全局屏蔽\n"
                        "/unblock 关键字 - 取消全局屏蔽\n"
                        "/blocklist - 查看全局屏蔽列表\n"
                        "/setsummary on/off - 摘要匹配\n"
                        "/setfullword on/off - 完整词匹配\n"
                        "/setregex on/off - 正则匹配\n"
                        "/setinterval min max - 设置间隔\n"
                        "/setnotifylimit N - 设置上限\n"
                        "/status - 状态\n"
                    )
                    send_telegram_message(help_msg, config, msg_id)
            time.sleep(2)
        except Exception as e:
            logger.error(f"Telegram指令监听异常: {e}")
            time.sleep(5)
            
def init_config_from_env():
    """从环境变量初始化配置"""
    config = load_config()
    bot_token = os.environ.get('TG_BOT_TOKEN', '').strip()
    chat_id = os.environ.get('TG_CHAT_ID', '').strip()
    changed = False
    if bot_token and config['telegram']['bot_token'] != bot_token:
        config['telegram']['bot_token'] = bot_token
        changed = True
    if chat_id and config['telegram']['chat_id'] != chat_id:
        config['telegram']['chat_id'] = chat_id
        changed = True
    if changed:
        save_config(config)
    return config

if __name__ == "__main__":
    # 检查必要的库是否已安装
    missing_libraries = []
    try:
        import psutil
    except ImportError:
        missing_libraries.append("psutil")
    try:
        import feedparser
    except ImportError:
        missing_libraries.append("feedparser")
    if missing_libraries:
        print("检测到缺少以下库，请先安装:")
        for lib in missing_libraries:
            print(f"  - {lib}")
        print(f"pip install {' '.join(missing_libraries)}")
        sys.exit(1)

    # 初始化配置（从环境变量）
    config = init_config_from_env()
    if not config['telegram']['bot_token'] or not config['telegram']['chat_id']:
        logger.error("请设置TG_BOT_TOKEN和TG_CHAT_ID环境变量")
        print("请设置TG_BOT_TOKEN和TG_CHAT_ID环境变量")
        sys.exit(1)

    # 启动Telegram指令监听线程
    t = Thread(target=telegram_command_listener, daemon=True)
    t.start()

    # 启动监控主循环
    monitor_loop()
