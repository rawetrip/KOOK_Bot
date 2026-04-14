import asyncio
import aiohttp
import os
import datetime
import time
import json
import urllib.parse
import random
import logging
import certifi
from aiohttp import web
from khl import Bot, Message, EventTypes, Event
from khl.card import CardMessage, Card, Module, Element, Types
from huggingface_hub import HfApi, hf_hub_download
from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup
from functools import lru_cache
from motor.motor_asyncio import AsyncIOMotorClient

# ==========================================
# 1. 基础配置与数据库初始化
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get('BOT_TOKEN')
STEAM_API_KEY = os.environ.get('STEAM_API_KEY')
MONGO_URI = os.environ.get('MONGO_URI')
HF_TOKEN = os.environ.get('HF_TOKEN')
REPO_ID = os.environ.get('HF_REPO_ID')
OWNER_ID = os.environ.get('OWNER_ID')
APEX_API_KEY = os.environ.get('APEX_API_KEY')
AFDIAN_SECRET = os.environ.get('AFDIAN_SECRET')

async def afdian_webhook(request):
    """接收爱发电的付款通知，全自动给群主发货（支持时长无缝叠加）"""
    # 1. 简易安全校验：防止黑客恶意伪造付款通知
    secret = request.query.get('secret')
    if secret != AFDIAN_SECRET:
        return web.Response(status=403, text="Forbidden: 秘钥错误")
        
    try:
        data = await request.json()
        order_data = data.get('data', {}).get('order', {})
        
        # 2. 提取订单关键信息
        remark = str(order_data.get('remark', '')).strip() # 玩家在备注里填的 KOOK 频道 ID
        total_amount = float(order_data.get('total_amount', 0.0))
        out_trade_no = order_data.get('out_trade_no', 'unknown')
        
        # 3. 过滤掉瞎填备注的订单（KOOK 频道 ID 是纯数字，通常很长）
        if not remark.isdigit() or len(remark) < 10:
            logger.warning(f"[SaaS] 收到付款 {total_amount} 元，但买家备注的频道ID格式不对: '{remark}'，需人工核对。")
            return web.json_response({"ec": 200, "em": "ok"}) # 必须回 200，不然爱发电会一直重发
            
        # 4. 汇率换算：假设 30 元 = 30 天包月权限 (你可以根据自己的定价随便改)
        # 假设你想设置 1元 = 1天 的汇率（30元就是30天，300元就是300天）
        days = int(total_amount) 

        # 4. 汇率换算：精准匹配咱们设置的三档爱发电赞助方案
        if total_amount >= 250.0:
            days = 365  # 💎 违禁级：包年特权
        elif total_amount >= 30.0:
            days = 30   # 🥇 隐秘级：包月特权
        else:
            days = 0    # 🥉 军规级 (<30元)：纯打赏不发货
        
        if days > 0 and AUTH_COLLECTION is not None:
            # 5. 获取该频道当前的授权信息
            current_auth = await AUTH_COLLECTION.find_one({"_id": remark})
            now = datetime.datetime.now()
            
            # 6. 计算新的到期时间（核心优化：未过期则叠加，已过期则重置）
            if current_auth and current_auth.get('expire_at') and current_auth['expire_at'] > now:
                new_expire_at = current_auth['expire_at'] + datetime.timedelta(days=days)
            else:
                new_expire_at = now + datetime.timedelta(days=days)

            # 7. 全自动发货：改写 MongoDB 门禁时间
            await AUTH_COLLECTION.update_one(
                {"_id": guild_id}, # 👈 存入服务器 ID
                {"$set": {"expire_at": expire_at, "authorized_by": msg.author.username}},
                upsert=True
            )
            await msg.reply(f"✅ 授权成功！\n服务器 ID：`{guild_id}`\n有效期至：{expire_at.strftime('%Y-%m-%d')}")
            
        return web.json_response({"ec": 200, "em": "success"})
    except Exception as e:
        logger.error(f"[Webhook] 处理订单时发生异常: {e}")
        return web.Response(status=500, text="Internal Server Error")

DB_CLIENT = None
ECO_COLLECTION = None
AUTH_COLLECTION = None

async def init_db():
    global DB_CLIENT, ECO_COLLECTION, AUTH_COLLECTION
    if MONGO_URI:
        try:
            DB_CLIENT = AsyncIOMotorClient(
                MONGO_URI, 
                serverSelectionTimeoutMS=5000
            )
            db = DB_CLIENT['cs2_bot_db']
            ECO_COLLECTION = db['economy']
            AUTH_COLLECTION = db['authorized_channels']
            await DB_CLIENT.admin.command('ping')
            logger.info("[System] MongoDB 云端金库连接成功！")
        except Exception as e:
            logger.error(f"[System] 数据库连接失败: {e}")
    
PRICE_DICT = []
PRICE_CN_MAP = {}
PRICE_EN_MAP = {}
CRATES_DICT, CRATES_CASES, CRATES_CAPSULES = [], [], []
AFFORDABLE_CASES, AFFORDABLE_CAPSULES = [], []
DISPLAY_TRANS = {}
IS_PRICE_READY = False 
AIO_SESSION: aiohttp.ClientSession = None

api = HfApi() if HF_TOKEN and REPO_ID else None
bot = Bot(token=BOT_TOKEN)

WEAPON_MAP = {
    "ak47": "AK47", "awp": "AWP", "m4a1": "M4A4", "m4a1_silencer": "M4A1-S",      
    "deagle": "沙鹰", "glock": "格洛克", "usp_silencer": "USP", "ssg08": "鸟狙", 
    "knife": "近战武器", "p90": "P90", "mp9": "MP9", "mac10": "吹风机", 
    "taser": "电击枪", "famas": "法玛斯", "galilar": "加利尔", "sg556": "SG553", "aug": "AUG"
}
MAP_MAP = {
    "de_mirage": "荒漠迷城", "de_inferno": "炼狱小镇", "de_overpass": "死亡游乐园",
    "de_vertigo": "殒命大厦", "de_nuke": "核子危机", "de_ancient": "远古遗迹",
    "de_anubis": "阿努比斯", "de_dust2": "炙热沙城2"
}
CUSTOM_TRANS = {
    "Factory New": "崭新出厂", "Minimal Wear": "略有磨损", "Field-Tested": "久经沙场", 
    "Well-Worn": "破损不堪", "Battle-Scarred": "战痕累累", "StatTrak™": "暗金", "StatTrak": "暗金", 
    "Souvenir": "纪念品", "Butterfly Knife": "蝴蝶刀", "Karambit": "爪子刀" 
}
STD_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}

# ==========================================
# 2. 核心辅助工具
# ==========================================
@lru_cache(maxsize=128)
def _sync_search_skin_cached(search_tuple):
    return sorted(
        [i for i in PRICE_DICT if all(t in i["search_text"] for t in search_tuple)], 
        key=lambda x: x["price"], 
        reverse=True
    )

async def safe_delete_msg(bot_instance, msg_obj):
    if not msg_obj: return
    try:
        msg_id = getattr(msg_obj, 'id', getattr(msg_obj, 'msg_id', None))
        if isinstance(msg_obj, dict):
            msg_id = msg_obj.get('msg_id', msg_obj.get('id'))
        if msg_id:
            await bot_instance.client.gate.request('POST', 'message/delete', data={'msg_id': msg_id})
    except Exception as e: 
        logger.debug(f"[Clean] 消息删除失败: {e}")

# ==========================================
# 3. 异步数据同步模块
# ==========================================
async def async_fetch_json(url, headers=None):
    if not AIO_SESSION:
        return []
        
    try:
        async with AIO_SESSION.get(url, headers=headers or STD_HEADERS, timeout=60) as resp:
            if resp.status == 200:
                try:
                    return await resp.json()
                except Exception as e:
                    logger.error(f"[JSON解析错误] {url}: {e}")
    except Exception as e:
        logger.debug(f"[网络请求错误] {url}: {e}")
    return []

def update_affordable_crates():
    global AFFORDABLE_CASES, AFFORDABLE_CAPSULES
    AFFORDABLE_CASES = [c for c in CRATES_CASES if PRICE_CN_MAP.get(c.get('name'), {}).get('price', 5.0) <= 800]
    AFFORDABLE_CAPSULES = [c for c in CRATES_CAPSULES if PRICE_CN_MAP.get(c.get('name'), {}).get('price', 1.5) <= 800]

async def init_crates_data():
    global CRATES_DICT, CRATES_CASES, CRATES_CAPSULES, AFFORDABLE_CASES, AFFORDABLE_CAPSULES
    url = "https://cdn.jsdelivr.net/gh/ByMykel/CSGO-API@main/public/api/zh-CN/crates.json"
    try:
        CRATES_DICT = await async_fetch_json(url)
        if CRATES_DICT:
            CRATES_CASES = [c for c in CRATES_DICT if c.get('type') == 'Weapon Case' or '武器箱' in c.get('name', '')]
            CRATES_CAPSULES = [c for c in CRATES_DICT if c.get('type') == 'Sticker Capsule' or '胶囊' in c.get('name', '')]
            update_affordable_crates()
            logger.info(f"[Init] 掉落表加载成功。")
    except Exception as e:
        logger.error(f"[Init] 掉落表拉取失败: {e}")

async def init_translation_dictionary():
    global DISPLAY_TRANS
    dict_file = 'auto_dict_v4.json'
    if api:
        try: await asyncio.to_thread(hf_hub_download, repo_id=REPO_ID, filename=dict_file, repo_type="dataset", local_dir=".", token=HF_TOKEN)
        except: pass

    if os.path.exists(dict_file):
        try:
            with open(dict_file, 'r', encoding='utf-8') as f: 
                DISPLAY_TRANS = json.load(f).get("DISPLAY_TRANS", {})
                if DISPLAY_TRANS: return
        except: pass

    logger.info("[Init] 词库缓存失效，开始重建...")
    base_en = "https://cdn.jsdelivr.net/gh/ByMykel/CSGO-API@main/public/api/en"
    base_cn = "https://cdn.jsdelivr.net/gh/ByMykel/CSGO-API@main/public/api/zh-CN"
    trans = {}
    for cat in ["skins.json", "stickers.json", "crates.json", "agents.json"]:
        en, cn_data = await asyncio.gather(async_fetch_json(f"{base_en}/{cat}"), async_fetch_json(f"{base_cn}/{cat}"))
        cn = {i.get('id'): i.get('name') for i in cn_data if isinstance(i, dict)}
        for item in en:
            if isinstance(item, dict) and item.get('id') in cn: trans[item.get('name')] = cn[item.get('id')]
    trans.update(CUSTOM_TRANS)
    DISPLAY_TRANS = dict(sorted(trans.items(), key=lambda x: len(x[0]), reverse=True))
    with open(dict_file, 'w', encoding='utf-8') as f: json.dump({"DISPLAY_TRANS": DISPLAY_TRANS}, f, ensure_ascii=False)
    if api: await asyncio.to_thread(api.upload_file, path_or_fileobj=dict_file, path_in_repo=dict_file, repo_id=REPO_ID, repo_type="dataset", token=HF_TOKEN)

# 💡 1. 将辅助函数放在全局（所有函数的外面）
def _load_json_sync(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)

def _save_json_sync(data, filepath):
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)


# 💡 2. 更新器主函数
async def price_auto_updater():
    global IS_PRICE_READY, PRICE_DICT, PRICE_CN_MAP, PRICE_EN_MAP
    cache_file = 'price_cache_v4.json'
    
    if os.path.exists(cache_file):
        try:
            # ⬇️ 修复：读取缓存时也使用异步线程池，防止启动时卡顿
            PRICE_DICT = await asyncio.to_thread(_load_json_sync, cache_file)
            if PRICE_DICT:
                PRICE_CN_MAP = {i['cn_name']: i for i in PRICE_DICT}; PRICE_EN_MAP = {i['en_name']: i for i in PRICE_DICT}
                IS_PRICE_READY = True
        except: pass

    while True:
        try:
            async with AsyncSession(impersonate="chrome110", timeout=60) as session:
                resp = await session.get("https://api.skinport.com/v1/items?app_id=730&currency=CNY")
                if resp.status_code == 200:
                    data = resp.json()
                    new_prices = []
                    for item in data:
                        en_name = item.get('market_hash_name')
                        price = item.get('min_price') or item.get('suggested_price') or 0
                        if en_name and price:
                            cn_name = en_name
                            for eng, chn in DISPLAY_TRANS.items():
                                if eng in cn_name: cn_name = cn_name.replace(eng, chn)
                            cn_name = cn_name.replace("(崭新出厂)", "(崭新)").replace("(略有磨损)", "(略磨)").replace("(久经沙场)", "(久经)").replace("(破损不堪)", "(破损)").replace("(战痕累累)", "(战痕)")
                            new_prices.append({"en_name": en_name, "cn_name": cn_name, "search_text": f"{en_name} {cn_name}".lower(), "price": float(price)})
                    
                    if len(new_prices) > 1000:
                        PRICE_DICT = new_prices
                        PRICE_CN_MAP = {i['cn_name']: i for i in PRICE_DICT}; PRICE_EN_MAP = {i['en_name']: i for i in PRICE_DICT}
                        IS_PRICE_READY = True
                        
                        # ⬇️ 修复：写入缓存时使用异步线程池（你这里写得很对！）
                        await asyncio.to_thread(_save_json_sync, PRICE_DICT, cache_file)
                        
                        logger.info(f"[Price] 价格库同步成功，共 {len(PRICE_DICT)} 条。")
                        _sync_search_skin_cached.cache_clear()
                        await asyncio.sleep(86400)
                        continue
        except Exception as e: 
            logger.error(f"[Price] 更新异常: {e}")
            
        await asyncio.sleep(300)

async def get_all_data(steam_id: str):
    """并发获取玩家的个人信息、等级、封禁状态、CS2数据和库存"""
    if not AIO_SESSION:
        return {"summary": {}, "level": {}, "bans": {}, "stats": {}, "inv": {}}

    urls = {
        "summary": f"https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/?key={STEAM_API_KEY}&steamids={steam_id}",
        "level": f"https://api.steampowered.com/IPlayerService/GetSteamLevel/v1/?key={STEAM_API_KEY}&steamid={steam_id}",
        "bans": f"https://api.steampowered.com/ISteamUser/GetPlayerBans/v1/?key={STEAM_API_KEY}&steamids={steam_id}",
        "stats": f"https://api.steampowered.com/ISteamUserStats/GetUserStatsForGame/v2/?key={STEAM_API_KEY}&appid=730&steamid={steam_id}",
        "inv": f"https://steamcommunity.com/inventory/{steam_id}/730/2?l=schinese&count=1000"
    }
    
    async def fetch(key, url):
        try:
            async with AIO_SESSION.get(url, timeout=10) as resp:
                if resp.status == 200:
                    return key, await resp.json()
        except Exception as e:
            logger.debug(f"[Steam API] 获取 {key} 失败: {e}")
        return key, {}

    # 并发执行所有请求
    results = await asyncio.gather(*(fetch(k, v) for k, v in urls.items()))
    return dict(results)

# ==========================================
# 4. 指令逻辑
# ==========================================
@bot.command(name='help', prefixes=['/'])
async def show_help(msg: Message):
    """机器人的使用说明书与指令菜单"""
    help_text = """**🤖 游戏助手 | 指令菜单**
---
**🔫 CS2 核心功能**
> `/cs [SteamID64]` 
> 📊 查询官匹数据及实战评级（*注：需将 Steam 资料与库存设为公开*）

> `/skin [物品] [皮肤] [磨损]` 
> 💰 检索全网底价（*示例：`/skin AWP 可燃冰 崭新`*）
> 
> `/status` 
> 📡 查询官方服务器及匹配节点状态

> `/hltv` 
> 🏆 获取当前赛事比分及最新战况

**📦 娱乐经济系统**
> `/open [数量(1-10)]` 
> 🎰 模拟开箱并记录你的历史盈亏！（*示例：`/open 10` 进行十连抽*）

**🛸 APEX 英雄系统**
> `/apexstat [EA_ID] [平台]` 
> 🏅 查询排位分数及在线状态（*平台选填：PC / PS4 / X1*）

> `/apexmap` 
> 🗺️ 获取实时匹配与排位地图轮换

> `/apex [数量(50-500)]` 
> 🔴 模拟 APEX 开包，测测你的传家宝运气！（*示例：`/apex 50` 进行50连抽*）
---
💡 *如需开通本频道的高级娱乐授权，请访问服主爱发电主页赞助解锁。*"""

    card = Card(color="#4A90E2")
    card.append(Module.Section(Element.Text(help_text, type=Types.Text.KMD)))
    await msg.reply(CardMessage(card))
    
@bot.command(name='auth', prefixes=['/'])
async def authorize_guild(msg: Message, guild_id: str = "", days: str = "30"):
    """手动授权指令：改为授权整个服务器"""
    if msg.author.id != OWNER_ID:
        return await msg.reply("❌ 权限不足：仅限机器人开发者操作。")
    
    # 修复：确保变量名统一为 guild_id
    if not guild_id or not days.isdigit():
        return await msg.reply("用法：/auth [服务器ID] [天数]")

    expire_at = datetime.datetime.now() + datetime.timedelta(days=int(days))
    
    if AUTH_COLLECTION is None:
        return await msg.reply("❌ 数据库未连接，无法执行授权。")
        
    await AUTH_COLLECTION.update_one(
        {"_id": guild_id},  # 👈 数据库存入服务器 ID
        {"$set": {"expire_at": expire_at, "authorized_by": msg.author.username}},
        upsert=True
    )
    
    await msg.reply(f"✅ 授权成功！\n服务器 ID：`{guild_id}`\n有效期至：{expire_at.strftime('%Y-%m-%d')}")

@bot.command(name='open', prefixes=['/'])
async def simulate_case_opening(msg: Message, *args):
    """CS2 开箱：改为校验服务器 ID"""
    # 核心：使用 msg.guild_id 进行门禁检查
    auth_info = await AUTH_COLLECTION.find_one({"_id": msg.guild_id})
    
    if not auth_info:
        return await msg.reply("⚠️ 本服务器未获得高级授权。请联系服主前往爱发电赞助，备注填入【服务器ID】即可自动解锁。")
    
    if auth_info['expire_at'] < datetime.datetime.now():
        return await msg.reply("⏰ 本服务器授权已过期。请续费后继续使用。")
        
    if not IS_PRICE_READY or not CRATES_CASES:
        return await msg.reply("系统正在同步数据，请稍后...")
    
    count = 1
    if args and args[0].isdigit():
        count = max(1, min(10, int(args[0])))

    try:
        opened_items = []
        total_cost_all, total_earned_all = 0.0, 0.0
        tiers_list = ['gold', 'red', 'pink', 'purple', 'blue']
        tiers_weights = [1.00, 2.50, 10.00, 30.00, 56.50]

        for _ in range(count):
            is_capsule = random.random() < 0.25
            valid_crates = AFFORDABLE_CAPSULES if is_capsule else AFFORDABLE_CASES 
            if not valid_crates: continue
            
            crate = random.choice(valid_crates)
            crate_name = crate.get('name')

            crate_price_data = PRICE_CN_MAP.get(crate_name)
            crate_market_price = crate_price_data['price'] if crate_price_data else (1.5 if is_capsule else 5.0)

            is_key_required = (crate.get('type') in ['Weapon Case', '武器箱'] or '武器箱' in crate_name)
            single_cost = crate_market_price + (17.5 if is_key_required else 0.0)

            contains = crate.get('contains', [])
            contains_rare = crate.get('contains_rare', []) 

            tiers = {'gold': [], 'red': [], 'pink': [], 'purple': [], 'blue': []}
            for item in contains:
                rarity_name = str(item.get('rarity', {}).get('name', '') if isinstance(item.get('rarity'), dict) else item.get('rarity', ''))
                item_name = str(item.get('name', ''))

                if any(k in rarity_name for k in ['违禁', 'Contraband']) or any(k in item_name for k in ['★', '纯金', 'Gold', '金色']):
                    tiers['gold'].append(item)
                elif any(k in rarity_name for k in ['隐秘', 'Covert', '非凡', 'Extraordinary', '大师', 'Master']):
                    tiers['red'].append(item)
                elif any(k in rarity_name for k in ['保密', 'Classified', '奇异', 'Exotic']):
                    tiers['pink'].append(item)
                elif any(k in rarity_name for k in ['受限', 'Restricted', '卓越', 'Remarkable', 'Exceptional']):
                    tiers['purple'].append(item)
                else: 
                    tiers['blue'].append(item)

            selected_tier = random.choices(tiers_list, weights=tiers_weights, k=1)[0]
            won_item_raw, won_item_name, won_item_price = None, "未知物品", 0.0

            if selected_tier == 'gold':
                if contains_rare: won_item_raw = random.choice(contains_rare)
                elif tiers['gold']: won_item_raw = random.choice(tiers['gold'])
                else:
                    if not is_capsule:
                        # 修复：防止 PRICE_DICT 中没有刀具时触发 IndexError
                        knives = [i for i in PRICE_DICT if any(k in i['cn_name'] for k in ["刀", "手套", "★"])]
                        if knives:
                            won_item_dict = random.choice(knives)
                            won_item_name = won_item_dict['cn_name']
                            won_item_price = won_item_dict['price']
                        else:
                            won_item_name = "未知罕见物品"
                            won_item_price = 2000.0
                        won_item_raw = "GOLDBACK"
                    else:
                        selected_tier = 'red'
                        
            if not won_item_raw:
                fallback_order = ['red', 'pink', 'purple', 'blue', 'gold']
                start_idx = fallback_order.index(selected_tier)
                search_list = fallback_order[start_idx:] + fallback_order[:start_idx]
                
                for t in search_list:
                    if tiers[t]:
                        won_item_raw = random.choice(tiers[t])
                        selected_tier = t
                        break
            
            if not won_item_raw and won_item_raw != "GOLDBACK":
                logger.error(f"[Open] 掉落池为空 - 箱子: {crate_name}")
                return await msg.reply(f"[Error] 模拟中断：{crate_name} 的数据池异常。")

            if won_item_raw != "GOLDBACK":
                base_name = str(won_item_raw.get('name', ''))
                
                matched_items = []
                if base_name:
                    for i in PRICE_DICT:
                        if base_name in i['cn_name'] or base_name in i['en_name']:
                            cn = i['cn_name']
                            if "闪亮" in cn and "闪亮" not in base_name: continue
                            if "全息" in cn and "全息" not in base_name: continue
                            if "斑斓" in cn and "斑斓" not in base_name: continue
                            if "暗金" in cn and "暗金" not in base_name and "StatTrak" not in base_name: continue
                            if "纪念品" in cn and "纪念品" not in base_name: continue
                            if ("金" in cn or "Gold" in cn) and ("金" not in base_name and "Gold" not in base_name): continue
                            
                            matched_items.append(i)
                
                if matched_items:
                    chosen_wear_item = random.choice(matched_items)
                    won_item_name = chosen_wear_item['cn_name']
                    won_item_price = chosen_wear_item['price']
                else:
                    won_item_name = base_name if base_name else "未知物品"
                    won_item_price = {'gold': 2000.0, 'red': 200.0, 'pink': 50.0, 'purple': 15.0}.get(selected_tier, 1.0)

            tier_info = {
                'gold': ("罕见级", "#FFD700"), 'red': ("隐秘级", "#EB4B4B"),
                'pink': ("保密级", "#D32CE6"), 'purple': ("受限级", "#8847FF"),
                'blue': ("军规级", "#4B69FF")
            }
            rarity_name_display, color_code = tier_info.get(selected_tier, tier_info['blue'])

            total_cost_all += single_cost
            total_earned_all += won_item_price

            opened_items.append({
                'crate_name': crate_name, 'item_name': won_item_name, 'price': won_item_price,
                'cost': single_cost, 'tier': selected_tier, 'rarity_name': rarity_name_display, 'color': color_code
            })

        if not opened_items:
            return await msg.reply("[Error] 模拟执行失败，底层随机种子生成终止。")

        profit_all = total_earned_all - total_cost_all
        user_id, user_name = str(msg.author.id), msg.author.username
        
        if ECO_COLLECTION is not None:
            updated_doc = await ECO_COLLECTION.find_one_and_update(
                {"_id": user_id}, 
                {
                    "$set": {"name": user_name},
                    "$inc": {
                        "spent": total_cost_all,
                        "earned": total_earned_all,
                        "profit": profit_all,
                        "opens": count
                    }
                },
                upsert=True, 
                return_document=True 
            )
            total_profit = updated_doc.get("profit", profit_all)
            total_opens = updated_doc.get("opens", count)
        else:
            total_profit, total_opens = profit_all, count
            logger.error("[Economy] 数据库未连接，本次数据未保存！")
            
        best_item = max(opened_items, key=lambda x: x['price'])
        card = Card(color=best_item['color']) 
        
        if count == 1:
            item = opened_items[0]
            card.append(Module.Header(f"单次开箱结果："))
            cost_text = f"¥ {item['cost']:.2f}" if "钥匙" not in item['crate_name'] and "箱" not in item['crate_name'] else f"¥ {item['cost']:.2f} (含匙)"
            lines = [
                f"**模拟箱子**：{item['crate_name']}", f"**品质**：{item['rarity_name']}", "---",
                f"**物品**：{item['item_name']}", f"**价格**：¥ {item['price']:.2f}", f"**成本**：{cost_text}",
                f"**本轮盈亏**：{'+ ¥ ' if profit_all > 0 else '- ¥ '}{abs(profit_all):.2f}",
                f"**历史净值**：{'¥ ' if total_profit > 0 else '- ¥ '}{abs(total_profit):.2f} (调用量: {total_opens})"
            ]
            card.append(Module.Section(Element.Text("\n".join(lines), type=Types.Text.KMD)))
        else:
            card.append(Module.Header(f"随机箱子{count}连开"))
            lines = [f"`#{idx+1}` [{i['rarity_name'].split('/')[0]}] **{i['item_name']}** ▶ `¥ {i['price']:.2f}`" for idx, i in enumerate(opened_items)]
            lines.extend([
                "---", f"**总消耗**：¥ {total_cost_all:.2f}  |  **总产出**：¥ {total_earned_all:.2f}",
                f"**本轮盈亏**：{'+ ¥ ' if profit_all > 0 else '- ¥ '}{abs(profit_all):.2f}",
                f"**历史净值**：{'¥ ' if total_profit > 0 else '- ¥ '}{abs(total_profit):.2f} (总开箱数: {total_opens})"
            ])
            card.append(Module.Section(Element.Text("\n".join(lines), type=Types.Text.KMD)))

        context_text = f"[ 用户: {user_name} ] "
        if best_item['tier'] == 'gold' or profit_all > 150: context_text += "你牛大了"
        elif profit_all < -(15 * count): context_text += "亏麻了"
        else: context_text += "不赖"
            
        card.append(Module.Context(Element.Text(context_text, type=Types.Text.KMD)))
        await msg.reply(CardMessage(card))
        
    except Exception as e:
        logger.error(f"[Open] 模块崩溃: {e}", exc_info=True)
        await msg.reply("[Error] 计算引擎发生意外终止。")

@bot.command(name='skin', prefixes=['/'])
async def search_skin(msg: Message, *args):
    try:
        if not IS_PRICE_READY: return await msg.reply("数据库正在初始化，请稍后尝试...")
        
        if not args:
            card = Card(color="#4A90E2")
            card.append(Module.Header("市场饰品快速检索"))
            card.append(Module.Context(Element.Text("输入参数为空，请点击下方按钮获取推荐：", type=Types.Text.KMD)))
            card.append(Module.ActionGroup(
                Element.Button("主战武器", value="skin_random|rifle", click=Types.Click.RETURN_VAL, theme=Types.Theme.PRIMARY),
                Element.Button("近战匕首", value="skin_random|knife", click=Types.Click.RETURN_VAL, theme=Types.Theme.DANGER),
                Element.Button("专业手套", value="skin_random|glove", click=Types.Click.RETURN_VAL, theme=Types.Theme.WARNING),
                Element.Button("狙击步枪", value="skin_random|sniper", click=Types.Click.RETURN_VAL, theme=Types.Theme.INFO)
            ))
            return await msg.reply(CardMessage(card))
        
        search_terms = tuple(t.lower() for t in args)
        results = _sync_search_skin_cached(search_terms)
        if not results: return await msg.reply("未找到符合该特征的物品，请减少搜索关键词。")
        
        card = Card(color="#4A90E2")
        card.append(Module.Header(f"检索结果：{' '.join(args)}"))
        
        lines = [f"`#{idx+1}` {i['cn_name']} | ¥ {i['price']:.2f}" for idx, i in enumerate(results[:10])]
        card.append(Module.Section(Element.Text("```\n" + "\n".join(lines) + "\n```", type=Types.Text.KMD)))
        card.append(Module.Context(Element.Text("**点击下方对应编号，查阅详细数据：**", type=Types.Text.KMD)))
        
        for i in range(0, min(8, len(results)), 4):
            group_btns = [Element.Button(f"详细 #{i+idx+1}", value=f"skin_chart|{item['en_name']}", click=Types.Click.RETURN_VAL, theme=Types.Theme.PRIMARY) 
                          for idx, item in enumerate(results[i:i+4])]
            card.append(Module.ActionGroup(*group_btns))
            
        await msg.reply(CardMessage(card))
    except Exception as e:
        logger.error(f"[Skin] 检索模块异常: {e}", exc_info=True)
        await msg.reply("[Error] 检索模块触发异常。")

@bot.on_event(EventTypes.MESSAGE_BTN_CLICK)
async def on_skin_button_click(b: Bot, e: Event):
    val = e.body.get('value', '')
    channel_id = e.body.get('target_id')
    user_id = e.body.get('user_info', {}).get('id')

    try:
        channel = await b.client.fetch_public_channel(channel_id)
    except Exception as ex:
        return logger.error(f"[Button] 无法获取频道对象: {ex}")

    if val.startswith("skin_random|"):
        category = val.split("|")[1]
        pool, title = [], ""
        exclude_words = ["印花", "布章", "Charm", "挂饰", "挂件", "探员", "音乐盒", "徽章"]

        if category == "rifle":
            pool = [i for i in PRICE_DICT if any(k in i['cn_name'] for k in ["AK-47", "M4A4", "M4A1-S", "AUG", "SG 553", "法玛斯", "加利尔"]) and not any(e in i['cn_name'] for e in exclude_words)]
            title = "推荐分类：主战"
        elif category == "knife":
            pool = [i for i in PRICE_DICT if "★" in i['cn_name'] and "手套" not in i['cn_name'] and "绑带" not in i['cn_name'] and not any(e in i['cn_name'] for e in exclude_words)]
            title = "推荐分类：匕首"
        elif category == "glove":
            pool = [i for i in PRICE_DICT if "★" in i['cn_name'] and ("手套" in i['cn_name'] or "绑带" in i['cn_name'])]
            title = "推荐分类：手套"
        elif category == "sniper":
            pool = [i for i in PRICE_DICT if ("AWP" in i['cn_name'] or "SSG 08" in i['cn_name']) and not any(e in i['cn_name'] for e in exclude_words)]
            title = "推荐分类：大狙"

        if not pool: return

        selected_items = sorted(random.sample(pool, min(8, len(pool))), key=lambda x: x["price"], reverse=True)

        card = Card(color="#4A90E2")
        card.append(Module.Header(title))

        lines = [f"`#{idx+1}` {i['cn_name']} | ¥ {i['price']:.2f}" for idx, i in enumerate(selected_items)]
        card.append(Module.Section(Element.Text("```\n" + "\n".join(lines) + "\n```", type=Types.Text.KMD)))
        card.append(Module.Context(Element.Text(f"<@{user_id}> **点击对应编号，获取市场明细：**", type=Types.Text.KMD)))

        for i in range(0, len(selected_items), 4):
            group_btns = [Element.Button(f"详细 #{i+idx+1}", value=f"skin_chart|{item['en_name']}", click=Types.Click.RETURN_VAL, theme=Types.Theme.PRIMARY) 
                          for idx, item in enumerate(selected_items[i:i+4])]
            card.append(Module.ActionGroup(*group_btns))

        return await channel.send(CardMessage(card))

    if val.startswith("skin_chart|"):
        en_name = val.split("|", 1)[1]
        target_item = PRICE_EN_MAP.get(en_name)
        if not target_item: return
            
        card = Card(color="#2F3136")
        card.append(Module.Header(f"物品数据档案：{target_item['cn_name']}"))
        card.append(Module.Section(Element.Text(f"**市场参考底价**：`¥ {target_item['price']:.2f}`", type=Types.Text.KMD)))
        
        full_en_name = urllib.parse.quote(target_item["en_name"])
        base_en_name = urllib.parse.quote(target_item["en_name"].split(" (")[0])
        
        card.append(Module.ActionGroup(
            Element.Button("Steam 社区市场", f"https://steamcommunity.com/market/search?appid=730&q={full_en_name}", Types.Click.LINK, theme=Types.Theme.PRIMARY),
            Element.Button("Skinport 交易流", f"https://skinport.com/market?search={base_en_name}", Types.Click.LINK, theme=Types.Theme.SECONDARY)
        ))
        card.append(Module.Context(Element.Text("注：Buff,UUyp,IGXE 因底层架构需携带内部 ID 且强制登录拦截，暂不提供快捷直达。", type=Types.Text.KMD)))
        
        await channel.send(CardMessage(card))

@bot.command(name='cs', prefixes=['/'])
async def query_full_profile(msg: Message, steam_id: str = ""):
    if not steam_id or not steam_id.isdigit():
        return await msg.reply("[Error] 参数校验失败：请输入17位数字型 SteamID。")

    loading_msg = await msg.reply(f"正在连接 Steam 官方数据节点，同步玩家 {steam_id} 的档案...")
    try:
        d = await get_all_data(steam_id)
        # 修复 1：防止 players 列表为空时触发 IndexError
        players = d['summary'].get('response', {}).get('players', [])
        summary = players[0] if players else None
    
        if not summary:
            await safe_delete_msg(bot, loading_msg)
            return await msg.reply("[Error] 查无此人。可能原因：数据私密或参数无效。")

        raw_avatar = str(summary.get('avatarfull', '')).strip()
        avatar_url = raw_avatar if raw_avatar.startswith("http") else "https://avatars.steamstatic.com/fef49e7fa7e1997310d705b2a6158ff8dc1cdfeb_full.jpg"
        raw_name = str(summary.get('personaname', 'Unknown'))
        safe_name = "".join(c for c in raw_name if c.isprintable()).strip()[:15] or "Unknown"
        profile_url = str(summary.get('profileurl', f"https://steamcommunity.com/profiles/{steam_id}")).strip()

        created = summary.get('timecreated')
        years = (datetime.datetime.now() - datetime.datetime.fromtimestamp(created)).days // 365 if created else 0
        level = d['level'].get('response', {}).get('player_level', 0) if isinstance(d['level'], dict) else 0
        bans = d['bans'].get('players', [{}])[0] if isinstance(d['bans'], dict) else {}
        is_banned = bans.get('VACBanned') or (bans.get('NumberOfGameBans', 0) > 0)

        stats_res = d['stats']
        weapon_line, map_line, has_stats = "无相关数据", "无相关数据", isinstance(stats_res, dict) and 'playerstats' in stats_res
        recent_stats = None 
        
        if has_stats:
            s = {i['name']: i['value'] for i in stats_res.get('playerstats', {}).get('stats', [])}
            k, dt = s.get('total_kills', 0), s.get('total_deaths', 1)
            k_d = round(k/dt, 2) if dt > 0 else 0
            
            invalid_weapons = ['headshot', 'enemy_weapon', 'enemy_blinded', 'knife_fight', 'against_zoomed_sniper', 'taser']
            w_stats = {key.replace('total_kills_', ''): v for key, v in s.items() if key.startswith('total_kills_') and key != 'total_kills' and not any(x in key for x in invalid_weapons)}
            top_w = sorted(w_stats.items(), key=lambda x: x[1], reverse=True)[:3]
            weapon_line = " / ".join([f"{WEAPON_MAP.get(w, w)}:{v}" for w, v in top_w])
            
            m_stats = {key.replace('total_wins_map_', ''): v for key, v in s.items() if key.startswith('total_wins_map_de_')}
            top_m = sorted(m_stats.items(), key=lambda x: x[1], reverse=True)
            map_line = f"{MAP_MAP.get(top_m[0][0], top_m[0][0])} (胜场: {top_m[0][1]})" if top_m else "无胜场记录"

            dmg = s.get('total_damage_done', 0)
            rounds = s.get('total_rounds_played', 0) # 修复 2：默认值改为 0
            won_matches = s.get('total_matches_won', 0)
            play_matches = s.get('total_matches_played', 0)
    
            adr_val = dmg / rounds if rounds > 0 else 0
    
            if play_matches > 0 and won_matches > 0:
                win_rate_str = f"{(won_matches / play_matches) * 100:.1f}%"
            else:
                won_rounds = s.get('total_wins', 0)
                # 修复 3：防止除以零
                win_rate_str = f"{(won_rounds / rounds) * 100:.1f}%" if rounds > 0 else "0.0%"

            if rounds > 0:
                kpr = min(k / rounds, 1.5)           
                dpr = min(dt / rounds, 1.0)          
                spr = max(0.0, 1.0 - dpr)            
                clamped_adr = min(adr_val, 150.0)    

                estimated_impact = 2.13 * kpr + (0.42 * 0.14) - 0.41
                estimated_kast = (spr * 100) + 38.0
                estimated_kast = max(50.0, min(estimated_kast, 85.0)) 

                raw_rating = (
                    0.0073 * estimated_kast +
                    0.3591 * kpr -
                    0.5329 * dpr +
                    0.2372 * estimated_impact +
                    0.0032 * clamped_adr +
                    0.1587
                )
                custom_rating = min(round(raw_rating, 2), 2.99)
            else:
                custom_rating = 0.00
                
            recent_stats = {
                "avg_rating": f"{custom_rating:.2f}", 
                "avg_adr": f"{adr_val:.1f}",          
                "win_rate": win_rate_str,
                "matches_count": "Steam 全模式综合 (含死斗)"
            }

        inv = d['inv']
        total_val = 0
        top_3_items = []
        if isinstance(inv, dict) and 'assets' in inv:
            item_count = inv.get('total_inventory_count', 0)
            if IS_PRICE_READY:
                desc_map = {desc['classid']: desc.get('market_hash_name', '') for desc in inv.get('descriptions', [])}
                item_list = []
                for asset in inv.get('assets', []):
                    en_name = desc_map.get(asset['classid'], '')
                    if en_name and en_name in PRICE_EN_MAP:
                        item_price = PRICE_EN_MAP[en_name]['price']
                        item_list.append({"name": PRICE_EN_MAP[en_name]['cn_name'], "price": item_price})
                        total_val += item_price
                item_list.sort(key=lambda x: x['price'], reverse=True)
                top_3_items = item_list[:3]
                inv_text = f"持有 {item_count} 项 | 预估市值 ¥ {total_val:,.2f}"
            else:
                inv_text = f"持有 {item_count} 项 | 预估市值 (同步中...)"
        else:
            inv_text = "[受限权限] 资产设定为不可见"

        card = Card(color="#2F3136") 
        card.append(Module.Container(Element.Image(src="https://media.st.dl.eccdnx.com/steam/apps/730/capsule_616x353.jpg")))
        card.append(Module.Header(f"CS2 官匹档案: {safe_name}"))

        lines = [
            f"**账户年限**：{years} 年 | **等级**：Lv.{level}",
            f"**违规状态**：{'被封禁' if is_banned else '正常'}",
            f"**资产状况**：{inv_text}"
        ]
        if top_3_items:
            lines.append("---")
            lines.append("**前三饰品：**")
            for idx, it in enumerate(top_3_items):
                lines.append(f"`{idx+1}.` {it['name'].replace('`', '')}  ▶  `¥ {it['price']:,.2f}`")
        card.append(Module.Section(Element.Text("\n".join(lines), type=Types.Text.KMD), accessory=Element.Image(src=avatar_url, size=Types.Size.LG)))

        if has_stats:
            stat_lines = [
                "---",
                f"**主枪**：{weapon_line}\n**强图**：{map_line}",
                f"**K/D**：`{k_d}` | **爆头率**：`{round((s.get('total_kills_headshot', 0)/k)*100, 1) if k > 0 else 0}%`"
            ]
            card.append(Module.Section(Element.Text("\n".join(stat_lines), type=Types.Text.KMD)))
        else:
            card.append(Module.Section(Element.Text("---\n> [数据加密] 服务器离线或没有公开个人资料", type=Types.Text.KMD)))

        if recent_stats:
            card.append(Module.Divider())
            title_suffix = f"近 {recent_stats['matches_count']} 场" if isinstance(recent_stats['matches_count'], int) else recent_stats['matches_count']
            recent_lines = [
                f"**胜率**：`{recent_stats['win_rate']}`  |  **Rating**：`{recent_stats['avg_rating']}`  |  **ADR**：`{recent_stats['avg_adr']}`",
            ]
            try:
                r_val = float(recent_stats['avg_rating'])
                if r_val >= 1.25: 
                    recent_lines.append("> 评价：S级 | 绝对的大哥数据")
                elif r_val >= 1.10:
                    recent_lines.append("> 评价：A级 | 队伍核心火力，近期发挥相当出色")
                elif r_val >= 0.95:
                    recent_lines.append("> 评价：B级 | 中规中矩的正常玩家，团队的稳定基石")
                elif r_val >= 0.85:
                    recent_lines.append("> 评价：C级 | 状态略显低迷，可能正在抗压")
                else: 
                    recent_lines.append("> 评价：D级 | 纯纯的绿色环保玩家，主打一个陪伴")
            except: 
                pass
                
            card.append(Module.Section(Element.Text("\n".join(recent_lines), type=Types.Text.KMD)))
            card.append(Module.Context(Element.Text("💡 核心数据因Value数据源问题可能不准确", type=Types.Text.KMD)))
            
        else:
            card.append(Module.Divider())
            warning_text = (
                "**[ 核心实战评级 ]**\n"
                "> ⚠️ **未获取到公开评级**。可能因账号为新号或被第三方平台拦截。\n"
                "> 💡 **解锁特权**：建议前往 [Leetify官网](https://leetify.com/) 登录 Steam。只需一次，即可永久为您解锁【1秒极速查战绩】通道！"
            )
            card.append(Module.Section(Element.Text(warning_text, type=Types.Text.KMD)))

        card.append(Module.ActionGroup(
            Element.Button("查看Steam主页", profile_url, Types.Click.LINK, theme=Types.Theme.SECONDARY),
            Element.Button("完整数据（需登录）", f"https://csstats.gg/player/{steam_id}", Types.Click.LINK, theme=Types.Theme.PRIMARY)
        ))
        
        await safe_delete_msg(bot, loading_msg)
        await msg.reply(CardMessage(card))
        
    except Exception as e:
        logger.error(f"[CS] 档案聚合异常: {e}", exc_info=True)
        await safe_delete_msg(bot, loading_msg)
        await msg.reply("[Error] 卡片解析发生异常。")

@bot.command(name='status', prefixes=['/'])
async def check_cs2_status(msg: Message):
    loading_msg = await msg.reply("正在查询服务器状态...")
    try:
        url_official = f"https://api.steampowered.com/ICSGOServers_730/GetGameServersStatus/v1/?key={STEAM_API_KEY}"
        url_web_test = "https://steamcommunity.com/market/search/render/?appid=730&count=1"
        
        # 修复：封装异步请求，确保使用 async with 正确释放连接
        async def fetch_off():
            async with AIO_SESSION.get(url_official, timeout=12) as resp:
                return resp.status, await resp.json() if resp.status == 200 else {}
                
        async def fetch_web():
            async with AIO_SESSION.get(url_web_test, timeout=12) as resp:
                return resp.status

        off_res, web_status = await asyncio.gather(fetch_off(), fetch_web(), return_exceptions=True)

        services, mm = {}, {}
        if isinstance(off_res, tuple) and off_res[0] == 200:
            data = off_res[1]
            res = data.get('result', {})
            services = res.get('services', {})
            mm = res.get('matchmaking', {})

        real_inv_status = "Offline"
        if isinstance(web_status, int):
            if web_status == 200:
                real_inv_status = "Online"
            elif web_status == 429:
                real_inv_status = "Surge"
            else:
                real_inv_status = f"[ERROR] HTTP {web_status}"

        status_map = {
            "normal": "Online",
            "offline": "Offline",
            "delayed": "Delay",
            "surge": "Surge"
        }

        s_logon = services.get('SessionsLogon', 'unknown')
        s_gc = mm.get('scheduler', 'unknown')

        logon_str = status_map.get(s_logon, "[UNKNOWN] 未知状态")
        gc_str = status_map.get(s_gc, "[UNKNOWN] 未知状态")

        all_good = (s_logon == "normal") and ("ONLINE" in real_inv_status)
        card_color = "#4CAF50" if all_good else "#FF9800"

        card = Card(color=card_color)
        card.append(Module.Header("Steam 服务器状态"))
        
        lines = [
            f"**Steam 登录系统：** {logon_str}",
            f"**库存服务：** {real_inv_status}",
            f"**游戏协调器：** {gc_str}",
            "---",
            f"**全球在线玩家：** `{mm.get('online_players', 0):,}`",
            f"**正在匹配人数：** `{mm.get('searching_players', 0):,}`"
        ]
        
        card.append(Module.Section(Element.Text("\n".join(lines), type=Types.Text.KMD)))
        card.append(Module.Divider())
        card.append(Module.Context(Element.Text("注：仅供参考", type=Types.Text.KMD)))

        await safe_delete_msg(bot, loading_msg)
        await msg.reply(CardMessage(card))
        
    except (asyncio.TimeoutError, TimeoutError):
        await safe_delete_msg(bot, loading_msg)
        await msg.reply("[Timeout] 探活线程超时未响应。")
    except Exception as e:
        logger.error(f"[Status] 嗅探故障: {e}", exc_info=True)
        await safe_delete_msg(bot, loading_msg)
        await msg.reply("[Error] 解析堆栈发生内部错误。")


@bot.command(name='hltv', prefixes=['/'])
async def query_hltv_matches(msg: Message):
    loading_msg = await msg.reply("正在连接至 HLTV 服务器...")
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"}
        async with AsyncSession(impersonate="chrome120", timeout=15, headers=headers) as session:
            resp_matches = await session.get("https://www.hltv.org/matches")
            
        if resp_matches.status_code != 200:
            await safe_delete_msg(bot, loading_msg)
            return await msg.reply(f"[Error] 抓取被拦截 (HTTP {resp_matches.status_code})。建议稍后再试。")

        soup = BeautifulSoup(resp_matches.text, 'html.parser')
        
        page_title = soup.title.string if soup.title else ""
        if "Just a moment" in page_title or "Cloudflare" in page_title:
            await safe_delete_msg(bot, loading_msg)
            return await msg.reply("[Error] 被 HLTV 的 Cloudflare 盾拦截了，请稍后重试。")

        live_matches = []
        
        live_elements = soup.find_all(class_=lambda c: c and ('matchLive' in c or 'liveMatch-container' in c))
        
        for time_elem in soup.find_all(class_='matchTime'):
            if 'LIVE' in time_elem.get_text(strip=True).upper():
                live_elements.append(time_elem)

        for elem in live_elements:
            a_tag = elem if elem.name == 'a' else elem.find_parent('a')
            if not a_tag:
                a_tag = elem.find('a')
                
            if not a_tag or not a_tag.has_attr('href') or '/matches/' not in a_tag['href']:
                continue
                
            container = a_tag if a_tag.find(class_='matchTeamName') else elem
            teams = container.find_all(class_='matchTeamName')
            if len(teams) < 2: continue
            
            team1 = teams[0].get_text(strip=True)
            team2 = teams[1].get_text(strip=True)
            
            event_elem = container.find(class_='matchEventName')
            event_name = event_elem.get_text(strip=True) if event_elem else "未知赛事"
            
            scores = container.find_all('span', class_='matchTeamScore')
            if len(scores) >= 2:
                score_str = f"{scores[0].get_text(strip=True)} : {scores[1].get_text(strip=True)}"
            else:
                score_str = "LIVE"
                
            href = a_tag['href']
            link = href if href.startswith('http') else "https://www.hltv.org" + href
            
            if not any(m['team1'] == team1 for m in live_matches):
                live_matches.append({
                    "team1": team1, "team2": team2,
                    "event": event_name, "score": score_str, "link": link
                })

        recent_matches = []
        if not live_matches:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"}
            async with AsyncSession(impersonate="chrome120", timeout=15, headers=headers) as session:
                resp_results = await session.get("https://www.hltv.org/results")
            if resp_results.status_code == 200:
                soup_res = BeautifulSoup(resp_results.text, 'html.parser')
                results = soup_res.find_all('div', class_='result-con')
                for res in results[:3]:
                    t1_elem = res.find('div', class_='team1')
                    t2_elem = res.find('div', class_='team2')
                    t1 = t1_elem.find('div', class_='team').get_text(strip=True) if t1_elem else "队伍1"
                    t2 = t2_elem.find('div', class_='team').get_text(strip=True) if t2_elem else "队伍2"
                    
                    sc_elem = res.find('td', class_='result-score')
                    if sc_elem:
                        sc_spans = sc_elem.find_all('span')
                        sc = f"{sc_spans[0].get_text(strip=True)} : {sc_spans[1].get_text(strip=True)}" if len(sc_spans) >= 2 else sc_elem.get_text(strip=True)
                    else:
                        sc = "N/A"
                        
                    ev_elem = res.find('span', class_='event-name')
                    ev = ev_elem.get_text(strip=True) if ev_elem else "未知赛事"
                    
                    a_tag = res.find('a', class_='a-reset')
                    res_href = a_tag['href'] if a_tag and a_tag.has_attr('href') else "/results"
                    res_link = res_href if res_href.startswith('http') else "https://www.hltv.org" + res_href
                    
                    recent_matches.append({"team1": t1, "team2": t2, "event": ev, "score": sc, "link": res_link})

        card = Card(color="#2F3136")
        card.append(Module.Header("HLTV 赛事数据"))
        card.append(Module.Context(Element.Text("数据源: HLTV.org", type=Types.Text.KMD)))
        card.append(Module.Divider())

        if live_matches:
            card.append(Module.Section(Element.Text("**正在进行的比赛**", type=Types.Text.KMD)))
            for m in live_matches[:5]:
                card.append(Module.Section(
                    Element.Text(f"**{m['event']}**\n> `{m['team1']}` **[ {m['score']} ]** `{m['team2']}`\n> [前往观看 / 数据页]({m['link']})", type=Types.Text.KMD)
                ))
        else:
            card.append(Module.Section(Element.Text("**当前暂无进行中的比赛**", type=Types.Text.KMD)))
            card.append(Module.Context(Element.Text("已自动为您抓取最近的赛事结果：", type=Types.Text.KMD)))
            
            for m in recent_matches:
                card.append(Module.Section(
                    Element.Text(f"**{m['event']}**\n> 🏁 `{m['team1']}` **[ {m['score']} ]** `{m['team2']}`\n> [查看赛后数据]({m['link']})", type=Types.Text.KMD)
                ))

        await safe_delete_msg(bot, loading_msg)
        await msg.reply(CardMessage(card))

    except Exception as e:
        logger.error(f"[HLTV] 战况抓取异常: {e}", exc_info=True)
        await safe_delete_msg(bot, loading_msg)
        await msg.reply("[Error] 解析战况数据时发生内部异常。")

@bot.command(name='apex', prefixes=['/'])
async def simulate_apex_packs(msg: Message, count_str: str = "100"):
    if AUTH_COLLECTION is not None:
        auth_info = await AUTH_COLLECTION.find_one({"_id": msg.guild_id})
        if not auth_info:
            return await msg.reply("⚠️ 本频道未获得开箱授权。请联系服主申请开通。")
        if auth_info['expire_at'] < datetime.datetime.now():
            return await msg.reply("⏰ 授权已过期。请续费后继续使用。")

    try:
        count = int(count_str)
        count = max(1, min(500, count))
    except ValueError:
        count = 100

    try:
        cost_per_pack = 7.0
        total_cost = count * cost_per_pack
        
        results = {'heirloom': 0, 'legendary': 0, 'epic': 0, 'rare': 0, 'common': 0}
        total_earned = 0.0

        values = {'heirloom': 1500.0, 'legendary': 20.0, 'epic': 5.0, 'rare': 1.0, 'common': 0.1}

        for _ in range(count):
            if random.random() < 0.002:
                results['heirloom'] += 1
                total_earned += values['heirloom']

            pack_items = []
            for i in range(3):
                roll = random.random()
                if roll < 0.0246:      
                    pack_items.append('legendary')
                elif roll < 0.1072:    
                    pack_items.append('epic')
                elif roll < 0.3072:    
                    pack_items.append('rare')
                else:                  
                    pack_items.append('common')
            
            if all(x == 'common' for x in pack_items):
                pack_items[0] = 'rare'

            for item in pack_items:
                results[item] += 1
                total_earned += values[item]

        profit = total_earned - total_cost
        user_id, user_name = str(msg.author.id), msg.author.username
        
        if ECO_COLLECTION is not None:
            updated_doc = await ECO_COLLECTION.find_one_and_update(
                {"_id": user_id}, 
                {
                    "$set": {"name": user_name},
                    "$inc": {
                        "spent": total_cost,
                        "earned": total_earned,
                        "profit": profit,
                        "apex_opens": count
                    }
                },
                upsert=True, 
                return_document=True 
            )
            total_profit = updated_doc.get("profit", profit)
        else:
            total_profit = profit

        if results['heirloom'] > 0:
            card_color = "#FF0000"  
        elif results['legendary'] > 0:
            card_color = "#FFD700" 
        elif results['epic'] > 0:
            card_color = "#800080" 
        else:
            card_color = "#0000FF" 

        card = Card(color=card_color)
        card.append(Module.Header(f"APEX 英雄 {count} 包模拟结果"))
        
        lines = [
            f"**总花费**：`¥ {total_cost:.2f}` (按 7元/包 估算)",
            f"**总估值**：`¥ {total_earned:.2f}`",
            f"**本轮盈亏**：{'+ ¥ ' if profit > 0 else '- ¥ '}{abs(profit):.2f}",
            "---",
            f"🟥 **传家宝 (神话)**：`{results['heirloom']}` 次",
            f"🟨 **传说 (金)**：`{results['legendary']}` 个",
            f"🟪 **史诗 (紫)**：`{results['epic']}` 个",
            f"🟦 **稀有 (蓝)**：`{results['rare']}` 个",
            f"⬜ **普通 (白)**：`{results['common']}` 个",
            "---",
            f"**历史总净值**：{'¥ ' if total_profit > 0 else '- ¥ '}{abs(total_profit):.2f}"
        ]
        
        card.append(Module.Section(Element.Text("\n".join(lines), type=Types.Text.KMD)))
        
        context_text = f"[ 用户: {user_name} ] "
        if results['heirloom'] > 0:
            context_text += "🎉 出传了！重生是你爹？"
        elif results['legendary'] >= count * 0.1:
            context_text += "✨ 运气不错，金光闪闪！"
        elif profit < -total_cost * 0.5:
            context_text += "😭 蓝天白云，至少说明模拟很贴合现实"
        else:
            context_text += "👍 中规中矩，EA 感谢你的赞助"
            
        card.append(Module.Context(Element.Text(context_text, type=Types.Text.KMD)))
        
        await msg.reply(CardMessage(card))
        
    except Exception as e:
        logger.error(f"[Apex] 模拟崩溃: {e}", exc_info=True)
        await msg.reply("[Error] APEX 模拟引擎发生意外终止。")

@bot.command(name='apexmap', prefixes=['/'])
async def query_apex_map(msg: Message):
    if not APEX_API_KEY or APEX_API_KEY == '你的APEX_KEY':
        return await msg.reply("⚠️ 机器人未配置 APEX_API_KEY，无法请求数据。")

    loading_msg = await msg.reply("正在连接 APEX 卫星网络获取地图数据...")
    try:
        url = f"https://api.mozambiquehe.re/maprotation?auth={APEX_API_KEY}&version=2"
        async with AIO_SESSION.get(url, timeout=15) as resp:
            if resp.status == 404:
                await safe_delete_msg(bot, loading_msg)
                return await msg.reply(f"❌ 无法连接到APEX服务器，可能是土豆熟了")
            elif resp.status == 429:
                await safe_delete_msg(bot, loading_msg)
                return await msg.reply("⏳ 接口请求过于频繁！Apex 官方 API 限制了查询速率，请等待 5~10 秒后再试。")
            elif resp.status != 200:
                await safe_delete_msg(bot, loading_msg)
                return await msg.reply(f"[Error] API 拒绝请求 (HTTP {resp.status})")
            data = await resp.json()

        pubs = data.get('battle_royale', {})
        ranked = data.get('ranked', {})

        card = Card(color="#E82E2E")
        card.append(Module.Header("APEX 英雄 - 实时地图轮换"))
        
        if pubs:
            curr_pub = pubs.get('current', {})
            next_pub = pubs.get('next', {})
            card.append(Module.Section(
                Element.Text(
                    f"**🎮 普通匹配 (大逃杀)**\n"
                    f"> **当前地图**：`{curr_pub.get('map', '未知')}`\n"
                    f"> **剩余时间**：{curr_pub.get('remainingTimer', '未知')}\n"
                    f"> **下一张图**：{next_pub.get('map', '未知')}", 
                    type=Types.Text.KMD
                ),
                accessory=Element.Image(src=curr_pub.get('asset', ''), size=Types.Size.LG)
            ))
            card.append(Module.Divider())

        if ranked:
            curr_rank = ranked.get('current', {})
            next_rank = ranked.get('next', {})
            card.append(Module.Section(
                Element.Text(
                    f"**🏆 排位赛 (Ranked)**\n"
                    f"> **当前地图**：`{curr_rank.get('map', '未知')}`\n"
                    f"> **剩余时间**：{curr_rank.get('remainingTimer', '未知')}\n"
                    f"> **下一张图**：{next_rank.get('map', '未知')}", 
                    type=Types.Text.KMD
                ),
                accessory=Element.Image(src=curr_rank.get('asset', ''), size=Types.Size.LG)
            ))

        card.append(Module.Context(Element.Text("数据源: Apex Legends Status", type=Types.Text.KMD)))
        
        await safe_delete_msg(bot, loading_msg)
        await msg.reply(CardMessage(card))

    except Exception as e:
        logger.error(f"[ApexMap] 地图获取失败: {e}")
        await safe_delete_msg(bot, loading_msg)
        await msg.reply("[Error] 获取地图轮换数据时发生异常。")

@bot.command(name='apexstat', prefixes=['/'])
async def query_apex_stat(msg: Message, player_name: str = "", platform: str = "PC"):
    if not APEX_API_KEY or APEX_API_KEY == '你的APEX_KEY':
        return await msg.reply("⚠️ 机器人未配置 APEX_API_KEY。")
        
    if not player_name:
        return await msg.reply("用法：`/apexstat [玩家ID] [平台(可选: PC/PS4/X1)]`\n示例：`/apexstat iiTzTimmy PC`")

    loading_msg = await msg.reply(f"正在检索玩家 {player_name} 的档案...")
    try:
        if player_name.isdigit() and len(player_name) > 8:
            url = f"https://api.mozambiquehe.re/bridge?auth={APEX_API_KEY}&uid={player_name}&platform={platform.upper()}"
        else:
            safe_name = urllib.parse.quote(player_name)
            url = f"https://api.mozambiquehe.re/bridge?auth={APEX_API_KEY}&player={safe_name}&platform={platform.upper()}"
        
        async with AIO_SESSION.get(url, timeout=15) as resp:
            if resp.status == 404:
                await safe_delete_msg(bot, loading_msg)
                return await msg.reply(f"❌ 未找到玩家 `{player_name}`。\n💡 提示：请使用 **EA ID** 查询，不要使用 Steam 昵称（Steam同名太多查不到）。或者尝试更换平台参数（PC/PS4/X1）。")
            elif resp.status == 429: # 👈 新增 429 拦截
                await safe_delete_msg(bot, loading_msg)
                return await msg.reply("⏳ 接口请求过于频繁！Apex 官方 API 限制了查询速率，请等待 5~10 秒后再试。")
            elif resp.status != 200:
                await safe_delete_msg(bot, loading_msg)
                return await msg.reply(f"[Error] API 拒绝请求 (HTTP {resp.status})")
                
            data = await resp.json()

        global_data = data.get('global', {})
        realtime = data.get('realtime', {})
        legends = data.get('legends', {}).get('selected', {})

        level = global_data.get('level', 0)
        rank_info = global_data.get('rank', {})
        rank_name = rank_info.get('rankName', 'Unranked')
        rank_div = rank_info.get('rankDiv', '')
        rank_score = rank_info.get('rankScore', 0)
        rank_img = rank_info.get('rankImg', '')

        is_online = realtime.get('isOnline', 0)
        state_txt = "🟢 在线" if is_online else "⚪ 离线"
        if is_online and realtime.get('isInGame'):
            state_txt = "🔴 游戏中"

        legend_name = legends.get('LegendName', '未知英雄')
        legend_img = legends.get('ImgAssets', {}).get('icon', '')

        card = Card(color="#E82E2E")
        card.append(Module.Header(f"APEX 档案：{global_data.get('name', player_name)}"))
        
        lines = [
            f"**平台**：`{global_data.get('platform', platform.upper())}` | **状态**：{state_txt}",
            f"**账号等级**：`Lv.{level}`",
            "---",
            f"**当前段位**：`{rank_name} {rank_div}`",
            f"**排位分数**：`{rank_score:,} RP`",
            "---",
            f"**当前选择传奇**：`{legend_name}`"
        ]
        
        trackers = legends.get('data', [])
        if trackers:
            lines.append("**传奇追踪器数据**：")
            for t in trackers:
                lines.append(f"> {t.get('name', '未知')}: `{t.get('value', 0)}`")

        card.append(Module.Section(
            Element.Text("\n".join(lines), type=Types.Text.KMD),
            accessory=Element.Image(src=rank_img, size=Types.Size.SM) if rank_img else None
        ))
        
        if legend_img:
            card.append(Module.Container(Element.Image(src=legend_img)))

        await safe_delete_msg(bot, loading_msg)
        await msg.reply(CardMessage(card))

    except Exception as e:
        logger.error(f"[ApexStat] 战绩获取失败: {e}")
        await safe_delete_msg(bot, loading_msg)
        await msg.reply("[Error] 解析战绩数据时发生异常。")
        
# ==========================================
# 5. 启动入口
# ==========================================
async def main():
    global AIO_SESSION
    AIO_SESSION = aiohttp.ClientSession(headers=STD_HEADERS)
    
    await init_db()

    asyncio.create_task(init_crates_data())
    asyncio.create_task(init_translation_dictionary())
    asyncio.create_task(price_auto_updater())

    app = web.Application()
    app.router.add_get('/', lambda r: web.Response(text="Bot Alive"))
    app.router.add_post('/webhook/afdian', afdian_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', 7860).start()

    logger.info("[System] 所有组件就绪，Bot 启动！")
    try:
        await bot.start()
    finally:
        if AIO_SESSION: await AIO_SESSION.close()

if __name__ == '__main__':
    asyncio.run(main())
