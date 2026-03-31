import asyncio
import aiohttp
import os
import datetime
import time
import json
import urllib.parse
import random
import logging
from aiohttp import web
from khl import Bot, Message, EventTypes, Event
from khl.card import CardMessage, Card, Module, Element, Types
from huggingface_hub import HfApi, hf_hub_download
from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup
from functools import lru_cache
from dotenv import load_dotenv
load_dotenv() # 这一行会让代码自动去寻找并读取本地的 .env 文件

# 使用 LRU 缓存最近的 128 种搜索结果，极大节省算力
@lru_cache(maxsize=128)
def _sync_search_skin_cached(search_tuple):
    # 将传入的 tuple 转换回列表逻辑进行匹配
    return sorted(
        [i for i in PRICE_DICT if all(t in i["search_text"] for t in search_tuple)], 
        key=lambda x: x["price"], 
        reverse=True
    )

# 在你需要更新价格时（例如 price_auto_updater 成功抓取新价格后），清空缓存防止数据过期：
# _sync_search_skin_cached.cache_clear()

# ==========================================
# 1. 基础配置与核心映射
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get('BOT_TOKEN', '你的TOKEN')
STEAM_API_KEY = os.environ.get('STEAM_API_KEY', '你的KEY')
HF_TOKEN = os.environ.get('HF_TOKEN')
REPO_ID = os.environ.get('HF_REPO_ID')
ALLOWED_CHANNEL_ID = os.environ.get('ALLOWED_CHANNEL_ID')

# 全局数据字典
PRICE_DICT = []
PRICE_CN_MAP = {}
PRICE_EN_MAP = {}
CRATES_DICT, CRATES_CASES, CRATES_CAPSULES = [], [], []

# 预计算缓存池（极大提升 /open 并发性能）
AFFORDABLE_CASES, AFFORDABLE_CAPSULES = [], []
DISPLAY_TRANS = {}

IS_PRICE_READY = False 
ECONOMY_FILE = 'economy_data_v1.json'
ECONOMY_DIRTY = False  
GLOBAL_ECO_DATA = {}  # 将经济数据常驻内存

# 👇 添加这行全局共享 HTTP 客户端 (提升并发性能)
AIO_SESSION: aiohttp.ClientSession = None

api = HfApi() if HF_TOKEN and REPO_ID else None
bot = Bot(token=BOT_TOKEN)

# 字典常量
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
# 2. 辅助工具与云端持久化经济系统
# ==========================================
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

def upload_to_hf(filename):
    if api and os.path.exists(filename):
        try:
            api.upload_file(
                path_or_fileobj=filename, path_in_repo=filename,
                repo_id=REPO_ID, repo_type="dataset", token=HF_TOKEN
            )
            logger.info(f"[HF Sync] {filename} 成功推送到云端仓库。")
            return True
        except Exception as e:
            logger.error(f"[HF Sync] {filename} 上传失败: {e}")
    return False

def load_economy():
    if api:
        try:
            hf_hub_download(repo_id=REPO_ID, filename=ECONOMY_FILE, repo_type="dataset", local_dir=".", token=HF_TOKEN)
        except Exception:
            logger.warning("[Storage] 云端未找到历史经济数据，将使用新账本。")
    if os.path.exists(ECONOMY_FILE):
        try:
            with open(ECONOMY_FILE, 'r', encoding='utf-8') as f: 
                return json.load(f)
        except Exception as e:
            logger.error(f"[Storage] 经济数据读取失败: {e}")
    return {}

def save_economy(data):
    try:
        with open(ECONOMY_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"[Storage] 经济数据保存失败: {e}")

async def cloud_sync_worker():
    global ECONOMY_DIRTY, GLOBAL_ECO_DATA
    while True:
        await asyncio.sleep(1800) # 30分钟检查一次
        if ECONOMY_DIRTY:
            logger.info("[Storage] 检测到数据变动，开始保存并同步至云端...")
            await asyncio.to_thread(save_economy, GLOBAL_ECO_DATA)
            success = await asyncio.to_thread(upload_to_hf, ECONOMY_FILE)
            if success:
                ECONOMY_DIRTY = False
    
# ==========================================
# 3. 异步数据抓取与缓存
# ==========================================
async def async_fetch_json(url, headers=None):
    async with aiohttp.ClientSession(headers=headers or STD_HEADERS) as session:
        async with session.get(url, timeout=60) as resp:
            if resp.status == 200:
                return await resp.json()
    return []

def update_affordable_crates():
    global AFFORDABLE_CASES, AFFORDABLE_CAPSULES
    AFFORDABLE_CASES = [c for c in CRATES_CASES if PRICE_CN_MAP.get(c.get('name'), {}).get('price', 5.0) <= 800]
    AFFORDABLE_CAPSULES = [c for c in CRATES_CAPSULES if PRICE_CN_MAP.get(c.get('name'), {}).get('price', 1.5) <= 800]
    logger.info(f"[Cache] 重新构建平民池: 箱子={len(AFFORDABLE_CASES)}, 胶囊={len(AFFORDABLE_CAPSULES)}")

async def init_crates_data():
    global CRATES_DICT, CRATES_CASES, CRATES_CAPSULES, AFFORDABLE_CASES, AFFORDABLE_CAPSULES
    url = "https://cdn.jsdelivr.net/gh/ByMykel/CSGO-API@main/public/api/zh-CN/crates.json"
    try:
        CRATES_DICT = await async_fetch_json(url)
        if CRATES_DICT:
            CRATES_CASES = [c for c in CRATES_DICT if c.get('type') == 'Weapon Case' or '武器箱' in c.get('name', '')]
            CRATES_CAPSULES = [c for c in CRATES_DICT if c.get('type') == 'Sticker Capsule' or '胶囊' in c.get('name', '')]
            AFFORDABLE_CASES, AFFORDABLE_CAPSULES = CRATES_CASES, CRATES_CAPSULES 
            logger.info(f"[Init] 官方掉落表加载成功，箱子: {len(CRATES_CASES)}，胶囊: {len(CRATES_CAPSULES)}。")
            
            # 这一行必须和上面 logger.info 的缩进完全一致（8个空格）
            update_affordable_crates()
            
    except Exception as e:
        logger.error(f"[Init] 掉落表拉取失败: {e}")

async def init_translation_dictionary():
    global DISPLAY_TRANS
    dict_file = 'auto_dict_v4.json'
    
    if api:
        try:
            await asyncio.to_thread(hf_hub_download, repo_id=REPO_ID, filename=dict_file, repo_type="dataset", local_dir=".", token=HF_TOKEN)
        except Exception: pass

    if os.path.exists(dict_file):
        try:
            def _load_dict():
                with open(dict_file, 'r', encoding='utf-8') as f: 
                    return json.load(f).get("DISPLAY_TRANS", {})
            DISPLAY_TRANS = await asyncio.to_thread(_load_dict)
            if DISPLAY_TRANS:
                logger.info("[Init] 成功加载缓存词库。")
                return
        except Exception: pass

    logger.info("[Init] 开始全量同步翻译词库...")
    base_en = "https://cdn.jsdelivr.net/gh/ByMykel/CSGO-API@main/public/api/en"
    base_cn = "https://cdn.jsdelivr.net/gh/ByMykel/CSGO-API@main/public/api/zh-CN"
    
    trans = {}
    for cat in ["skins.json", "stickers.json", "crates.json", "agents.json"]:
        en, cn_data = await asyncio.gather(async_fetch_json(f"{base_en}/{cat}"), async_fetch_json(f"{base_cn}/{cat}"))
        cn = {i.get('id'): i.get('name') for i in cn_data if isinstance(i, dict)}
        for item in en:
            if isinstance(item, dict) and item.get('id') in cn: 
                trans[item.get('name')] = cn[item.get('id')]
    
    trans.update(CUSTOM_TRANS)
    def _sort_and_save():
        sorted_trans = dict(sorted(trans.items(), key=lambda x: len(x[0]), reverse=True))
        with open(dict_file, 'w', encoding='utf-8') as f: 
            json.dump({"DISPLAY_TRANS": sorted_trans}, f, ensure_ascii=False)
        return sorted_trans

    DISPLAY_TRANS = await asyncio.to_thread(_sort_and_save)
    await asyncio.to_thread(upload_to_hf, dict_file)

async def fetch_skinport_prices():
    url = "https://api.skinport.com/v1/items?app_id=730&currency=CNY"
    try:
        # 替换 aiohttp 为 curl_cffi，伪装浏览器 TLS 指纹，防止 HF 节点被 Cloudflare 拦截
        async with AsyncSession(impersonate="chrome110", timeout=60) as session:
            resp = await session.get(url, headers={'Accept': 'application/json'})
            if resp.status_code == 200: 
                return resp.json() # 注意：curl_cffi 的 json() 是同步方法，不需要 await
            else:
                logger.error(f"[Price] 接口被拦截或异常，状态码: {resp.status_code}")
                logger.debug(f"[Price] 返回内容片段: {resp.text[:200]}")
    except Exception as e:
        logger.error(f"[Price] 获取异常: {e}")
    return None

def process_and_save_prices(data, cache_file):
    global PRICE_DICT, PRICE_CN_MAP, PRICE_EN_MAP, IS_PRICE_READY
    new_prices = []
    for item in data:
        en_name = item.get('market_hash_name')
        price = item.get('min_price') or item.get('suggested_price') or 0
        
        if en_name and price:
            cn_name = en_name
            for eng, chn in DISPLAY_TRANS.items():
                if eng in cn_name: cn_name = cn_name.replace(eng, chn)
            
            rarity = "Extraordinary" if any(k in cn_name for k in ["刀", "手套", "★"]) else item.get('rarity')
            cn_name = cn_name.replace("(崭新出厂)", "(崭新)").replace("(略有磨损)", "(略磨)").replace("(久经沙场)", "(久经)").replace("(破损不堪)", "(破损)").replace("(战痕累累)", "(战痕)")

            new_prices.append({
                "en_name": en_name, "cn_name": cn_name,
                "search_text": f"{en_name} {cn_name}".lower(), "price": float(price), "rarity": rarity
            })
            
    if len(new_prices) > 500:
        PRICE_DICT = new_prices
        PRICE_CN_MAP = {i['cn_name']: i for i in PRICE_DICT}
        PRICE_EN_MAP = {i['en_name']: i for i in PRICE_DICT}
        
        with open(cache_file, 'w', encoding='utf-8') as f: 
            json.dump(PRICE_DICT, f, ensure_ascii=False)
        return True
    return False

async def price_auto_updater():
    global IS_PRICE_READY
    cache_file = 'price_cache_v4.json'
    
    if api:
        try:
            await asyncio.to_thread(hf_hub_download, repo_id=REPO_ID, filename=cache_file, repo_type="dataset", local_dir=".", token=HF_TOKEN)
        except Exception: pass

    if os.path.exists(cache_file):
        try:
            def _load_cache():
                global PRICE_DICT, PRICE_CN_MAP, PRICE_EN_MAP
                with open(cache_file, 'r', encoding='utf-8') as f: 
                    PRICE_DICT = json.load(f)
                if PRICE_DICT: 
                    PRICE_CN_MAP = {i['cn_name']: i for i in PRICE_DICT}
                    PRICE_EN_MAP = {i['en_name']: i for i in PRICE_DICT}
            
            await asyncio.to_thread(_load_cache)
            if PRICE_DICT:
                await asyncio.to_thread(update_affordable_crates)
                IS_PRICE_READY = True
                logger.info(f"[Price] 本地缓存加载完毕，共 {len(PRICE_DICT)} 条。")
        except Exception as e: 
            logger.error(f"[Price] 缓存加载异常: {e}")

    while not DISPLAY_TRANS: await asyncio.sleep(2)
        
    while True:
        data = await fetch_skinport_prices()
        if data and isinstance(data, list):
            success = await asyncio.to_thread(process_and_save_prices, data, cache_file)
            if success:
                await asyncio.to_thread(update_affordable_crates)
                IS_PRICE_READY = True
                logger.info("[Price] 价格同步完成。")
                await asyncio.to_thread(upload_to_hf, cache_file)
                await asyncio.sleep(86400) # 1天更新一次
                continue
                
        logger.warning("[Price] 价格更新失败或数据量过低，3分钟后重试...")
        await asyncio.sleep(180)

# ==========================================
# 4. 指令路由与业务逻辑
# ==========================================
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
        
        # 因为 args 本身就是 tuple，直接全小写化后传入
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

@bot.command(name='open', prefixes=['/'])
async def simulate_case_opening(msg: Message, *args):
    if msg.target_id != ALLOWED_CHANNEL_ID: return
    global ECONOMY_DIRTY, GLOBAL_ECO_DATA
    count_str = args[0] if args else "1"
    try:
        count = max(1, min(10, int(count_str)))
    except ValueError:
        count = 1

    try:
        if not IS_PRICE_READY or not PRICE_DICT:
            return await msg.reply("[System] 价格同步中，请稍后。")
        if not CRATES_CASES:
            return await msg.reply("[System] 官方掉落概率表加载中，请稍后。")
        if not AFFORDABLE_CASES:
            return await msg.reply("[Error] 价格池过滤异常，未找到符合条件的武器箱。")

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

            # 修改后：兼容中英文类型，或者直接判断名字里有没有"武器箱"
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
                        won_item_dict = random.choice([i for i in PRICE_DICT if any(k in i['cn_name'] for k in ["刀", "手套", "★"])])
                        won_item_raw = "GOLDBACK"
                        won_item_name = won_item_dict['cn_name']
                        won_item_price = won_item_dict['price']
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
        
        user_eco = GLOBAL_ECO_DATA.setdefault(user_id, {"name": user_name, "spent": 0.0, "earned": 0.0, "profit": 0.0, "opens": 0})
        user_eco["name"] = user_name
        user_eco["spent"] += total_cost_all
        user_eco["earned"] += total_earned_all
        user_eco["profit"] += profit_all
        user_eco["opens"] += count
        total_profit, total_opens = user_eco["profit"], user_eco["opens"]
        
        ECONOMY_DIRTY = True 
        
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

async def get_all_data(steam_id):
    urls = {
        "summary": f"http://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/?key={STEAM_API_KEY}&steamids={steam_id}",
        "stats": f"http://api.steampowered.com/ISteamUserStats/GetUserStatsForGame/v0002/?appid=730&key={STEAM_API_KEY}&steamid={steam_id}",
        "bans": f"http://api.steampowered.com/ISteamUser/GetPlayerBans/v1/?key={STEAM_API_KEY}&steamids={steam_id}",
        "level": f"http://api.steampowered.com/IPlayerService/GetSteamLevel/v1/?key={STEAM_API_KEY}&steamid={steam_id}",
        "inv": f"https://steamcommunity.com/inventory/{steam_id}/730/2?l=zh-CN&count=2000"
    }
    # 🚀 使用全局 AIO_SESSION，避免每次查战绩新建 5 个 TCP 连接
    results = await asyncio.gather(*[AIO_SESSION.get(url) for url in urls.values()], return_exceptions=True)
    return {k: (await r.json() if isinstance(r, aiohttp.ClientResponse) and r.status == 200 else {}) for k, r in zip(urls.keys(), results)}

@bot.command(name='cs', prefixes=['/'])
async def query_full_profile(msg: Message, steam_id: str = ""):
    if not steam_id or not steam_id.isdigit():
        return await msg.reply("[Error] 参数校验失败：请输入17位数字型 SteamID。")

    loading_msg = await msg.reply(f"正在连接 Steam 官方数据节点，同步玩家 {steam_id} 的档案...")
    try:
        async with aiohttp.ClientSession(headers=STD_HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as session:
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
            card.append(Module.Context(Element.Text("💡 核心数据与评级分数采用社区 Rating 2.0 机制进行估算", type=Types.Text.KMD)))
            
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
    if msg.target_id != ALLOWED_CHANNEL_ID: return
    loading_msg = await msg.reply("正在查询服务器状态...")
    try:
        url_official = f"https://api.steampowered.com/ICSGOServers_730/GetGameServersStatus/v1/?key={STEAM_API_KEY}"
        url_web_test = "https://steamcommunity.com/market/search/render/?appid=730&count=1"
        
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(headers=STD_HEADERS, timeout=timeout) as session:
            task_off = session.get(url_official)
            task_web = session.get(url_web_test)
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
    loading_msg = await msg.reply("正在连接至 HLTV 服务器...")
    try:
        async with AsyncSession(impersonate="chrome110", timeout=15) as session:
            resp_matches = await session.get("https://www.hltv.org/matches")
            
        if resp_matches.status_code != 200:
            await safe_delete_msg(bot, loading_msg)
            return await msg.reply(f"[Error] 抓取被拦截 (HTTP {resp_matches.status_code})。")

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
            async with AsyncSession(impersonate="chrome110", timeout=15) as session:
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
        
# ==========================================
# 5. 保活与启动 (基于现代化 Asyncio 结构)
# ==========================================
async def health_check(request):
    return web.Response(text="Bot is Online (HF Spaces)")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 7860)
    await site.start()
    logger.info("[System] Web Health Check 启动于端口 7860")

async def main():
    """主启动器：统一管控依赖组件与 Bot 生命周期"""
    global GLOBAL_ECO_DATA, AIO_SESSION 
    
    # 🌟 初始化全局共享请求会话
    AIO_SESSION = aiohttp.ClientSession(headers=STD_HEADERS)
    
    logger.info("[System] 正在拉取云端经济数据存档...")
    GLOBAL_ECO_DATA = await asyncio.to_thread(load_economy)
    logger.info(f"[System] 历史数据加载完毕，当前已有 {len(GLOBAL_ECO_DATA)} 名用户数据。")

    # 挂载后台服务
    asyncio.create_task(start_web_server())
    asyncio.create_task(cloud_sync_worker())
    asyncio.create_task(init_crates_data())
    asyncio.create_task(init_translation_dictionary())
    asyncio.create_task(price_auto_updater())
    
    logger.info("[System] 内核组件挂载完毕，启动主线程循环...")
    try:
        await bot.start()
    finally:
        # 🌟 安全关闭全局 Session
        if AIO_SESSION:
            await AIO_SESSION.close()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("[System] 收到强制中断信号，正在关闭服务...")