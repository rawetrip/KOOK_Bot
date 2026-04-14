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
# 1. 基础配置与全局变量初始化
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 安全地从环境变量加载所有敏感凭证 (防止泄露到 GitHub)
BOT_TOKEN = os.environ.get('BOT_TOKEN', '你的TOKEN')
STEAM_API_KEY = os.environ.get('STEAM_API_KEY', '你的KEY')
MONGO_URI = os.environ.get('MONGO_URI')
HF_TOKEN = os.environ.get('HF_TOKEN')
REPO_ID = os.environ.get('HF_REPO_ID')
OWNER_ID = os.environ.get('OWNER_ID', '你的KOOK_ID')
APEX_API_KEY = os.environ.get('APEX_API_KEY', '你的APEX_KEY')

# MongoDB 全局实例
DB_CLIENT = None
ECO_COLLECTION = None   # 玩家经济账户集合
AUTH_COLLECTION = None  # 频道授权名单集合

async def init_db():
    """初始化 MongoDB 云端金库连接"""
    global DB_CLIENT, ECO_COLLECTION, AUTH_COLLECTION
    if MONGO_URI:
        try:
            # 建立异步连接，设置 5 秒超时
            DB_CLIENT = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)
            db = DB_CLIENT['cs2_bot_db']
            ECO_COLLECTION = db['economy']
            AUTH_COLLECTION = db['authorized_channels']
            
            # 发送 ping 命令测试握手状态
            await DB_CLIENT.admin.command('ping')
            logger.info("[System] MongoDB 云端金库连接成功！")
        except Exception as e:
            logger.error(f"[System] 数据库连接失败: {e}")
    
# 本地数据缓存 (由后台异步任务定期刷新)
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

# 静态常量映射表
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
    """利用 lru_cache 提升饰品搜索速度，减少内存遍历开销"""
    return sorted(
        [i for i in PRICE_DICT if all(t in i["search_text"] for t in search_tuple)], 
        key=lambda x: x["price"], 
        reverse=True
    )

async def safe_delete_msg(bot_instance, msg_obj):
    """安全撤回消息，通常用于清理 '正在加载...' 的过渡提示"""
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
# 3. 异步后台数据守护进程 (Daemon)
# ==========================================
async def async_fetch_json(url, headers=None):
    """通用异步 JSON 拉取器"""
    if not AIO_SESSION: return []
    try:
        async with AIO_SESSION.get(url, headers=headers or STD_HEADERS, timeout=60) as resp:
            if resp.status == 200:
                try: return await resp.json()
                except Exception as e: logger.error(f"[JSON解析错误] {url}: {e}")
    except Exception as e:
        logger.debug(f"[网络请求错误] {url}: {e}")
    return []

def update_affordable_crates():
    """过滤掉天价武器箱，避免影响日常模拟抽奖体验"""
    global AFFORDABLE_CASES, AFFORDABLE_CAPSULES
    AFFORDABLE_CASES = [c for c in CRATES_CASES if PRICE_CN_MAP.get(c.get('name'), {}).get('price', 5.0) <= 800]
    AFFORDABLE_CAPSULES = [c for c in CRATES_CAPSULES if PRICE_CN_MAP.get(c.get('name'), {}).get('price', 1.5) <= 800]

async def init_crates_data():
    """从开源仓库抓取 CS2 官方掉落概率表"""
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
    """构建中英文对照词典，优先从 Hugging Face 读取缓存以防 CDN 墙"""
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

async def price_auto_updater():
    """定时任务：通过 Skinport API 自动同步 20,000+ 件饰品的最新底价"""
    global IS_PRICE_READY, PRICE_DICT, PRICE_CN_MAP, PRICE_EN_MAP
    cache_file = 'price_cache_v4.json'
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r', encoding='utf-8') as f: PRICE_DICT = json.load(f)
            if PRICE_DICT:
                PRICE_CN_MAP = {i['cn_name']: i for i in PRICE_DICT}; PRICE_EN_MAP = {i['en_name']: i for i in PRICE_DICT}
                IS_PRICE_READY = True
        except: pass

    while True:
        try:
            # 使用 curl_cffi 伪装指纹，绕过部分基础防护
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
                            # 应用词典进行粗略汉化
                            for eng, chn in DISPLAY_TRANS.items():
                                if eng in cn_name: cn_name = cn_name.replace(eng, chn)
                            # 简化磨损后缀提升卡片显示效果
                            cn_name = cn_name.replace("(崭新出厂)", "(崭新)").replace("(略有磨损)", "(略磨)").replace("(久经沙场)", "(久经)").replace("(破损不堪)", "(破损)").replace("(战痕累累)", "(战痕)")
                            new_prices.append({"en_name": en_name, "cn_name": cn_name, "search_text": f"{en_name} {cn_name}".lower(), "price": float(price)})
                    
                    if len(new_prices) > 1000:
                        PRICE_DICT = new_prices
                        PRICE_CN_MAP = {i['cn_name']: i for i in PRICE_DICT}; PRICE_EN_MAP = {i['en_name']: i for i in PRICE_DICT}
                        IS_PRICE_READY = True
                        with open(cache_file, 'w', encoding='utf-8') as f: json.dump(PRICE_DICT, f, ensure_ascii=False)
                        logger.info(f"[Price] 价格库同步成功，共 {len(PRICE_DICT)} 条。")
                        _sync_search_skin_cached.cache_clear() # 清理检索缓存
                        await asyncio.sleep(86400) # 每天同步一次
                        continue
        except Exception as e: logger.error(f"[Price] 更新异常: {e}")
        await asyncio.sleep(300) # 失败重试间隔

async def get_all_data(steam_id: str):
    """并发请求 Steam 官方多个接口，获取全量玩家数据"""
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

    results = await asyncio.gather(*(fetch(k, v) for k, v in urls.items()))
    return dict(results)

# ==========================================
# 4. KOOK 交互指令逻辑模块
# ==========================================

@bot.command(name='auth', prefixes=['/'])
async def authorize_channel(msg: Message, channel_id: str = "", days: str = "30"):
    """
    [管理员指令] SaaS 核心逻辑：为指定频道签发开箱/抽卡权限。
    只有配置在环境变量 OWNER_ID 中的开发者才能调用。
    """
    if msg.author.id != OWNER_ID:
        return await msg.reply("❌ 权限不足：仅限机器人开发者操作。")
    
    if not channel_id or not days.isdigit():
        return await msg.reply("用法：/auth [频道ID] [天数(必须为整数)]")

    expire_at = datetime.datetime.now() + datetime.timedelta(days=int(days))
    
    if AUTH_COLLECTION is None:
        return await msg.reply("❌ 数据库未连接，无法执行授权。")
        
    # 原子更新频道授权表 (upsert 意味着如果频道不存在则新建记录)
    await AUTH_COLLECTION.update_one(
        {"_id": channel_id},
        {"$set": {"expire_at": expire_at, "authorized_by": msg.author.username}},
        upsert=True
    )
    
    await msg.reply(f"✅ 授权成功！\n频道：`{channel_id}`\n有效期至：{expire_at.strftime('%Y-%m-%d')}")
    
@bot.command(name='open', prefixes=['/'])
async def simulate_case_opening(msg: Message, *args):
    """
    CS2 全真模拟开箱系统。
    包含 SaaS 门禁校验、官方概率推演以及跨游戏的 MongoDB 经济结算。
    """
    # --- 1. 动态权限拦截 ---
    auth_info = await AUTH_COLLECTION.find_one({"_id": msg.target_id})
    if not auth_info:
        return await msg.reply("⚠️ 本频道未获得开箱授权。请联系服主申请开通。")
    if auth_info['expire_at'] < datetime.datetime.now():
        return await msg.reply("⏰ 授权已过期。请续费后继续使用。")
        
    if not IS_PRICE_READY or not CRATES_CASES:
        return await msg.reply("系统正在同步数据，请稍后...")

    count = 1
    if args and args[0].isdigit():
        count = max(1, min(10, int(args[0]))) # 限制单次最高十连抽防止刷屏

    try:
        opened_items = []
        total_cost_all, total_earned_all = 0.0, 0.0
        
        # CS2 官方掉落概率基准权重
        tiers_list = ['gold', 'red', 'pink', 'purple', 'blue']
        tiers_weights = [1.00, 2.50, 10.00, 30.00, 56.50]

        # --- 2. 抽奖算法运算 ---
        for _ in range(count):
            is_capsule = random.random() < 0.25 # 25% 概率抽胶囊
            valid_crates = AFFORDABLE_CAPSULES if is_capsule else AFFORDABLE_CASES 
            if not valid_crates: continue
            
            crate = random.choice(valid_crates)
            crate_name = crate.get('name')

            # 动态计算抽奖成本 (箱子市价 + 钥匙价格)
            crate_price_data = PRICE_CN_MAP.get(crate_name)
            crate_market_price = crate_price_data['price'] if crate_price_data else (1.5 if is_capsule else 5.0)
            is_key_required = (crate.get('type') in ['Weapon Case', '武器箱'] or '武器箱' in crate_name)
            single_cost = crate_market_price + (17.5 if is_key_required else 0.0)

            contains = crate.get('contains', [])
            contains_rare = crate.get('contains_rare', []) 

            # 根据武器稀有度清洗掉落池
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

            # 执行概率判定
            selected_tier = random.choices(tiers_list, weights=tiers_weights, k=1)[0]
            won_item_raw, won_item_name, won_item_price = None, "未知物品", 0.0

            # 特殊处理出金逻辑 (如果数据表里没有金，则强制去全站价格表里随机抓一把刀)
            if selected_tier == 'gold':
                if contains_rare: won_item_raw = random.choice(contains_rare)
                elif tiers['gold']: won_item_raw = random.choice(tiers['gold'])
                else:
                    if not is_capsule:
                        won_item_dict = random.choice([i for i in PRICE_DICT if any(k in i['cn_name'] for k in ["刀", "手套", "★"])])
                        won_item_raw = "GOLDBACK"
                        won_item_name = won_item_dict['cn_name']
                        won_item_price = won_item_dict['price']
                    else:
                        selected_tier = 'red' # 胶囊没金就降级

            # 稀有度兜底，防止因为数据库不完整导致报错
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

            # 匹配真实饰品并附加随机磨损/玄学
            if won_item_raw != "GOLDBACK":
                base_name = str(won_item_raw.get('name', ''))
                matched_items = []
                if base_name:
                    for i in PRICE_DICT:
                        if base_name in i['cn_name'] or base_name in i['en_name']:
                            cn = i['cn_name']
                            # 严格过滤印花后缀干扰
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

        # --- 3. MongoDB 事务结算 (高并发安全) ---
        profit_all = total_earned_all - total_cost_all
        user_id, user_name = str(msg.author.id), msg.author.username
        
        if ECO_COLLECTION is not None:
            # 使用 find_one_and_update 实现原子级排队递增，绝不串号
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
            
        # --- 4. 渲染 KMD 豪华卡片 ---
        best_item = max(opened_items, key=lambda x: x['price'])
        card = Card(color=best_item['color']) # 根据最贵饰品变换卡片边框色
        
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

        # 根据盈亏动态评价
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
    """
    基于全站价格库缓存的极速查价工具。
    支持交互式按钮流转查询明细。
    """
    try:
        if not IS_PRICE_READY: return await msg.reply("数据库正在初始化，请稍后尝试...")
        
        # 拦截空参数并提供快捷按钮
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
        
        # 动态生成外链查询按钮
        for i in range(0, min(8, len(results)), 4):
            group_btns = [Element.Button(f"详细 #{i+idx+1}", value=f"skin_chart|{item['en_name']}", click=Types.Click.RETURN_VAL, theme=Types.Theme.PRIMARY) 
                          for idx, item in enumerate(results[i:i+4])]
            card.append(Module.ActionGroup(*group_btns))
            
        await msg.reply(CardMessage(card))
    except Exception as e:
        logger.error(f"[Skin] 检索模块异常: {e}", exc_info=True)
        await msg.reply("[Error] 检索模块触发异常。")

# 按钮事件监听路由
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
    """
    玩家全量档案聚合。
    提取 Steam 官方底层统计，估算 Rating 2.0 及相关电竞级维度的实力数据。
    """
    if not steam_id or not steam_id.isdigit():
        return await msg.reply("[Error] 参数校验失败：请输入17位数字型 SteamID。")

    loading_msg = await msg.reply(f"正在连接 Steam 官方数据节点，同步玩家 {steam_id} 的档案...")
    try:
        d = await get_all_data(steam_id)
        summary = d['summary'].get('response', {}).get('players', [None])[0]
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
        
        # HLTV Rating 2.0 拟合算法模块
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
            rounds = s.get('total_rounds_played', 1)
            won_matches = s.get('total_matches_won', 0)
            play_matches = s.get('total_matches_played', 0)
            
            adr_val = dmg / rounds if rounds > 0 else 0
            
            if play_matches > 0 and won_matches > 0:
                win_rate_str = f"{(won_matches / play_matches) * 100:.1f}%"
            else:
                won_rounds = s.get('total_wins', 0)
                win_rate_str = f"{(won_rounds / rounds) * 100:.1f}%"

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
    """查询 CS2 官方服务器群落连通状态"""
    loading_msg = await msg.reply("正在查询服务器状态...")
    try:
        url_official = f"https://api.steampowered.com/ICSGOServers_730/GetGameServersStatus/v1/?key={STEAM_API_KEY}"
        url_web_test = "https://steamcommunity.com/market/search/render/?appid=730&count=1"
        
        task_off = AIO_SESSION.get(url_official, timeout=12)
        task_web = AIO_SESSION.get(url_web_test, timeout=12)
        off_resp, web_resp = await asyncio.gather(task_off, task_web, return_exceptions=True)

        services, mm = {}, {}
        if isinstance(off_resp, aiohttp.ClientResponse) and off_resp.status == 200:
            data = await off_resp.json()
            res = data.get('result', {})
            services = res.get('services', {})
            mm = res.get('matchmaking', {})

        real_inv_status = "Offline"
        if isinstance(web_resp, aiohttp.ClientResponse):
            if web_resp.status == 200:
                real_inv_status = "Online"
            elif web_resp.status == 429:
                real_inv_status = "Surge"
            else:
                real_inv_status = f"[ERROR] HTTP {web_resp.status}"

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
    """
    HLTV 电竞门户爬虫。
    获取实时的 Major/杯赛 队伍比分并提供数据流直达链接。
    使用了 curl_cffi 绕过 Cloudflare 盾。
    """
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
    """
    跨游戏生态支持：APEX 传家宝模拟抽奖。
    与 CS2 模块共享同样的 MongoDB 虚拟经济账户，体现 SaaS 产品的一致性。
    """
    if AUTH_COLLECTION is not None:
        auth_info = await AUTH_COLLECTION.find_one({"_id": msg.target_id})
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

        # APEX 极其感人的概率掉落算法
        for _ in range(count):
            if random.random() < 0.002: # 传家宝 0.2% 概率
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
            
            # 每个组合包保底包含一件稀有(蓝)物品
            if all(x == 'common' for x in pack_items):
                pack_items[0] = 'rare'

            for item in pack_items:
                results[item] += 1
                total_earned += values[item]

        profit = total_earned - total_cost
        user_id, user_name = str(msg.author.id), msg.author.username
        
        # 将盈利状况跨服同步到统一钱包
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

        if results['heirloom'] > 0: card_color = "#FF0000"  
        elif results['legendary'] > 0: card_color = "#FFD700" 
        elif results['epic'] > 0: card_color = "#800080" 
        else: card_color = "#0000FF" 

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
        if results['heirloom'] > 0: context_text += "🎉 出传了！重生是你爹？"
        elif results['legendary'] >= count * 0.1: context_text += "✨ 运气不错，金光闪闪！"
        elif profit < -total_cost * 0.5: context_text += "😭 蓝天白云，至少说明模拟很贴合现实"
        else: context_text += "👍 中规中矩，EA 感谢你的赞助"
            
        card.append(Module.Context(Element.Text(context_text, type=Types.Text.KMD)))
        await msg.reply(CardMessage(card))
        
    except Exception as e:
        logger.error(f"[Apex] 模拟崩溃: {e}", exc_info=True)
        await msg.reply("[Error] APEX 模拟引擎发生意外终止。")

@bot.command(name='apexmap', prefixes=['/'])
async def query_apex_map(msg: Message):
    """APEX 地图轮换时间表追踪 API"""
    if not APEX_API_KEY or APEX_API_KEY == '你的APEX_KEY':
        return await msg.reply("⚠️ 机器人未配置 APEX_API_KEY，无法请求数据。")

    loading_msg = await msg.reply("正在连接 APEX 卫星网络获取地图数据...")
    try:
        url = f"https://api.mozambiquehe.re/maprotation?auth={APEX_API_KEY}&version=2"
        async with AIO_SESSION.get(url, timeout=15) as resp:
            if resp.status == 404:
                await safe_delete_msg(bot, loading_msg)
                return await msg.reply(f"❌ 无法连接到APEX服务器，可能是土豆熟了")
            elif resp.status == 429: # 针对第三方频控的专业拦截处理
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
    """全平台 APEX 战绩档案及在线状态监测 API"""
    if not APEX_API_KEY or APEX_API_KEY == '你的APEX_KEY':
        return await msg.reply("⚠️ 机器人未配置 APEX_API_KEY。")
        
    if not player_name:
        return await msg.reply("用法：`/apexstat [玩家ID] [平台(可选: PC/PS4/X1)]`\n示例：`/apexstat iiTzTimmy PC`")

    loading_msg = await msg.reply(f"正在检索玩家 {player_name} 的档案...")
    try:
        # 支持以 UID 或 昵称 两种维度去拉取数据
        if player_name.isdigit() and len(player_name) > 8:
            url = f"https://api.mozambiquehe.re/bridge?auth={APEX_API_KEY}&uid={player_name}&platform={platform.upper()}"
        else:
            safe_name = urllib.parse.quote(player_name)
            url = f"https://api.mozambiquehe.re/bridge?auth={APEX_API_KEY}&player={safe_name}&platform={platform.upper()}"
        
        async with AIO_SESSION.get(url, timeout=15) as resp:
            if resp.status == 404:
                await safe_delete_msg(bot, loading_msg)
                return await msg.reply(f"❌ 未找到玩家 `{player_name}`。\n💡 提示：请使用 **EA ID** 查询，不要使用 Steam 昵称（Steam同名太多查不到）。或者尝试更换平台参数（PC/PS4/X1）。")
            elif resp.status == 429: 
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
# 5. 守护进程及应用启动入口
# ==========================================
async def main():
    global AIO_SESSION
    AIO_SESSION = aiohttp.ClientSession(headers=STD_HEADERS)
    
    # 🌟 1. 建立 MongoDB 永久连接
    await init_db()

    # 🌟 2. 并发挂载基础数据后台刷新任务
    asyncio.create_task(init_crates_data())
    asyncio.create_task(init_translation_dictionary())
    asyncio.create_task(price_auto_updater())

    # 🌟 3. 初始化占位用 Web 服务器 (规避 Serverless 平台无网络流量即休眠的问题)
    app = web.Application()
    app.router.add_get('/', lambda r: web.Response(text="Bot Alive - Serverless Keepalive Probe Ready."))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', 7860).start()

    logger.info("[System] 所有核心链路组件与 Web 探针已就绪，KHL 通讯信道准备开启...")
    try:
        await bot.start()
    finally:
        if AIO_SESSION: await AIO_SESSION.close()

if __name__ == '__main__':
    asyncio.run(main())
